"""
DEV.to channel adapter for the Content Distribution MCP.

Wraps the Forem API v1 (https://developers.forem.com/api/v1) to publish,
unpublish, and query hints for the DEV.to platform.

Auth: API key in ``api-key`` request header, loaded from the operator profile
      (a ``dict[str, Any]`` containing ``DEV_TO_API_KEY``).
Rate limits: 10 requests / 30 s. Honoured by retrying once on HTTP 429.
Canonical URL: natively supported via the ``canonical_url`` article field.
Unpublish: implemented as PUT with ``published: false`` (no hard-delete in DEV.to).

Contract
--------
Adapter call signatures match what the scheduler and ``RetryPolicy.wrap`` invoke:
- ``can_publish(variant) -> tuple[bool, str]``
- ``publish(variant, profile, state_backend) -> PublishResult``
- ``hints(profile) -> ChannelHints``
- ``unpublish(live_url, profile) -> bool``

The idempotency key is sourced from ``variant.extras["content_id"]``. Callers
(server.publish tool, scheduler) are responsible for populating that key from
the canonical ``Content.id``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import ChannelHints, PublishResult, Variant

# ---------------------------------------------------------------------------
# In-memory tag-vocabulary cache (no Redis; TTL = 24 h)
# ---------------------------------------------------------------------------

_TAG_CACHE: dict[str, Any] = {
    "tags": None,       # list[str] | None
    "fetched_at": 0.0,  # POSIX timestamp of last successful fetch
}
_TAG_CACHE_TTL = 86_400  # 24 hours in seconds

_DEVTO_API_BASE = "https://dev.to/api"


class DevToAdapter:
    """Channel adapter for DEV.to (Forem API v1).

    Channels handled: ``devto:*`` (typically ``devto:main``).

    The operator profile is a ``dict[str, Any]`` (as returned by
    ``YamlBackend.load_profile``) that must contain ``DEV_TO_API_KEY``.
    """

    # ------------------------------------------------------------------
    # ChannelAdapter interface
    # ------------------------------------------------------------------

    async def hints(self, profile: dict[str, Any]) -> ChannelHints:
        """Return channel-level publishing hints for DEV.to.

        Fetches the tag vocabulary lazily (24h cache) so the LLM caller can
        pick valid tags before constructing a Variant.
        """
        tags = await self._get_tag_vocab(profile)
        return ChannelHints(
            max_length=None,                  # DEV.to has no documented max
            supported_md_features={
                "bold", "italic", "code_inline", "code_block",
                "links", "headers", "images", "lists", "tables", "blockquote",
            },
            tag_vocab=tags,
            cta_placement="bottom",
            canonical_url_supported=True,
            browser_only=False,
        )

    def can_publish(self, variant: Variant) -> tuple[bool, str]:
        """Return ``(ok, reason)`` for ``scheduler.publish_immediate``.

        ``reason`` is empty on success and describes the rejection on failure.
        """
        if not variant.channel.startswith("devto:"):
            return False, f"channel-not-devto: {variant.channel}"
        if not variant.title:
            return False, "empty-title"
        if not variant.body:
            return False, "empty-body"
        return True, ""

    async def publish(
        self,
        variant: Variant,
        profile: dict[str, Any] | None,
        state_backend: Any,
    ) -> PublishResult:
        """Publish a variant to DEV.to.

        The idempotency key is ``(content_id, variant.channel)`` where
        ``content_id`` is read from ``variant.extras["content_id"]``. When the
        key was previously claimed and a ``"live"`` row exists in the post log,
        the existing result is returned without hitting the API.
        """
        if profile is None:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="missing-profile",
            )

        content_id = self._content_id_from_variant(variant)
        if content_id is None:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="missing-content-id-in-variant-extras",
            )

        # --- 1. Idempotency check (sync API on YamlBackend) ---
        claimed = state_backend.claim_idempotency_key(content_id, variant.channel)
        if not claimed:
            existing = state_backend.lookup_published(content_id, variant.channel)
            if existing is not None:
                return PublishResult(
                    channel=variant.channel,
                    state="live",
                    live_url=existing.get("published_url"),
                )
            # Claimed but no live row — treat as in-flight and refuse.
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="idempotency-claimed-but-no-live-row",
            )

        # --- 2. Build request body ---
        article: dict[str, Any] = {
            "title": variant.title,
            "body_markdown": variant.body,
            "published": True,
            "tags": list(variant.tags or []),
        }
        if variant.canonical_url:
            article["canonical_url"] = str(variant.canonical_url)
        payload = {"article": article}

        api_key = self._get_api_key(profile)

        # --- 3 & 4. POST with single rate-limit retry ---
        result = await self._post_article(payload, api_key, channel=variant.channel)

        # --- 5. Persist final state on the post-log claiming stub ---
        state_backend.mark_published(
            content_id,
            variant.channel,
            state=result.state,
            published_url=str(result.live_url) if result.live_url else None,
            error=result.error,
        )

        return result

    async def unpublish(self, live_url: str, profile: dict[str, Any]) -> bool:
        """Set ``published: false`` on a DEV.to article. No hard-delete."""
        article_id = self._parse_article_id_from_url(live_url)
        if article_id is None:
            return False

        api_key = self._get_api_key(profile)
        url = f"{_DEVTO_API_BASE}/articles/{article_id}"
        headers = {"api-key": api_key, "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.put(
                    url,
                    json={"article": {"published": False}},
                    headers=headers,
                )
        except httpx.RequestError:
            return False

        return resp.status_code in (200, 204)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _content_id_from_variant(variant: Variant) -> str | None:
        cid = variant.extras.get("content_id") if variant.extras else None
        return cid if isinstance(cid, str) and cid else None

    async def _post_article(
        self,
        payload: dict[str, Any],
        api_key: str,
        channel: str,
    ) -> PublishResult:
        """Execute ``POST /api/articles`` with one retry on HTTP 429."""
        url = f"{_DEVTO_API_BASE}/articles"
        headers = {"api-key": api_key, "Content-Type": "application/json"}

        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(url, json=payload, headers=headers)
            except httpx.RequestError as exc:
                return PublishResult(
                    channel=channel,
                    state="failed",
                    error=f"http-request-error: {exc}",
                )

            if resp.status_code == 201:
                return self._parse_success(resp, channel=channel)

            if resp.status_code == 429:
                if attempt == 0:
                    retry_after = _parse_retry_after(resp)
                    await asyncio.sleep(retry_after)
                    continue
                return PublishResult(channel=channel, state="failed", error="429 rate-limited")

            # 4xx / 5xx (non-429)
            return PublishResult(
                channel=channel,
                state="failed",
                error=f"{resp.status_code}: {resp.text[:200]}",
            )

        return PublishResult(channel=channel, state="failed", error="unexpected publish loop exit")

    @staticmethod
    def _parse_success(resp: httpx.Response, *, channel: str) -> PublishResult:
        try:
            data = resp.json()
            live_url = data.get("url") or None
        except Exception:  # noqa: BLE001
            live_url = None

        return PublishResult(
            channel=channel,
            state="live",
            live_url=live_url,
            published_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _parse_article_id_from_url(live_url: str) -> str | None:
        """Resolve DEV.to article ID by GETting ``/api/articles/{user}/{slug}``."""
        try:
            path = live_url.rstrip("/").split("dev.to/", 1)[1]
            parts = path.split("/")
            if len(parts) < 2:
                return None
            username, slug = parts[0], parts[1]
        except (IndexError, ValueError):
            return None

        lookup_url = f"{_DEVTO_API_BASE}/articles/{username}/{slug}"
        try:
            resp = httpx.get(lookup_url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                return str(data.get("id", ""))
        except httpx.RequestError:
            pass
        return None

    @staticmethod
    def _get_api_key(profile: dict[str, Any]) -> str:
        """Read ``DEV_TO_API_KEY`` from the profile dict.

        Accepts either ``profile["DEV_TO_API_KEY"]`` (flat) or
        ``profile["credentials"]["DEV_TO_API_KEY"]`` (nested), so this works
        with both the bare profile dict and Notion-shaped profiles that nest
        secrets under a ``credentials`` key.
        """
        if "DEV_TO_API_KEY" in profile:
            return str(profile["DEV_TO_API_KEY"])
        creds = profile.get("credentials") or {}
        if isinstance(creds, dict) and "DEV_TO_API_KEY" in creds:
            return str(creds["DEV_TO_API_KEY"])
        raise KeyError("DEV_TO_API_KEY missing from profile")

    async def _get_tag_vocab(self, profile: dict[str, Any]) -> list[str] | None:
        now = time.monotonic()
        if (
            _TAG_CACHE["tags"] is not None
            and (now - _TAG_CACHE["fetched_at"]) < _TAG_CACHE_TTL
        ):
            return _TAG_CACHE["tags"]

        api_key = self._get_api_key(profile)
        tags = await _fetch_tags(api_key)
        if tags is not None:
            _TAG_CACHE["tags"] = tags
            _TAG_CACHE["fetched_at"] = now
        return _TAG_CACHE["tags"]


# ---------------------------------------------------------------------------
# Module-level async helpers
# ---------------------------------------------------------------------------


async def _fetch_tags(api_key: str) -> list[str] | None:
    """Fetch the first page of DEV.to tags (100 items)."""
    url = f"{_DEVTO_API_BASE}/tags"
    headers = {"api-key": api_key}
    params = {"per_page": 100, "page": 1}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code == 200:
            data = resp.json()
            return [item["name"] for item in data if "name" in item]
    except (httpx.RequestError, KeyError, ValueError):
        pass
    return None


def _parse_retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("retry-after", "")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 30.0
