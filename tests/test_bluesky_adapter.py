"""End-to-end test of the Bluesky adapter with a monkeypatched atproto client.

Mirrors the structure of test_hashnode_adapter.py / test_reddit_adapter.py —
exercises publish, idempotency, and failure paths against a stubbed
``_send_bluesky_post`` so the test suite never touches the real Bluesky API.
"""

from __future__ import annotations

import pytest

from content_distribution_mcp.adapters import bluesky as bsky_module
from content_distribution_mcp.adapters.bluesky import (
    BlueskyAdapter,
    _at_uri_to_bsky_url,
    _build_post_text,
)
from content_distribution_mcp.models import Variant


_CHANNEL = "bluesky:main"
_HANDLE = "automatelab.bsky.social"
_AT_URI = f"at://did:plc:abc123/app.bsky.feed.post/3kfn123"
_BSKY_URL = f"https://bsky.app/profile/{_HANDLE}/post/3kfn123"


def _variant(**overrides) -> Variant:
    base = dict(
        channel=_CHANNEL,
        title="",  # Bluesky has no title
        body="Hello from AutomateLab! Check out our new MCP server.",
        extras={"content_id": "hello@2026-05-19"},
    )
    base.update(overrides)
    return Variant(**base)


def _profile(**overrides) -> dict:
    base = {
        "BLUESKY_HANDLE": _HANDLE,
        "BLUESKY_PASSWORD": "app-password-xyz",
    }
    base.update(overrides)
    return base


def _install_send_mock(monkeypatch, *, uri: str = _AT_URI, cid: str = "cid_xyz"):
    """Replace ``_send_bluesky_post`` so it returns (uri, cid) without network."""
    calls: list[tuple[str, str, str]] = []

    def fake_send(handle: str, password: str, text: str) -> tuple[str, str]:
        calls.append((handle, password, text))
        return uri, cid

    monkeypatch.setattr(bsky_module, "_send_bluesky_post", fake_send)
    return calls


# ---------------------------------------------------------------------------
# can_publish — tuple[bool, str] contract
# ---------------------------------------------------------------------------


def test_can_publish_accepts_bluesky_variant():
    adapter = BlueskyAdapter()
    ok, reason = adapter.can_publish(_variant())
    assert ok is True
    assert reason == ""


def test_can_publish_rejects_wrong_channel():
    adapter = BlueskyAdapter()
    ok, reason = adapter.can_publish(_variant(channel="devto:main"))
    assert ok is False
    assert "bluesky" in reason.lower() or "channel" in reason.lower()


def test_can_publish_rejects_missing_content_id():
    adapter = BlueskyAdapter()
    ok, reason = adapter.can_publish(_variant(extras={}))
    assert ok is False
    assert "content" in reason.lower()


def test_can_publish_rejects_empty_body():
    adapter = BlueskyAdapter()
    ok, reason = adapter.can_publish(_variant(body=""))
    assert ok is False


# ---------------------------------------------------------------------------
# publish — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_returns_live_on_success(monkeypatch, yaml_backend):
    calls = _install_send_mock(monkeypatch)

    adapter = BlueskyAdapter()
    result = await adapter.publish(_variant(), _profile(), yaml_backend)

    assert result.state == "live"
    assert str(result.live_url) == _BSKY_URL
    assert result.channel == _CHANNEL

    # send_post was called once with the right credentials.
    assert len(calls) == 1
    handle, password, text = calls[0]
    assert handle == _HANDLE
    assert password == "app-password-xyz"
    assert "AutomateLab" in text

    logged = yaml_backend.lookup_published("hello@2026-05-19", _CHANNEL)
    assert logged is not None
    assert logged["state"] == "live"
    assert logged["published_url"] == _BSKY_URL


@pytest.mark.asyncio
async def test_publish_appends_canonical_url(monkeypatch, yaml_backend):
    calls = _install_send_mock(monkeypatch)

    adapter = BlueskyAdapter()
    v = _variant(
        body="Short teaser.",
        canonical_url="https://automatelab.tech/posts/hello",
    )
    await adapter.publish(v, _profile(), yaml_backend)

    _, _, text = calls[0]
    assert text == "Short teaser. https://automatelab.tech/posts/hello"


# ---------------------------------------------------------------------------
# publish — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_is_idempotent(monkeypatch, yaml_backend):
    calls = _install_send_mock(monkeypatch)

    adapter = BlueskyAdapter()
    v = _variant(extras={"content_id": "once@2026-05-19"})

    r1 = await adapter.publish(v, _profile(), yaml_backend)
    r2 = await adapter.publish(v, _profile(), yaml_backend)

    assert r1.state == "live"
    assert r2.state == "live"
    assert str(r2.live_url) == _BSKY_URL
    # Second call short-circuits — send_post hit exactly once.
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# publish — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_returns_failed_on_missing_profile(yaml_backend):
    adapter = BlueskyAdapter()
    result = await adapter.publish(_variant(), None, yaml_backend)
    assert result.state == "failed"
    assert "profile" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_publish_returns_failed_on_missing_credentials(yaml_backend):
    adapter = BlueskyAdapter()
    result = await adapter.publish(_variant(), {"BLUESKY_HANDLE": _HANDLE}, yaml_backend)
    assert result.state == "failed"
    assert "credential" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_publish_returns_failed_on_sdk_exception(monkeypatch, yaml_backend):
    def boom(handle: str, password: str, text: str) -> tuple[str, str]:
        raise RuntimeError("login refused")

    monkeypatch.setattr(bsky_module, "_send_bluesky_post", boom)

    adapter = BlueskyAdapter()
    result = await adapter.publish(
        _variant(extras={"content_id": "fail@2026-05-19"}),
        _profile(),
        yaml_backend,
    )

    assert result.state == "failed"
    assert "login refused" in (result.error or "")

    # Stub must be resolved to failed so a retry can re-claim.
    logged = yaml_backend.list_post_log(
        content_id="fail@2026-05-19", channel=_CHANNEL
    )
    assert any(r["state"] == "failed" for r in logged)


@pytest.mark.asyncio
async def test_publish_returns_failed_on_unparseable_at_uri(monkeypatch, yaml_backend):
    _install_send_mock(monkeypatch, uri="not-an-at-uri")

    adapter = BlueskyAdapter()
    result = await adapter.publish(
        _variant(extras={"content_id": "badd@2026-05-19"}),
        _profile(),
        yaml_backend,
    )

    assert result.state == "failed"
    assert "unparseable" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# _build_post_text — composition + truncation
# ---------------------------------------------------------------------------


def test_build_post_text_short_body_no_url():
    assert _build_post_text("hello world", "") == "hello world"


def test_build_post_text_short_body_with_url():
    result = _build_post_text("hello", "https://example.com")
    assert result == "hello https://example.com"


def test_build_post_text_truncates_long_body_no_url():
    body = "x" * 400
    result = _build_post_text(body, "")
    assert len(result) == 300
    assert result.endswith("…")


def test_build_post_text_truncates_teaser_to_fit_url():
    body = "x" * 400
    url = "https://example.com/very-long-post-slug"
    result = _build_post_text(body, url)
    assert result.endswith(url)
    assert len(result) <= 300
    assert "…" in result


def test_build_post_text_url_only_when_body_doesnt_fit():
    body = "anything"
    long_url = "https://example.com/" + ("x" * 320)
    result = _build_post_text(body, long_url)
    assert result == long_url


# ---------------------------------------------------------------------------
# _at_uri_to_bsky_url — URI parsing
# ---------------------------------------------------------------------------


def test_at_uri_to_bsky_url_valid():
    url = _at_uri_to_bsky_url(_AT_URI, _HANDLE)
    assert url == _BSKY_URL


def test_at_uri_to_bsky_url_invalid_prefix():
    assert _at_uri_to_bsky_url("https://example.com/foo", _HANDLE) is None


def test_at_uri_to_bsky_url_too_few_parts():
    assert _at_uri_to_bsky_url("at://did:plc:abc", _HANDLE) is None


def test_at_uri_to_bsky_url_empty_rkey():
    assert _at_uri_to_bsky_url("at://did:plc:abc/app.bsky.feed.post/", _HANDLE) is None


# ---------------------------------------------------------------------------
# unpublish — always returns False (manual operation)
# ---------------------------------------------------------------------------


def test_unpublish_returns_manual_guidance():
    adapter = BlueskyAdapter()
    ok, reason = adapter.unpublish(_BSKY_URL)
    assert ok is False
    assert "manual" in reason.lower() or "not-implemented" in reason.lower()
    assert _BSKY_URL in reason


# ---------------------------------------------------------------------------
# hints — ChannelHints contract
# ---------------------------------------------------------------------------


def test_hints_returns_channelhints():
    adapter = BlueskyAdapter()
    hints = adapter.hints()
    assert hints.max_length == 300
    assert hints.canonical_url_supported is False
    assert hints.browser_only is False
    assert hints.cta_placement == "none"
    assert "links" in hints.supported_md_features
