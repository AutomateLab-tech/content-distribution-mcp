"""
Reddit browser adapter for the Content Distribution MCP.

Generates a markdown draft and a pre-filled Reddit compose URL for each
subreddit variant.  The operator pastes the draft into the Reddit editor
and clicks Submit manually.  No Reddit API credentials are required.

The API-based gate (PRAW, per-subreddit cooldown, self-promo ratio, account
age/karma) has been removed in favour of the upstream subreddit-suggestion
workflow in the al-content-distribution skill, which vets subreddit fitness
before any drafting happens.  This keeps the MCP layer thin and
credential-free for the common solo-operator case.

Channel format: ``reddit:<subreddit>`` — the ``r/`` prefix is optional and
normalised away.  Example: ``reddit:Python``, ``reddit:r/devops``.

Compose URL
-----------
Reddit's submit form accepts query-string pre-fill:

    https://www.reddit.com/r/<subreddit>/submit
        ?selftext=true
        &title=<encoded_title>
        &text=<encoded_body>

The URL pre-fills the title and body fields in the "Text" tab.  Character
limits (40 000 for the body, 300 for the title) are enforced by the editor;
this adapter does not truncate.

The idempotency key is sourced from ``variant.extras["content_id"]``.
"""

from __future__ import annotations

import re
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..models import ChannelHints, PublishResult, Variant


_REDDIT_MAX_BODY_CHARS: int = 40_000
_DRAFTS_DIR = Path.home() / ".distribution-mcp" / "drafts"

_REDDIT_MD_FEATURES: frozenset[str] = frozenset(
    {
        "bold", "italic", "code_inline", "code_block",
        "links", "headers", "lists", "blockquote", "hr", "superscript",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_r_prefix(subreddit: str) -> str:
    """Drop a leading ``r/`` from a subreddit name."""
    return re.sub(r"^r/", "", subreddit, flags=re.IGNORECASE)


def _parse_subreddit(channel: str) -> str:
    """Return the bare subreddit name from a ``reddit:<sub>`` channel string."""
    if not channel.startswith("reddit:"):
        raise ValueError(f"Channel {channel!r} is not a Reddit channel")
    return _strip_r_prefix(channel.split(":", 1)[1])


def _build_compose_url(subreddit: str, title: str, body: str) -> str:
    """Build a pre-filled Reddit submit URL for a text post."""
    base = f"https://www.reddit.com/r/{subreddit}/submit"
    # Reddit's compose URL supports selftext=true to pre-select the Text tab,
    # plus title= and text= for body pre-fill (both URL-encoded).
    params = (
        f"?selftext=true"
        f"&title={quote(title, safe='')}"
        f"&text={quote(body, safe='')}"
    )
    return base + params


def _safe_filename(value: str) -> str:
    """Sanitise a string into a filesystem-safe filename."""
    return re.sub(r"[^\w\-]", "-", value).strip("-")


# ---------------------------------------------------------------------------
# RedditAdapter
# ---------------------------------------------------------------------------


class RedditAdapter:
    """Channel adapter for Reddit text posts — browser-only, no API credentials.

    Channels handled: ``reddit:<subreddit>`` (the ``r/`` prefix is optional).
    """

    # ------------------------------------------------------------------
    # ChannelAdapter interface
    # ------------------------------------------------------------------

    def hints(self) -> ChannelHints:
        """Return static channel metadata for Reddit text posts."""
        return ChannelHints(
            max_length=_REDDIT_MAX_BODY_CHARS,
            supported_md_features=set(_REDDIT_MD_FEATURES),
            tag_vocab=None,
            cta_placement="none",
            canonical_url_supported=False,
            browser_only=True,
        )

    def can_publish(self, variant: Variant) -> tuple[bool, str]:
        """Return ``(ok, reason)`` — structural pre-flight only."""
        if not variant.channel.startswith("reddit:"):
            return False, f"channel-not-reddit: {variant.channel}"
        if not variant.title:
            return False, "empty-title"
        if not variant.body:
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
        """Run the Reddit browser-fallback publish flow.

        Writes a markdown draft to disk and returns a pre-filled Reddit
        compose URL.  Records ``state="needs_browser"`` in the post log.
        The operator submits manually and calls :func:`mark_live` afterwards.
        """
        content_id = variant.extras.get("content_id") if variant.extras else None
        if not isinstance(content_id, str) or not content_id:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="missing-content-id-in-variant-extras",
            )

        subreddit = _parse_subreddit(variant.channel)

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
            compose_url = _build_compose_url(subreddit, variant.title or "", variant.body)
            return PublishResult(
                channel=variant.channel,
                state="needs_browser",
                compose_url=compose_url,
            )

        # --- 2. Write draft file -----------------------------------------
        channel_slug = _safe_filename(variant.channel)
        draft_dir = _DRAFTS_DIR / _safe_filename(content_id)
        draft_dir.mkdir(parents=True, exist_ok=True)

        draft_path = draft_dir / f"{channel_slug}.md"
        draft_path.write_text(_build_draft_text(variant, subreddit), encoding="utf-8")

        # --- 3. Pre-filled compose URL ------------------------------------
        compose_url = _build_compose_url(subreddit, variant.title or "", variant.body)

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
            compose_url=compose_url,
            live_url=None,
        )

    def unpublish(self, live_url: str) -> tuple[bool, str]:
        """Reddit has no programmatic unpublish for browser-submitted posts."""
        return (
            False,
            f"reddit-unpublish-requires-manual: visit {live_url} and delete the post",
        )


# ---------------------------------------------------------------------------
# Operator helpers
# ---------------------------------------------------------------------------


def open_pending_in_tabs(
    content_id: str,
    state_backend: Any,
) -> list[str]:
    """Open every pending needs_browser Reddit variant for ``content_id``."""
    entries = state_backend.list_post_log(
        content_id=content_id, state="needs_browser"
    )
    compose_urls: list[str] = []
    for entry in entries:
        channel = entry.get("channel", "")
        if not channel.startswith("reddit:"):
            continue
        # Reconstruct a minimal compose URL (title/body not available here).
        subreddit = _parse_subreddit(channel)
        url = f"https://www.reddit.com/r/{subreddit}/submit?selftext=true"
        compose_urls.append(url)
        webbrowser.open_new_tab(url)
    return compose_urls


def mark_live(
    content_id: str,
    channel: str,
    live_url: str,
    state_backend: Any,
) -> None:
    """Record the live URL after the operator submits the post manually."""
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


def _build_draft_text(variant: Variant, subreddit: str) -> str:
    """Render the markdown draft for a Reddit text post.

    Includes a header comment block with the subreddit and title so the
    operator has all needed fields in one file.
    """
    lines: list[str] = []

    lines.append("<!--")
    lines.append(f"  REDDIT DRAFT — r/{subreddit}")
    if variant.title:
        lines.append(f"  TITLE:   {variant.title}")
    lines.append("  Paste the body below into the Reddit text editor.")
    lines.append("-->")
    lines.append("")

    body = variant.body.strip()
    if variant.cta_block:
        body = body + "\n\n" + variant.cta_block.strip()
    lines.append(body)
    lines.append("")

    return "\n".join(lines)
