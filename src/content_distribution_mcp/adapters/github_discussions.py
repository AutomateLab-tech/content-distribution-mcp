"""
GitHub Discussions channel adapter for Content Distribution MCP.

Wraps the GitHub GraphQL API (https://api.github.com/graphql) to create and
delete GitHub Discussions on behalf of an authenticated user or app.

Authentication
--------------
Personal Access Token (PAT) with the following scopes:

- ``repo`` (or ``public_repo`` for public repos) — needed to query the
  repository and resolve category IDs.
- ``read:discussion`` — read discussion categories.
- ``write:discussion`` — create new discussions.

For ``unpublish`` (``deleteDiscussion`` mutation) the PAT additionally needs
the ``admin:discussion`` scope (or the caller must be an organization owner /
team maintainer with admin rights on the repo).

The token is passed as ``Authorization: Bearer <GITHUB_TOKEN>`` per the
GitHub GraphQL documentation.

Channel format
--------------
``github-discussions:<owner>/<repo>``

Example: ``github-discussions:AutomateLab-tech/content-distribution-mcp``

Required ``variant.extras`` keys
---------------------------------
``category``
    The human-readable name of the Discussions category to post under
    (e.g. ``"Announcements"``, ``"Show and tell"``, ``"General"``).
    The adapter resolves this name to a category ID via GraphQL before
    posting.

Canonical URL handling
----------------------
GitHub Discussions has no native ``rel=canonical`` or ``originalArticleURL``
field.  When ``variant.canonical_url`` is set, the adapter appends a
reference footer to the body before posting::

    ---
    *Originally posted at [<url>](<url>).*

Rate limits (as of 2026)
------------------------
- 5,000 points per hour per authenticated user (PAT).
- Secondary limit: 80 content-generating requests per minute, 500 per hour.
- ``createDiscussion`` mutation costs approximately 1 point per call.
- ``deleteDiscussion`` mutation costs approximately 1 point per call.

The adapter surfaces ``429 Too Many Requests`` and ``403`` rate-limit
responses as transient errors (``state="failed"`` with the HTTP status in
the error message) so the MCP server's retry policy can back off and retry.

Caching
-------
The (owner, repo) → (repository_id, {category_name: category_id}) mapping is
cached in a module-level dict with a 1-hour TTL.  This avoids a repeated
round-trip on every publish call when the same repo is used multiple times
within an agent run.

Python 3.11+. Uses ``httpx.AsyncClient`` for all HTTP I/O.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from ..models import ChannelHints, PublishResult, Variant

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GQL_ENDPOINT = "https://api.github.com/graphql"

# Cache entry shape: {"repo_id": str, "categories": {name: id}, "fetched_at": float}
_REPO_CACHE: dict[str, dict[str, Any]] = {}
_REPO_CACHE_TTL = 3_600  # 1 hour in seconds

# Full GitHub-Flavored Markdown feature set (GFM).
_GFM_FEATURES: set[str] = {
    "bold",
    "italic",
    "strikethrough",
    "code_inline",
    "code_block",
    "links",
    "headers",
    "images",
    "lists",
    "ordered_lists",
    "tables",
    "blockquote",
    "horizontal_rule",
    "footnotes",
    "task_lists",
    "autolinks",
    "html_inline",
    "alerts",             # GitHub-specific: [!NOTE], [!TIP], [!WARNING], etc.
    "mermaid_diagrams",   # GitHub renders ```mermaid``` code fences as diagrams.
    "math_expressions",   # GitHub renders $..$ and $$...$$ via MathJax.
    "geojson_maps",       # GitHub renders ```geojson``` code fences as maps.
    "topojson_maps",
    "stl_3d",
    "embed_github_links",
}

# ---------------------------------------------------------------------------
# GraphQL query / mutation strings
# ---------------------------------------------------------------------------

_QUERY_REPO_AND_CATEGORIES = """
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    id
    discussionCategories(first: 50) {
      nodes {
        id
        name
      }
    }
  }
}
"""

_MUTATION_CREATE_DISCUSSION = """
mutation($input: CreateDiscussionInput!) {
  createDiscussion(input: $input) {
    discussion {
      id
      url
    }
  }
}
"""

_MUTATION_DELETE_DISCUSSION = """
mutation($input: DeleteDiscussionInput!) {
  deleteDiscussion(input: $input) {
    discussion {
      id
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Low-level GraphQL helper
# ---------------------------------------------------------------------------


async def _gql_request(
    client: httpx.AsyncClient,
    token: str,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    """Execute a single GraphQL request against the GitHub API.

    Parameters
    ----------
    client:
        A live ``httpx.AsyncClient`` instance owned by the caller.
    token:
        GitHub Personal Access Token.  Sent as ``Authorization: Bearer <token>``.
    query:
        GraphQL query or mutation string.
    variables:
        Variables dict passed alongside the operation.

    Returns
    -------
    dict
        Parsed JSON response body.  May contain ``data`` and/or ``errors``
        keys per the GraphQL over HTTP specification.

    Raises
    ------
    httpx.HTTPStatusError
        Raised by ``response.raise_for_status()`` on 4xx/5xx HTTP responses
        (i.e. transport-level errors as opposed to GraphQL application errors
        which appear in ``response["errors"]``).
    """
    payload: dict[str, Any] = {"query": query, "variables": variables}
    response = await client.post(
        _GQL_ENDPOINT,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # GitHub recommends including the API version header.
            "X-Github-Next-Global-ID": "1",
        },
    )
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _cache_key(owner: str, repo: str) -> str:
    """Return the module-level cache key for a (owner, repo) pair."""
    return f"{owner}/{repo}"


def _get_cached_repo_info(owner: str, repo: str) -> dict[str, Any] | None:
    """Return cached (repo_id, categories) info if still within TTL.

    Parameters
    ----------
    owner:
        GitHub organisation or user login (e.g. ``"AutomateLab-tech"``).
    repo:
        Repository name without the owner prefix (e.g.
        ``"content-distribution-mcp"``).

    Returns
    -------
    dict | None
        Cached entry with keys ``repo_id`` (str) and ``categories``
        (``dict[str, str]`` mapping category name → node ID), or ``None``
        if the cache is empty or stale.
    """
    key = _cache_key(owner, repo)
    entry = _REPO_CACHE.get(key)
    if entry is None:
        return None
    if time.monotonic() - entry["fetched_at"] > _REPO_CACHE_TTL:
        del _REPO_CACHE[key]
        return None
    return entry


def _set_cached_repo_info(
    owner: str,
    repo: str,
    repo_id: str,
    categories: dict[str, str],
) -> None:
    """Populate the module-level cache for a (owner, repo) pair.

    Parameters
    ----------
    owner:
        GitHub organisation or user login.
    repo:
        Repository name without the owner prefix.
    repo_id:
        The repository's global GraphQL node ID (e.g.
        ``"R_kgDOxxxx"``).
    categories:
        Mapping of category name (as shown in the GitHub UI) to its
        GraphQL node ID (e.g. ``{"Announcements": "DIC_xxxx", ...}``).
    """
    key = _cache_key(owner, repo)
    _REPO_CACHE[key] = {
        "repo_id": repo_id,
        "categories": categories,
        "fetched_at": time.monotonic(),
    }


# ---------------------------------------------------------------------------
# Repo / category resolution
# ---------------------------------------------------------------------------


async def _resolve_repo_and_category(
    client: httpx.AsyncClient,
    token: str,
    owner: str,
    repo: str,
    category_name: str,
) -> tuple[str, str]:
    """Resolve a repository ID and a category ID from their human-readable names.

    First checks the module-level cache (TTL = 1 h).  On a cache miss,
    issues the ``repository`` GraphQL query to fetch both the repository node
    ID and the full list of discussion categories (up to 50) in one round-trip.

    Parameters
    ----------
    client:
        An active ``httpx.AsyncClient``.
    token:
        GitHub PAT for authentication.
    owner:
        Repository owner login.
    repo:
        Repository name (no owner prefix).
    category_name:
        Human-readable category name (case-sensitive, must match the GitHub
        UI label exactly, e.g. ``"Show and tell"``).

    Returns
    -------
    tuple[str, str]
        ``(repository_node_id, category_node_id)``

    Raises
    ------
    ValueError
        If the repository is not found, the category name does not match any
        available category, or the GraphQL response contains errors.
    httpx.HTTPStatusError
        On transport-level HTTP errors (4xx/5xx).
    """
    cached = _get_cached_repo_info(owner, repo)
    if cached is not None:
        repo_id: str = cached["repo_id"]
        categories: dict[str, str] = cached["categories"]
    else:
        body = await _gql_request(
            client,
            token,
            _QUERY_REPO_AND_CATEGORIES,
            {"owner": owner, "repo": repo},
        )

        if gql_errors := body.get("errors"):
            first_msg = gql_errors[0].get("message", str(gql_errors[0]))
            raise ValueError(f"GraphQL error resolving repo {owner}/{repo}: {first_msg}")

        repo_data = (body.get("data") or {}).get("repository")
        if not repo_data:
            raise ValueError(
                f"Repository '{owner}/{repo}' not found or PAT lacks 'repo' scope."
            )

        repo_id = repo_data["id"]
        categories = {
            node["name"]: node["id"]
            for node in repo_data["discussionCategories"]["nodes"]
        }
        _set_cached_repo_info(owner, repo, repo_id, categories)

    category_id = categories.get(category_name)
    if not category_id:
        available = ", ".join(f'"{n}"' for n in sorted(categories))
        raise ValueError(
            f"Category '{category_name}' not found in {owner}/{repo}. "
            f"Available: [{available}]"
        )

    return repo_id, category_id


# ---------------------------------------------------------------------------
# Channel name parsing
# ---------------------------------------------------------------------------


def _parse_channel(channel: str) -> tuple[str, str]:
    """Parse ``github-discussions:<owner>/<repo>`` into ``(owner, repo)``.

    Parameters
    ----------
    channel:
        The full channel identifier string from ``variant.channel``.

    Returns
    -------
    tuple[str, str]
        ``(owner, repo)``

    Raises
    ------
    ValueError
        If the channel string does not match the expected format.
    """
    prefix = "github-discussions:"
    if not channel.startswith(prefix):
        raise ValueError(
            f"Channel '{channel}' does not start with '{prefix}'."
        )
    repo_path = channel[len(prefix):]
    parts = repo_path.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Channel '{channel}' must follow 'github-discussions:<owner>/<repo>'."
        )
    return parts[0], parts[1]


# ---------------------------------------------------------------------------
# Discussion URL → node ID parsing
# ---------------------------------------------------------------------------


def _parse_discussion_node_id_from_url(live_url: str) -> str | None:
    """Attempt to extract a GitHub Discussion node ID from its web URL.

    GitHub Discussion URLs follow the form::

        https://github.com/<owner>/<repo>/discussions/<number>

    The numeric ``discussion_number`` is NOT the GraphQL node ID.  Because
    GitHub's GraphQL API requires the node ID for the ``deleteDiscussion``
    mutation (not the integer number), this function returns ``None`` and the
    caller must perform a separate GraphQL lookup (``repository.discussion``)
    to resolve the node ID from the number.

    Returns
    -------
    str | None
        Always ``None`` — node ID cannot be parsed from the URL alone.
        The ``unpublish`` implementation handles this via a follow-up query.

    Note
    ----
    The discussion number (integer) CAN be parsed from the URL and is
    returned by :func:`_parse_discussion_number_from_url` which is used by
    ``unpublish`` to issue the node ID lookup.
    """
    # Node IDs are not embedded in discussion URLs; callers use
    # _parse_discussion_number_from_url instead.
    return None


def _parse_discussion_number_from_url(live_url: str) -> int | None:
    """Parse the discussion number from a GitHub Discussion URL.

    URL shape: ``https://github.com/<owner>/<repo>/discussions/<number>``

    Parameters
    ----------
    live_url:
        The public discussion URL as returned by ``createDiscussion``.

    Returns
    -------
    int | None
        The integer discussion number, or ``None`` if the URL cannot be parsed.
    """
    try:
        # Split on "/discussions/" and take the trailing segment.
        _, after = live_url.split("/discussions/", 1)
        number_str = after.rstrip("/").split("/")[0].split("#")[0]
        return int(number_str)
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# GitHubDiscussionsAdapter
# ---------------------------------------------------------------------------


class GitHubDiscussionsAdapter:
    """Channel adapter that creates and deletes GitHub Discussions via GraphQL.

    This adapter is stateless; a single instance may be shared across
    concurrent publish calls.  All I/O methods are ``async`` and use
    ``httpx.AsyncClient``.

    Channel format
    --------------
    ``github-discussions:<owner>/<repo>``

    Example: ``github-discussions:AutomateLab-tech/content-distribution-mcp``

    Profile credentials
    -------------------
    The operator profile must supply the key ``GITHUB_TOKEN`` (a PAT with
    ``repo``, ``read:discussion``, and ``write:discussion`` scopes).  Pass
    the profile as a plain ``dict[str, Any]`` with at least::

        {"GITHUB_TOKEN": "<pat>"}

    Extras contract
    ---------------
    ==================  =======  ============================================
    Key                 Req?     Description
    ==================  =======  ============================================
    ``category``        Yes      Human-readable discussion category name
                                 (e.g. ``"Announcements"``, ``"Show and tell"``).
                                 The adapter resolves this to a category node ID
                                 via GraphQL before posting.
    ==================  =======  ============================================

    Caching
    -------
    (owner, repo) → (repo_id, {category_name: category_id}) is cached in a
    module-level dict with a 1-hour TTL so repeated calls for the same repo
    do not issue redundant network round-trips.

    Rate limits
    -----------
    GitHub GraphQL: 5,000 points/hour per PAT; secondary limit of 80
    content-generating mutations/minute and 500/hour.  ``createDiscussion``
    costs ~1 point.  The adapter does not implement its own rate-limit
    tracking — it relies on the MCP server's retry policy for 429/403
    responses.

    Usage example
    -------------
    ::

        adapter = GitHubDiscussionsAdapter()
        if adapter.can_publish(variant):
            result = await adapter.publish(variant, profile, state_backend)
    """

    # ------------------------------------------------------------------
    # hints
    # ------------------------------------------------------------------

    def hints(self) -> ChannelHints:
        """Return static publishing constraints for the GitHub Discussions channel.

        GitHub Discussions imposes no practical body length limit (very long
        posts are accepted), renders full GitHub-Flavored Markdown (including
        diagrams, math, and GitHub-specific alerts), does not have a tag
        vocabulary (Discussions use categories, not tags), and does not natively
        support ``rel=canonical`` metadata.

        Returns
        -------
        ChannelHints
            Static metadata for LLM callers constructing a ``Variant`` targeting
            this channel.
        """
        return ChannelHints(
            max_length=None,                      # No enforced body length limit.
            supported_md_features=_GFM_FEATURES,  # Full GitHub-Flavored Markdown.
            tag_vocab=None,                       # Categories, not tags.
            cta_placement="footer",               # CTA after a horizontal rule.
            canonical_url_supported=False,        # No native canonical_url field.
            browser_only=False,
        )

    # ------------------------------------------------------------------
    # can_publish
    # ------------------------------------------------------------------

    def can_publish(self, variant: Variant) -> tuple[bool, str]:
        """Return ``(ok, reason)`` per the scheduler contract.

        Conditions:
        1. ``variant.channel`` starts with ``"github-discussions:"``.
        2. ``variant.extras`` contains ``"category"`` (the discussion category).
        3. Title and body are non-empty.
        """
        if not variant.channel.startswith("github-discussions:"):
            return False, f"channel-not-github-discussions: {variant.channel}"
        if not variant.extras.get("category"):
            return False, "missing-category-in-variant-extras"
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
        """Publish a variant as a new GitHub Discussion.

        Workflow
        --------
        1. **Idempotency claim** — calls ``state_backend.claim_idempotency_key()``.
           If the key is already claimed (content was already published to this
           channel), returns the existing result without making any API calls.
        2. **Parse owner/repo** from ``variant.channel``.
        3. **Resolve repository node ID and category node ID** via a single
           ``repository`` GraphQL query (cached for 1 hour per (owner, repo)
           pair to reduce API calls on repeated runs).
        4. **Build the discussion body** — if ``variant.canonical_url`` is set,
           appends a reference footer because GitHub Discussions has no native
           ``rel=canonical`` field::

               ---
               *Originally posted at [<url>](<url>).*

        5. **Execute ``createDiscussion`` mutation** with the assembled input.
        6. On success: calls ``state_backend.mark_published()`` and returns
           a :class:`PublishResult` with ``state="live"`` and the discussion URL.
        7. On any error: returns a :class:`PublishResult` with ``state="failed"``
           and a descriptive ``error`` message.

        Rate-limit responses (HTTP 429, or HTTP 403 with
        ``X-RateLimit-Remaining: 0``) are surfaced as failed results so the
        MCP server's retry policy (exponential backoff, 3 attempts) can handle
        them.

        Parameters
        ----------
        variant:
            Channel-adapted content variant.  Must pass :meth:`can_publish`.
        profile:
            Operator credential dict.  Must contain ``"GITHUB_TOKEN"`` (PAT).
        state_backend:
            Backend used for idempotency claims and publish-log writes.

        Returns
        -------
        PublishResult
            Outcome with ``state="live"`` on success or ``state="failed"`` with
            an ``error`` description on any failure.
        """
        token: str = profile["GITHUB_TOKEN"]
        category_name: str = variant.extras["category"]
        content_id: str = variant.extras.get("content_id") or variant.channel

        # --- 1. Idempotency check (sync API on backend) ---
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

        def _fail(error: str) -> PublishResult:
            state_backend.mark_published(
                content_id,
                variant.channel,
                state="failed",
                published_url=None,
                error=error,
            )
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error=error,
            )

        # --- 2. Parse owner/repo ---
        try:
            owner, repo = _parse_channel(variant.channel)
        except ValueError as exc:
            return _fail(str(exc))

        async with httpx.AsyncClient(timeout=30.0) as client:
            # --- 3. Resolve repo ID and category ID ---
            try:
                repo_id, category_id = await _resolve_repo_and_category(
                    client, token, owner, repo, category_name
                )
            except ValueError as exc:
                return _fail(str(exc))
            except httpx.HTTPStatusError as exc:
                return _fail(
                    f"HTTP {exc.response.status_code} resolving repo/"
                    f"categories: {exc.response.text[:200]}"
                )

            # --- 4. Build discussion body ---
            body_text = variant.body
            canonical = variant.canonical_url
            if canonical:
                body_text = (
                    f"{body_text}\n\n---\n"
                    f"*Originally posted at [{canonical}]({canonical}).*"
                )

            # --- 5. Execute createDiscussion mutation ---
            mutation_input: dict[str, Any] = {
                "repositoryId": repo_id,
                "categoryId": category_id,
                "title": variant.title,
                "body": body_text,
            }

            try:
                resp_body = await _gql_request(
                    client,
                    token,
                    _MUTATION_CREATE_DISCUSSION,
                    {"input": mutation_input},
                )
            except httpx.HTTPStatusError as exc:
                return _fail(
                    f"HTTP {exc.response.status_code} on createDiscussion: "
                    f"{exc.response.text[:200]}"
                )

        # --- 6. Handle GraphQL-level errors ---
        if gql_errors := resp_body.get("errors"):
            first_msg = gql_errors[0].get("message", str(gql_errors[0]))
            return _fail(f"GraphQL error: {first_msg}")

        try:
            discussion = resp_body["data"]["createDiscussion"]["discussion"]
            live_url: str = discussion["url"]
        except (KeyError, TypeError):
            return _fail(
                f"Unexpected createDiscussion response shape: {str(resp_body)[:200]}"
            )

        # --- 7. Persist and return success ---
        published_at = datetime.now(UTC)
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
        """Delete a previously created GitHub Discussion.

        Parses the repository owner/name and the discussion number from
        ``live_url``, resolves the discussion's GraphQL node ID via
        ``repository.discussion(number: N)``, then issues a
        ``deleteDiscussion`` mutation.

        .. note::
            The PAT must have the ``admin:discussion`` scope (or the
            authenticated user must be an organisation owner or team
            maintainer with admin access to the repository).  If the PAT
            lacks this scope, GitHub returns HTTP 401 or a GraphQL
            ``NOT_ALLOWED`` error and this method returns ``False``.

        Parameters
        ----------
        live_url:
            The public URL of the discussion to delete, as returned by
            :meth:`publish` in ``PublishResult.live_url``.  Expected form:
            ``https://github.com/<owner>/<repo>/discussions/<number>``
        profile:
            Operator credential dict.  Must contain ``"GITHUB_TOKEN"``.

        Returns
        -------
        bool
            ``True`` if the discussion was successfully deleted; ``False``
            on any error (parse failure, HTTP error, GraphQL error, or
            insufficient permissions).
        """
        token: str = profile["GITHUB_TOKEN"]

        # Parse discussion number and repo path from the URL.
        discussion_number = _parse_discussion_number_from_url(live_url)
        if discussion_number is None:
            return False

        # Reconstruct owner/repo from the URL path.
        # URL shape: https://github.com/<owner>/<repo>/discussions/<number>
        try:
            path_part = live_url.split("github.com/", 1)[1]
            path_segments = path_part.rstrip("/").split("/")
            # path_segments = [owner, repo, "discussions", number]
            if len(path_segments) < 4 or path_segments[2] != "discussions":
                return False
            owner, repo = path_segments[0], path_segments[1]
        except (IndexError, ValueError):
            return False

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Fetch the discussion node ID via a targeted query.
            get_discussion_query = """
            query($owner: String!, $repo: String!, $number: Int!) {
              repository(owner: $owner, name: $repo) {
                discussion(number: $number) {
                  id
                }
              }
            }
            """
            try:
                query_body = await _gql_request(
                    client,
                    token,
                    get_discussion_query,
                    {"owner": owner, "repo": repo, "number": discussion_number},
                )
            except httpx.HTTPStatusError:
                return False

            if query_body.get("errors"):
                return False

            try:
                node_id: str = (
                    query_body["data"]["repository"]["discussion"]["id"]
                )
            except (KeyError, TypeError):
                return False

            # Issue the deleteDiscussion mutation.
            try:
                del_body = await _gql_request(
                    client,
                    token,
                    _MUTATION_DELETE_DISCUSSION,
                    {"input": {"id": node_id}},
                )
            except httpx.HTTPStatusError:
                return False

        if del_body.get("errors"):
            return False

        try:
            deleted_id: str = del_body["data"]["deleteDiscussion"]["discussion"]["id"]
            return bool(deleted_id)
        except (KeyError, TypeError):
            return False
