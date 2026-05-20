"""
Hashnode browser-fallback adapter for the Content Distribution MCP.

Hashnode's public GraphQL API moved to a paid tier on 2026-05-13.  This
adapter provides the internal-use replacement: write a markdown draft with
a header comment block containing the title and canonical URL, return the
Hashnode compose URL, and (optionally) pre-fill the editor via Playwright.
The operator sets title / canonical URL / tags in the Hashnode editor and
clicks Publish. They then call :func:`mark_live` to record the live URL.

The public HashnodeAdapter (``hashnode.py``) stays in the package for users
who subscribe to the paid API tier.  This adapter is the *browser fallback*
for operators who do not.

Channel format: ``hashnode-browser:<publication-slug>`` where
``<publication-slug>`` is the Hashnode publication/blog slug (e.g.
``automatelab``).  Use ``personal`` for the user's default personal blog.

Hashnode natively supports the full markdown feature set.  There is no
practical character cap; the editor enforces none.  Canonical URL is set via
the post's Settings panel ("Add canonical URL") — this adapter includes it
prominently in the draft header comment so the operator cannot miss it.

The idempotency key is sourced from ``variant.extras["content_id"]``.
"""

from __future__ import annotations

import re
import webbrowser
from pathlib import Path
from typing import Any

from ..models import ChannelHints, PublishResult, Variant


_HASHNODE_COMPOSE_URL = "https://hashnode.com/post"
_DRAFTS_DIR = Path.home() / ".distribution-mcp" / "drafts"

# Hashnode renders the full CommonMark + GFM feature set.
_SUPPORTED_MD_FEATURES: set[str] = {
    "headings",
    "bold",
    "italic",
    "code",
    "fenced_code_blocks",
    "tables",
    "links",
    "images",
    "blockquotes",
    "lists",
    "hr",
}


class HashnodeBrowserAdapter:
    """Channel adapter for Hashnode — browser-only fallback (API is paid-tier)."""

    # ------------------------------------------------------------------
    # ChannelAdapter interface
    # ------------------------------------------------------------------

    def hints(self) -> ChannelHints:
        """Return static channel metadata for Hashnode browser."""
        return ChannelHints(
            max_length=None,
            supported_md_features=_SUPPORTED_MD_FEATURES,
            tag_vocab=None,
            cta_placement="bottom",
            canonical_url_supported=True,
            browser_only=True,
        )

    def can_publish(self, variant: Variant) -> tuple[bool, str]:
        """Return ``(ok, reason)`` — structural pre-flight only."""
        if not variant.channel.startswith("hashnode-browser:"):
            return False, f"channel-not-hashnode-browser: {variant.channel}"
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
        """Run the Hashnode browser-fallback publish flow.

        Writes a markdown draft to disk, returns the Hashnode compose URL,
        and records ``state="needs_browser"`` in the post log.  The operator
        pastes the draft into the editor, sets the canonical URL in the post
        Settings panel, and submits manually.  They then call
        :func:`mark_live` with the published URL.
        """
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
            # Prior needs_browser stub — surface compose URL so operator can finish.
            return PublishResult(
                channel=variant.channel,
                state="needs_browser",
                compose_url=_HASHNODE_COMPOSE_URL,
            )

        # --- 2. Write draft file -----------------------------------------
        channel_slug = _safe_filename(variant.channel)
        draft_dir = _DRAFTS_DIR / _safe_filename(content_id)
        draft_dir.mkdir(parents=True, exist_ok=True)

        draft_path = draft_dir / f"{channel_slug}.md"
        draft_path.write_text(_build_draft_text(variant), encoding="utf-8")

        # --- 3. Compose URL + optional Playwright pre-fill ---------------
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
                compose_url=_HASHNODE_COMPOSE_URL,
                body=variant.body.strip(),
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
            compose_url=_HASHNODE_COMPOSE_URL,
            live_url=None,
        )

    def unpublish(self, live_url: str) -> tuple[bool, str]:
        """Hashnode has no browser-initiated programmatic unpublish."""
        return (
            False,
            f"hashnode-unpublish-requires-manual: visit {live_url} and delete the post",
        )


# ---------------------------------------------------------------------------
# Operator helpers
# ---------------------------------------------------------------------------


def open_pending_in_tabs(
    content_id: str,
    state_backend: Any,
) -> list[str]:
    """Open every pending needs_browser Hashnode variant for ``content_id``."""
    entries = state_backend.list_post_log(
        content_id=content_id, state="needs_browser"
    )
    compose_urls: list[str] = []
    for entry in entries:
        if not entry.get("channel", "").startswith("hashnode-browser:"):
            continue
        webbrowser.open_new_tab(_HASHNODE_COMPOSE_URL)
        compose_urls.append(_HASHNODE_COMPOSE_URL)
    return compose_urls


def mark_live(
    content_id: str,
    channel: str,
    live_url: str,
    state_backend: Any,
) -> None:
    """Record the live URL after the operator publishes the post manually."""
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
    """Render the markdown draft for a Hashnode variant.

    Prepends a header comment block with the title and canonical URL so the
    operator can copy them into the Hashnode editor's Title field and Settings
    panel without hunting through the body.
    """
    lines: list[str] = []

    # Header block — human instructions, not part of the article body.
    lines.append("<!--")
    lines.append("  HASHNODE DRAFT — paste body below into the editor.")
    lines.append("")
    if variant.title:
        lines.append(f"  TITLE:        {variant.title}")
    if variant.canonical_url:
        lines.append(f"  CANONICAL URL (set in Settings > Add canonical URL):")
        lines.append(f"                {variant.canonical_url}")
    if variant.tags:
        lines.append(f"  TAGS:         {', '.join(variant.tags)}")
    lines.append("-->")
    lines.append("")

    body = variant.body.strip()
    if variant.cta_block:
        body = body + "\n\n" + variant.cta_block.strip()
    lines.append(body)
    lines.append("")

    return "\n".join(lines)


def _safe_filename(value: str) -> str:
    """Sanitise *value* into a filesystem-safe filename."""
    return re.sub(r"[^\w\-]", "-", value).strip("-")


# ---------------------------------------------------------------------------
# Optional Playwright pre-fill
# ---------------------------------------------------------------------------


async def _playwright_prefill(
    compose_url: str,
    body: str,
    profile_dir: str,
) -> None:
    """Best-effort pre-fill of the Hashnode editor via headed Chromium.

    Silently returns if Playwright is not installed or any step fails.
    The operator must still set the title and canonical URL in the Settings
    panel and click Publish manually.
    """
    try:
        from playwright.async_api import async_playwright
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

            # Hashnode's editor uses a ProseMirror-based contenteditable div.
            try:
                editor_selector = "div.ProseMirror, div[contenteditable='true']"
                await page.click(editor_selector, timeout=8_000)
                await page.keyboard.insert_text(body)
            except Exception:  # noqa: BLE001
                pass

            await page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001
        return
