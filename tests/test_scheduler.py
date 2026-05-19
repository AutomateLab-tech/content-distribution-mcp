"""Integration tests for scheduler.py against the real YamlBackend.

These pin down the Variant↔dict conversion at the scheduler/backend boundary
that the original spec missed. They also cover the happy publish_immediate
path with a stub adapter so we don't drag respx in here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from content_distribution_mcp import scheduler
from content_distribution_mcp.models import PublishResult, Variant


# ---------------------------------------------------------------------------
# Stub adapter — synchronous-ish: returns a fixed result for any call.
# ---------------------------------------------------------------------------


class _StubAdapter:
    def __init__(self, result: PublishResult) -> None:
        self._result = result
        self.calls: list[Variant] = []

    def can_publish(self, variant: Variant) -> tuple[bool, str]:
        if variant.channel.startswith("devto:"):
            return True, ""
        return False, f"channel-not-devto: {variant.channel}"

    async def publish(self, variant, profile, state_backend):  # noqa: ARG002
        self.calls.append(variant)
        return self._result


# ---------------------------------------------------------------------------
# publish_immediate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_immediate_dispatches_by_prefix(yaml_backend):
    adapter = _StubAdapter(
        PublishResult(channel="devto:main", state="live", live_url="https://dev.to/x")
    )
    adapters = {"devto": adapter}
    v = Variant(channel="devto:main", title="t", body="b")

    results = await scheduler.publish_immediate(
        content=None, variants=[v], profile={"DEV_TO_API_KEY": "k"},
        adapters=adapters, state_backend=yaml_backend,
    )

    assert len(results) == 1
    assert results[0].state == "live"
    assert adapter.calls == [v]


@pytest.mark.asyncio
async def test_publish_immediate_no_adapter_returns_failed(yaml_backend):
    v = Variant(channel="someplace:main", title="t", body="b")
    results = await scheduler.publish_immediate(
        content=None, variants=[v], profile=None,
        adapters={}, state_backend=yaml_backend,
    )
    assert results[0].state == "failed"
    assert "no-adapter-for-channel" in (results[0].error or "")


# ---------------------------------------------------------------------------
# schedule — enqueue + immediate split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_enqueues_future_variants(yaml_backend):
    """A scheduled variant must be persisted as a dict (via model_dump)."""
    adapter = _StubAdapter(
        PublishResult(channel="devto:main", state="live", live_url="https://dev.to/now")
    )
    adapters = {"devto": adapter}

    future = datetime.now(timezone.utc) + timedelta(hours=24)
    later = Variant(channel="devto:main", title="later", body="b", schedule_at=future)

    out = await scheduler.schedule(
        content=None, variants=[later], profile={"DEV_TO_API_KEY": "k"},
        adapters=adapters, state_backend=yaml_backend,
    )

    # schedule() returns the scheduled_id (a uuid4 hex) for non-immediate items.
    assert isinstance(out["devto:main"], str)
    assert len(out["devto:main"]) == 32

    # The variant must serialise to a dict on disk — read it back via list_post_log
    # is not the right shape; check the pending file directly.
    pending_path = yaml_backend._path(yaml_backend._PENDING_FILE)
    import yaml
    raw = yaml.safe_load(pending_path.read_text(encoding="utf-8")) or []
    assert len(raw) == 1
    assert raw[0]["title"] == "later"
    assert raw[0]["channel"] == "devto:main"
    # schedule_at must be an ISO string (model_dump(mode="json") behaviour),
    # not a datetime object — yaml can't load arbitrary tagged datetimes.
    assert isinstance(raw[0]["schedule_at"], str)


@pytest.mark.asyncio
async def test_schedule_publishes_immediate_variants(yaml_backend):
    """A variant without schedule_at must fire now via publish_immediate."""
    adapter = _StubAdapter(
        PublishResult(channel="devto:main", state="live", live_url="https://dev.to/now")
    )
    adapters = {"devto": adapter}

    now = Variant(channel="devto:main", title="now", body="b")
    out = await scheduler.schedule(
        content=None, variants=[now], profile={"DEV_TO_API_KEY": "k"},
        adapters=adapters, state_backend=yaml_backend,
    )

    assert isinstance(out["devto:main"], PublishResult)
    assert out["devto:main"].state == "live"
    assert adapter.calls == [now]


# ---------------------------------------------------------------------------
# drain — round-trips Variant through the queue and publishes it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_publishes_due_variant(yaml_backend):
    adapter = _StubAdapter(
        PublishResult(channel="devto:main", state="live", live_url="https://dev.to/q")
    )
    adapters = {"devto": adapter}

    # Enqueue a variant whose schedule_at is in the past so drain picks it up.
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    yaml_backend.enqueue_scheduled(
        Variant(
            channel="devto:main", title="t", body="b", schedule_at=past,
        ).model_dump(mode="json")
    )

    results = await scheduler.drain(adapters=adapters, state_backend=yaml_backend)

    assert len(results) == 1
    assert results[0].state == "live"
    assert adapter.calls[0].title == "t"
    # Queue is now empty.
    assert yaml_backend.drain_scheduled() == []


@pytest.mark.asyncio
async def test_drain_empty_returns_empty_list(yaml_backend):
    results = await scheduler.drain(adapters={}, state_backend=yaml_backend)
    assert results == []
