"""Tests for content_distribution_mcp.backends.yaml_backend.YamlBackend."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Profile management
# ---------------------------------------------------------------------------


def test_save_and_load_profile(yaml_backend):
    yaml_backend.save_profile("dev-platforms", {"channels": ["devto:main"]})
    loaded = yaml_backend.load_profile("dev-platforms")
    assert loaded == {"channels": ["devto:main"]}


def test_load_unknown_profile_returns_none(yaml_backend):
    assert yaml_backend.load_profile("missing") is None


def test_list_profiles(yaml_backend):
    yaml_backend.save_profile("a", {"x": 1})
    yaml_backend.save_profile("b", {"x": 2})
    names = yaml_backend.list_profiles()
    assert set(names) == {"a", "b"}


# ---------------------------------------------------------------------------
# Subreddit rules
# ---------------------------------------------------------------------------


def test_save_and_load_subreddit_rules(yaml_backend):
    rules = {"cooldown_hours": 168, "require_flair": True}
    yaml_backend.save_subreddit_rules("LocalLLaMA", rules)
    loaded = yaml_backend.load_subreddit_rules("LocalLLaMA")
    assert loaded == rules


def test_load_unknown_subreddit_returns_none(yaml_backend):
    assert yaml_backend.load_subreddit_rules("notreal") is None


def test_list_subreddits(yaml_backend):
    yaml_backend.save_subreddit_rules("a", {"x": 1})
    yaml_backend.save_subreddit_rules("b", {"x": 2})
    assert set(yaml_backend.list_subreddits()) == {"a", "b"}


# ---------------------------------------------------------------------------
# Idempotency: claim + mark_published + lookup
# ---------------------------------------------------------------------------


def test_claim_idempotency_key_first_call_succeeds(yaml_backend):
    ok = yaml_backend.claim_idempotency_key("post1", "devto:main")
    assert ok is True


def test_claim_idempotency_key_after_live_returns_false(yaml_backend):
    yaml_backend.claim_idempotency_key("post1", "devto:main")
    yaml_backend.mark_published(
        "post1", "devto:main", state="live", published_url="https://dev.to/x"
    )
    # Second claim should be refused.
    ok = yaml_backend.claim_idempotency_key("post1", "devto:main")
    assert ok is False


def test_claim_then_mark_failed_allows_reclaim(yaml_backend):
    """A failed publish should not block a future retry — only live/queued do."""
    yaml_backend.claim_idempotency_key("post1", "devto:main")
    yaml_backend.mark_published("post1", "devto:main", state="failed", error="boom")
    ok = yaml_backend.claim_idempotency_key("post1", "devto:main")
    assert ok is True


def test_mark_published_updates_claiming_stub(yaml_backend):
    yaml_backend.claim_idempotency_key("post1", "devto:main")
    yaml_backend.mark_published(
        "post1", "devto:main", state="live", published_url="https://dev.to/x"
    )
    found = yaml_backend.lookup_published("post1", "devto:main")
    assert found is not None
    assert found["state"] == "live"
    assert found["published_url"] == "https://dev.to/x"


def test_lookup_published_missing_returns_none(yaml_backend):
    assert yaml_backend.lookup_published("nope", "devto:main") is None


def test_list_post_log_filters(yaml_backend):
    yaml_backend.claim_idempotency_key("p1", "devto:main")
    yaml_backend.mark_published("p1", "devto:main", state="live", published_url="u1")
    yaml_backend.claim_idempotency_key("p2", "devto:main")
    yaml_backend.mark_published("p2", "devto:main", state="failed", error="e")

    all_records = yaml_backend.list_post_log()
    assert len(all_records) == 2

    live_only = yaml_backend.list_post_log(state="live")
    assert len(live_only) == 1
    assert live_only[0]["content_id"] == "p1"

    p2_only = yaml_backend.list_post_log(content_id="p2")
    assert len(p2_only) == 1


# ---------------------------------------------------------------------------
# Scheduler queue
# ---------------------------------------------------------------------------


def test_enqueue_returns_scheduled_id(yaml_backend):
    sid = yaml_backend.enqueue_scheduled(
        {
            "content_id": "p1",
            "channel": "devto:main",
            "schedule_at": "2030-01-01T00:00:00+00:00",
        }
    )
    assert isinstance(sid, str)
    assert len(sid) == 32  # uuid4 hex


def test_drain_returns_only_due_variants(yaml_backend):
    # Far past → due
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    yaml_backend.enqueue_scheduled(
        {"content_id": "due", "channel": "devto:main", "schedule_at": past_iso}
    )
    # Far future → not due
    future_iso = "2099-01-01T00:00:00+00:00"
    yaml_backend.enqueue_scheduled(
        {"content_id": "later", "channel": "devto:main", "schedule_at": future_iso}
    )

    due = yaml_backend.drain_scheduled()
    assert len(due) == 1
    assert due[0]["content_id"] == "due"

    # Second drain returns nothing — due item was already removed.
    assert yaml_backend.drain_scheduled() == []


def test_drain_treats_missing_schedule_at_as_due(yaml_backend):
    yaml_backend.enqueue_scheduled({"content_id": "now", "channel": "devto:main"})
    due = yaml_backend.drain_scheduled()
    assert len(due) == 1


def test_cancel_scheduled(yaml_backend):
    sid = yaml_backend.enqueue_scheduled(
        {
            "content_id": "p1",
            "channel": "devto:main",
            "schedule_at": "2099-01-01T00:00:00+00:00",
        }
    )
    assert yaml_backend.cancel_scheduled(sid) is True
    # Drain returns nothing — the only item was cancelled.
    assert yaml_backend.drain_scheduled() == []


def test_cancel_scheduled_missing_returns_false(yaml_backend):
    assert yaml_backend.cancel_scheduled("not-a-real-id") is False


# ---------------------------------------------------------------------------
# Reddit log
# ---------------------------------------------------------------------------


def test_record_reddit_and_count_today(yaml_backend):
    now_iso = datetime.now(timezone.utc).isoformat()
    yaml_backend.record_reddit_post(
        {
            "account": "csreyes92",
            "subreddit": "LocalLLaMA",
            "content_id": "p1",
            "posted_at": now_iso,
        }
    )
    assert yaml_backend.count_reddit_posts_today("csreyes92") == 1
    assert yaml_backend.count_reddit_posts_today("other") == 0


def test_count_reddit_excludes_yesterday(yaml_backend):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1, hours=2)).isoformat()
    yaml_backend.record_reddit_post(
        {
            "account": "csreyes92",
            "subreddit": "LocalLLaMA",
            "content_id": "p1",
            "posted_at": yesterday,
        }
    )
    assert yaml_backend.count_reddit_posts_today("csreyes92") == 0


# ---------------------------------------------------------------------------
# purge_all
# ---------------------------------------------------------------------------


def test_purge_all_wipes_everything(yaml_backend):
    yaml_backend.save_profile("x", {"k": "v"})
    yaml_backend.claim_idempotency_key("p1", "devto:main")
    yaml_backend.purge_all()
    assert yaml_backend.list_profiles() == []
    assert yaml_backend.list_post_log() == []
