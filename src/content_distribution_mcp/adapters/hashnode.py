"""
Hashnode channel adapter for Content Distribution MCP.

Wraps the Hashnode GraphQL API (https://gql.hashnode.com/) to publish and
unpublish posts on behalf of an authenticated Hashnode user.

Authentication
--------------
Hashnode uses a Personal Access Token passed in the ``Authorization`` header
(no ``Bearer`` prefix — just the raw token). Generate one at:
https://hashnode.com/settings/developer

Channel format
--------------
``hashnode:<any-suffix>``  (e.g. ``hashnode:main``, ``hashnode:personal``)

Required ``variant.extras`` keys
---------------------------------
``publicationId``   The Hashnode publication UUID to post under. Every Hashnode
                    post must belong to a publication; there is no "user feed"
                    without one.

Optional ``variant.extras`` keys
----------------------------------
``coverImageURL``   Absolute URL to a cover image.
``metaTitle``       SEO meta title (overrides the post title in <head>).
``metaDescription`` SEO meta description.
``subtitle``        Subtitle / deck displayed below the headline.

Python 3.11+. Uses ``httpx.AsyncClient`` for all HTTP I/O.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import ChannelHints, PublishResult, Variant

# ---------------------------------------------------------------------------
# GraphQL mutation constants
# ---------------------------------------------------------------------------

_PUBLISH_POST_MUTATION = """
mutation PublishPost($input: PublishPostInput!) {
  publishPost(input: $input) {
    post {
      id
      url
      slug
    }
  }
}
"""

_REMOVE_POST_MUTATION = """
mutation RemovePost($input: RemovePostInput!) {
  removePost(input: $input) {
    post {
      id
      slug
    }
  }
}
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GQL_ENDPOINT = "https://gql.hashnode.com/"

_FULL_MD_FEATURES: set[str] = {
    "bold",
    "italic",
    "strikethrough",
    "code_inline",
    "code_block",
    "links",
    "headers",
    "images",
    "lists",
    "tables",
    "blockquote",
    "horizontal_rule",
    "footnotes",
    "task_lists",
    # Hashnode-specific embed tokens understood by their renderer
    "hashnode_embed_youtube",
    "hashnode_embed_codepen",
    "hashnode_embed_codesandbox",
    "hashnode_embed_github_gist",
}


def _build_tags(tags: list[str]) -> list[dict[str, str]]:
    """Convert a plain tag list to Hashnode's ``{ name }`` object list."""
    return [{"name": tag} for tag in tags]


def _extract_post_id_from_url(live_url: str) -> str | None:
    """
    Parse a Hashnode post URL to extract the post slug, which the API uses as
    a stable identifier for ``removePost``.

    Hashnode post URLs follow the pattern::

        https://<publication-slug>.hashnode.dev/<post-slug>
        https://<custom-domain>/<post-slug>

    The last non-empty path segment is the post slug.

    Returns the slug string, or ``None`` if the URL cannot be parsed.
    """
    match = re.search(r"/([^/]+?)(?:/)?$", live_url.rstrip("/"))
    return match.group(1) if match else None


async def _gql_request(
    client: httpx.AsyncClient,
    token: str,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    """
    Execute a single GraphQL request against the Hashnode API.

    Parameters
    ----------
    client:
        A live ``httpx.AsyncClient`` instance (caller owns lifecycle).
    token:
        Hashnode Personal Access Token. Sent as ``Authorization: <token>``.
    query:
        GraphQL query/mutation string.
    variables:
        Variables dict for the operation.

    Returns
    -------
    dict
        Parsed JSON response body (may contain ``data`` and/or ``errors``).

    Raises
    ------
    httpx.HTTPStatusError
        On 4xx/5xx responses (raised by ``response.raise_for_status()``).
    """
    payload = {"query": query, "variables": variables}
    response = await client.post(
        _GQL_ENDPOINT,
        json=payload,
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
        },
    )
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# HashnodeAdapter
# ---------------------------------------------------------------------------


class HashnodeAdapter:
    """Channel adapter that publishes content to the Hashnode GraphQL API.

    One adapter instance is stateless and can be shared across concurrent
    publish calls. All I/O methods are ``async`` and use ``httpx.AsyncClient``.

    Usage
    -----
    The adapter is called by the MCP ``distribute`` tool which constructs a
    :class:`~models.Variant`, resolves the operator :class:`Profile`, and
    calls :meth:`publish`. Callers should not hold state between calls.

    Extras contract
    ---------------
    The following keys are read from ``variant.extras``:

    ==================  =======  ============================================
    Key                 Req?     Description
    ==================  =======  ============================================
    ``publicationId``   Yes      Hashnode publication UUID
    ``coverImageURL``   No       Cover/hero image URL
    ``metaTitle``       No       SEO <title> override
    ``metaDescription`` No       SEO meta description
    ``subtitle``        No       Post subtitle / deck
    ==================  =======  ============================================
    """

    # ------------------------------------------------------------------
    # hints
    # ------------------------------------------------------------------

    def hints(self) -> ChannelHints:
        """Return static publishing constraints for the Hashnode channel.

        Hashnode imposes no practical body length limit, supports full
        Markdown plus its own embed syntax, accepts free-form tags (no
        controlled vocabulary fetch required), and natively stores the
        canonical URL via the ``originalArticleURL`` field on
        ``PublishPostInput``.

        Returns
        -------
        ChannelHints
            Static metadata for LLM callers constructing a ``Variant``.
        """
        return ChannelHints(
            max_length=None,
            supported_md_features=_FULL_MD_FEATURES,
            tag_vocab=None,  # Hashnode uses free-form tags
            cta_placement="bottom",
            canonical_url_supported=True,
            browser_only=False,
        )

    # ------------------------------------------------------------------
    # can_publish
    # ------------------------------------------------------------------

    def can_publish(self, variant: Variant) -> tuple[bool, str]:
        """Return ``(ok, reason)`` per the scheduler contract.

        Conditions:
        1. ``variant.channel`` starts with ``"hashnode:"``.
        2. ``variant.extras`` contains ``"publicationId"`` — Hashnode requires
           every post to belong to a publication.
        3. Title and body are non-empty.
        """
        if not variant.channel.startswith("hashnode:"):
            return False, f"channel-not-hashnode: {variant.channel}"
        if not variant.extras.get("publicationId"):
            return False, "missing-publicationId-in-variant-extras"
        if not variant.title:
            return False, "empty-title"
        if not variant.body:
            return False, "empty-body"
        return True, ""

    # ------------------------------------------------------------------
    # publish
    # ------------------------------------------------------------------

    async def publish(
        self,
        variant: Variant,
        profile: dict[str, Any],
        state_backend: Any,
    ) -> PublishResult:
        """Publish a variant to Hashnode via the ``publishPost`` GraphQL mutation.

        Workflow
        --------
        1. **Idempotency claim** — constructs a key from
           ``<channel>:<content_id>`` (using ``variant.extras.get("content_id",
           variant.channel)``). If the slot is already claimed a previously
           successful publish exists; the method returns early with its
           stored URL if available, or a ``failed`` result if the state is
           ambiguous.
        2. **Build input** — assembles ``PublishPostInput`` from the variant,
           including optional cover image, SEO meta tags, and canonical URL.
        3. **POST mutation** — sends ``publishPost`` to the Hashnode GraphQL
           endpoint with the operator's Personal Access Token.
        4. **Result handling**:
           - ``data.publishPost.post.url`` present → ``state="live"``,
             calls ``state_backend.mark_published``, returns result.
           - ``errors`` array present in response → ``state="failed"``,
             first error message surfaced.
           - HTTP 4xx/5xx → ``state="failed"``, response body prefix
             surfaced (max 200 chars).

        Parameters
        ----------
        variant:
            Channel-adapted content variant. Must pass :meth:`can_publish`.
        profile:
            Operator credential dict. Must contain ``"hashnode_token"``
            (the Personal Access Token string).
        state_backend:
            Backend used for idempotency claims and publish log writes.

        Returns
        -------
        PublishResult
            Outcome of the publish attempt with ``channel`` set to
            ``variant.channel``.
        """
        token: str = profile["hashnode_token"]
        publication_id: str = variant.extras["publicationId"]

        # --- 1. Idempotency claim ---
        # Key is the (content_id, channel) tuple per the StateBackend
        # protocol. claim_idempotency_key is sync and returns bool.
        content_id: str = variant.extras.get("content_id", variant.channel)

        claimed = state_backend.claim_idempotency_key(content_id, variant.channel)
        if not claimed:
            existing = state_backend.lookup_published(content_id, variant.channel)
            if existing is not None:
                return PublishResult(
                    channel=variant.channel,
                    state="live",
                    live_url=existing.get("published_url"),
                )
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="idempotency-claimed-but-no-live-row",
            )

        # --- 2. Build PublishPostInput ---
        input_payload: dict[str, Any] = {
            "title": variant.title,
            "contentMarkdown": variant.body,
            "tags": _build_tags(variant.tags),
            "publicationId": publication_id,
        }

        # Canonical URL (SEO — tells search engines where the original lives)
        canonical_url = variant.canonical_url
        if canonical_url:
            input_payload["originalArticleURL"] = str(canonical_url)

        # Cover image
        cover_image_url = variant.extras.get("coverImageURL")
        if not cover_image_url and variant.extras.get("cover_image"):
            cover_image_url = str(variant.extras["cover_image"])
        if cover_image_url:
            input_payload["coverImageOptions"] = {"coverImageURL": cover_image_url}

        # SEO meta tags
        meta_title = variant.extras.get("metaTitle")
        meta_description = variant.extras.get("metaDescription")
        if meta_title or meta_description:
            meta: dict[str, str] = {}
            if meta_title:
                meta["title"] = meta_title
            if meta_description:
                meta["description"] = meta_description
            input_payload["metaTags"] = meta

        # Subtitle (optional deck / subheading)
        subtitle = variant.extras.get("subtitle")
        if subtitle:
            input_payload["subtitle"] = subtitle

        # --- 3. POST mutation ---
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                body = await _gql_request(
                    client,
                    token,
                    _PUBLISH_POST_MUTATION,
                    {"input": input_payload},
                )
            except httpx.HTTPStatusError as exc:
                error_snippet = exc.response.text[:200]
                error_msg = f"HTTP {exc.response.status_code}: {error_snippet}"
                state_backend.mark_published(
                    content_id,
                    variant.channel,
                    state="failed",
                    published_url=None,
                    error=error_msg,
                )
                return PublishResult(
                    channel=variant.channel,
                    state="failed",
                    error=error_msg,
                )

        # --- 4. Result handling ---
        if errors := body.get("errors"):
            first_error = errors[0].get("message", str(errors[0]))
            state_backend.mark_published(
                content_id,
                variant.channel,
                state="failed",
                published_url=None,
                error=first_error,
            )
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error=first_error,
            )

        try:
            live_url: str = body["data"]["publishPost"]["post"]["url"]
        except (KeyError, TypeError):
            shape_err = f"Unexpected response shape: {str(body)[:200]}"
            state_backend.mark_published(
                content_id,
                variant.channel,
                state="failed",
                published_url=None,
                error=shape_err,
            )
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error=shape_err,
            )

        published_at = datetime.now(tz=timezone.utc)
        state_backend.mark_published(
            content_id,
            variant.channel,
            state="live",
            published_url=live_url,
            error=None,
        )

        return PublishResult(
            channel=variant.channel,
            state="live",
            live_url=live_url,  # type: ignore[arg-type]
            published_at=published_at,
        )

    # ------------------------------------------------------------------
    # unpublish
    # ------------------------------------------------------------------

    async def unpublish(
        self,
        live_url: str,
        profile: dict[str, Any],
    ) -> bool:
        """Remove a previously published Hashnode post.

        Parses the post slug from ``live_url`` (the last path segment) and
        issues a ``removePost`` GraphQL mutation. Hashnode's ``removePost``
        mutation accepts a ``postId``; however, the post ID is embedded in the
        URL as the slug. Since the Hashnode API does not expose a
        ``getPostBySlug`` query that returns the internal UUID in all API
        versions, this method uses the slug as a best-effort identifier.

        .. note::
            If the slug cannot be parsed from ``live_url``, or if the API
            returns an error, the method returns ``False`` without raising.

        Parameters
        ----------
        live_url:
            The public URL of the post to remove, as returned by
            :meth:`publish` in ``PublishResult.live_url``.
        profile:
            Operator credential dict. Must contain ``"hashnode_token"``.

        Returns
        -------
        bool
            ``True`` if the removal mutation succeeded, ``False`` otherwise.
        """
        token: str = profile["hashnode_token"]

        post_slug = _extract_post_id_from_url(live_url)
        if not post_slug:
            return False

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                body = await _gql_request(
                    client,
                    token,
                    _REMOVE_POST_MUTATION,
                    {"input": {"id": post_slug}},
                )
            except httpx.HTTPStatusError:
                return False

        if body.get("errors"):
            return False

        try:
            # Presence of post.id in the response confirms removal
            removed_id = body["data"]["removePost"]["post"]["id"]
            return bool(removed_id)
        except (KeyError, TypeError):
            return False
