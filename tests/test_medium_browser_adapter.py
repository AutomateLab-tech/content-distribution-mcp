"""End-to-end tests for the Medium browser-fallback adapter.

Medium has no public Partner Program API, so the adapter writes a Markdown
draft, returns a compose URL, and records `state="needs_browser"`. Tests
verify the draft + needs_browser handoff, idempotency short-circuits, the
`mark_live` flip, and the publication-slug → compose-URL routing.

Playwright pre-fill is intentionally NOT exercised — it's an optional dep
that defaults to off and the test runner does not have Chrome installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from content_distribution_mcp.adapters import medium_browser as mb_module
from content_distribution_mcp.adapters.medium_browser import (
    MediumBrowserAdapter,
    mark_live,
    open_pending_in_tabs,
)
from content_distribution_mcp.models import Variant


_CHANNEL_PERSONAL = "medium-browser:personal"
_CHANNEL_PUB = "medium-browser:automatelab"


@pytest.fixture(autouse=True)
def _redirect_drafts_dir(tmp_path: Path, monkeypatch):
    """Send all draft writes into pytest tmp_path instead of the user's home."""
    monkeypatch.setattr(mb_module, "_DRAFTS_DIR", tmp_path / "drafts")


def _variant(**overrides) -> Variant:
    base = dict(
        channel=_CHANNEL_PERSONAL,
        title="Hello Medium",
        body="# hi\n\nbody copy here.",
        extras={"content_id": "hello@2026-05-19"},
    )
    base.update(overrides)
    return Variant(**base)


# ---------------------------------------------------------------------------
# can_publish — tuple[bool, str] contract
# ---------------------------------------------------------------------------


def test_can_publish_accepts_medium_browser_variant():
    adapter = MediumBrowserAdapter()
    ok, reason = adapter.can_publish(_variant())
    assert ok is True
    assert reason == ""


def test_can_publish_rejects_wrong_channel():
    adapter = MediumBrowserAdapter()
    ok, reason = adapter.can_publish(_variant(channel="devto:main"))
    assert ok is False
    assert "medium" in reason.lower() or "channel" in reason.lower()


def test_can_publish_rejects_missing_content_id():
    adapter = MediumBrowserAdapter()
    ok, reason = adapter.can_publish(_variant(extras={}))
    assert ok is False
    assert "content" in reason.lower()


def test_can_publish_rejects_empty_title():
    adapter = MediumBrowserAdapter()
    ok, reason = adapter.can_publish(_variant(title=""))
    assert ok is False


def test_can_publish_rejects_empty_body():
    adapter = MediumBrowserAdapter()
    ok, reason = adapter.can_publish(_variant(body=""))
    assert ok is False


# ---------------------------------------------------------------------------
# publish — happy path (personal feed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_personal_writes_draft_and_returns_needs_browser(
    yaml_backend, tmp_path: Path
):
    adapter = MediumBrowserAdapter()
    result = await adapter.publish(_variant(), profile=None, state_backend=yaml_backend)

    assert result.state == "needs_browser"
    assert result.channel == _CHANNEL_PERSONAL
    assert str(result.compose_url) == "https://medium.com/new-story"
    assert result.live_url is None

    # Draft file exists with frontmatter + body.
    assert result.draft_path is not None
    draft_path = Path(result.draft_path)
    assert draft_path.exists()
    text = draft_path.read_text(encoding="utf-8")
    assert "title: Hello Medium" in text
    assert "# hi" in text

    # Post-log records needs_browser (lookup_published is live-only; use list_post_log).
    rows = yaml_backend.list_post_log(
        content_id="hello@2026-05-19", channel=_CHANNEL_PERSONAL
    )
    assert any(r["state"] == "needs_browser" for r in rows)


# ---------------------------------------------------------------------------
# publish — publication slug routes to /p/<slug>/edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_to_publication_uses_pub_edit_url(yaml_backend):
    adapter = MediumBrowserAdapter()
    result = await adapter.publish(
        _variant(channel=_CHANNEL_PUB, extras={"content_id": "pub@2026-05-19"}),
        profile=None,
        state_backend=yaml_backend,
    )

    assert result.state == "needs_browser"
    assert str(result.compose_url) == "https://medium.com/p/automatelab/edit"


# ---------------------------------------------------------------------------
# publish — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_is_idempotent_when_already_live(yaml_backend):
    """A second publish after mark_live short-circuits to state="live"."""
    adapter = MediumBrowserAdapter()
    v = _variant(extras={"content_id": "twice@2026-05-19"})

    r1 = await adapter.publish(v, profile=None, state_backend=yaml_backend)
    assert r1.state == "needs_browser"

    # Operator submitted manually, flipped to live.
    mark_live(
        "twice@2026-05-19",
        _CHANNEL_PERSONAL,
        "https://medium.com/@me/twice-abc123",
        yaml_backend,
    )

    r2 = await adapter.publish(v, profile=None, state_backend=yaml_backend)
    assert r2.state == "live"
    assert str(r2.live_url) == "https://medium.com/@me/twice-abc123"


@pytest.mark.asyncio
async def test_publish_returns_needs_browser_again_when_prior_not_live(yaml_backend):
    """Second call before mark_live re-surfaces the compose URL."""
    adapter = MediumBrowserAdapter()
    v = _variant(extras={"content_id": "pending@2026-05-19"})

    r1 = await adapter.publish(v, profile=None, state_backend=yaml_backend)
    r2 = await adapter.publish(v, profile=None, state_backend=yaml_backend)

    assert r1.state == "needs_browser"
    assert r2.state == "needs_browser"
    assert str(r2.compose_url) == "https://medium.com/new-story"


# ---------------------------------------------------------------------------
# publish — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_returns_failed_on_missing_content_id_extras(yaml_backend):
    """Variant without content_id in extras fails before any state write."""
    adapter = MediumBrowserAdapter()
    # Bypass can_publish's structural check by calling publish directly with
    # a variant that has empty extras.
    v = Variant(
        channel=_CHANNEL_PERSONAL,
        title="t",
        body="b",
        extras={},
    )
    result = await adapter.publish(v, profile=None, state_backend=yaml_backend)
    assert result.state == "failed"
    assert "content" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# mark_live — flips needs_browser → live
# ---------------------------------------------------------------------------


def test_mark_live_writes_live_state(yaml_backend):
    # Seed a needs_browser row via claim + mark_published.
    yaml_backend.claim_idempotency_key("ml@2026-05-19", _CHANNEL_PERSONAL)
    yaml_backend.mark_published(
        "ml@2026-05-19",
        _CHANNEL_PERSONAL,
        state="needs_browser",
        published_url=None,
        error=None,
    )

    mark_live(
        "ml@2026-05-19",
        _CHANNEL_PERSONAL,
        "https://medium.com/@me/ml-xyz",
        yaml_backend,
    )

    logged = yaml_backend.lookup_published("ml@2026-05-19", _CHANNEL_PERSONAL)
    assert logged is not None
    assert logged["state"] == "live"
    assert logged["published_url"] == "https://medium.com/@me/ml-xyz"


# ---------------------------------------------------------------------------
# open_pending_in_tabs — enumerates needs_browser entries
# ---------------------------------------------------------------------------


def test_open_pending_in_tabs_returns_compose_urls(yaml_backend, monkeypatch):
    """Pending Medium variants get their compose URLs reconstructed."""
    opened: list[str] = []
    monkeypatch.setattr(
        mb_module.webbrowser, "open_new_tab", lambda url: opened.append(url)
    )

    # Two pending Medium entries for the same content_id.
    for channel in (_CHANNEL_PERSONAL, _CHANNEL_PUB):
        yaml_backend.claim_idempotency_key("multi@2026-05-19", channel)
        yaml_backend.mark_published(
            "multi@2026-05-19",
            channel,
            state="needs_browser",
            published_url=None,
            error=None,
        )

    urls = open_pending_in_tabs("multi@2026-05-19", yaml_backend)

    assert "https://medium.com/new-story" in urls
    assert "https://medium.com/p/automatelab/edit" in urls
    # webbrowser.open_new_tab was called for each.
    assert set(opened) == set(urls)


def test_open_pending_in_tabs_skips_non_medium_channels(yaml_backend, monkeypatch):
    """Only medium-browser:* entries get a compose URL."""
    monkeypatch.setattr(mb_module.webbrowser, "open_new_tab", lambda url: None)

    yaml_backend.claim_idempotency_key("mix@2026-05-19", "devto:main")
    yaml_backend.mark_published(
        "mix@2026-05-19",
        "devto:main",
        state="needs_browser",
        published_url=None,
        error=None,
    )
    yaml_backend.claim_idempotency_key("mix@2026-05-19", _CHANNEL_PERSONAL)
    yaml_backend.mark_published(
        "mix@2026-05-19",
        _CHANNEL_PERSONAL,
        state="needs_browser",
        published_url=None,
        error=None,
    )

    urls = open_pending_in_tabs("mix@2026-05-19", yaml_backend)
    assert urls == ["https://medium.com/new-story"]


# ---------------------------------------------------------------------------
# unpublish — always returns False (manual operation)
# ---------------------------------------------------------------------------


def test_unpublish_returns_manual_guidance():
    adapter = MediumBrowserAdapter()
    ok, reason = adapter.unpublish("https://medium.com/@me/post-abc")
    assert ok is False
    assert "manual" in reason.lower()
    assert "post-abc" in reason


# ---------------------------------------------------------------------------
# hints — ChannelHints contract
# ---------------------------------------------------------------------------


def test_hints_returns_browser_only_channelhints():
    adapter = MediumBrowserAdapter()
    hints = adapter.hints()
    assert hints.browser_only is True
    assert hints.canonical_url_supported is True
    assert hints.cta_placement == "bottom"
    assert "bold" in hints.supported_md_features
    assert "headers" in hints.supported_md_features
    # Medium does not support tables.
    assert "tables" not in hints.supported_md_features


# ---------------------------------------------------------------------------
# Draft frontmatter — subtitle, tags, canonical_url, cta_block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_includes_subtitle_tags_canonical_cta(yaml_backend):
    adapter = MediumBrowserAdapter()
    v = Variant(
        channel=_CHANNEL_PERSONAL,
        title="Full Frontmatter",
        body="post body.",
        tags=["python", "mcp"],
        canonical_url="https://automatelab.tech/posts/full",
        cta_block="Subscribe for more.",
        extras={
            "content_id": "fm@2026-05-19",
            "subtitle": "An MCP teardown",
        },
    )
    result = await adapter.publish(v, profile=None, state_backend=yaml_backend)

    text = Path(result.draft_path).read_text(encoding="utf-8")
    assert "title: Full Frontmatter" in text
    assert "subtitle: An MCP teardown" in text
    assert "tags: python, mcp" in text
    assert "canonical_url: https://automatelab.tech/posts/full" in text
    assert "cta_block: |" in text
    assert "  Subscribe for more." in text
    # CTA also appended to body.
    assert text.rstrip().endswith("Subscribe for more.")
