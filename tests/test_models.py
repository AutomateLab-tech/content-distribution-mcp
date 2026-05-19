"""Tests for content_distribution_mcp.models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from content_distribution_mcp.models import (
    ChannelHints,
    Content,
    PublishResult,
    Variant,
)


def test_content_minimal_construction():
    c = Content(
        id="post@2026-05-19",
        title="Hello",
        body_md="# Hi",
        author="me",
    )
    assert c.id == "post@2026-05-19"
    assert c.tags == []
    assert c.cover_image is None


def test_content_rejects_unknown_field():
    with pytest.raises(ValidationError):
        Content(
            id="x",
            title="y",
            body_md="z",
            author="a",
            unknown="oops",  # type: ignore[call-arg]
        )


def test_variant_defaults():
    v = Variant(channel="devto:main", title="t", body="b")
    assert v.tags == []
    assert v.schedule_at is None
    assert v.extras == {}


def test_variant_rejects_unknown_field():
    with pytest.raises(ValidationError):
        Variant(
            channel="devto:main",
            title="t",
            body="b",
            unknown="oops",  # type: ignore[call-arg]
        )


def test_publish_result_live_with_url():
    r = PublishResult(
        channel="devto:main",
        state="live",
        live_url="https://dev.to/me/post-1",
        published_at=datetime(2026, 5, 19, tzinfo=timezone.utc),
    )
    assert r.state == "live"
    assert str(r.live_url).startswith("https://dev.to/")


def test_publish_result_rejects_invalid_state():
    with pytest.raises(ValidationError):
        PublishResult(channel="x", state="bogus")  # type: ignore[arg-type]


def test_channel_hints_defaults():
    h = ChannelHints()
    assert h.cta_placement == "bottom"
    assert h.canonical_url_supported is True
    assert h.browser_only is False
    assert h.supported_md_features == set()


def test_publish_result_round_trip_json():
    """model_dump(mode='json') must round-trip via model_validate."""
    r = PublishResult(
        channel="devto:main",
        state="live",
        live_url="https://dev.to/me/post-1",
        published_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
    )
    dumped = r.model_dump(mode="json")
    restored = PublishResult.model_validate(dumped)
    assert restored.channel == r.channel
    assert restored.state == r.state
