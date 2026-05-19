"""End-to-end tests of the Reddit adapter with mocked PRAW.

Mirrors test_hashnode_adapter.py — exercises publish, idempotency, and
gate-failure paths against a monkeypatched ``_build_praw_reddit`` so the
test suite never touches the real Reddit API.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from content_distribution_mcp.adapters import reddit as reddit_module
from content_distribution_mcp.adapters.reddit import RedditAdapter
from content_distribution_mcp.models import Variant


_CHANNEL = "reddit:LocalLLaMA"
_SUBREDDIT = "LocalLLaMA"
_LIVE_URL = "https://reddit.com/r/LocalLLaMA/comments/abc123/hello/"


@pytest.fixture(autouse=True)
def _fast_automod_poll(monkeypatch):
    """Collapse the AutoMod poll loop to a single fast iteration."""
    monkeypatch.setattr(reddit_module, "_AUTOMOD_POLL_INTERVAL_SECS", 0.0)
    monkeypatch.setattr(reddit_module, "_AUTOMOD_POLL_ATTEMPTS", 1)


def _variant(**overrides) -> Variant:
    base = dict(
        channel=_CHANNEL,
        title="Hello Reddit",
        body="hi from automatelab",
        extras={"content_id": "hello@2026-05-19"},
    )
    base.update(overrides)
    return Variant(**base)


def _profile(**overrides) -> dict:
    base = {
        "REDDIT_CLIENT_ID": "cid",
        "REDDIT_CLIENT_SECRET": "csec",
        "REDDIT_USERNAME": "automatelab_bot",
        "REDDIT_PASSWORD": "pw",
        "REDDIT_USER_AGENT": "automatelab-test/0.1",
    }
    base.update(overrides)
    return base


def _make_submission(
    *,
    shortlink: str = _LIVE_URL,
    removed: bool = False,
    locked: bool = False,
) -> MagicMock:
    """Return a MagicMock that quacks like a praw.models.Submission."""
    sub = MagicMock()
    sub.shortlink = shortlink
    sub.url = shortlink
    sub.removed = removed
    sub.locked = locked
    # _fetch is called by _poll_automod_removal; default no-op.
    sub._fetch = MagicMock(return_value=None)
    return sub


def _install_praw_mock(monkeypatch, submission: MagicMock) -> MagicMock:
    """Replace ``_build_praw_reddit`` so submit() returns ``submission``."""
    fake_subreddit = MagicMock()
    fake_subreddit.submit = MagicMock(return_value=submission)
    fake_reddit = MagicMock()
    fake_reddit.subreddit = MagicMock(return_value=fake_subreddit)
    monkeypatch.setattr(
        reddit_module, "_build_praw_reddit", lambda profile: fake_reddit
    )
    return fake_reddit


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
    assert "content_id" in reason.lower() or "content-id" in reason.lower()


def test_can_publish_rejects_empty_title():
    adapter = RedditAdapter()
    ok, reason = adapter.can_publish(_variant(title=""))
    assert ok is False


def test_can_publish_rejects_empty_body():
    adapter = RedditAdapter()
    ok, reason = adapter.can_publish(_variant(body=""))
    assert ok is False


# ---------------------------------------------------------------------------
# publish — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_returns_live_on_success(monkeypatch, yaml_backend):
    submission = _make_submission()
    fake_reddit = _install_praw_mock(monkeypatch, submission)

    adapter = RedditAdapter()
    result = await adapter.publish(_variant(), _profile(), yaml_backend)

    assert result.state == "live"
    assert str(result.live_url) == _LIVE_URL
    assert result.channel == _CHANNEL

    # PRAW submit was called with the right subreddit + title.
    fake_reddit.subreddit.assert_called_once_with(_SUBREDDIT)
    submit_kwargs = fake_reddit.subreddit.return_value.submit.call_args.kwargs
    assert submit_kwargs["title"] == "Hello Reddit"
    assert submit_kwargs["selftext"] == "hi from automatelab"

    logged = yaml_backend.lookup_published("hello@2026-05-19", _CHANNEL)
    assert logged is not None
    assert logged["state"] == "live"
    assert logged["published_url"] == _LIVE_URL

    reddit_log = yaml_backend.list_reddit_log(account="automatelab_bot")
    assert len(reddit_log) == 1
    assert reddit_log[0]["subreddit"] == _SUBREDDIT
    assert reddit_log[0]["content_id"] == "hello@2026-05-19"


# ---------------------------------------------------------------------------
# publish — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_is_idempotent(monkeypatch, yaml_backend):
    submission = _make_submission()
    fake_reddit = _install_praw_mock(monkeypatch, submission)

    adapter = RedditAdapter()
    v = _variant(extras={"content_id": "once@2026-05-19"})

    r1 = await adapter.publish(v, _profile(), yaml_backend)
    r2 = await adapter.publish(v, _profile(), yaml_backend)

    assert r1.state == "live"
    assert r2.state == "live"
    # Second call short-circuits — submit() is hit exactly once.
    assert fake_reddit.subreddit.return_value.submit.call_count == 1


# ---------------------------------------------------------------------------
# publish — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_returns_failed_on_missing_profile(yaml_backend):
    adapter = RedditAdapter()
    result = await adapter.publish(_variant(), None, yaml_backend)
    assert result.state == "failed"
    assert "profile" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_publish_returns_failed_when_daily_cap_reached(
    monkeypatch, yaml_backend
):
    # Pre-seed 5 entries today for this account.
    now_iso = datetime.now(timezone.utc).isoformat()
    for i in range(reddit_module._GLOBAL_DAILY_CAP):
        yaml_backend.record_reddit_post({
            "account": "automatelab_bot",
            "subreddit": _SUBREDDIT,
            "content_id": f"prev-{i}@2026-05-19",
            "channel": _CHANNEL,
            "posted_at": now_iso,
            "url": f"https://reddit.com/r/{_SUBREDDIT}/comments/x{i}/",
        })

    # PRAW should never be touched if the gate fails.
    submission = _make_submission()
    fake_reddit = _install_praw_mock(monkeypatch, submission)

    adapter = RedditAdapter()
    result = await adapter.publish(
        _variant(extras={"content_id": "cap@2026-05-19"}),
        _profile(),
        yaml_backend,
    )

    assert result.state == "failed"
    assert "cap" in (result.error or "").lower()
    fake_reddit.subreddit.return_value.submit.assert_not_called()

    # Stub must be resolved to failed so a later retry can re-claim.
    logged = yaml_backend.list_post_log(
        content_id="cap@2026-05-19", channel=_CHANNEL
    )
    assert any(r["state"] == "failed" for r in logged)


@pytest.mark.asyncio
async def test_publish_returns_failed_on_active_cooldown(
    monkeypatch, yaml_backend
):
    # Seed the post-log with a live entry from 2h ago (well inside default 168h cooldown).
    yaml_backend.claim_idempotency_key("prev@2026-05-19", _CHANNEL)
    yaml_backend.mark_published(
        "prev@2026-05-19",
        _CHANNEL,
        state="live",
        published_url=_LIVE_URL,
        error=None,
    )
    # Rewrite the updated_at field on the live row so it parses as 2h ago.
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    log_path = yaml_backend._path(yaml_backend._POST_LOG_FILE)
    import yaml
    raw = yaml.safe_load(log_path.read_text(encoding="utf-8")) or []
    for record in raw:
        if record.get("content_id") == "prev@2026-05-19" and record.get("state") == "live":
            record["updated_at"] = recent
    log_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    submission = _make_submission()
    fake_reddit = _install_praw_mock(monkeypatch, submission)

    adapter = RedditAdapter()
    result = await adapter.publish(
        _variant(extras={"content_id": "cooldown@2026-05-19"}),
        _profile(),
        yaml_backend,
    )

    assert result.state == "failed"
    assert "cooldown" in (result.error or "").lower()
    fake_reddit.subreddit.return_value.submit.assert_not_called()


@pytest.mark.asyncio
async def test_publish_returns_failed_on_automod_removal(
    monkeypatch, yaml_backend
):
    submission = _make_submission(removed=True)
    _install_praw_mock(monkeypatch, submission)

    adapter = RedditAdapter()
    result = await adapter.publish(
        _variant(extras={"content_id": "automod@2026-05-19"}),
        _profile(),
        yaml_backend,
    )

    assert result.state == "failed"
    assert "automod" in (result.error or "").lower()

    logged = yaml_backend.list_post_log(
        content_id="automod@2026-05-19", channel=_CHANNEL
    )
    assert any(r["state"] == "failed" for r in logged)


# ---------------------------------------------------------------------------
# hints — ChannelHints contract
# ---------------------------------------------------------------------------


def test_hints_returns_channelhints():
    adapter = RedditAdapter()
    hints = adapter.hints()
    assert hints.max_length == 40_000
    assert hints.canonical_url_supported is False
    assert hints.browser_only is False
    assert hints.cta_placement == "none"
    assert "bold" in hints.supported_md_features
    assert "headers" in hints.supported_md_features
    # Reddit does not support tables/images in self-text posts.
    assert "tables" not in hints.supported_md_features
