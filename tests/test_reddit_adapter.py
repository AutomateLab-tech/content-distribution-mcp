"""Tests for the Reddit browser-fallback adapter.

The adapter writes a markdown draft, returns a pre-filled compose URL, and
records state="needs_browser". No API credentials are required or used.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from content_distribution_mcp.adapters import reddit as reddit_module
from content_distribution_mcp.adapters.reddit import (
    RedditAdapter,
    _build_compose_url,
    mark_live,
)
from content_distribution_mcp.models import Variant


_CHANNEL = "reddit:LocalLLaMA"
_SUBREDDIT = "LocalLLaMA"
_LIVE_URL = "https://reddit.com/r/LocalLLaMA/comments/abc123/hello/"


@pytest.fixture(autouse=True)
def _redirect_drafts_dir(tmp_path: Path, monkeypatch):
    """Send all draft writes into pytest tmp_path instead of the user's home."""
    monkeypatch.setattr(reddit_module, "_DRAFTS_DIR", tmp_path / "drafts")


def _variant(**overrides) -> Variant:
    base = dict(
        channel=_CHANNEL,
        title="Hello Reddit",
        body="hi from automatelab",
        extras={"content_id": "hello@2026-05-19"},
    )
    base.update(overrides)
    return Variant(**base)


# ---------------------------------------------------------------------------
# can_publish — tuple[bool, str] contract
# ---------------------------------------------------------------------------


def test_can_publish_accepts_reddit_variant():
    adapter = RedditAdapter()
    ok, reason = adapter.can_publish(_variant())
    assert ok is True
    assert reason == ""


def test_can_publish_rejects_wrong_channel():
    adapter = RedditAdapter()
    ok, reason = adapter.can_publish(_variant(channel="devto:main"))
    assert ok is False
    assert "reddit" in reason.lower() or "channel" in reason.lower()


def test_can_publish_rejects_missing_content_id():
    adapter = RedditAdapter()
    ok, reason = adapter.can_publish(_variant(extras={}))
    assert ok is False
    assert "content" in reason.lower()


def test_can_publish_rejects_empty_title():
    adapter = RedditAdapter()
    ok, reason = adapter.can_publish(_variant(title=""))
    assert ok is False


def test_can_publish_rejects_empty_body():
    adapter = RedditAdapter()
    ok, reason = adapter.can_publish(_variant(body=""))
    assert ok is False


# ---------------------------------------------------------------------------
# hints — ChannelHints contract
# ---------------------------------------------------------------------------


def test_hints_returns_channelhints():
    adapter = RedditAdapter()
    h = adapter.hints()
    assert h.max_length == 40_000
    assert h.browser_only is True
    assert h.canonical_url_supported is False
    assert h.cta_placement == "none"
    assert "bold" in h.supported_md_features
    assert "headers" in h.supported_md_features


# ---------------------------------------------------------------------------
# publish — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_returns_needs_browser(yaml_backend, tmp_path):
    adapter = RedditAdapter()
    result = await adapter.publish(_variant(), {}, yaml_backend)

    assert result.state == "needs_browser"
    assert result.channel == _CHANNEL
    assert result.compose_url is not None
    assert "LocalLLaMA" in str(result.compose_url)
    assert result.draft_path is not None
    assert result.draft_path.exists()

    entries = yaml_backend.list_post_log(content_id="hello@2026-05-19", channel=_CHANNEL)
    assert any(e["state"] == "needs_browser" for e in entries)


@pytest.mark.asyncio
async def test_publish_draft_contains_title_and_body(yaml_backend):
    adapter = RedditAdapter()
    result = await adapter.publish(_variant(), {}, yaml_backend)

    draft_text = result.draft_path.read_text(encoding="utf-8")
    assert "Hello Reddit" in draft_text
    assert "hi from automatelab" in draft_text
    assert "LocalLLaMA" in draft_text


@pytest.mark.asyncio
async def test_publish_compose_url_prefilled(yaml_backend):
    adapter = RedditAdapter()
    result = await adapter.publish(_variant(), {}, yaml_backend)

    url = str(result.compose_url)
    assert "selftext=true" in url
    assert "Hello+Reddit" in url or "Hello%20Reddit" in url


# ---------------------------------------------------------------------------
# publish — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_is_idempotent_after_mark_live(yaml_backend):
    """After mark_live, a second publish short-circuits to state="live"."""
    adapter = RedditAdapter()
    v = _variant(extras={"content_id": "once@2026-05-19"})

    r1 = await adapter.publish(v, {}, yaml_backend)
    assert r1.state == "needs_browser"

    mark_live("once@2026-05-19", _CHANNEL, _LIVE_URL, yaml_backend)

    r2 = await adapter.publish(v, {}, yaml_backend)
    assert r2.state == "live"
    assert str(r2.live_url) == _LIVE_URL


# ---------------------------------------------------------------------------
# publish — r/ prefix normalisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_strips_r_prefix(yaml_backend):
    adapter = RedditAdapter()
    v = _variant(channel="reddit:r/LocalLLaMA")
    result = await adapter.publish(v, {}, yaml_backend)

    assert result.state == "needs_browser"
    assert "LocalLLaMA" in str(result.compose_url)


# ---------------------------------------------------------------------------
# mark_live
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_live_records_live_url(yaml_backend):
    adapter = RedditAdapter()
    await adapter.publish(_variant(), {}, yaml_backend)

    mark_live("hello@2026-05-19", _CHANNEL, _LIVE_URL, yaml_backend)

    log = yaml_backend.lookup_published("hello@2026-05-19", _CHANNEL)
    assert log is not None
    assert log["state"] == "live"
    assert log["published_url"] == _LIVE_URL


# ---------------------------------------------------------------------------
# _build_compose_url helper
# ---------------------------------------------------------------------------


def test_build_compose_url_includes_subreddit():
    url = _build_compose_url("Python", "My Title", "body text")
    assert "reddit.com/r/Python/submit" in url
    assert "selftext=true" in url


def test_build_compose_url_encodes_title_and_body():
    url = _build_compose_url("Python", "Hello World", "some body")
    assert "Hello" in url
    assert "some" in url
