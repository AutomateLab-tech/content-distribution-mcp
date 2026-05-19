"""End-to-end tests for the MCP server's tool callables.

We don't spin up an actual MCP server here — we exercise the same Python
callables registered with ``@mcp.tool()``. This pins down the status tool's
wiring to YamlBackend's list_post_log and protects against future drift.
"""

from __future__ import annotations

import pytest


def test_server_imports_clean():
    """Importing the server must not error, and it must register tools."""
    from content_distribution_mcp.server import mcp, adapter_map, state_backend

    assert mcp is not None
    assert len(adapter_map) >= 1  # at least DEV.to
    # state_backend may be None if env vars are missing; that's an init-deferred
    # path, not a failure.


def test_status_tool_reads_post_log(monkeypatch, tmp_path):
    """status() must return dicts shaped like the docstring promises."""
    from pathlib import Path

    # Repoint the backend to a clean tmp dir so we don't pick up the user's
    # ~/.distribution-mcp data.
    monkeypatch.setenv("DISTRIBUTION_BACKEND", "yaml")
    monkeypatch.setenv("DISTRIBUTION_BACKEND_DIR", str(tmp_path))

    # Re-build the server module so it picks up the patched env. We import
    # server *after* monkeypatch.setenv so _build_backend uses the new dir.
    import importlib
    import content_distribution_mcp.server as srv
    importlib.reload(srv)

    # Seed the post log via the freshly-pointed backend.
    srv.state_backend.claim_idempotency_key("post1", "devto:main")
    srv.state_backend.mark_published(
        "post1", "devto:main", state="live", published_url="https://dev.to/p1"
    )

    rows = srv.status(content_id="post1")  # @mcp.tool wraps the fn

    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == "devto:main"
    assert row["state"] == "live"
    assert row["live_url"] == "https://dev.to/p1"
    assert row["content_id"] == "post1"
    assert row["error"] is None


def test_status_tool_filter_by_channel(monkeypatch, tmp_path):
    monkeypatch.setenv("DISTRIBUTION_BACKEND", "yaml")
    monkeypatch.setenv("DISTRIBUTION_BACKEND_DIR", str(tmp_path))

    import importlib
    import content_distribution_mcp.server as srv
    importlib.reload(srv)

    srv.state_backend.claim_idempotency_key("a", "devto:main")
    srv.state_backend.mark_published("a", "devto:main", state="live", published_url="u1")
    srv.state_backend.claim_idempotency_key("b", "reddit:foo")
    srv.state_backend.mark_published("b", "reddit:foo", state="failed", error="x")

    devto_only = srv.status(channel="devto:main")
    assert len(devto_only) == 1
    assert devto_only[0]["content_id"] == "a"
