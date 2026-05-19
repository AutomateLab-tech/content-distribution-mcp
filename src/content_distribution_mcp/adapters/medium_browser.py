"""
Medium browser-fallback adapter for the Content Distribution MCP.

Medium has no public Partner Program API in 2026, so this adapter writes a
local Markdown draft, returns a compose URL, and (optionally) pre-fills the
editor via Playwright. The operator then submits manually and calls
:func:`mark_live` once the post is live.

Channel format: ``medium-browser:<publication>`` where ``<publication>`` is
either ``personal`` for the personal feed or a Medium publication slug.

The idempotency key is sourced from ``variant.extras["content_id"]`` — the
same convention used by every other adapter in this package. Re-publishing
the same ``(content_id, channel)`` short-circuits to the recorded needs-browser
result without rewriting the draft.
"""

from __future__ import annotations

import re
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import ChannelHints, PublishResult, Variant


_MEDIUM_COMPOSE_BASE = "https://medium.com"
_DRAFTS_DIR = Path.home() / ".distribution-mcp" / "drafts"

_SUPPORTED_MD_FEATURES: set[str] = {
    "bold", "italic", "code_inline", "code_block",
    "headers", "links", "blockquote", "lists",
    "images", "horizontal_rule",
}


class MediumBrowserAdapter:
    """Channel adapter for Medium — browser-only (no public API)."""

    # ------------------------------------------------------------------
    # ChannelAdapter interface
    # ------------------------------------------------------------------

    def hints(self) -> ChannelHints:
        """Return static channel metadata for Medium."""
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
        if not variant.channel.startswith("medium-browser:"):
            return False, f"channel-not-medium-browser: {variant.channel}"
        if not variant.title.strip():
            return False, "empty-title"
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
        """Run the Medium browser-fallback publish flow.

        Writes a Markdown draft to disk, returns a compose URL, and records
        ``state="needs_browser"`` in the post log. The operator submits the
        draft manually and later calls :func:`mark_live`.
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
            # No live row yet — surface the prior needs_browser handoff via
            # the compose URL so the operator can finish submitting.
            pub_slug = _publication_slug(variant.channel)
            return PublishResult(
                channel=variant.channel,
                state="needs_browser",
                compose_url=_build_compose_url(pub_slug),  # type: ignore[arg-type]
            )

        # --- 2. Write draft file -----------------------------------------
        pub_slug = _publication_slug(variant.channel)
        channel_slug = _safe_filename(variant.channel)
        draft_dir = _DRAFTS_DIR / _safe_filename(content_id)
        draft_dir.mkdir(parents=True, exist_ok=True)

        draft_path = draft_dir / f"{channel_slug}.md"
        draft_path.write_text(_build_draft_markdown(variant), encoding="utf-8")

        # --- 3. Compose URL + optional Playwright pre-fill ---------------
        compose_url = _build_compose_url(pub_slug)

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
                title=variant.title,
                body=variant.body,
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
        """Medium has no programmatic unpublish path — always returns False."""
        return (
            False,
            f"medium-unpublish-requires-manual: visit {live_url}/edit and unpublish",
        )


# ---------------------------------------------------------------------------
# Operator helpers
# ---------------------------------------------------------------------------


def open_pending_in_tabs(
    content_id: str,
    state_backend: Any,
) -> list[str]:
    """Open every pending needs_browser Medium variant for ``content_id``.

    Looks up post-log entries with ``state="needs_browser"`` for the given
    content, reconstructs each compose URL from the channel slug, and opens
    each one in a new browser tab.
    """
    entries = state_backend.list_post_log(
        content_id=content_id, state="needs_browser"
    )

    compose_urls: list[str] = []
    for entry in entries:
        channel = entry.get("channel", "")
        if not channel.startswith("medium-browser:"):
            continue
        url = _build_compose_url(_publication_slug(channel))
        compose_urls.append(url)
        webbrowser.open_new_tab(url)

    return compose_urls


def mark_live(
    content_id: str,
    channel: str,
    live_url: str,
    state_backend: Any,
) -> None:
    """Append a ``state="live"`` row after the operator submits manually.

    The publish flow leaves a ``needs_browser`` row in the post-log, which
    :meth:`StateBackend.mark_published` will not update (it only flips
    ``claiming`` stubs). We claim a fresh idempotency stub for the live URL
    and flip that to ``live``, so subsequent ``publish()`` calls dedupe via
    the new live row.
    """
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


def _publication_slug(channel: str) -> str:
    """Extract the publication slug from ``medium-browser:<publication>``."""
    return channel.split("medium-browser:", 1)[-1]


def _build_compose_url(pub_slug: str) -> str:
    """Build the Medium compose/editor URL for a publication slug."""
    if not pub_slug or pub_slug.lower() == "personal":
        return f"{_MEDIUM_COMPOSE_BASE}/new-story"
    return f"{_MEDIUM_COMPOSE_BASE}/p/{pub_slug}/edit"


def _build_draft_markdown(variant: Variant) -> str:
    """Render the YAML-frontmatter + body Markdown file for a Medium variant."""
    canonical = str(variant.canonical_url) if variant.canonical_url else ""
    tags_line = ", ".join(variant.tags) if variant.tags else ""
    subtitle = variant.extras.get("subtitle", "") if variant.extras else ""
    cta = variant.cta_block or ""

    lines = ["---", f"title: {variant.title}"]
    if subtitle:
        lines.append(f"subtitle: {subtitle}")
    if tags_line:
        lines.append(f"tags: {tags_line}")
    if canonical:
        lines.append(f"canonical_url: {canonical}")
    if cta:
        lines.append("cta_block: |")
        for cta_line in cta.splitlines():
            lines.append(f"  {cta_line}")
    lines.append("---")

    body = variant.body.strip()
    if cta:
        body = body + "\n\n" + cta.strip()
    return "\n".join(lines) + "\n\n" + body + "\n"


def _safe_filename(value: str) -> str:
    """Sanitise *value* into a filesystem-safe filename."""
    return re.sub(r"[^\w\-]", "-", value).strip("-")


# ---------------------------------------------------------------------------
# Optional Playwright pre-fill
# ---------------------------------------------------------------------------


async def _playwright_prefill(
    compose_url: str,
    title: str,
    body: str,
    profile_dir: str,
) -> None:
    """Best-effort pre-fill of the Medium compose editor via headed Chromium.

    Silently returns if Playwright is not installed or any step fails;
    pre-fill must never block the draft + compose-URL flow.
    """
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
                title_selector = "h3[data-testid='post-title'], [placeholder='Title']"
                await page.click(title_selector, timeout=5_000)
                await page.keyboard.insert_text(title)
            except Exception:  # noqa: BLE001
                pass

            try:
                body_selector = "div.section-content, div[data-testid='post-body']"
                await page.click(body_selector, timeout=5_000)
                await page.keyboard.insert_text(body)
            except Exception:  # noqa: BLE001
                pass

            await page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001
        return
