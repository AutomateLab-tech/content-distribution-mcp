"""
LinkedIn browser-fallback adapter for the Content Distribution MCP.

LinkedIn's posting APIs require company-application approval and don't cover
the everyday personal-feed / company-page admin posting flow we actually use,
so this adapter mirrors the Medium browser-fallback pattern: write a local
plain-text draft and return a compose URL. The operator pastes the draft into
the editor and calls :func:`mark_live` once the post is live.

Channel format: ``linkedin-browser:<target>`` where ``<target>`` is either:

* ``personal`` — the authenticated user's own feed
  (https://www.linkedin.com/feed/?shareActive=true)
* a numeric company page id (e.g. ``116012269``) — the company admin feed
  (https://www.linkedin.com/company/<id>/admin/)

The idempotency key is sourced from ``variant.extras["content_id"]`` — same
convention as every other adapter in this package. Re-publishing the same
``(content_id, channel)`` short-circuits to the recorded needs-browser result
without rewriting the draft.

LinkedIn posts are plain text with line-break formatting. The 3000-character
cap is informational only — the adapter does not truncate, because LinkedIn's
own composer rejects overflows with a clear error.
"""

from __future__ import annotations

import re
import webbrowser
from pathlib import Path
from typing import Any

from ..models import ChannelHints, PublishResult, Variant


_LINKEDIN_BASE = "https://www.linkedin.com"
_DRAFTS_DIR = Path.home() / ".distribution-mcp" / "drafts"
_MAX_POST_LENGTH = 3000

_SUPPORTED_MD_FEATURES: set[str] = {"links"}


class LinkedInBrowserAdapter:
    """Channel adapter for LinkedIn — browser-only (no usable public API)."""

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
            browser_only=True,
        )

    def can_publish(self, variant: Variant) -> tuple[bool, str]:
        """Return ``(ok, reason)`` — structural pre-flight only."""
        if not variant.channel.startswith("linkedin-browser:"):
            return False, f"channel-not-linkedin-browser: {variant.channel}"
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
        """Run the LinkedIn browser-fallback publish flow.

        Writes a plain-text draft to disk, returns a compose URL, and records
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
            target = _target_slug(variant.channel)
            return PublishResult(
                channel=variant.channel,
                state="needs_browser",
                compose_url=_build_compose_url(target),  # type: ignore[arg-type]
            )

        # --- 2. Write draft file -----------------------------------------
        target = _target_slug(variant.channel)
        channel_slug = _safe_filename(variant.channel)
        draft_dir = _DRAFTS_DIR / _safe_filename(content_id)
        draft_dir.mkdir(parents=True, exist_ok=True)

        draft_path = draft_dir / f"{channel_slug}.txt"
        draft_path.write_text(_build_draft_text(variant), encoding="utf-8")

        # --- 3. Compose URL -----------------------------------------------
        compose_url = _build_compose_url(target)

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
        """LinkedIn has no programmatic unpublish — always returns False."""
        return (
            False,
            f"linkedin-unpublish-requires-manual: visit {live_url} and delete the post",
        )


# ---------------------------------------------------------------------------
# Operator helpers
# ---------------------------------------------------------------------------


def open_pending_in_tabs(
    content_id: str,
    state_backend: Any,
) -> list[str]:
    """Open every pending needs_browser LinkedIn variant for ``content_id``."""
    entries = state_backend.list_post_log(
        content_id=content_id, state="needs_browser"
    )

    compose_urls: list[str] = []
    for entry in entries:
        channel = entry.get("channel", "")
        if not channel.startswith("linkedin-browser:"):
            continue
        url = _build_compose_url(_target_slug(channel))
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


def _target_slug(channel: str) -> str:
    """Extract the target slug from ``linkedin-browser:<target>``."""
    return channel.split("linkedin-browser:", 1)[-1]


def _build_compose_url(target: str) -> str:
    """Build the LinkedIn compose/editor URL for a target slug.

    * ``personal`` → personal feed share dialog
    * numeric ``<id>`` → company page admin feed
    """
    if not target or target.lower() == "personal":
        return f"{_LINKEDIN_BASE}/feed/?shareActive=true"
    return f"{_LINKEDIN_BASE}/company/{target}/admin/"


def _build_draft_text(variant: Variant) -> str:
    """Render the plain-text draft body for a LinkedIn variant.

    LinkedIn posts don't render markdown, so we strip frontmatter framing
    entirely and concatenate body + optional CTA with a blank line between.
    """
    body = variant.body.strip()
    if variant.cta_block:
        body = body + "\n\n" + variant.cta_block.strip()
    return body + "\n"


def _safe_filename(value: str) -> str:
    """Sanitise *value* into a filesystem-safe filename."""
    return re.sub(r"[^\w\-]", "-", value).strip("-")

