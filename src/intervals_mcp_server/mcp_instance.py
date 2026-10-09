"""
Shared MCP instance module.

This module provides a shared FastMCP instance that can be imported by both
the server module and tool modules without creating cyclic imports.
"""

import os
from collections.abc import Callable, Mapping
from typing import Any, TypeVar, cast

from mcp.server.fastmcp import FastMCP  # pylint: disable=import-error

from intervals_mcp_server.api.client import setup_api_client
from intervals_mcp_server.auth import install_login_routes, oauth_from_env
from intervals_mcp_server.config import PERMISSION_CLASSES, get_config

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


def fastmcp_settings_from_env(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build FastMCP keyword arguments from FASTMCP_* environment variables.

    Only variables that are set (and non-empty) are returned, so FastMCP's own
    defaults still apply otherwise.

    Raises:
        ValueError: If FASTMCP_PORT is set but is not a valid integer.
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

    return settings


# Optional built-in OAuth 2.1 authorization server (MCP_AUTH=oauth), see auth.py.
_oauth = oauth_from_env()

mcp: FastMCP = FastMCP(  # pylint: disable=invalid-name
    "intervals-icu", lifespan=setup_api_client, **fastmcp_settings_from_env(), **_oauth
)
if _oauth:
    install_login_routes(mcp, _oauth["auth_server_provider"])

F = TypeVar("F", bound=Callable[..., Any])

# Permission class of every tool defined with @tool(...), registered or not.
_TOOL_PERMISSIONS: dict[str, str] = {}
# Tools that were not registered because their class is not enabled.
_DISABLED_TOOLS: dict[str, str] = {}


def tool(permission: str = "read", **kwargs: Any) -> Callable[[F], F]:
    """Register an MCP tool only when its permission class is enabled.

    Classes: "read" (never changes anything), "write" (creates or edits calendar
    entries, notes, RPE/feel, subjective wellness), "destructive" (deletes data) and
    "admin" (configuration and mass operations). The enabled classes come from the
    MCP_PERMISSIONS environment variable (default: read). A tool of a disabled class
    is not exposed to clients at all; the Python function stays importable.
    """
    if permission not in PERMISSION_CLASSES:
        raise ValueError(f"Unknown permission class {permission!r}; use one of {PERMISSION_CLASSES}")

    def decorator(func: F) -> F:
        _TOOL_PERMISSIONS[func.__name__] = permission
        if permission in get_config().permissions:
            return cast(F, mcp.tool(**kwargs)(func))
        _DISABLED_TOOLS[func.__name__] = permission
        return func

    return decorator


def tool_permissions() -> dict[str, str]:
    """Permission class per defined tool name."""
    return dict(_TOOL_PERMISSIONS)


def disabled_tools() -> dict[str, str]:
    """Tools hidden from clients because their permission class is not enabled."""
    return dict(_DISABLED_TOOLS)
