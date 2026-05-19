"""End-to-end tests for the LinkedIn browser-fallback adapter.

LinkedIn has no public posting API that covers personal feed / company-page
admin posting, so the adapter writes a plain-text draft, returns a compose
URL, and records `state="needs_browser"`. Tests verify the draft +
needs_browser handoff, idempotency short-circuits, the `mark_live` flip,
and the target → compose-URL routing.

Playwright pre-fill is intentionally NOT exercised — it's an optional dep
that defaults to off.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from content_distribution_mcp.adapters import linkedin_browser as lb_module
from content_distribution_mcp.adapters.linkedin_browser import (
    LinkedInBrowserAdapter,
    mark_live,
    open_pending_in_tabs,
)
from content_distribution_mcp.models import Variant


_CHANNEL_PERSONAL = "linkedin-browser:personal"
_CHANNEL_COMPANY = "linkedin-browser:116012269"


@pytest.fixture(autouse=True)
def _redirect_drafts_dir(tmp_path: Path, monkeypatch):
    """Send all draft writes into pytest tmp_path instead of the user's home."""
    monkeypatch.setattr(lb_module, "_DRAFTS_DIR", tmp_path / "drafts")


def _variant(**overrides) -> Variant:
    base = dict(
        channel=_CHANNEL_PERSONAL,
        title="",  # LinkedIn has no separate title field
        body="Excited to share that AutomateLab shipped a new MCP server.",
        extras={"content_id": "hello@2026-05-19"},
    )
    base.update(overrides)
    return Variant(**base)


# ---------------------------------------------------------------------------
# can_publish — tuple[bool, str] contract
# ---------------------------------------------------------------------------


def test_can_publish_accepts_linkedin_variant():
    adapter = LinkedInBrowserAdapter()
    ok, reason = adapter.can_publish(_variant())
    assert ok is True
    assert reason == ""


def test_can_publish_rejects_wrong_channel():
    adapter = LinkedInBrowserAdapter()
    ok, reason = adapter.can_publish(_variant(channel="devto:main"))
    assert ok is False
    assert "linkedin" in reason.lower() or "channel" in reason.lower()


def test_can_publish_rejects_missing_content_id():
    adapter = LinkedInBrowserAdapter()
    ok, reason = adapter.can_publish(_variant(extras={}))
    assert ok is False
    assert "content" in reason.lower()


def test_can_publish_rejects_empty_body():
    adapter = LinkedInBrowserAdapter()
    ok, reason = adapter.can_publish(_variant(body=""))
    assert ok is False


# ---------------------------------------------------------------------------
# publish — happy path (personal feed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_personal_writes_draft_and_returns_needs_browser(
    yaml_backend, tmp_path: Path
):
    adapter = LinkedInBrowserAdapter()
    result = await adapter.publish(_variant(), profile=None, state_backend=yaml_backend)

    assert result.state == "needs_browser"
    assert result.channel == _CHANNEL_PERSONAL
    assert str(result.compose_url) == "https://www.linkedin.com/feed/?shareActive=true"
    assert result.live_url is None

    # Draft file exists with the body content.
    assert result.draft_path is not None
    draft_path = Path(result.draft_path)
    assert draft_path.exists()
    text = draft_path.read_text(encoding="utf-8")
    assert "Excited to share" in text

    # Post-log records needs_browser.
    rows = yaml_backend.list_post_log(
        content_id="hello@2026-05-19", channel=_CHANNEL_PERSONAL
    )
    assert any(r["state"] == "needs_browser" for r in rows)


# ---------------------------------------------------------------------------
# publish — company target routes to /company/<id>/admin/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_to_company_uses_admin_url(yaml_backend):
    adapter = LinkedInBrowserAdapter()
    result = await adapter.publish(
        _variant(channel=_CHANNEL_COMPANY, extras={"content_id": "company@2026-05-19"}),
        profile=None,
        state_backend=yaml_backend,
    )

    assert result.state == "needs_browser"
    assert str(result.compose_url) == "https://www.linkedin.com/company/116012269/admin/"


# ---------------------------------------------------------------------------
# publish — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_is_idempotent_when_already_live(yaml_backend):
    """A second publish after mark_live short-circuits to state="live"."""
    adapter = LinkedInBrowserAdapter()
    v = _variant(extras={"content_id": "twice@2026-05-19"})

    r1 = await adapter.publish(v, profile=None, state_backend=yaml_backend)
    assert r1.state == "needs_browser"

    mark_live(
        "twice@2026-05-19",
        _CHANNEL_PERSONAL,
        "https://www.linkedin.com/posts/automatelab-activity-123",
        yaml_backend,
    )

    r2 = await adapter.publish(v, profile=None, state_backend=yaml_backend)
    assert r2.state == "live"
    assert str(r2.live_url) == "https://www.linkedin.com/posts/automatelab-activity-123"


@pytest.mark.asyncio
async def test_publish_returns_needs_browser_again_when_prior_not_live(yaml_backend):
    """Second call before mark_live re-surfaces the compose URL."""
    adapter = LinkedInBrowserAdapter()
    v = _variant(extras={"content_id": "pending@2026-05-19"})

    r1 = await adapter.publish(v, profile=None, state_backend=yaml_backend)
    r2 = await adapter.publish(v, profile=None, state_backend=yaml_backend)

    assert r1.state == "needs_browser"
    assert r2.state == "needs_browser"
    assert str(r2.compose_url) == "https://www.linkedin.com/feed/?shareActive=true"


# ---------------------------------------------------------------------------
# publish — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_returns_failed_on_missing_content_id_extras(yaml_backend):
    """Variant without content_id in extras fails before any state write."""
    adapter = LinkedInBrowserAdapter()
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
        "https://www.linkedin.com/posts/me-activity-xyz",
        yaml_backend,
    )

    logged = yaml_backend.lookup_published("ml@2026-05-19", _CHANNEL_PERSONAL)
    assert logged is not None
    assert logged["state"] == "live"
    assert logged["published_url"] == "https://www.linkedin.com/posts/me-activity-xyz"


# ---------------------------------------------------------------------------
# open_pending_in_tabs — enumerates needs_browser entries
# ---------------------------------------------------------------------------


def test_open_pending_in_tabs_returns_compose_urls(yaml_backend, monkeypatch):
    """Pending LinkedIn variants get their compose URLs reconstructed."""
    opened: list[str] = []
    monkeypatch.setattr(
        lb_module.webbrowser, "open_new_tab", lambda url: opened.append(url)
    )

    for channel in (_CHANNEL_PERSONAL, _CHANNEL_COMPANY):
        yaml_backend.claim_idempotency_key("multi@2026-05-19", channel)
        yaml_backend.mark_published(
            "multi@2026-05-19",
            channel,
            state="needs_browser",
            published_url=None,
            error=None,
        )

    urls = open_pending_in_tabs("multi@2026-05-19", yaml_backend)

    assert "https://www.linkedin.com/feed/?shareActive=true" in urls
    assert "https://www.linkedin.com/company/116012269/admin/" in urls
    assert set(opened) == set(urls)


def test_open_pending_in_tabs_skips_non_linkedin_channels(yaml_backend, monkeypatch):
    """Only linkedin-browser:* entries get a compose URL."""
    monkeypatch.setattr(lb_module.webbrowser, "open_new_tab", lambda url: None)

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
    assert urls == ["https://www.linkedin.com/feed/?shareActive=true"]


# ---------------------------------------------------------------------------
# unpublish — always returns False (manual operation)
# ---------------------------------------------------------------------------


def test_unpublish_returns_manual_guidance():
    adapter = LinkedInBrowserAdapter()
    ok, reason = adapter.unpublish("https://www.linkedin.com/posts/me-activity-abc")
    assert ok is False
    assert "manual" in reason.lower()
    assert "me-activity-abc" in reason


# ---------------------------------------------------------------------------
# hints — ChannelHints contract
# ---------------------------------------------------------------------------


def test_hints_returns_browser_only_channelhints():
    adapter = LinkedInBrowserAdapter()
    hints = adapter.hints()
    assert hints.browser_only is True
    assert hints.canonical_url_supported is False
    assert hints.cta_placement == "bottom"
    assert hints.max_length == 3000
    assert "links" in hints.supported_md_features


# ---------------------------------------------------------------------------
# Draft body — cta_block appended
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_appends_cta_block(yaml_backend):
    adapter = LinkedInBrowserAdapter()
    v = Variant(
        channel=_CHANNEL_PERSONAL,
        title="",
        body="Main body line.",
        cta_block="Subscribe for more.",
        extras={"content_id": "cta@2026-05-19"},
    )
    result = await adapter.publish(v, profile=None, state_backend=yaml_backend)

    text = Path(result.draft_path).read_text(encoding="utf-8")
    assert "Main body line." in text
    assert text.rstrip().endswith("Subscribe for more.")
