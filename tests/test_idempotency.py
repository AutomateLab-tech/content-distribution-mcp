"""Tests for content_distribution_mcp.idempotency."""

from __future__ import annotations

import pytest

from content_distribution_mcp.idempotency import (
    RetryPolicy,
    make_idempotency_key,
    retry_publish,
    should_retry,
)
from content_distribution_mcp.models import PublishResult, Variant


# ---------------------------------------------------------------------------
# make_idempotency_key
# ---------------------------------------------------------------------------


def test_make_idempotency_key_format():
    key = make_idempotency_key("post@2026-05-19", "devto:main")
    assert key == "post@2026-05-19::devto:main"


def test_make_idempotency_key_includes_subreddit():
    """Reddit subreddits become part of the channel, so each sub gets its own key."""
    k1 = make_idempotency_key("p1", "reddit:LocalLLaMA")
    k2 = make_idempotency_key("p1", "reddit:n8n")
    assert k1 != k2


# ---------------------------------------------------------------------------
# should_retry
# ---------------------------------------------------------------------------


def test_should_retry_permanent_signal():
    do_retry, sleep = should_retry("401 unauthorized", attempt=1, max_attempts=3)
    assert do_retry is False
    assert sleep == 0.0


def test_should_retry_transient_signal():
    do_retry, sleep = should_retry("503 service unavailable", attempt=1, max_attempts=3)
    assert do_retry is True
    assert sleep == 2.0  # 2 ** 1


def test_should_retry_backoff_grows():
    _, s1 = should_retry("timeout", attempt=1, max_attempts=5)
    _, s2 = should_retry("timeout", attempt=2, max_attempts=5)
    _, s3 = should_retry("timeout", attempt=3, max_attempts=5)
    assert s1 < s2 < s3


def test_should_retry_capped_at_60_seconds():
    _, sleep = should_retry("timeout", attempt=10, max_attempts=20)
    assert sleep == 60.0


def test_should_retry_exhausted_attempts():
    do_retry, sleep = should_retry("timeout", attempt=3, max_attempts=3)
    assert do_retry is False
    assert sleep == 0.0


def test_should_retry_unknown_error_treated_as_transient():
    """Conservative default: unrecognised errors should still be retried."""
    do_retry, sleep = should_retry("something weird", attempt=1, max_attempts=3)
    assert do_retry is True
    assert sleep > 0


# ---------------------------------------------------------------------------
# RetryPolicy
# ---------------------------------------------------------------------------


def test_retry_policy_builtins():
    p = RetryPolicy()
    assert p.max_attempts_for("reddit:LocalLLaMA") == 1
    assert p.max_attempts_for("devto:main") == 3
    assert p.max_attempts_for("medium_browser:main") == 1
    assert p.max_attempts_for("medium-browser:main") == 1


def test_retry_policy_unknown_channel_falls_back_to_default():
    p = RetryPolicy()
    assert p.max_attempts_for("unknown:foo") == 3


def test_retry_policy_override():
    """User overrides merge on top of built-ins; unset built-ins survive."""
    p = RetryPolicy({"devto": 5, "default": 2})
    # Explicit override wins.
    assert p.max_attempts_for("devto:main") == 5
    # linkedin was not overridden → built-in 3 is preserved.
    assert p.max_attempts_for("linkedin:personal") == 3
    # Truly unknown channel → falls through to overridden default.
    assert p.max_attempts_for("nonexistent:foo") == 2


# ---------------------------------------------------------------------------
# retry_publish (async)
# ---------------------------------------------------------------------------


class _StubAdapter:
    """Minimal stub that mimics the ChannelAdapter.publish() coroutine."""

    def __init__(self, results: list[PublishResult]) -> None:
        self._results = list(results)
        self.calls = 0

    async def publish(self, variant, profile, state_backend):  # noqa: ARG002
        self.calls += 1
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_retry_publish_returns_live_immediately():
    adapter = _StubAdapter([
        PublishResult(channel="devto:main", state="live", live_url="https://dev.to/x"),
    ])
    v = Variant(channel="devto:main", title="t", body="b")
    result = await retry_publish(adapter, v, profile=None, state_backend=None, max_attempts=3)
    assert result.state == "live"
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_retry_publish_stops_on_permanent_error():
    adapter = _StubAdapter([
        PublishResult(channel="devto:main", state="failed", error="401 unauthorized"),
    ])
    v = Variant(channel="devto:main", title="t", body="b")
    result = await retry_publish(adapter, v, profile=None, state_backend=None, max_attempts=3)
    assert result.state == "failed"
    assert adapter.calls == 1  # no retries


@pytest.mark.asyncio
async def test_retry_publish_retries_transient_then_succeeds(monkeypatch):
    """Transient failure → backoff → second attempt succeeds."""
    # Patch asyncio.sleep so test runs instantly.
    import asyncio

    async def _instant_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    adapter = _StubAdapter([
        PublishResult(channel="devto:main", state="failed", error="503"),
        PublishResult(channel="devto:main", state="live", live_url="https://dev.to/x"),
    ])
    v = Variant(channel="devto:main", title="t", body="b")
    result = await retry_publish(adapter, v, profile=None, state_backend=None, max_attempts=3)
    assert result.state == "live"
    assert adapter.calls == 2
