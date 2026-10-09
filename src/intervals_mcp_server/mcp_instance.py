"""
Shared MCP instance module.

This module provides a shared FastMCP instance that can be imported by both
the server module and tool modules without creating cyclic imports.
"""

import os
from collections.abc import Callable, Mapping
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP  # pylint: disable=import-error
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from intervals_mcp_server.api.client import setup_api_client
from intervals_mcp_server.auth import SingleUserOAuthProvider, granted_classes, install_login_routes, oauth_from_env
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

    security = transport_security_from_env(env)
    if security is not None:
        settings["transport_security"] = security

    return settings


class IntervalsFastMCP(FastMCP[Any]):
    """FastMCP that honours the permission scopes of an OAuth access token.

    With the built-in OAuth server the athlete grants each connection a subset of the
    enabled permission classes (``intervals:read``, ``intervals:write``, ...).  Tools of
    classes the token was not granted are hidden from ``tools/list`` and refused by
    ``tools/call``.  Without OAuth (stdio, secret path) nothing changes: the server-wide
    ``MCP_PERMISSIONS`` decide which tools exist at all.
    """

    def _token_scopes(self) -> list[str] | None:
        request: Any = None
        try:
            request = self.get_context().request_context.request
        except (ValueError, LookupError, AttributeError):
            request = None
        user: Any = None
        if request is not None:
            try:
                user = request.user
            except (AssertionError, AttributeError):
                user = None
        token = getattr(user, "access_token", None) or get_access_token()
        return list(token.scopes) if token is not None else None

    async def list_tools(self) -> list[Any]:
        """Tools of the classes the current connection was granted."""
        tools = await super().list_tools()
        allowed = granted_classes(self._token_scopes())
        if allowed is None:
            return tools
        return [t for t in tools if _TOOL_PERMISSIONS.get(t.name, "read") in allowed]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Refuse tools whose permission class the access token does not include."""
        allowed = granted_classes(self._token_scopes())
        permission = _TOOL_PERMISSIONS.get(name, "read")
        if allowed is not None and permission not in allowed:
            raise ToolError(
                f"Tool '{name}' needs the '{permission}' permission, but this connection was only granted "
                f"{', '.join(sorted(allowed))}. Reconnect the MCP client and allow '{permission}' on the consent page."
            )
        return await super().call_tool(name, arguments)


# Optional built-in OAuth 2.1 authorization server (MCP_AUTH=oauth), see auth.py.
_oauth = oauth_from_env()
oauth_provider: SingleUserOAuthProvider | None = _oauth.get("auth_server_provider")

mcp: FastMCP = IntervalsFastMCP(  # pylint: disable=invalid-name
    "intervals-icu", lifespan=setup_api_client, **fastmcp_settings_from_env(), **_oauth
)
if oauth_provider is not None:
    install_login_routes(mcp, oauth_provider)

F = TypeVar("F", bound=Callable[..., Any])

# Permission class of every tool defined with @tool(...), registered or not.
_TOOL_PERMISSIONS: dict[str, str] = {}
# Tools that were not registered because their class is not enabled.
_DISABLED_TOOLS: dict[str, str] = {}


# MCP tool annotations per permission class. Clients such as ChatGPT use them to decide which
# tools may run without asking (read-only) and which need a confirmation (writes, deletions).
# The tools only talk to the athlete's own Intervals.icu account: a closed domain.
PERMISSION_ANNOTATIONS: dict[str, dict[str, bool]] = {
    "read": {"readOnlyHint": True, "openWorldHint": False},
    "write": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    "destructive": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    "admin": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
}


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
            options = dict(kwargs)
            options.setdefault("annotations", ToolAnnotations.model_validate(PERMISSION_ANNOTATIONS[permission]))
            return cast(F, mcp.tool(**options)(func))
        _DISABLED_TOOLS[func.__name__] = permission
        return func

    return decorator


def tool_permissions() -> dict[str, str]:
    """Permission class per defined tool name."""
    return dict(_TOOL_PERMISSIONS)


def disabled_tools() -> dict[str, str]:
    """Tools hidden from clients because their permission class is not enabled."""
    return dict(_DISABLED_TOOLS)
