"""
Tests for the LinkedIn adapter (linkedin.py) and OAuth helpers (linkedin_oauth.py).

Only non-OAuth paths are tested here (no browser, no interactive install flow).
HTTP calls are mocked via respx.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from content_distribution_mcp.adapters.linkedin import (
    LinkedInAdapter,
    _build_post_payload,
    _build_post_text,
    _post_urn_from_url,
    _resolve_author_urn,
    _target_slug,
)
from content_distribution_mcp.adapters.linkedin_oauth import (
    LINKEDIN_ACCESS_TOKEN,
    LINKEDIN_CLIENT_ID,
    LINKEDIN_CLIENT_SECRET,
    LINKEDIN_PERSON_ID,
    LINKEDIN_REFRESH_TOKEN,
    LINKEDIN_TOKEN_EXPIRY,
    is_token_expired,
    refresh_access_token,
)
from content_distribution_mcp.models import Variant

_POSTS_URL = "https://api.linkedin.com/rest/posts"
_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _profile(
    *,
    access_token: str = "test-access-token",
    refresh_token: str = "test-refresh-token",
    person_id: str = "urn:li:person:ABC123",
    expiry_offset_days: int = 30,
    include_expiry: bool = True,
) -> dict:
    p = {
        LINKEDIN_ACCESS_TOKEN: access_token,
        LINKEDIN_REFRESH_TOKEN: refresh_token,
        LINKEDIN_PERSON_ID: person_id,
        LINKEDIN_CLIENT_ID: "cli-id",
        LINKEDIN_CLIENT_SECRET: "cli-secret",
    }
    if include_expiry:
        expiry = datetime.now(timezone.utc) + timedelta(days=expiry_offset_days)
        p[LINKEDIN_TOKEN_EXPIRY] = expiry.isoformat()
    return p


def _variant(**kwargs) -> Variant:
    defaults = dict(
        channel="linkedin:personal",
        title="Test post",
        body="Hello LinkedIn",
        extras={"content_id": "test@2026-05-20"},
    )
    defaults.update(kwargs)
    return Variant(**defaults)


# ---------------------------------------------------------------------------
# can_publish
# ---------------------------------------------------------------------------


def test_can_publish_accepts_valid_variant():
    adapter = LinkedInAdapter()
    ok, reason = adapter.can_publish(_variant())
    assert ok is True
    assert reason == ""


def test_can_publish_rejects_wrong_channel():
    adapter = LinkedInAdapter()
    ok, reason = adapter.can_publish(_variant(channel="devto:main"))
    assert ok is False
    assert "linkedin" in reason


def test_can_publish_rejects_empty_body():
    adapter = LinkedInAdapter()
    ok, reason = adapter.can_publish(_variant(body=""))
    assert ok is False
    assert "empty-body" in reason


def test_can_publish_rejects_whitespace_body():
    adapter = LinkedInAdapter()
    ok, reason = adapter.can_publish(_variant(body="   "))
    assert ok is False
    assert "empty-body" in reason


def test_can_publish_rejects_missing_content_id():
    adapter = LinkedInAdapter()
    ok, reason = adapter.can_publish(_variant(extras={}))
    assert ok is False
    assert "content-id" in reason


def test_can_publish_rejects_no_extras():
    adapter = LinkedInAdapter()
    ok, reason = adapter.can_publish(_variant(extras={}))
    assert ok is False


# ---------------------------------------------------------------------------
# hints
# ---------------------------------------------------------------------------


def test_hints_returns_channelhints():
    adapter = LinkedInAdapter()
    h = adapter.hints()
    assert h.browser_only is False
    assert h.canonical_url_supported is False
    assert h.cta_placement == "bottom"
    assert h.max_length == 3000
    assert "links" in h.supported_md_features


# ---------------------------------------------------------------------------
# publish — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_happy_path(yaml_backend):
    post_urn = "urn:li:share:7123456789012345678"
    respx.post(_POSTS_URL).mock(
        return_value=httpx.Response(201, headers={"x-restli-id": post_urn}, content=b"")
    )

    adapter = LinkedInAdapter()
    variant = _variant()
    profile = _profile()

    result = await adapter.publish(variant, profile, yaml_backend)

    assert result.state == "live"
    assert post_urn in str(result.live_url)
    assert result.channel == "linkedin:personal"

    # Post log should have a live entry.
    log = yaml_backend.lookup_published("test@2026-05-20", "linkedin:personal")
    assert log is not None
    assert log["state"] == "live"


@pytest.mark.asyncio
@respx.mock
async def test_publish_org_channel(yaml_backend):
    """Numeric org-ID target should map to urn:li:organization:..."""
    post_urn = "urn:li:share:9999"
    route = respx.post(_POSTS_URL).mock(
        return_value=httpx.Response(201, headers={"x-restli-id": post_urn}, content=b"")
    )

    adapter = LinkedInAdapter()
    variant = _variant(channel="linkedin:116012269")
    profile = _profile()  # person_id not used for org posting

    result = await adapter.publish(variant, profile, yaml_backend)

    assert result.state == "live"
    # Verify the payload sent the org URN.
    request_body = route.calls[0].request
    import json
    payload = json.loads(request_body.content)
    assert payload["author"] == "urn:li:organization:116012269"


# ---------------------------------------------------------------------------
# publish — missing / bad profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_missing_profile(yaml_backend):
    adapter = LinkedInAdapter()
    result = await adapter.publish(_variant(), None, yaml_backend)
    assert result.state == "failed"
    assert "missing-profile" in result.error


@pytest.mark.asyncio
@respx.mock
async def test_publish_missing_access_token(yaml_backend):
    """Profile present but no LINKEDIN_ACCESS_TOKEN → failed (via refresh error)."""
    adapter = LinkedInAdapter()
    # No token and no refresh credentials — _ensure_valid_token raises LinkedInOAuthError.
    profile = {LINKEDIN_PERSON_ID: "urn:li:person:ABC123"}  # no token, no credentials
    result = await adapter.publish(_variant(), profile, yaml_backend)
    assert result.state == "failed"
    assert result.error is not None


@pytest.mark.asyncio
@respx.mock
async def test_publish_missing_person_id_on_personal_channel(yaml_backend):
    """Personal channel with no LINKEDIN_PERSON_ID in profile → failed."""
    adapter = LinkedInAdapter()
    profile = {LINKEDIN_ACCESS_TOKEN: "tok"}  # no person_id
    result = await adapter.publish(_variant(channel="linkedin:personal"), profile, yaml_backend)
    assert result.state == "failed"
    assert "URN" in result.error or "person" in result.error.lower()


# ---------------------------------------------------------------------------
# publish — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_is_idempotent(yaml_backend):
    route = respx.post(_POSTS_URL).mock(
        return_value=httpx.Response(
            201,
            headers={"x-restli-id": "urn:li:share:1"},
            content=b"",
        )
    )
    adapter = LinkedInAdapter()
    variant = _variant()
    profile = _profile()

    r1 = await adapter.publish(variant, profile, yaml_backend)
    r2 = await adapter.publish(variant, profile, yaml_backend)

    assert r1.state == "live"
    assert r2.state == "live"
    assert route.call_count == 1  # API hit exactly once


# ---------------------------------------------------------------------------
# publish — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_fails_on_4xx(yaml_backend):
    respx.post(_POSTS_URL).mock(return_value=httpx.Response(401, text="Unauthorized"))

    adapter = LinkedInAdapter()
    result = await adapter.publish(_variant(), _profile(), yaml_backend)
    assert result.state == "failed"
    assert "401" in result.error


@pytest.mark.asyncio
@respx.mock
async def test_publish_retries_once_on_429(yaml_backend, monkeypatch):
    """429 on first attempt → sleep → 201 on second attempt."""
    import asyncio as _asyncio

    async def _instant_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(_asyncio, "sleep", _instant_sleep)

    route = respx.post(_POSTS_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0"}, content=b""),
            httpx.Response(201, headers={"x-restli-id": "urn:li:share:2"}, content=b""),
        ]
    )

    adapter = LinkedInAdapter()
    result = await adapter.publish(_variant(), _profile(), yaml_backend)

    assert result.state == "live"
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# publish — token refresh on expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_publish_refreshes_expired_token(yaml_backend):
    """Expired access token triggers a refresh before posting."""
    new_token = "refreshed-access-token"

    # Mock the token refresh endpoint.
    respx.post(_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": new_token,
                "expires_in": 5183944,
                "refresh_token": "new-refresh-token",
            },
        )
    )
    # Mock the Posts API — must accept the *new* token.
    route = respx.post(_POSTS_URL).mock(
        return_value=httpx.Response(
            201, headers={"x-restli-id": "urn:li:share:3"}, content=b""
        )
    )

    adapter = LinkedInAdapter()
    expired_profile = _profile(
        access_token="old-expired-token",
        expiry_offset_days=-1,  # expired yesterday
    )

    result = await adapter.publish(_variant(), expired_profile, yaml_backend)

    assert result.state == "live"
    # The Posts API should have been called with the new token.
    assert route.call_count == 1
    sent_auth = route.calls[0].request.headers.get("Authorization")
    assert f"Bearer {new_token}" == sent_auth


# ---------------------------------------------------------------------------
# unpublish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_unpublish_success():
    post_urn = "urn:li:share:7123456789012345678"
    live_url = f"https://www.linkedin.com/feed/update/{post_urn}/"
    encoded = "urn%3Ali%3Ashare%3A7123456789012345678"

    respx.delete(f"https://api.linkedin.com/rest/posts/{encoded}").mock(
        return_value=httpx.Response(204, content=b"")
    )

    adapter = LinkedInAdapter()
    ok, reason = await adapter.unpublish(live_url, _profile())

    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
async def test_unpublish_unrecognised_url():
    adapter = LinkedInAdapter()
    ok, reason = await adapter.unpublish("https://www.linkedin.com/posts/foo", _profile())
    assert ok is False
    assert "urn" in reason.lower() or "parse" in reason.lower()


@pytest.mark.asyncio
async def test_unpublish_missing_access_token():
    adapter = LinkedInAdapter()
    ok, reason = await adapter.unpublish(
        "https://www.linkedin.com/feed/update/urn:li:share:1/",
        {},  # empty profile
    )
    assert ok is False
    assert "LINKEDIN_ACCESS_TOKEN" in reason


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def test_target_slug_personal():
    assert _target_slug("linkedin:personal") == "personal"


def test_target_slug_org_id():
    assert _target_slug("linkedin:116012269") == "116012269"


def test_resolve_author_urn_personal():
    profile = {LINKEDIN_PERSON_ID: "urn:li:person:ABC123"}
    assert _resolve_author_urn("personal", profile) == "urn:li:person:ABC123"


def test_resolve_author_urn_personal_bare_id():
    """A bare member ID (not a full URN) should be wrapped automatically."""
    profile = {LINKEDIN_PERSON_ID: "ABC123"}
    assert _resolve_author_urn("personal", profile) == "urn:li:person:ABC123"


def test_resolve_author_urn_org():
    assert _resolve_author_urn("116012269", {}) == "urn:li:organization:116012269"


def test_resolve_author_urn_full_urn_passthrough():
    urn = "urn:li:organization:99999"
    assert _resolve_author_urn(urn, {}) == urn


def test_resolve_author_urn_missing_person_id():
    assert _resolve_author_urn("personal", {}) is None


def test_build_post_text_body_only():
    v = _variant(body="Hello world", cta_block=None, canonical_url=None)
    assert _build_post_text(v) == "Hello world"


def test_build_post_text_with_cta():
    v = _variant(body="Hello world", cta_block="Read more →")
    assert _build_post_text(v) == "Hello world\n\nRead more →"


def test_build_post_text_with_canonical_url():
    v = _variant(
        body="Hello world",
        cta_block=None,
        canonical_url="https://automatelab.tech/my-post/",
    )
    text = _build_post_text(v)
    assert "Read more: https://automatelab.tech/my-post/" in text


def test_build_post_payload_structure():
    payload = _build_post_payload("urn:li:person:X", "Hello")
    assert payload["author"] == "urn:li:person:X"
    assert payload["commentary"] == "Hello"
    assert payload["lifecycleState"] == "PUBLISHED"
    assert payload["visibility"] == "PUBLIC"


def test_post_urn_from_url_standard():
    url = "https://www.linkedin.com/feed/update/urn:li:share:7123456789012345678/"
    assert _post_urn_from_url(url) == "urn:li:share:7123456789012345678"


def test_post_urn_from_url_percent_encoded():
    url = "https://www.linkedin.com/feed/update/urn%3Ali%3Ashare%3A7123456789012345678/"
    assert _post_urn_from_url(url) == "urn:li:share:7123456789012345678"


def test_post_urn_from_url_no_match():
    assert _post_urn_from_url("https://www.linkedin.com/posts/someone_title-12345") is None


# ---------------------------------------------------------------------------
# is_token_expired
# ---------------------------------------------------------------------------


def test_is_token_expired_no_token():
    assert is_token_expired({}) is True


def test_is_token_expired_future_expiry():
    expiry = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    profile = {LINKEDIN_ACCESS_TOKEN: "tok", LINKEDIN_TOKEN_EXPIRY: expiry}
    assert is_token_expired(profile) is False


def test_is_token_expired_past_expiry():
    expiry = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    profile = {LINKEDIN_ACCESS_TOKEN: "tok", LINKEDIN_TOKEN_EXPIRY: expiry}
    assert is_token_expired(profile) is True


def test_is_token_expired_no_expiry_field():
    """No LINKEDIN_TOKEN_EXPIRY on record — treated as still valid."""
    profile = {LINKEDIN_ACCESS_TOKEN: "tok"}
    assert is_token_expired(profile) is False


# ---------------------------------------------------------------------------
# refresh_access_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_refresh_access_token_success():
    new_token = "brand-new-access-token"
    respx.post(_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": new_token,
                "expires_in": 5183944,
                "refresh_token": "new-refresh",
            },
        )
    )

    profile = {
        LINKEDIN_ACCESS_TOKEN: "old",
        LINKEDIN_REFRESH_TOKEN: "ref-tok",
        LINKEDIN_CLIENT_ID: "cid",
        LINKEDIN_CLIENT_SECRET: "csec",
    }
    updated = await refresh_access_token(profile)

    assert updated[LINKEDIN_ACCESS_TOKEN] == new_token
    assert updated[LINKEDIN_REFRESH_TOKEN] == "new-refresh"
    assert LINKEDIN_TOKEN_EXPIRY in updated


@pytest.mark.asyncio
@respx.mock
async def test_refresh_access_token_http_error():
    respx.post(_TOKEN_URL).mock(return_value=httpx.Response(401, text="Unauthorized"))

    profile = {
        LINKEDIN_ACCESS_TOKEN: "old",
        LINKEDIN_REFRESH_TOKEN: "ref-tok",
        LINKEDIN_CLIENT_ID: "cid",
        LINKEDIN_CLIENT_SECRET: "csec",
    }
    from content_distribution_mcp.adapters.linkedin_oauth import LinkedInOAuthError

    with pytest.raises(LinkedInOAuthError, match="401"):
        await refresh_access_token(profile)


@pytest.mark.asyncio
async def test_refresh_access_token_missing_credentials():
    from content_distribution_mcp.adapters.linkedin_oauth import LinkedInOAuthError

    with pytest.raises(LinkedInOAuthError, match="missing"):
        await refresh_access_token({LINKEDIN_ACCESS_TOKEN: "tok"})  # no client_id/secret
