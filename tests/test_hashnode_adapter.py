"""End-to-end test of the Hashnode adapter using respx.

Mirrors the structure of test_devto_adapter.py — exercises publish, idempotency,
and failure paths against a fake GraphQL server, with state recorded via the
real YamlBackend fixture.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from content_distribution_mcp.adapters.hashnode import HashnodeAdapter
from content_distribution_mcp.models import Variant


_GQL_URL = "https://gql.hashnode.com/"


def _variant(**overrides) -> Variant:
    base = dict(
        channel="hashnode:main",
        title="Hello Hashnode",
        body="# hi",
        extras={
            "content_id": "hello@2026-05-19",
            "publicationId": "pub_abc123",
        },
    )
    base.update(overrides)
    return Variant(**base)


# ---------------------------------------------------------------------------
# can_publish — tuple[bool, str] contract
# ---------------------------------------------------------------------------


def test_can_publish_accepts_hashnode_variant():
    adapter = HashnodeAdapter()
    ok, reason = adapter.can_publish(_variant())
    assert ok is True
    assert reason == ""


def test_can_publish_rejects_wrong_channel():
    adapter = HashnodeAdapter()
    ok, reason = adapter.can_publish(_variant(channel="devto:main"))
    assert ok is False
    assert "hashnode" in reason.lower() or "channel" in reason.lower()


def test_can_publish_rejects_missing_publication_id():
    adapter = HashnodeAdapter()
    ok, reason = adapter.can_publish(
        _variant(extras={"content_id": "x"})
    )
    assert ok is False
    assert "publication" in reason.lower()


def test_can_publish_rejects_empty_title():
    adapter = HashnodeAdapter()
    ok, reason = adapter.can_publish(_variant(title=""))
    assert ok is False


def test_can_publish_rejects_empty_body():
    adapter = HashnodeAdapter()
    ok, reason = adapter.can_publish(_variant(body=""))
    assert ok is False


# ---------------------------------------------------------------------------
# publish — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_returns_live_on_success(yaml_backend):
    respx.post(_GQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "publishPost": {
                        "post": {
                            "id": "post_xyz",
                            "url": "https://blog.example.com/hello-hashnode",
                            "slug": "hello-hashnode",
                        }
                    }
                }
            },
        )
    )

    adapter = HashnodeAdapter()
    profile = {"hashnode_token": "test-token"}
    result = await adapter.publish(_variant(), profile, yaml_backend)

    assert result.state == "live"
    assert str(result.live_url) == "https://blog.example.com/hello-hashnode"
    assert result.channel == "hashnode:main"

    logged = yaml_backend.lookup_published("hello@2026-05-19", "hashnode:main")
    assert logged is not None
    assert logged["state"] == "live"
    assert logged["published_url"] == "https://blog.example.com/hello-hashnode"


# ---------------------------------------------------------------------------
# publish — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_is_idempotent(yaml_backend):
    route = respx.post(_GQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "publishPost": {
                        "post": {
                            "id": "post_xyz",
                            "url": "https://blog.example.com/once-only",
                            "slug": "once-only",
                        }
                    }
                }
            },
        )
    )

    adapter = HashnodeAdapter()
    profile = {"hashnode_token": "test-token"}
    v = _variant(extras={"content_id": "once@2026-05-19", "publicationId": "pub_abc123"})

    r1 = await adapter.publish(v, profile, yaml_backend)
    r2 = await adapter.publish(v, profile, yaml_backend)

    assert r1.state == "live"
    assert r2.state == "live"
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# publish — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_returns_failed_on_graphql_errors(yaml_backend):
    respx.post(_GQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={"errors": [{"message": "Invalid publication"}]},
        )
    )

    adapter = HashnodeAdapter()
    profile = {"hashnode_token": "test-token"}
    result = await adapter.publish(
        _variant(extras={"content_id": "fail@2026-05-19", "publicationId": "bad"}),
        profile,
        yaml_backend,
    )

    assert result.state == "failed"
    assert "Invalid publication" in (result.error or "")
    # Stub must be resolved to failed so a retry can re-claim the slot.
    logged = yaml_backend.list_post_log(
        content_id="fail@2026-05-19", channel="hashnode:main"
    )
    assert any(r["state"] == "failed" for r in logged)


@pytest.mark.asyncio
@respx.mock
async def test_publish_returns_failed_on_4xx(yaml_backend):
    respx.post(_GQL_URL).mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )

    adapter = HashnodeAdapter()
    profile = {"hashnode_token": "bad-token"}
    result = await adapter.publish(
        _variant(extras={"content_id": "auth-fail@2026-05-19", "publicationId": "p"}),
        profile,
        yaml_backend,
    )

    assert result.state == "failed"
    assert "401" in (result.error or "")


# ---------------------------------------------------------------------------
# hints — ChannelHints contract
# ---------------------------------------------------------------------------


def test_hints_returns_channelhints():
    adapter = HashnodeAdapter()
    hints = adapter.hints()
    assert hints.canonical_url_supported is True
    assert hints.browser_only is False
    assert hints.cta_placement == "bottom"
    # Hashnode uses free-form tags
    assert hints.tag_vocab is None
