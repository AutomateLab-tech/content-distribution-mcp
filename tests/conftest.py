"""Shared pytest fixtures for the Content Distribution MCP test suite."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from content_distribution_mcp.backends.yaml_backend import YamlBackend


@pytest.fixture
def yaml_backend(tmp_path: Path) -> YamlBackend:
    """Fresh YamlBackend rooted in a pytest tmp_path."""
    return YamlBackend(base_dir=tmp_path)


@pytest.fixture
def devto_api_key() -> str:
    """Load DEVTO_API_KEY from project .env or environment.

    Falls back to a dummy value so tests with mocked HTTP still execute.
    """
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("DEVTO_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("DEVTO_API_KEY", "test-key")
