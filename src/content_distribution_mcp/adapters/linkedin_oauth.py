"""
LinkedIn OAuth 2.0 helper for the Content Distribution MCP.

Handles the three-legged OAuth install flow and access-token refresh.
Tokens are stored in the operator's distribution profile (profiles.yaml)
as flat keys — see PROFILE_KEYS below for the full set.

Security note: tokens are stored as plain text in profiles.yaml (same
convention as DEV_TO_API_KEY and BLUESKY_APP_PASSWORD elsewhere in this
package). Encrypt the base_dir (~/.distribution-mcp/) at the filesystem
level for defence-in-depth.
# TODO: add optional fernet encryption for LINKEDIN_ACCESS_TOKEN +
# LINKEDIN_REFRESH_TOKEN when cryptography>=42 is available.
"""

from __future__ import annotations

import http.server
import secrets
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# LinkedIn OAuth endpoints
# ---------------------------------------------------------------------------

_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"

# OAuth scopes needed for personal-feed posting.
# w_organization_social must be added separately for org-page posts.
_DEFAULT_SCOPES = ["openid", "profile", "w_member_social"]

# ---------------------------------------------------------------------------
# Profile key constants
# ---------------------------------------------------------------------------

LINKEDIN_ACCESS_TOKEN = "LINKEDIN_ACCESS_TOKEN"
LINKEDIN_REFRESH_TOKEN = "LINKEDIN_REFRESH_TOKEN"
LINKEDIN_TOKEN_EXPIRY = "LINKEDIN_TOKEN_EXPIRY"   # ISO-8601 UTC
LINKEDIN_CLIENT_ID = "LINKEDIN_CLIENT_ID"
LINKEDIN_CLIENT_SECRET = "LINKEDIN_CLIENT_SECRET"
LINKEDIN_PERSON_ID = "LINKEDIN_PERSON_ID"          # urn:li:person:<id>

# Refresh proactively 5 minutes before actual expiry.
_EXPIRY_BUFFER_SEC = 300


class LinkedInOAuthError(Exception):
    """Raised when the OAuth flow or token refresh fails."""


# ---------------------------------------------------------------------------
# Token-state helpers
# ---------------------------------------------------------------------------


def is_token_expired(profile: dict[str, Any]) -> bool:
    """Return True if the access token is missing or within the expiry buffer.

    A missing expiry timestamp is treated as *not* expired (the operator
    captured the token manually and didn't record an expiry); the adapter
    will only attempt a refresh when the API returns a 401.
    """
    token = profile.get(LINKEDIN_ACCESS_TOKEN)
    if not token:
        return True

    expiry_raw = profile.get(LINKEDIN_TOKEN_EXPIRY)
    if not expiry_raw:
        return False  # no expiry on record — assume still valid

    try:
        expiry = datetime.fromisoformat(str(expiry_raw))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expiry - timedelta(seconds=_EXPIRY_BUFFER_SEC)
    except (ValueError, TypeError):
        return False


async def refresh_access_token(profile: dict[str, Any]) -> dict[str, Any]:
    """Exchange the refresh token for a fresh access token.

    Returns a copy of *profile* with LINKEDIN_ACCESS_TOKEN,
    LINKEDIN_REFRESH_TOKEN (if rotated), and LINKEDIN_TOKEN_EXPIRY updated.

    Raises LinkedInOAuthError on any failure.
    """
    refresh_token = profile.get(LINKEDIN_REFRESH_TOKEN)
    client_id = profile.get(LINKEDIN_CLIENT_ID)
    client_secret = profile.get(LINKEDIN_CLIENT_SECRET)

    if not all([refresh_token, client_id, client_secret]):
        raise LinkedInOAuthError(
            "Cannot refresh: LINKEDIN_REFRESH_TOKEN, LINKEDIN_CLIENT_ID, or "
            "LINKEDIN_CLIENT_SECRET missing from profile. "
            "Run `content-distribution-mcp linkedin install` to re-authorise."
        )

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(_TOKEN_URL, data=data)
    except httpx.RequestError as exc:
        raise LinkedInOAuthError(f"Token refresh HTTP error: {exc}") from exc

    if resp.status_code != 200:
        raise LinkedInOAuthError(
            f"Token refresh failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )

    body = resp.json()
    access_token = body.get("access_token")
    if not access_token:
        raise LinkedInOAuthError(f"Token refresh returned no access_token: {body}")

    # LinkedIn default ~60 days (5183944 seconds).
    expires_in = body.get("expires_in", 5183944)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    updated = dict(profile)
    updated[LINKEDIN_ACCESS_TOKEN] = access_token
    updated[LINKEDIN_TOKEN_EXPIRY] = expiry.isoformat()

    # LinkedIn may rotate the refresh token on each exchange.
    new_refresh = body.get("refresh_token")
    if new_refresh:
        updated[LINKEDIN_REFRESH_TOKEN] = new_refresh

    return updated


# ---------------------------------------------------------------------------
# Install flow (operator-interactive; synchronous — no event loop running)
# ---------------------------------------------------------------------------


def run_install_flow(
    client_id: str,
    client_secret: str,
    redirect_port: int = 0,
    scopes: list[str] | None = None,
) -> dict[str, str]:
    """Run the interactive OAuth install flow.

    Opens the user's browser to LinkedIn's authorisation page, starts a
    temporary local HTTP server to capture the redirect, and exchanges
    the authorisation code for tokens.

    Returns a dict ready to merge into a distribution profile:
        LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET,
        LINKEDIN_ACCESS_TOKEN, LINKEDIN_REFRESH_TOKEN,
        LINKEDIN_TOKEN_EXPIRY, LINKEDIN_PERSON_ID.

    Raises LinkedInOAuthError on any failure.

    Parameters
    ----------
    client_id:
        LinkedIn developer app client ID.
    client_secret:
        LinkedIn developer app client secret.
    redirect_port:
        Local port for the redirect listener (0 = OS-assigned random).
    scopes:
        OAuth scopes to request. Defaults to ``_DEFAULT_SCOPES``.
        Add ``"w_organization_social"`` for org-page posting.
    """
    if scopes is None:
        scopes = _DEFAULT_SCOPES

    code_holder: dict[str, str] = {}
    state_value = secrets.token_urlsafe(16)
    server_done = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(parsed.query))
            if "code" in params and params.get("state") == state_value:
                code_holder["code"] = params["code"]
            elif "error" in params:
                code_holder["error"] = params.get("error_description", params["error"])
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>LinkedIn authorisation complete. "
                b"You can close this tab.</h2></body></html>"
            )
            server_done.set()

        def log_message(self, *args: Any) -> None:  # noqa: ANN002
            pass  # silence default request logging

    with http.server.HTTPServer(("127.0.0.1", redirect_port), _Handler) as httpd:
        actual_port = httpd.server_address[1]
        redirect_uri = f"http://127.0.0.1:{actual_port}/callback"

        auth_params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state_value,
        })
        auth_url = f"{_AUTH_URL}?{auth_params}"

        t = threading.Thread(target=httpd.handle_request, daemon=True)
        t.start()

        webbrowser.open(auth_url)
        print(f"\nOpening browser for LinkedIn authorisation...\n{auth_url}\n")
        print("Waiting for redirect (timeout: 120 s)...")

        server_done.wait(timeout=120)
        t.join(timeout=5)

    if "error" in code_holder:
        raise LinkedInOAuthError(
            f"LinkedIn denied authorisation: {code_holder['error']}"
        )
    if "code" not in code_holder:
        raise LinkedInOAuthError(
            "No authorisation code received — timed out or browser window closed."
        )

    # Exchange the authorisation code for tokens.
    token_resp = httpx.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code_holder["code"],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if token_resp.status_code != 200:
        raise LinkedInOAuthError(
            f"Token exchange failed: HTTP {token_resp.status_code} — {token_resp.text[:200]}"
        )

    token_body = token_resp.json()
    access_token = token_body.get("access_token")
    if not access_token:
        raise LinkedInOAuthError(
            f"Token exchange returned no access_token: {token_body}"
        )

    expires_in = token_body.get("expires_in", 5183944)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    # Resolve the member's person URN via the OpenID Connect userinfo endpoint.
    userinfo_resp = httpx.get(
        _USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if userinfo_resp.status_code != 200:
        raise LinkedInOAuthError(
            f"Person ID lookup failed: HTTP {userinfo_resp.status_code} — "
            f"{userinfo_resp.text[:200]}"
        )

    userinfo = userinfo_resp.json()
    sub = userinfo.get("sub")  # OIDC subject == LinkedIn member ID
    if not sub:
        raise LinkedInOAuthError(
            f"Person ID (sub) not found in userinfo response: {userinfo}"
        )

    person_urn = f"urn:li:person:{sub}"

    return {
        LINKEDIN_CLIENT_ID: client_id,
        LINKEDIN_CLIENT_SECRET: client_secret,
        LINKEDIN_ACCESS_TOKEN: access_token,
        LINKEDIN_REFRESH_TOKEN: token_body.get("refresh_token", ""),
        LINKEDIN_TOKEN_EXPIRY: expiry.isoformat(),
        LINKEDIN_PERSON_ID: person_urn,
    }
