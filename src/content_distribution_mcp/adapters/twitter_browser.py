"""
Twitter / X browser-fallback adapter for the Content Distribution MCP.

X's v2 API now requires a paid Basic tier ($200/month) for posting tweets, and
even the free tier rate-limits writes to a degree that makes it unusable for
small-batch distribution. So this adapter mirrors the Medium / LinkedIn
browser-fallback pattern: write a plain-text draft, return the compose URL,
and (optionally) pre-fill the editor via Playwright. The operator submits
manually and calls :func:`mark_live` once the tweet is live.

Channel format: ``twitter-browser:<handle>`` where ``<handle>`` is either:

* ``personal`` — the authenticated user's default account
* a specific account handle (e.g. ``automatelab``) — informational only,
  since X's compose URL doesn't distinguish accounts beyond what the browser
  session has logged in

The compose URL is always ``https://x.com/compose/post`` (X retired the
twitter.com domain for compose in 2024). Tweets are capped at 280 chars for
free accounts and 25_000 for X Premium; the adapter does not truncate,
because the in-browser composer enforces both limits clearly.

The idempotency key is sourced from ``variant.extras["content_id"]``.
"""

from __future__ import annotations

import re
import webbrowser
from pathlib import Path
from typing import Any

from ..models import ChannelHints, PublishResult, Variant


_COMPOSE_URL = "https://x.com/compose/post"
_DRAFTS_DIR = Path.home() / ".distribution-mcp" / "drafts"
_MAX_TWEET_LENGTH = 280  # free-tier cap; informational

_SUPPORTED_MD_FEATURES: set[str] = {"links"}


class TwitterBrowserAdapter:
    """Channel adapter for Twitter / X — browser-only (paid API not used)."""

    # ------------------------------------------------------------------
    # ChannelAdapter interface
    # ------------------------------------------------------------------

    def hints(self) -> ChannelHints:
        """Return static channel metadata for Twitter / X."""
        return ChannelHints(
            max_length=_MAX_TWEET_LENGTH,
            supported_md_features=_SUPPORTED_MD_FEATURES,
            tag_vocab=None,
            cta_placement="none",
            canonical_url_supported=False,
            browser_only=True,
        )

    def can_publish(self, variant: Variant) -> tuple[bool, str]:
        """Return ``(ok, reason)`` — structural pre-flight only."""
        if not variant.channel.startswith("twitter-browser:"):
            return False, f"channel-not-twitter-browser: {variant.channel}"
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
        """Run the Twitter / X browser-fallback publish flow."""
        content_id = variant.extras.get("content_id") if variant.extras else None
        if not isinstance(content_id, str) or not content_id:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="missing-content-id-in-variant-extras",
            )

        # --- 1. Idempotency check ----------------------------------------
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
                state="needs_browser",
                compose_url=_COMPOSE_URL,  # type: ignore[arg-type]
            )

        # --- 2. Write draft file -----------------------------------------
        channel_slug = _safe_filename(variant.channel)
        draft_dir = _DRAFTS_DIR / _safe_filename(content_id)
        draft_dir.mkdir(parents=True, exist_ok=True)

        draft_path = draft_dir / f"{channel_slug}.txt"
        draft_path.write_text(_build_draft_text(variant), encoding="utf-8")

        # --- 3. Compose URL + optional Playwright pre-fill ---------------
        compose_url = _COMPOSE_URL

        prefill = False
        if isinstance(profile, dict):
            extras = profile.get("extras")
            if isinstance(extras, dict):
                prefill = bool(extras.get("playwright_prefill"))

        if prefill:
            assert isinstance(profile, dict)
            extras = profile.get("extras", {}) or {}
            profile_dir = extras.get(
                "playwright_profile_dir",
                str(Path.home() / ".distribution-mcp" / "playwright-profile"),
            )
            await _playwright_prefill(
                compose_url=compose_url,
                body=_build_draft_text(variant),
                profile_dir=profile_dir,
            )

        # --- 4. Persist needs_browser state ------------------------------
        state_backend.mark_published(
            content_id,
            variant.channel,
            state="needs_browser",
            published_url=None,
            error=None,
        )

        return PublishResult(
            channel=variant.channel,
            state="needs_browser",
            draft_path=draft_path,
            compose_url=compose_url,  # type: ignore[arg-type]
            live_url=None,
        )

    def unpublish(self, live_url: str) -> tuple[bool, str]:
        """X has no programmatic unpublish via this adapter — manual delete."""
        return (
            False,
            f"twitter-unpublish-requires-manual: visit {live_url} and delete the post",
        )


# ---------------------------------------------------------------------------
# Operator helpers
# ---------------------------------------------------------------------------


def open_pending_in_tabs(
    content_id: str,
    state_backend: Any,
) -> list[str]:
    """Open every pending needs_browser Twitter / X variant for ``content_id``."""
    entries = state_backend.list_post_log(
        content_id=content_id, state="needs_browser"
    )

    compose_urls: list[str] = []
    for entry in entries:
        channel = entry.get("channel", "")
        if not channel.startswith("twitter-browser:"):
            continue
        compose_urls.append(_COMPOSE_URL)
        webbrowser.open_new_tab(_COMPOSE_URL)

    return compose_urls


def mark_live(
    content_id: str,
    channel: str,
    live_url: str,
    state_backend: Any,
) -> None:
    """Append a ``state="live"`` row after the operator submits manually."""
    state_backend.claim_idempotency_key(content_id, channel)
    state_backend.mark_published(
        content_id,
        channel,
        state="live",
        published_url=live_url,
        error=None,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_draft_text(variant: Variant) -> str:
    """Render the plain-text draft body for a tweet."""
    body = variant.body.strip()
    if variant.cta_block:
        body = body + "\n\n" + variant.cta_block.strip()
    return body + "\n"


def _safe_filename(value: str) -> str:
    return re.sub(r"[^\w\-]", "-", value).strip("-")


# ---------------------------------------------------------------------------
# Optional Playwright pre-fill
# ---------------------------------------------------------------------------


async def _playwright_prefill(
    compose_url: str,
    body: str,
    profile_dir: str,
) -> None:
    """Best-effort pre-fill of the X compose editor via headed Chromium."""
    try:
        from playwright.async_api import async_playwright  # optional dep
    except ImportError:
        return

    try:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                channel="chrome",
            )
            page = await context.new_page()
            await page.goto(compose_url, wait_until="networkidle", timeout=30_000)

            try:
                editor_selector = "div[data-testid='tweetTextarea_0'], div[role='textbox']"
                await page.click(editor_selector, timeout=5_000)
                await page.keyboard.insert_text(body)
            except Exception:  # noqa: BLE001
                pass

            await page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001
        return
