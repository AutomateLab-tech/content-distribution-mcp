"""End-to-end test of the GitHub Discussions adapter using respx.

Mirrors the structure of test_hashnode_adapter.py — exercises publish,
idempotency, and failure paths against a mocked GitHub GraphQL endpoint,
with state recorded via the real YamlBackend fixture.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from content_distribution_mcp.adapters import github_discussions as gh_module
from content_distribution_mcp.adapters.github_discussions import (
    GitHubDiscussionsAdapter,
)
from content_distribution_mcp.models import Variant


_GQL_URL = "https://api.github.com/graphql"
_OWNER = "AutomateLab-tech"
_REPO = "content-distribution-mcp"
_CHANNEL = f"github-discussions:{_OWNER}/{_REPO}"


@pytest.fixture(autouse=True)
def _clear_repo_cache():
    """Reset the module-level (owner, repo) cache between tests."""
    gh_module._REPO_CACHE.clear()
    yield
    gh_module._REPO_CACHE.clear()


def _variant(**overrides) -> Variant:
    base = dict(
        channel=_CHANNEL,
        title="Hello GitHub",
        body="# hi",
        extras={
            "content_id": "hello@2026-05-19",
            "category": "Announcements",
        },
    )
    base.update(overrides)
    return Variant(**base)


def _resolve_response() -> dict:
    return {
        "data": {
            "repository": {
                "id": "R_kgDOxxxx",
                "discussionCategories": {
                    "nodes": [
                        {"id": "DIC_announce", "name": "Announcements"},
                        {"id": "DIC_general", "name": "General"},
                    ]
                },
            }
        }
    }


def _create_response(url: str = "https://github.com/AutomateLab-tech/content-distribution-mcp/discussions/1") -> dict:
    return {
        "data": {
            "createDiscussion": {
                "discussion": {"id": "D_kw123", "url": url}
            }
        }
    }


# ---------------------------------------------------------------------------
# can_publish — tuple[bool, str] contract
# ---------------------------------------------------------------------------


def test_can_publish_accepts_github_discussions_variant():
    adapter = GitHubDiscussionsAdapter()
    ok, reason = adapter.can_publish(_variant())
    assert ok is True
    assert reason == ""


def test_can_publish_rejects_wrong_channel():
    adapter = GitHubDiscussionsAdapter()
    ok, reason = adapter.can_publish(_variant(channel="devto:main"))
    assert ok is False
    assert "github" in reason.lower() or "channel" in reason.lower()


def test_can_publish_rejects_missing_category():
    adapter = GitHubDiscussionsAdapter()
    ok, reason = adapter.can_publish(_variant(extras={"content_id": "x"}))
    assert ok is False
    assert "category" in reason.lower()


def test_can_publish_rejects_empty_title():
    adapter = GitHubDiscussionsAdapter()
    ok, reason = adapter.can_publish(_variant(title=""))
    assert ok is False


def test_can_publish_rejects_empty_body():
    adapter = GitHubDiscussionsAdapter()
    ok, reason = adapter.can_publish(_variant(body=""))
    assert ok is False


# ---------------------------------------------------------------------------
# publish — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_returns_live_on_success(yaml_backend):
    route = respx.post(_GQL_URL).mock(
        side_effect=[
            httpx.Response(200, json=_resolve_response()),
            httpx.Response(200, json=_create_response()),
        ]
    )

    adapter = GitHubDiscussionsAdapter()
    profile = {"GITHUB_TOKEN": "ghp_test"}
    result = await adapter.publish(_variant(), profile, yaml_backend)

    assert result.state == "live"
    assert str(result.live_url) == (
        "https://github.com/AutomateLab-tech/content-distribution-mcp/discussions/1"
    )
    assert result.channel == _CHANNEL
    assert route.call_count == 2

    logged = yaml_backend.lookup_published("hello@2026-05-19", _CHANNEL)
    assert logged is not None
    assert logged["state"] == "live"
    assert logged["published_url"].endswith("/discussions/1")


# ---------------------------------------------------------------------------
# publish — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_is_idempotent(yaml_backend):
    route = respx.post(_GQL_URL).mock(
        side_effect=[
            httpx.Response(200, json=_resolve_response()),
            httpx.Response(200, json=_create_response()),
        ]
    )

    adapter = GitHubDiscussionsAdapter()
    profile = {"GITHUB_TOKEN": "ghp_test"}
    v = _variant(extras={"content_id": "once@2026-05-19", "category": "Announcements"})

    r1 = await adapter.publish(v, profile, yaml_backend)
    r2 = await adapter.publish(v, profile, yaml_backend)

    assert r1.state == "live"
    assert r2.state == "live"
    # Second publish hits no API at all (idempotent short-circuit).
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# publish — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_returns_failed_on_unknown_category(yaml_backend):
    respx.post(_GQL_URL).mock(
        return_value=httpx.Response(200, json=_resolve_response()),
    )

    adapter = GitHubDiscussionsAdapter()
    profile = {"GITHUB_TOKEN": "ghp_test"}
    result = await adapter.publish(
        _variant(extras={"content_id": "bad-cat@2026-05-19", "category": "DoesNotExist"}),
        profile,
        yaml_backend,
    )

    assert result.state == "failed"
    assert "DoesNotExist" in (result.error or "")

    logged = yaml_backend.list_post_log(
        content_id="bad-cat@2026-05-19", channel=_CHANNEL
    )
    assert any(r["state"] == "failed" for r in logged)


@pytest.mark.asyncio
@respx.mock
async def test_publish_returns_failed_on_graphql_errors(yaml_backend):
    respx.post(_GQL_URL).mock(
        side_effect=[
            httpx.Response(200, json=_resolve_response()),
            httpx.Response(200, json={"errors": [{"message": "Forbidden"}]}),
        ]
    )

    adapter = GitHubDiscussionsAdapter()
    profile = {"GITHUB_TOKEN": "ghp_test"}
    result = await adapter.publish(
        _variant(extras={"content_id": "fail@2026-05-19", "category": "Announcements"}),
        profile,
        yaml_backend,
    )

    assert result.state == "failed"
    assert "Forbidden" in (result.error or "")

    logged = yaml_backend.list_post_log(
        content_id="fail@2026-05-19", channel=_CHANNEL
    )
    assert any(r["state"] == "failed" for r in logged)


@pytest.mark.asyncio
@respx.mock
async def test_publish_returns_failed_on_4xx(yaml_backend):
    respx.post(_GQL_URL).mock(
        return_value=httpx.Response(401, text="Bad credentials"),
    )

    adapter = GitHubDiscussionsAdapter()
    profile = {"GITHUB_TOKEN": "bad-token"}
    result = await adapter.publish(
        _variant(extras={"content_id": "auth-fail@2026-05-19", "category": "Announcements"}),
        profile,
        yaml_backend,
    )

    assert result.state == "failed"
    assert "401" in (result.error or "")


# ---------------------------------------------------------------------------
# hints — ChannelHints contract
# ---------------------------------------------------------------------------


def test_hints_returns_channelhints():
    adapter = GitHubDiscussionsAdapter()
    hints = adapter.hints()
    assert hints.canonical_url_supported is False
    assert hints.browser_only is False
    assert hints.cta_placement == "footer"
    # Discussions use categories, not tags.
    assert hints.tag_vocab is None
