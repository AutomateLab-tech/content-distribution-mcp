"""
LinkedIn channel adapter for the Content Distribution MCP.

Wraps the LinkedIn Posts API
(https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api)
to publish text posts to personal profiles or organisation pages.

Auth: OAuth 2.0 bearer token stored in the distribution profile. Run
``content-distribution-mcp linkedin install`` once per profile to capture
access/refresh tokens. The adapter auto-rotates expired access tokens using
the stored refresh token.

Channel format: ``linkedin:<target>`` where:
- ``personal``    — post to the authenticated user's personal feed
- ``<org-id>``    — numeric organisation ID (e.g. ``116012269``)
- ``urn:li:...``  — full URN passthrough (advanced use)

Content format: LinkedIn posts are plain text (no Markdown rendering). The
adapter emits the body verbatim; callers should strip MD syntax beforehand.
Canonical URL is appended as a ``Read more: <url>`` footer line.

Post length limit: ~3 000 characters (LinkedIn enforces server-side; the
adapter does not truncate).

Unpublish: DELETE /rest/posts/<urn> — works within LinkedIn's editorial window.
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import ChannelHints, PublishResult, Variant
from .linkedin_oauth import (
    LINKEDIN_ACCESS_TOKEN,
    LINKEDIN_PERSON_ID,
    LinkedInOAuthError,
    is_token_expired,
    refresh_access_token,
)

# ---------------------------------------------------------------------------
# LinkedIn API constants
# ---------------------------------------------------------------------------

_LINKEDIN_API_BASE = "https://api.linkedin.com"
_POSTS_ENDPOINT = f"{_LINKEDIN_API_BASE}/rest/posts"
_API_VERSION = "202501"  # LinkedIn-Version header (YYYYMM)

_MAX_POST_LENGTH = 3000
_SUPPORTED_MD_FEATURES: set[str] = {"links"}  # plain text only; bare URLs survive


class LinkedInAdapter:
    """Channel adapter for LinkedIn (Posts API, OAuth 2.0).

    Handles ``linkedin:*`` channels.
    """

    # ------------------------------------------------------------------
    # ChannelAdapter interface
    # ------------------------------------------------------------------

    def hints(self) -> ChannelHints:
        """Return static channel metadata for LinkedIn."""
        return ChannelHints(
            max_length=_MAX_POST_LENGTH,
            supported_md_features=_SUPPORTED_MD_FEATURES,
            tag_vocab=None,
            cta_placement="bottom",
            canonical_url_supported=False,
            browser_only=False,
        )

    def can_publish(self, variant: Variant) -> tuple[bool, str]:
        """Return ``(ok, reason)`` — structural pre-flight only (no API calls)."""
        if not variant.channel.startswith("linkedin:"):
            return False, f"channel-not-linkedin: {variant.channel}"
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
        """Publish a variant to LinkedIn via the Posts API.

        The idempotency key is ``(content_id, variant.channel)``. Re-running
        with the same pair returns the existing live result without re-posting.
        """
        if profile is None:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="missing-profile",
            )

        content_id = (variant.extras or {}).get("content_id")
        if not isinstance(content_id, str) or not content_id:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="missing-content-id-in-variant-extras",
            )

        # --- 1. Idempotency check ---
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

        # --- 2. Ensure valid access token ---
        try:
            profile = await self._ensure_valid_token(profile, state_backend)
        except LinkedInOAuthError as exc:
            state_backend.mark_published(
                content_id, variant.channel, state="failed", error=str(exc)
            )
            return PublishResult(channel=variant.channel, state="failed", error=str(exc))

        access_token = profile.get(LINKEDIN_ACCESS_TOKEN)
        if not access_token:
            err = "LINKEDIN_ACCESS_TOKEN missing from profile — run linkedin install"
            state_backend.mark_published(
                content_id, variant.channel, state="failed", error=err
            )
            return PublishResult(channel=variant.channel, state="failed", error=err)

        # --- 3. Resolve author URN ---
        target = _target_slug(variant.channel)
        author_urn = _resolve_author_urn(target, profile)
        if author_urn is None:
            err = (
                f"Cannot resolve author URN for target={target!r}. "
                "For 'personal', ensure LINKEDIN_PERSON_ID is set in the profile. "
                "For org pages, pass a numeric org ID as the channel sub-target."
            )
            state_backend.mark_published(
                content_id, variant.channel, state="failed", error=err
            )
            return PublishResult(channel=variant.channel, state="failed", error=err)

        # --- 4. POST to LinkedIn ---
        text = _build_post_text(variant)
        result = await self._post_to_linkedin(
            author_urn=author_urn,
            text=text,
            access_token=access_token,
            channel=variant.channel,
        )

        # --- 5. Persist ---
        state_backend.mark_published(
            content_id,
            variant.channel,
            state=result.state,
            published_url=str(result.live_url) if result.live_url else None,
            error=result.error,
        )
        return result

    async def unpublish(self, live_url: str, profile: dict[str, Any]) -> tuple[bool, str]:
        """Delete a LinkedIn post via DELETE /rest/posts/<urn>.

        Returns ``(True, "")`` on success, ``(False, reason)`` on failure.
        """
        post_urn = _post_urn_from_url(live_url)
        if post_urn is None:
            return False, f"cannot-parse-post-urn-from-url: {live_url}"

        access_token = profile.get(LINKEDIN_ACCESS_TOKEN, "")
        if not access_token:
            return False, "LINKEDIN_ACCESS_TOKEN missing from profile"

        encoded_urn = urllib.parse.quote(post_urn, safe="")
        url = f"{_POSTS_ENDPOINT}/{encoded_urn}"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.delete(url, headers=_api_headers(access_token))
        except httpx.RequestError as exc:
            return False, f"http-request-error: {exc}"

        if resp.status_code in (200, 204):
            return True, ""
        return False, f"delete-failed: HTTP {resp.status_code} — {resp.text[:200]}"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _ensure_valid_token(
        self, profile: dict[str, Any], state_backend: Any
    ) -> dict[str, Any]:
        """Refresh the access token if expired; persist updated profile."""
        if not is_token_expired(profile):
            return profile

        updated = await refresh_access_token(profile)

        # Persist updated tokens back to the profile in the StateBackend.
        # Match by LINKEDIN_PERSON_ID so we update the right profile.
        if hasattr(state_backend, "list_profiles") and hasattr(state_backend, "save_profile"):
            person_id = updated.get(LINKEDIN_PERSON_ID)
            for name in state_backend.list_profiles():
                stored = state_backend.load_profile(name) or {}
                if stored.get(LINKEDIN_PERSON_ID) == person_id:
                    merged = {
                        **stored,
                        **{
                            k: updated[k]
                            for k in (
                                "LINKEDIN_ACCESS_TOKEN",
                                "LINKEDIN_REFRESH_TOKEN",
                                "LINKEDIN_TOKEN_EXPIRY",
                            )
                            if k in updated
                        },
                    }
                    state_backend.save_profile(name, merged)
                    break

        return updated

    async def _post_to_linkedin(
        self,
        author_urn: str,
        text: str,
        access_token: str,
        channel: str,
    ) -> PublishResult:
        """Execute POST /rest/posts with a single retry on HTTP 429."""
        payload = _build_post_payload(author_urn, text)
        headers = _api_headers(access_token)

        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(_POSTS_ENDPOINT, json=payload, headers=headers)
            except httpx.RequestError as exc:
                return PublishResult(
                    channel=channel,
                    state="failed",
                    error=f"http-request-error: {exc}",
                )

            if resp.status_code == 201:
                # LinkedIn returns the post URN in the x-restli-id response header.
                post_urn = resp.headers.get("x-restli-id") or resp.headers.get("id")
                live_url = (
                    f"https://www.linkedin.com/feed/update/{post_urn}/"
                    if post_urn
                    else None
                )
                return PublishResult(
                    channel=channel,
                    state="live",
                    live_url=live_url,  # type: ignore[arg-type]
                    published_at=datetime.now(timezone.utc),
                )

            if resp.status_code == 429 and attempt == 0:
                retry_after = _parse_retry_after(resp)
                await asyncio.sleep(retry_after)
                continue

            return PublishResult(
                channel=channel,
                state="failed",
                error=f"{resp.status_code}: {resp.text[:200]}",
            )

        return PublishResult(
            channel=channel, state="failed", error="unexpected publish loop exit"
        )


# ---------------------------------------------------------------------------
# Module-level pure helpers (easily unit-tested without instantiating the adapter)
# ---------------------------------------------------------------------------


def _target_slug(channel: str) -> str:
    """Extract the target from ``linkedin:<target>``."""
    return channel.split("linkedin:", 1)[-1]


def _resolve_author_urn(target: str, profile: dict[str, Any]) -> str | None:
    """Map a channel target slug to a LinkedIn author URN.

    * ``personal`` (or empty) → ``LINKEDIN_PERSON_ID`` from profile
    * numeric string           → ``urn:li:organization:<id>``
    * full URN passthrough     → returned as-is
    """
    if not target or target.lower() == "personal":
        person_id = profile.get(LINKEDIN_PERSON_ID)
        if not person_id:
            return None
        pid = str(person_id)
        return pid if pid.startswith("urn:li:") else f"urn:li:person:{pid}"
    if target.isdigit():
        return f"urn:li:organization:{target}"
    if target.startswith("urn:li:"):
        return target
    return None


def _build_post_text(variant: Variant) -> str:
    """Compose the post text: body + optional CTA + optional canonical footer."""
    parts = [variant.body.strip()]
    if variant.cta_block:
        parts.append(variant.cta_block.strip())
    if variant.canonical_url:
        parts.append(f"Read more: {variant.canonical_url}")
    return "\n\n".join(parts)


def _build_post_payload(author_urn: str, text: str) -> dict[str, Any]:
    """Build the LinkedIn Posts API request body."""
    return {
        "author": author_urn,
        "lifecycleState": "PUBLISHED",
        "visibility": "PUBLIC",
        "commentary": text,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
    }


def _api_headers(access_token: str) -> dict[str, str]:
    """Return the standard headers required by the LinkedIn Posts API."""
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "LinkedIn-Version": _API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }


def _post_urn_from_url(live_url: str) -> str | None:
    """Parse the post URN from a ``/feed/update/<urn>/`` URL."""
    match = re.search(r"/feed/update/([^/?#]+)", live_url)
    if match:
        return urllib.parse.unquote(match.group(1))
    return None


def _parse_retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("retry-after", "")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 30.0
