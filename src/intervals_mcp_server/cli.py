"""Command line entry point ``futureweb-intervals-mcp`` (also ``python src/intervals_mcp_server/server.py``).

``--version`` and ``--help`` are answered before any server module is imported, so they
work with a broken configuration. ``--doctor`` checks the configuration piece by piece and
lists every problem; when it is sound it also asks Intervals.icu whether the API key works.
Without flags the server starts; a configuration error then ends the process with a
one-line message instead of a traceback (systemd restarts would otherwise fill the journal
with stack traces that hide the cause).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping

from intervals_mcp_server import __version__

__all__ = ["configuration_problems", "main"]

PROG = "futureweb-intervals-mcp"


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__


def configuration_problems(environ: Mapping[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Check the configuration without starting anything; return (errors, warnings)."""
    # pylint: disable=import-outside-toplevel
    from intervals_mcp_server.config import load_config
    from intervals_mcp_server.server_setup import NETWORK_TRANSPORTS, fastmcp_settings_from_env, setup_transport

    env = os.environ if environ is None else environ
    errors: list[str] = []
    warnings: list[str] = []
    config = None
    try:
        config = load_config()
    except ValueError as exc:
        errors.append(_one_line(exc))
    transport = None
    try:
        transport = setup_transport()
    except ValueError as exc:
        errors.append(f"MCP_TRANSPORT: {_one_line(exc)}")
    # One variable at a time, so that every wrong FASTMCP_* value is reported.
    for name in sorted(k for k in env if k.startswith("FASTMCP_")):
        try:
            fastmcp_settings_from_env({name: env[name]})
        except ValueError as exc:
            errors.append(_one_line(exc))
    if config is not None and transport in NETWORK_TRANSPORTS:
        for name, value in (("API_KEY", config.api_key), ("ATHLETE_ID", config.athlete_id)):
            if not value:
                warnings.append(f"{name} is not set: tool calls over {transport.value} will fail without it")
    errors.extend(_oauth_problems(env))
    setting_errors, setting_warnings = _settings_problems(env)
    errors.extend(setting_errors)
    warnings.extend(setting_warnings)
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def _settings_problems(env: Mapping[str, str]) -> tuple[list[str], list[str]]:  # pylint: disable=too-many-locals
    """Limits, time zone, tool set and OAuth fine-tuning, each checked on its own.

    The server falls back to the default for a wrong limit or time zone instead of failing; the
    doctor reports it, because the operator meant something else.
    """
    # pylint: disable=import-outside-toplevel
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from intervals_mcp_server.api.client import DEFAULT_TOOL_MAX_REQUESTS, DEFAULT_TOOL_TIMEOUT_S, limit_setting
    from intervals_mcp_server.auth import _env_bool, _env_int  # pylint: disable=protected-access
    from intervals_mcp_server.tool_guard import output_limit_setting
    from intervals_mcp_server.toolsets import parse_toolset
    from intervals_mcp_server.utils.dates import SERVER_CLOCK

    errors: list[str] = []
    warnings: list[str] = []
    # The same parsers as the server: the message names the value the server really uses.
    for setting in (
        limit_setting("MCP_TOOL_MAX_REQUESTS", DEFAULT_TOOL_MAX_REQUESTS, integer=True, environ=env),
        limit_setting("MCP_TOOL_TIMEOUT_S", DEFAULT_TOOL_TIMEOUT_S, integer=False, environ=env),
    ):
        errors.extend([setting.error] if setting.error else [])
        warnings.extend([setting.warning] if setting.warning else [])
    _, chars_error, chars_warning = output_limit_setting(env)
    errors.extend([chars_error] if chars_error else [])
    warnings.extend([chars_warning] if chars_warning else [])
    zone = env.get("ATHLETE_TIMEZONE", "").strip()
    if zone and zone.lower() not in SERVER_CLOCK:
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(
                f"ATHLETE_TIMEZONE must be an IANA time zone such as Europe/Vienna or 'server', got {zone!r}; "
                "the server uses the server clock"
            )
    try:
        parse_toolset(env.get("MCP_TOOLSET", ""))
    except ValueError as exc:
        errors.append(_one_line(exc))
    oauth = (env.get("MCP_AUTH", "none").strip().lower() or "none") == "oauth"
    checks = (
        lambda: _env_int(env, "OAUTH_REFRESH_REUSE_GRACE", 0, minimum=0),
        lambda: _env_bool(env, "OAUTH_REFRESH_REUSE_REVOKE", True),
        lambda: _env_int(env, "OAUTH_LOGIN_GLOBAL_RATE_LIMIT", 1),
        lambda: _env_bool(env, "OAUTH_REQUIRE_PRIVATE_KEY_JWT", True),
    )
    for check in checks:
        try:
            check()
        except ValueError as exc:
            if oauth:
                errors.append(_one_line(exc))
            else:
                warnings.append(f"{_one_line(exc)} (ignored without MCP_AUTH=oauth)")
    return errors, warnings


def _oauth_problems(env: Mapping[str, str]) -> list[str]:
    # pylint: disable=import-outside-toplevel
    from intervals_mcp_server.auth import SingleUserOAuthProvider, oauth_config_from_env
    from intervals_mcp_server.config import parse_permissions

    mode = env.get("MCP_AUTH", "none").strip().lower() or "none"
    if mode == "none":
        return []
    if mode != "oauth":
        return [f"MCP_AUTH must be 'none' or 'oauth', got {mode!r}"]
    checked = dict(env)
    try:
        parse_permissions(checked.get("MCP_PERMISSIONS", "read"))
    except ValueError:
        checked["MCP_PERMISSIONS"] = "read"  # reported already; check the rest of the OAuth setup
    try:
        # Reads the state file (never writes it) and checks that its directory is writable.
        SingleUserOAuthProvider(oauth_config_from_env(checked))
    except ValueError as exc:
        return [_one_line(exc)]
    return []


def doctor() -> int:
    """Print configuration problems, or the full status report when there are none."""
    errors, warnings = configuration_problems()
    for warning in warnings:
        print(f"warning: {warning}")
    if errors:
        print("Configuration problems:")
        for error in errors:
            print(f"  - {error}")
        return 1
    from intervals_mcp_server.tools.status import format_status, server_status  # pylint: disable=import-outside-toplevel

    print(format_status(asyncio.run(server_status(include_private=True))))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Handle the command line; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Futureweb Intervals MCP: an MCP server for Intervals.icu. Configured through environment "
        "variables (see .env.example and docs/REMOTE_ACCESS.md).",
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("--doctor", action="store_true", help="check the configuration and the Intervals.icu API key")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.version:
        print(__version__)
        return 0
    # Plain log lines from the start, so that the messages logged while the server modules
    # are imported (OAuth state loaded, ...) are not lost and the SDK installs no rich handler.
    from intervals_mcp_server.server_setup import LOG_LEVELS, configure_logging  # pylint: disable=import-outside-toplevel

    level = os.environ.get("FASTMCP_LOG_LEVEL", "").strip().upper()
    configure_logging(level if level in LOG_LEVELS else "INFO")
    try:
        if args.doctor:
            return doctor()
        from intervals_mcp_server import server  # pylint: disable=import-outside-toplevel

        server.run()
    except (ValueError, OSError) as exc:
        print(f"{PROG}: configuration error ({type(exc).__name__}): {_one_line(exc)}", file=sys.stderr)
        print(f"{PROG}: run '{PROG} --doctor' to check the whole configuration", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
