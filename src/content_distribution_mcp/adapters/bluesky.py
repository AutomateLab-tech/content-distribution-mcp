"""
Bluesky channel adapter for Content Distribution MCP.

Wraps the atproto Python SDK (https://github.com/MarshalX/atproto) to publish
short posts to Bluesky on behalf of an authenticated account.

Authentication
--------------
Bluesky uses handle + app-password login. Generate an app-password at:
https://bsky.app/settings/app-passwords (NOT your main account password).

Channel format
--------------
``bluesky:<any-suffix>``  (e.g. ``bluesky:main``)

Required ``variant.extras`` keys
---------------------------------
``content_id``  Stable idempotency anchor. The adapter refuses to publish if
                missing.

Optional ``variant.extras`` keys
----------------------------------
None.

Behaviour
---------
Bluesky posts are capped at 300 graphemes. The adapter builds the post text
as ``<teaser> <canonical_url>`` and truncates the teaser so the total length
fits under the cap. Bluesky's renderer auto-detects URLs, so we do not need
explicit rich-text facets for the link to be clickable.

Python 3.11+.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ..models import ChannelHints, PublishResult, Variant


_MAX_POST_LENGTH = 300
_ELLIPSIS = "…"  # single-codepoint ellipsis: cheaper than "..."

_SUPPORTED_MD_FEATURES: set[str] = {"links"}


class BlueskyAdapter:
    """Channel adapter for Bluesky via the atproto SDK."""

    # ------------------------------------------------------------------
    # ChannelAdapter interface
    # ------------------------------------------------------------------

    def hints(self) -> ChannelHints:
        """Return static channel metadata for Bluesky."""
        return ChannelHints(
            max_length=_MAX_POST_LENGTH,
            supported_md_features=_SUPPORTED_MD_FEATURES,
            tag_vocab=None,
            cta_placement="none",
            canonical_url_supported=False,
            browser_only=False,
        )

    def can_publish(self, variant: Variant) -> tuple[bool, str]:
        """Return ``(ok, reason)`` — structural pre-flight only."""
        if not variant.channel.startswith("bluesky:"):
            return False, f"channel-not-bluesky: {variant.channel}"
        if not variant.body.strip():
            return False, "empty-body"
        if not (variant.extras and variant.extras.get("content_id")):
            return False, "missing-content-id-in-variant-extras"
        return True, ""

    async def publish(
        self,
        variant: Variant,
        profile: dict[str, Any] | None,
        state_backend: Any,
    ) -> PublishResult:
        """Publish a variant to Bluesky via ``Client.send_post``."""
        if not isinstance(profile, dict):
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="missing-profile",
            )

        handle = profile.get("BLUESKY_HANDLE")
        password = profile.get("BLUESKY_PASSWORD")
        if not handle or not password:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="missing-bluesky-credentials",
            )

        content_id = variant.extras.get("content_id")
        if not isinstance(content_id, str) or not content_id:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="missing-content-id-in-variant-extras",
            )

        # --- 1. Idempotency claim -----------------------------------------
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

        # --- 2. Build post text -------------------------------------------
        canonical = str(variant.canonical_url) if variant.canonical_url else ""
        text = _build_post_text(variant.body, canonical)

        # --- 3. Login + send_post (atproto is sync — wrap in to_thread) ---
        try:
            uri, _cid = await asyncio.to_thread(
                _send_bluesky_post, handle, password, text
            )
        except Exception as exc:  # noqa: BLE001 — surface every SDK error
            error_msg = f"bluesky-send-failed: {type(exc).__name__}: {exc}"
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

        # --- 4. Convert at:// URI to public bsky.app URL ------------------
        live_url = _at_uri_to_bsky_url(uri, handle)
        if live_url is None:
            shape_err = f"unparseable-at-uri: {uri}"
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

        # --- 5. Persist live state ----------------------------------------
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
            published_at=datetime.now(tz=timezone.utc),
        )

    def unpublish(self, live_url: str) -> tuple[bool, str]:
        """Bluesky deletion needs the AT URI; not implemented here."""
        return (
            False,
            f"bluesky-unpublish-not-implemented: visit {live_url} and delete manually",
        )


# ---------------------------------------------------------------------------
# Helpers (module-scope so tests can monkeypatch _send_bluesky_post)
# ---------------------------------------------------------------------------


def _send_bluesky_post(handle: str, password: str, text: str) -> tuple[str, str]:
    """Login + send a post synchronously. Returns (uri, cid)."""
    from atproto import Client  # optional dep — imported lazily

    client = Client()
    client.login(handle, password)
    response = client.send_post(text=text)
    return response.uri, response.cid


def _build_post_text(body: str, canonical_url: str) -> str:
    """Compose ``<teaser> <canonical_url>``, truncating teaser if needed.

    Bluesky counts graphemes, not characters; the SDK does that check
    server-side. Using ``len(str)`` is a close-enough approximation for
    ASCII-dominant teasers and matches what the SDK will reject.
    """
    body = body.strip()
    if not canonical_url:
        if len(body) <= _MAX_POST_LENGTH:
            return body
        return body[: _MAX_POST_LENGTH - 1] + _ELLIPSIS

    # Budget: full text = teaser + " " + url, plus possible ellipsis.
    suffix = " " + canonical_url
    suffix_len = len(suffix)
    if suffix_len >= _MAX_POST_LENGTH:
        # URL alone exceeds the cap — return just the URL.
        return canonical_url

    teaser_budget = _MAX_POST_LENGTH - suffix_len
    if len(body) <= teaser_budget:
        return body + suffix

    teaser = body[: teaser_budget - 1].rstrip() + _ELLIPSIS
    return teaser + suffix


def _at_uri_to_bsky_url(at_uri: str, handle: str) -> str | None:
    """Convert ``at://<did>/app.bsky.feed.post/<rkey>`` to a public bsky.app URL.

    Returns ``None`` if the URI is unparseable.
    """
    if not at_uri.startswith("at://"):
        return None
    parts = at_uri[len("at://") :].split("/")
    if len(parts) < 3:
        return None
    rkey = parts[-1]
    if not rkey:
        return None
    return f"https://bsky.app/profile/{handle}/post/{rkey}"
