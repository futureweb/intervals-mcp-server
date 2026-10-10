"""
Shared MCP instance module.

This module provides a shared FastMCP instance that can be imported by both
the server module and tool modules without creating cyclic imports.
"""

from collections.abc import Callable
from typing import Any, TypeVar, cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP  # pylint: disable=import-error
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from intervals_mcp_server.api.client import setup_api_client
from intervals_mcp_server.auth import SingleUserOAuthProvider, granted_classes, install_login_routes, oauth_from_env
from intervals_mcp_server.config import PERMISSION_CLASSES, get_config
from intervals_mcp_server.tool_guard import guarded

# Re-exported: the FASTMCP_* helpers live in server_setup so that --doctor can check them
# without building the server.
from intervals_mcp_server.server_setup import fastmcp_settings_from_env, transport_security_from_env

__all__ = [
    "IntervalsFastMCP",
    "disabled_tools",
    "fastmcp_settings_from_env",
    "mcp",
    "oauth_provider",
    "tool",
    "tool_permissions",
    "transport_security_from_env",
]

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
# Write tools that can REPLACE existing values (an update of an event, a wellness comment, an
# activity name) are not purely additive: per the MCP spec they carry destructiveHint=true, so
# clients ask before running them. Their permission class stays "write".
OVERWRITE_ANNOTATIONS: dict[str, bool] = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}


def tool(permission: str = "read", *, overwrites: bool = False, **kwargs: Any) -> Callable[[F], F]:
    """Register an MCP tool only when its permission class is enabled.

    Classes: "read" (never changes anything), "write" (creates or edits calendar
    entries, notes, RPE/feel, subjective wellness), "destructive" (deletes data) and
    "admin" (configuration and mass operations). The enabled classes come from the
    MCP_PERMISSIONS environment variable (default: read). A tool of a disabled class
    is not exposed to clients at all; the Python function stays importable.

    ``overwrites=True`` marks a write tool that can replace existing values (destructiveHint).
    Every tool is wrapped by tool_guard.guarded (id checks, athlete time zone, request budget
    and deadline, output size cap), registered or not.
    """
    if permission not in PERMISSION_CLASSES:
        raise ValueError(f"Unknown permission class {permission!r}; use one of {PERMISSION_CLASSES}")

    def decorator(func: F) -> F:
        _TOOL_PERMISSIONS[func.__name__] = permission
        wrapped = guarded(func)
        if permission in get_config().permissions:
            options = dict(kwargs)
            hints = OVERWRITE_ANNOTATIONS if overwrites else PERMISSION_ANNOTATIONS[permission]
            options.setdefault("annotations", ToolAnnotations.model_validate(hints))
            return cast(F, mcp.tool(**options)(wrapped))
        _DISABLED_TOOLS[func.__name__] = permission
        return wrapped

    return decorator


def tool_permissions() -> dict[str, str]:
    """Permission class per defined tool name."""
    return dict(_TOOL_PERMISSIONS)


def disabled_tools() -> dict[str, str]:
    """Tools hidden from clients because their permission class is not enabled."""
    return dict(_DISABLED_TOOLS)
