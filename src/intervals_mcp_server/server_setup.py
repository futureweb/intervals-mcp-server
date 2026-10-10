"""
Server setup and initialization for Intervals.icu MCP Server.

This module handles transport configuration and server startup logic.
"""

import logging
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP  # pylint: disable=import-error
from mcp.server.transport_security import TransportSecuritySettings

from intervals_mcp_server.utils.types import TransportAliases

logger = logging.getLogger("intervals_icu_mcp_server")

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
NETWORK_TRANSPORTS = (TransportAliases.SSE, TransportAliases.STREAMABLE_HTTP, TransportAliases.HTTP_SSE)
_QUERY_VALUE = re.compile(r"=[^&]*")


# FastMCP passes explicit defaults (e.g. host="127.0.0.1", port=8000) to its
# settings model, which take precedence over FASTMCP_* environment variables.
# We therefore read the documented variables ourselves and pass them explicitly.
_ENV_STRING_SETTINGS = {
    "FASTMCP_HOST": "host",
    "FASTMCP_LOG_LEVEL": "log_level",
    "FASTMCP_MOUNT_PATH": "mount_path",
    "FASTMCP_SSE_PATH": "sse_path",
    "FASTMCP_MESSAGE_PATH": "message_path",
    "FASTMCP_STREAMABLE_HTTP_PATH": "streamable_http_path",
}


_LOCAL_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOCAL_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def _env_list(env: Mapping[str, str], name: str) -> list[str]:
    return [item.strip() for item in env.get(name, "").split(",") if item.strip()]


def transport_security_from_env(environ: Mapping[str, str] | None = None) -> TransportSecuritySettings | None:
    """Host/Origin allowlist for the HTTP transports (DNS rebinding protection).

    The MCP SDK enables DNS rebinding protection for servers bound to localhost and then
    accepts only localhost Host headers, which breaks a reverse proxy that preserves the
    public Host header. The public host is allowed automatically from ``MCP_PUBLIC_URL``;
    further hosts and origins come from ``FASTMCP_ALLOWED_HOSTS`` / ``FASTMCP_ALLOWED_ORIGINS``
    (comma-separated, ``host`` or ``host:*``; ``FASTMCP_ALLOWED_HOSTS=*`` turns the check off).
    Returns None (SDK default) when nothing is configured.
    """
    env = os.environ if environ is None else environ
    hosts = _env_list(env, "FASTMCP_ALLOWED_HOSTS")
    origins = _env_list(env, "FASTMCP_ALLOWED_ORIGINS")
    public = env.get("MCP_PUBLIC_URL", "").strip()
    if public:
        parts = urlsplit(public)
        if parts.netloc:
            hosts.append(parts.netloc)
            origins.append(f"{parts.scheme}://{parts.netloc}")
    if not hosts and not origins:
        return None
    if "*" in hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(_LOCAL_HOSTS + hosts)),
        allowed_origins=list(dict.fromkeys(_LOCAL_ORIGINS + origins)),
    )


def fastmcp_settings_from_env(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build FastMCP keyword arguments from FASTMCP_* environment variables.

    Only variables that are set (and non-empty) are returned, so FastMCP's own
    defaults still apply otherwise.

    Raises:
        ValueError: If FASTMCP_PORT is not a port number, FASTMCP_LOG_LEVEL is unknown or a
            path setting does not start with ``/``.
    """
    env = os.environ if environ is None else environ
    settings: dict[str, Any] = {}

    for var, key in _ENV_STRING_SETTINGS.items():
        value = env.get(var, "").strip()
        if value:
            settings[key] = value.upper() if key == "log_level" else value

    port = env.get("FASTMCP_PORT", "").strip()
    if port:
        try:
            settings["port"] = int(port)
        except ValueError as exc:
            raise ValueError(f"FASTMCP_PORT must be an integer, got {port!r}") from exc
        if not 1 <= settings["port"] <= 65535:
            raise ValueError(f"FASTMCP_PORT must be between 1 and 65535, got {port!r}")
    level = settings.get("log_level")
    if level is not None and level not in LOG_LEVELS:
        raise ValueError(f"FASTMCP_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {level!r}")
    for var, key in _ENV_STRING_SETTINGS.items():
        if key.endswith("_path") and key in settings and not settings[key].startswith("/"):
            raise ValueError(f"{var} must start with '/', got {settings[key]!r}")

    security = transport_security_from_env(env)
    if security is not None:
        settings["transport_security"] = security

    return settings


class QueryRedactingFilter(logging.Filter):  # pylint: disable=too-few-public-methods
    """Keep query values out of uvicorn's access log.

    ``/oauth/intervals/callback?code=...``, ``/oauth/login?request=...`` and
    ``/messages/?session_id=...`` carry short-lived secrets; the parameter names stay.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            path, query = args[2].split("?", 1)
            record.args = args[:2] + (f"{path}?{_QUERY_VALUE.sub('=…', query)}",) + args[3:]
        return True


def configure_logging(level: str) -> None:
    """Plain one-line log records on stderr, without per-request httpx lines below DEBUG.

    The SDK installs rich's handler, whose rendering time grows with the square of an
    unbroken string's length and runs on the event loop.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
        force=True,
    )
    if logging.getLevelName(level) != logging.DEBUG:
        # httpx logs every Intervals.icu request (with ids) at INFO.
        logging.getLogger("httpx").setLevel(logging.WARNING)


def setup_transport() -> TransportAliases:
    """
    Setup and validate the MCP transport configuration.

    Reads MCP_TRANSPORT environment variable and validates it against
    supported transport types.

    Returns:
        TransportAliases: The selected transport type.

    Raises:
        ValueError: If the transport type is not supported.
    """
    transport_env = os.getenv("MCP_TRANSPORT", TransportAliases.STDIO.value).strip().lower()
    try:
        transport_alias = TransportAliases(transport_env)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in TransportAliases)
        raise ValueError(f"Unsupported MCP_TRANSPORT value. Use one of: {allowed}.") from exc

    # Map HTTP to STREAMABLE_HTTP
    selected_transport = (
        TransportAliases.STREAMABLE_HTTP
        if transport_alias == TransportAliases.HTTP
        else transport_alias
    )

    return selected_transport


def start_server(mcp_instance: FastMCP, transport: TransportAliases, provider: Any = None) -> None:
    """
    Start the MCP server with the specified transport.

    Args:
        mcp_instance (FastMCP): The FastMCP server instance to start.
        transport (TransportAliases): The transport type to use.
        provider: The built-in OAuth provider when MCP_AUTH=oauth (adds the OAuth refinements).
    """
    host = mcp_instance.settings.host
    port = mcp_instance.settings.port

    if transport == TransportAliases.STDIO:
        logger.info("Starting MCP server with stdio transport.")
        mcp_instance.run()
        return

    import uvicorn  # pylint: disable=import-outside-toplevel

    from intervals_mcp_server.http_app import build_http_app  # pylint: disable=import-outside-toplevel

    settings = mcp_instance.settings
    endpoints = {
        TransportAliases.SSE: f"{settings.sse_path} (messages: {settings.message_path})",
        TransportAliases.STREAMABLE_HTTP: settings.streamable_http_path,
        TransportAliases.HTTP_SSE: (
            f"{settings.streamable_http_path} and {settings.sse_path} (messages: {settings.message_path})"
        ),
    }
    logger.info(
        "Starting MCP server with %s transport at http://%s:%s%s%s.",
        transport.value,
        host,
        port,
        endpoints.get(transport, ""),
        " with OAuth" if provider is not None else "",
    )
    app = build_http_app(mcp_instance, transport.value, provider=provider, mount_path=os.getenv("MCP_SSE_MOUNT_PATH"))
    config = uvicorn.Config(app, host=host, port=port, log_level=settings.log_level.lower())
    # uvicorn configured its loggers in Config(); keep secrets in query strings out of the access log.
    logging.getLogger("uvicorn.access").addFilter(QueryRedactingFilter())
    uvicorn.Server(config).run()
