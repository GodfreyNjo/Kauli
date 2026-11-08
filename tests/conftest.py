"""Shared test setup.

Why this file exists: the application loads the developer's real `.env` (API keys, public URL, feature switches) when it is imported. A test that
passed on one machine could therefore fail on another, or fail only when it ran after a test that had imported the app first. Five tests did
exactly that: they read an integration key, the public base URL or the Transkriptor key from the real `.env`.

The rule is that no test may depend on a developer's real configuration. Every test starts with the names listed in `.env` and `.env.example`
removed from the environment, and a test that needs a setting says so itself (`monkeypatch.setenv`).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENV_FILES = (ROOT / ".env", ROOT / ".env.example")
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def configured_names() -> set[str]:
    """Every setting name that appears in `.env` or `.env.example`."""
    names: set[str] = set()
    for env_file in ENV_FILES:
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = ASSIGNMENT.match(line)
            if match:
                names.add(match.group(1))
    return names


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch):
    """Start every test without the developer's real settings."""
    for name in configured_names():
        monkeypatch.delenv(name, raising=False)
