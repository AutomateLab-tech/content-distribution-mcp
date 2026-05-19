"""End-to-end tests for the Twitter / X browser-fallback adapter.

X's free posting API tier is unusable for outreach, so the adapter writes a
plain-text draft, returns the compose URL, and records `state="needs_browser"`.
Tests verify the draft + needs_browser handoff, idempotency short-circuits,
the `mark_live` flip, and the operator helpers.

Playwright pre-fill is intentionally NOT exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from content_distribution_mcp.adapters import twitter_browser as tw_module
from content_distribution_mcp.adapters.twitter_browser import (
    TwitterBrowserAdapter,
    mark_live,
    open_pending_in_tabs,
)
from content_distribution_mcp.models import Variant


_CHANNEL_PERSONAL = "twitter-browser:personal"
_CHANNEL_HANDLE = "twitter-browser:automatelab"
_COMPOSE_URL = "https://x.com/compose/post"


@pytest.fixture(autouse=True)
def _redirect_drafts_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tw_module, "_DRAFTS_DIR", tmp_path / "drafts")


def _variant(**overrides) -> Variant:
    base = dict(
        channel=_CHANNEL_PERSONAL,
        title="",
        body="Just shipped: a content-distribution MCP for outreach to indie blogs.",
        extras={"content_id": "hello@2026-05-19"},
    )
    base.update(overrides)
    return Variant(**base)


# ---------------------------------------------------------------------------
# can_publish — tuple[bool, str] contract
# ---------------------------------------------------------------------------


def test_can_publish_accepts_twitter_variant():
    adapter = TwitterBrowserAdapter()
    ok, reason = adapter.can_publish(_variant())
    assert ok is True
    assert reason == ""


def test_can_publish_rejects_wrong_channel():
    adapter = TwitterBrowserAdapter()
    ok, reason = adapter.can_publish(_variant(channel="devto:main"))
    assert ok is False
    assert "twitter" in reason.lower() or "channel" in reason.lower()


def test_can_publish_rejects_missing_content_id():
    adapter = TwitterBrowserAdapter()
    ok, reason = adapter.can_publish(_variant(extras={}))
    assert ok is False
    assert "content" in reason.lower()


def test_can_publish_rejects_empty_body():
    adapter = TwitterBrowserAdapter()
    ok, reason = adapter.can_publish(_variant(body=""))
    assert ok is False


# ---------------------------------------------------------------------------
# publish — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_writes_draft_and_returns_needs_browser(yaml_backend):
    adapter = TwitterBrowserAdapter()
    result = await adapter.publish(_variant(), profile=None, state_backend=yaml_backend)

    assert result.state == "needs_browser"
    assert result.channel == _CHANNEL_PERSONAL
    assert str(result.compose_url) == _COMPOSE_URL
    assert result.live_url is None

    draft_path = Path(result.draft_path)
    assert draft_path.exists()
    assert "Just shipped" in draft_path.read_text(encoding="utf-8")

    rows = yaml_backend.list_post_log(
        content_id="hello@2026-05-19", channel=_CHANNEL_PERSONAL
    )
    assert any(r["state"] == "needs_browser" for r in rows)


@pytest.mark.asyncio
async def test_publish_with_specific_handle_still_uses_x_compose(yaml_backend):
    """Channel suffix is informational; compose URL is constant."""
    adapter = TwitterBrowserAdapter()
    result = await adapter.publish(
        _variant(channel=_CHANNEL_HANDLE, extras={"content_id": "handle@2026-05-19"}),
        profile=None,
        state_backend=yaml_backend,
    )
    assert str(result.compose_url) == _COMPOSE_URL


# ---------------------------------------------------------------------------
# publish — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_is_idempotent_when_already_live(yaml_backend):
    adapter = TwitterBrowserAdapter()
    v = _variant(extras={"content_id": "twice@2026-05-19"})

    r1 = await adapter.publish(v, profile=None, state_backend=yaml_backend)
    assert r1.state == "needs_browser"

    mark_live(
        "twice@2026-05-19",
        _CHANNEL_PERSONAL,
        "https://x.com/automatelab/status/1234567890",
        yaml_backend,
    )

    r2 = await adapter.publish(v, profile=None, state_backend=yaml_backend)
    assert r2.state == "live"
    assert str(r2.live_url) == "https://x.com/automatelab/status/1234567890"


@pytest.mark.asyncio
async def test_publish_returns_needs_browser_again_when_prior_not_live(yaml_backend):
    adapter = TwitterBrowserAdapter()
    v = _variant(extras={"content_id": "pending@2026-05-19"})

    r1 = await adapter.publish(v, profile=None, state_backend=yaml_backend)
    r2 = await adapter.publish(v, profile=None, state_backend=yaml_backend)

    assert r1.state == "needs_browser"
    assert r2.state == "needs_browser"
    assert str(r2.compose_url) == _COMPOSE_URL


# ---------------------------------------------------------------------------
# publish — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_returns_failed_on_missing_content_id_extras(yaml_backend):
    adapter = TwitterBrowserAdapter()
    v = Variant(channel=_CHANNEL_PERSONAL, title="", body="b", extras={})
    result = await adapter.publish(v, profile=None, state_backend=yaml_backend)
    assert result.state == "failed"
    assert "content" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# mark_live — flips needs_browser → live
# ---------------------------------------------------------------------------


def test_mark_live_writes_live_state(yaml_backend):
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
        "https://x.com/me/status/xyz",
        yaml_backend,
    )

    logged = yaml_backend.lookup_published("ml@2026-05-19", _CHANNEL_PERSONAL)
    assert logged is not None
    assert logged["state"] == "live"
    assert logged["published_url"] == "https://x.com/me/status/xyz"


# ---------------------------------------------------------------------------
# open_pending_in_tabs
# ---------------------------------------------------------------------------


def test_open_pending_in_tabs_returns_compose_urls(yaml_backend, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(
        tw_module.webbrowser, "open_new_tab", lambda url: opened.append(url)
    )

    yaml_backend.claim_idempotency_key("multi@2026-05-19", _CHANNEL_PERSONAL)
    yaml_backend.mark_published(
        "multi@2026-05-19",
        _CHANNEL_PERSONAL,
        state="needs_browser",
        published_url=None,
        error=None,
    )

    urls = open_pending_in_tabs("multi@2026-05-19", yaml_backend)

    assert urls == [_COMPOSE_URL]
    assert opened == [_COMPOSE_URL]


def test_open_pending_in_tabs_skips_non_twitter_channels(yaml_backend, monkeypatch):
    monkeypatch.setattr(tw_module.webbrowser, "open_new_tab", lambda url: None)

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
    assert urls == [_COMPOSE_URL]


# ---------------------------------------------------------------------------
# unpublish — manual
# ---------------------------------------------------------------------------


def test_unpublish_returns_manual_guidance():
    adapter = TwitterBrowserAdapter()
    ok, reason = adapter.unpublish("https://x.com/me/status/abc123")
    assert ok is False
    assert "manual" in reason.lower()
    assert "abc123" in reason


# ---------------------------------------------------------------------------
# hints
# ---------------------------------------------------------------------------


def test_hints_returns_browser_only_channelhints():
    adapter = TwitterBrowserAdapter()
    hints = adapter.hints()
    assert hints.browser_only is True
    assert hints.canonical_url_supported is False
    assert hints.cta_placement == "none"
    assert hints.max_length == 280
    assert "links" in hints.supported_md_features


# ---------------------------------------------------------------------------
# Draft body — cta_block appended
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_appends_cta_block(yaml_backend):
    adapter = TwitterBrowserAdapter()
    v = Variant(
        channel=_CHANNEL_PERSONAL,
        title="",
        body="Main tweet.",
        cta_block="More: link",
        extras={"content_id": "cta@2026-05-19"},
    )
    result = await adapter.publish(v, profile=None, state_backend=yaml_backend)

    text = Path(result.draft_path).read_text(encoding="utf-8")
    assert "Main tweet." in text
    assert text.rstrip().endswith("More: link")
