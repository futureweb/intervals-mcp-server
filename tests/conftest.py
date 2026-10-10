"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _server_clock(monkeypatch):
    """Tools use the server clock for "today" in tests (no athlete time zone lookup).

    Tests of the time zone lookup unset ATHLETE_TIMEZONE themselves.
    """
    monkeypatch.setenv("ATHLETE_TIMEZONE", "server")
