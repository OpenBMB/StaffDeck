"""Repository-level pytest configuration for both suites (``tests`` and ``tests_harness``).

Keep every test hermetic with respect to the developer's local ``backend/.env``:
``Settings`` reads ``ULTRARAG_DOTENV`` at class-definition time, so a checked-out
``.env`` with ``HARNESS_V3_ENABLED=true`` (a live dev stack) would otherwise leak
into the legacy suite and boot the Harness v3 runtime / MCP server inside tests.

Opt out only for the live end-to-end tests, which read their configuration from the
environment on purpose (``HARNESS_V3_E2E=1``).
"""

from __future__ import annotations

import os


def pytest_configure(config):  # noqa: D401 - pytest hook
    if os.environ.get("HARNESS_V3_E2E"):
        return
    # A non-existent dotenv path makes pydantic-settings fall back to defaults + real env vars.
    os.environ.setdefault("ULTRARAG_DOTENV", "/nonexistent-staffdeck-test.env")
    os.environ.setdefault("HARNESS_V3_ENABLED", "false")
