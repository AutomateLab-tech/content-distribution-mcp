"""End-to-end test of the DEV.to adapter using respx to mock the Forem API.

These tests pin down the actual call signatures the rest of the system depends
on. They exercise the full publish path end-to-end against a fake HTTP server,
including idempotency tracking via the YamlBackend.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from content_distribution_mcp.adapters.devto import DevToAdapter
from content_distribution_mcp.models import Variant


# ---------------------------------------------------------------------------
# can_publish — must return (ok, reason) per scheduler.publish_immediate
# ---------------------------------------------------------------------------


def test_can_publish_accepts_devto_variant():
    adapter = DevToAdapter()
    v = Variant(channel="devto:main", title="t", body="b")
    ok, reason = adapter.can_publish(v)
    assert ok is True
    assert reason == ""


def test_can_publish_rejects_wrong_channel():
    adapter = DevToAdapter()
    v = Variant(channel="reddit:foo", title="t", body="b")
    ok, reason = adapter.can_publish(v)
    assert ok is False
    assert "devto" in reason.lower() or "channel" in reason.lower()


def test_can_publish_rejects_empty_title():
    adapter = DevToAdapter()
    v = Variant(channel="devto:main", title="", body="b")
    ok, reason = adapter.can_publish(v)
    assert ok is False


def test_can_publish_rejects_empty_body():
    adapter = DevToAdapter()
    v = Variant(channel="devto:main", title="t", body="")
    ok, reason = adapter.can_publish(v)
    assert ok is False


# ---------------------------------------------------------------------------
# publish — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_returns_live_on_201(yaml_backend):
    # Mock DEV.to POST /api/articles
    respx.post("https://dev.to/api/articles").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 12345,
                "url": "https://dev.to/test-user/hello-world-abc123",
                "title": "Hello World",
            },
        )
    )

    adapter = DevToAdapter()
    profile = {"DEV_TO_API_KEY": "test-key"}
    variant = Variant(
        channel="devto:main",
        title="Hello World",
        body="# hi",
        extras={"content_id": "hello@2026-05-19"},
    )

    result = await adapter.publish(variant, profile, yaml_backend)

    assert result.state == "live"
    assert str(result.live_url) == "https://dev.to/test-user/hello-world-abc123"
    assert result.channel == "devto:main"
    # post-log must have a 'live' row for this content_id+channel.
    looked_up = yaml_backend.lookup_published("hello@2026-05-19", "devto:main")
    assert looked_up is not None
    assert looked_up["state"] == "live"


# ---------------------------------------------------------------------------
# publish — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_is_idempotent(yaml_backend):
    """Second publish with same content_id+channel must not re-hit the API."""
    route = respx.post("https://dev.to/api/articles").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 999,
                "url": "https://dev.to/test-user/once-only",
            },
        )
    )

    adapter = DevToAdapter()
    profile = {"DEV_TO_API_KEY": "test-key"}
    variant = Variant(
        channel="devto:main",
        title="Once",
        body="# body",
        extras={"content_id": "once@2026-05-19"},
    )

    r1 = await adapter.publish(variant, profile, yaml_backend)
    r2 = await adapter.publish(variant, profile, yaml_backend)

    assert r1.state == "live"
    assert r2.state == "live"
    # API hit exactly once across both publishes.
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# publish — failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_returns_failed_on_4xx(yaml_backend):
    respx.post("https://dev.to/api/articles").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )

    adapter = DevToAdapter()
    profile = {"DEV_TO_API_KEY": "bad-key"}
    variant = Variant(
        channel="devto:main",
        title="t",
        body="b",
        extras={"content_id": "auth-fail@2026-05-19"},
    )

    result = await adapter.publish(variant, profile, yaml_backend)
    assert result.state == "failed"
    assert result.error is not None


@pytest.mark.asyncio
@respx.mock
async def test_publish_retries_once_on_429(yaml_backend, monkeypatch):
    """429 on first attempt → sleep → retry → 201."""
    # No-op sleep so the test runs fast.
    import asyncio

    async def _instant_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    route = respx.post("https://dev.to/api/articles").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "1"}),
            httpx.Response(
                201,
                json={"id": 1, "url": "https://dev.to/test-user/retry-ok"},
            ),
        ]
    )

    adapter = DevToAdapter()
    profile = {"DEV_TO_API_KEY": "test-key"}
    variant = Variant(
        channel="devto:main",
        title="t",
        body="b",
        extras={"content_id": "retry@2026-05-19"},
    )

    result = await adapter.publish(variant, profile, yaml_backend)
    assert result.state == "live"
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# hints — ChannelHints contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_hints_returns_channelhints(yaml_backend):
    # Mock /api/tags so hints() can complete its fetch.
    respx.get("https://dev.to/api/tags").mock(
        return_value=httpx.Response(
            200,
            json=[{"name": "python"}, {"name": "ai"}, {"name": "automation"}],
        )
    )

    adapter = DevToAdapter()
    profile = {"DEV_TO_API_KEY": "test-key"}
    hints = await adapter.hints(profile)

    assert hints.canonical_url_supported is True
    assert hints.browser_only is False
    assert hints.cta_placement == "bottom"
    assert hints.tag_vocab is not None
    assert "python" in hints.tag_vocab
