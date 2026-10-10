# pylint: disable=missing-function-docstring
"""OPS-9 / OPS-11 / SEC-12: command line front end, settings validation and logging hygiene."""

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from intervals_mcp_server import __version__
from intervals_mcp_server.server_setup import QueryRedactingFilter, configure_logging, fastmcp_settings_from_env

ROOT = Path(__file__).resolve().parent.parent


def write_state(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path

# --------------------------------------------------------------------------- #
# OPS-9 / OPS-11 / SEC-12: CLI, settings validation, logging
# --------------------------------------------------------------------------- #


def run_cli(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("MCP_", "FASTMCP_", "OAUTH_", "API_KEY", "ATHLETE_ID"))}
    clean.update(env)
    return subprocess.run(
        [sys.executable, str(ROOT / "src" / "intervals_mcp_server" / "server.py"), *args],
        capture_output=True, text=True, env=clean, timeout=60, check=False, cwd=ROOT / "tests",
    )


def test_cli_version_works_with_a_broken_configuration():
    result = run_cli("--version", MCP_PERMISSIONS="reed", FASTMCP_PORT="80a", MCP_AUTH="oauth")
    assert result.returncode == 0 and result.stdout.strip() == __version__


def test_cli_doctor_lists_every_problem(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    result = run_cli(
        "--doctor", MCP_PERMISSIONS="reed", FASTMCP_PORT="99999", FASTMCP_LOG_LEVEL="verbose", FASTMCP_SSE_PATH="secret",
        MCP_TRANSPORT="htttp", MCP_AUTH="oauth", MCP_PUBLIC_URL="https://mcp.example.com", OAUTH_PASSWORD="x",
        OAUTH_STATE_FILE=str(blocker / "state.json"),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    for fragment in ("MCP_PERMISSIONS", "MCP_TRANSPORT", "FASTMCP_PORT", "FASTMCP_LOG_LEVEL", "FASTMCP_SSE_PATH", "not writable"):
        assert fragment in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


def test_cli_doctor_checks_limits_time_zone_tool_set_and_oauth_tuning():
    """Stage 3: every new setting is validated on its own; the server would fall back to a default."""
    result = run_cli(
        "--doctor", MCP_TOOL_MAX_REQUESTS="abc", MCP_TOOL_TIMEOUT_S="-1", MCP_MAX_OUTPUT_CHARS="500",
        ATHLETE_TIMEZONE="Mars/Olympus", MCP_TOOLSET="tiny", OAUTH_REFRESH_REUSE_GRACE="-5",
        OAUTH_REFRESH_REUSE_REVOKE="maybe", OAUTH_LOGIN_GLOBAL_RATE_LIMIT="0", OAUTH_REQUIRE_PRIVATE_KEY_JWT="sure",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    out = result.stdout
    for fragment in (
        "MCP_TOOL_MAX_REQUESTS must be a positive integer, got 'abc' (the server would use the default 300)",
        "MCP_TOOL_TIMEOUT_S must be a positive number, got '-1'",
        "ATHLETE_TIMEZONE must be an IANA time zone such as Europe/Vienna or 'server', got 'Mars/Olympus'",
        "MCP_TOOLSET must be one of full, core, got 'tiny'",
        "warning: MCP_MAX_OUTPUT_CHARS=500 is below the minimum; 2000 is used",
        "warning: OAUTH_REFRESH_REUSE_GRACE must be an integer >= 0, got '-5' (ignored without MCP_AUTH=oauth)",
        "warning: OAUTH_REFRESH_REUSE_REVOKE must be true or false",
        "warning: OAUTH_LOGIN_GLOBAL_RATE_LIMIT must be a positive integer, got '0'",
        "warning: OAUTH_REQUIRE_PRIVATE_KEY_JWT must be true or false",
    ):
        assert fragment in out, fragment
    assert out.count("MCP_TOOLSET must be one of") == 1
    assert "Traceback" not in out + result.stderr
    good = run_cli("--doctor", MCP_TOOL_MAX_REQUESTS="50", MCP_TOOL_TIMEOUT_S="30.5", MCP_MAX_OUTPUT_CHARS="20000",
                   ATHLETE_TIMEZONE="Europe/Vienna", MCP_TOOLSET="core")
    assert good.returncode == 0, good.stdout + good.stderr
    assert "tool set core (MCP_TOOLSET)" in good.stdout and "must be" not in good.stdout


def test_cli_start_reports_a_configuration_error_in_one_line(tmp_path):
    path = write_state(tmp_path, {"clients": []})
    result = run_cli(MCP_AUTH="oauth", MCP_PUBLIC_URL="https://mcp.example.com", OAUTH_PASSWORD="x", OAUTH_STATE_FILE=str(path))
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert result.stderr.splitlines()[0].startswith("futureweb-intervals-mcp: configuration error")
    assert "'clients' is not an object" in result.stderr
    assert json.loads(path.read_text()) == {"clients": []}


def test_cli_rejects_unknown_flags():
    result = run_cli("--bogus")
    assert result.returncode == 2 and "unrecognized arguments" in result.stderr
    assert run_cli("--help").returncode == 0


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FASTMCP_PORT": "99999"}, "between 1 and 65535"),
        ({"FASTMCP_PORT": "0"}, "between 1 and 65535"),
        ({"FASTMCP_LOG_LEVEL": "verbose"}, "FASTMCP_LOG_LEVEL"),
        ({"FASTMCP_SSE_PATH": "secret"}, "must start with '/'"),
        ({"FASTMCP_MESSAGE_PATH": "messages/"}, "must start with '/'"),
    ],
)
def test_fastmcp_settings_are_validated(env, message):
    with pytest.raises(ValueError, match=message):
        fastmcp_settings_from_env(env)


def test_access_log_keeps_query_values_out():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/oauth/intervals/callback?code=SECRETCODE&state=SECRETSTATE", "1.1", 302), None,
    )
    assert QueryRedactingFilter().filter(record)
    line = record.getMessage()
    assert "SECRETCODE" not in line and "SECRETSTATE" not in line
    assert "/oauth/intervals/callback?code=…&state=…" in line
    plain = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                              ("127.0.0.1:1", "POST", "/token", "1.1", 200), None)
    QueryRedactingFilter().filter(plain)
    assert "/token" in plain.getMessage()


def test_logging_is_plain_and_httpx_quiet():
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    httpx_logger = logging.getLogger("httpx")
    saved_httpx = httpx_logger.level
    try:
        configure_logging("INFO")
        assert [type(h) for h in root.handlers] == [logging.StreamHandler]
        assert httpx_logger.level == logging.WARNING
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        httpx_logger.setLevel(saved_httpx)


def test_request_bodies_are_not_logged(monkeypatch, caplog):
    from intervals_mcp_server.api import client as api_client  # pylint: disable=import-outside-toplevel

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text='{"error": "bad weight 81.4 for athlete"}', request=request)

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def get_client() -> httpx.AsyncClient:
        return transport_client

    monkeypatch.setattr(api_client, "_get_httpx_client", get_client)
    with caplog.at_level(logging.DEBUG, logger=api_client.logger.name):
        asyncio.run(api_client.make_intervals_request("/athlete/i1/wellness/2026-01-01", api_key="k", method="PUT",
                                                      data={"weight": 81.4, "comments": "private note"}))
    assert "private note" not in caplog.text
    assert "body: " in caplog.text and "bytes" in caplog.text
