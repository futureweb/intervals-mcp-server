"""
Shared MCP instance module.

This module provides a shared FastMCP instance that can be imported by both
the server module and tool modules without creating cyclic imports.
"""

import inspect
import os
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol, TypeVar, cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import FastMCP  # pylint: disable=import-error
from mcp.server.fastmcp.exceptions import ResourceError, ToolError
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.types import GetPromptResult, Icon, TextContent, ToolAnnotations
from mcp.types import Tool as MCPTool
from pydantic import AnyUrl

from intervals_mcp_server.api.client import setup_api_client
from intervals_mcp_server.auth import (
    CredentialError,
    SingleUserOAuthProvider,
    granted_classes,
    install_login_routes,
    oauth_from_env,
)
from intervals_mcp_server.branding import WEBSITE_URL, install_icon_routes, server_icons
from intervals_mcp_server.config import PERMISSION_CLASSES, get_config
from intervals_mcp_server.guides import SERVER_INSTRUCTIONS
from intervals_mcp_server.tenancy import Credential, multi_user, use_credential
from intervals_mcp_server.tool_guard import guarded
from intervals_mcp_server.toolsets import in_toolset

# Re-exported: the FASTMCP_* helpers live in server_setup so that --doctor can check them
# without building the server.
from intervals_mcp_server.server_setup import fastmcp_settings_from_env, transport_security_from_env

__all__ = [
    "IntervalsFastMCP",
    "catalogue",
    "compact_schema",
    "disabled_tools",
    "fastmcp_settings_from_env",
    "mark_outside_toolset",
    "mcp",
    "oauth_provider",
    "tool",
    "tool_permissions",
    "tools_outside_toolset",
    "transport_security_from_env",
]


def compact_schema(schema: Any) -> Any:
    """A smaller but equivalent JSON schema for ``tools/list``.

    Drops the generated ``title`` of every schema (property names stay) and turns an optional
    value with a ``null`` default (``anyOf: [X, {"type": "null"}], default: null``) into ``X``:
    such a parameter is not required, so leaving it out means the same as null. Optional values
    with another default (e.g. a limit where null means "no limit") keep their ``anyOf``.
    """

    def walk(node: Any, mapping: bool = False) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        if mapping:  # "properties" / "$defs": names -> schemas
            return {name: walk(value) for name, value in node.items()}
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "title" and isinstance(value, str):
                continue
            out[key] = walk(value, mapping=key in ("properties", "$defs"))
        options = out.get("anyOf")
        if (
            "default" in out and out["default"] is None and isinstance(options, list)
            and len(options) == 2 and {"type": "null"} in options
        ):
            other = next(option for option in options if option != {"type": "null"})
            rest = {key: value for key, value in out.items() if key not in ("anyOf", "default")}
            out = {**other, **rest}
        return out

    return walk(schema)


class CredentialSource(Protocol):  # pylint: disable=too-few-public-methods
    """What resolves the Intervals.icu credential of a connection (the OAuth provider)."""

    def connection_credential(self, token: str) -> Awaitable[Credential]:
        """The credential of the connection holding access *token*; CredentialError when there is none."""


T = TypeVar("T")


class IntervalsFastMCP(FastMCP[Any]):
    """FastMCP that honours the permission scopes of an OAuth access token.

    With the built-in OAuth server the athlete grants each connection a subset of the
    enabled permission classes (``intervals:read``, ``intervals:write``, ...).  Tools of
    classes the token was not granted are hidden from ``tools/list`` and refused by
    ``tools/call``.  Without OAuth (stdio, secret path) nothing changes: the server-wide
    ``MCP_PERMISSIONS`` decide which tools exist at all.

    Every tool's input schema is compacted once at registration (:func:`compact_schema`). With a tool
    set other than ``full`` the tool names that set leaves out are marked "(full tool set)" in the
    tool descriptions and prompts (:func:`mark_outside_toolset`).
    """

    toolset: str = "full"
    # Multi-user mode: resolves each connection's Intervals.icu credential (the OAuth provider).
    credential_source: CredentialSource | None = None

    def add_tool(  # pylint: disable=too-many-arguments
        self,
        fn: Callable[..., Any],
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> None:
        """Register a tool like FastMCP does and compact its input schema."""
        registered = self._tool_manager.add_tool(
            fn, name=name, title=title, description=description, annotations=annotations,
            icons=icons, meta=meta, structured_output=structured_output,
        )
        registered.parameters = compact_schema(registered.parameters)

    def _token_scopes(self) -> list[str] | None:
        token = self._access_token()
        return list(token.scopes) if token is not None else None

    def _access_token(self) -> AccessToken | None:
        """The OAuth access token of the request being handled (that request's, not the session's)."""
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
        token = getattr(user, "access_token", None)
        if token is None and (request is None or not multi_user()):
            # Outside an HTTP request (in-process calls). In the multi-user mode an HTTP request
            # without its own token is never served with the session's or another request's token.
            token = get_access_token()
        return token if isinstance(token, AccessToken) else None

    async def _as_connection(self, action: Callable[[], Awaitable[T]], refuse: Callable[[str], Exception]) -> T:
        """Run *action* with the calling connection's Intervals.icu credential (multi-user mode).

        Single-user mode: unchanged, the API client uses API_KEY. Multi-user mode: the
        credential comes from the request's own access token, never from arguments; without
        one nothing runs.
        """
        if not multi_user():
            return await action()
        token = self._access_token()
        if token is None or self.credential_source is None:
            raise refuse(
                "This server runs in multi-user mode: every request needs a connection signed in with OAuth. "
                "Reconnect the server in your MCP client."
            )
        try:
            credential = await self.credential_source.connection_credential(token.token)
        except CredentialError as exc:
            raise refuse(str(exc)) from None
        with use_credential(credential):
            return await action()

    async def list_tools(self) -> list[Any]:
        """Tools of the classes the current connection was granted."""
        tools = await super().list_tools()
        if self.toolset != "full":
            tools = [
                t.model_copy(update={"description": mark_outside_toolset(t.description, self.toolset)}) if t.description else t
                for t in tools
            ]
        allowed = granted_classes(self._token_scopes())
        if allowed is None:
            return tools
        return [t for t in tools if _TOOL_PERMISSIONS.get(t.name, "read") in allowed]

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> GetPromptResult:
        """A prompt; tools outside the active tool set are marked in its text."""
        result = await super().get_prompt(name, arguments)
        if self.toolset != "full":
            for message in result.messages:
                if isinstance(message.content, TextContent):
                    message.content.text = mark_outside_toolset(message.content.text, self.toolset)
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Refuse tools whose permission class the access token does not include."""
        allowed = granted_classes(self._token_scopes())
        permission = _TOOL_PERMISSIONS.get(name, "read")
        if allowed is not None and permission not in allowed:
            raise ToolError(
                f"Tool '{name}' needs the '{permission}' permission, but this connection was only granted "
                f"{', '.join(sorted(allowed))}. Reconnect the MCP client and allow '{permission}' on the consent page."
            )
        return await self._as_connection(lambda: FastMCP.call_tool(self, name, arguments), ToolError)

    async def read_resource(self, uri: AnyUrl | str) -> Iterable[ReadResourceContents]:
        """Read a resource with the calling connection's credential (multi-user mode)."""
        return await self._as_connection(lambda: FastMCP.read_resource(self, uri), ResourceError)


# Optional built-in OAuth 2.1 authorization server (MCP_AUTH=oauth), see auth.py.
_oauth = oauth_from_env()
oauth_provider: SingleUserOAuthProvider | None = _oauth.get("auth_server_provider")

mcp: IntervalsFastMCP = IntervalsFastMCP(  # pylint: disable=invalid-name
    "intervals-icu", instructions=SERVER_INSTRUCTIONS, website_url=WEBSITE_URL, icons=server_icons(os.environ),
    lifespan=setup_api_client, **fastmcp_settings_from_env(), **_oauth
)
mcp.toolset = get_config().toolset
install_icon_routes(mcp)
if oauth_provider is not None:
    install_login_routes(mcp, oauth_provider)
    if oauth_provider.config.multi_user:
        mcp.credential_source = oauth_provider

F = TypeVar("F", bound=Callable[..., Any])

# Permission class of every tool defined with @tool(...), registered or not.
_TOOL_PERMISSIONS: dict[str, str] = {}
# Tools that were not registered because their class is not enabled.
_DISABLED_TOOLS: dict[str, str] = {}
# Tools of an enabled class that were not registered because they are not in MCP_TOOLSET.
_OUTSIDE_TOOLSET: dict[str, str] = {}


@dataclass(frozen=True)
class _ToolSpec:
    """How a defined tool is registered (kept for every tool, registered or not)."""

    func: Callable[..., Any]
    permission: str
    options: dict[str, Any] = field(default_factory=dict)


_TOOL_SPECS: dict[str, _ToolSpec] = {}


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

    MCP_TOOLSET=core registers only the curated tools of toolsets.CORE_TOOLS.

    ``overwrites=True`` marks a write tool that can replace existing values (destructiveHint).
    Every tool is wrapped by tool_guard.guarded (id checks, athlete time zone, request budget
    and deadline, output size cap), registered or not.

    The tool's docstring is its description (indentation removed). Results are plain text
    content only (``structured_output=False``): the tools return text or JSON text, and FastMCP
    would otherwise repeat the same text as ``{"result": ...}`` structured content with an
    output schema. A tool that deliberately returns a JSON object passes
    ``structured_output=True``.
    """
    if permission not in PERMISSION_CLASSES:
        raise ValueError(f"Unknown permission class {permission!r}; use one of {PERMISSION_CLASSES}")

    def decorator(func: F) -> F:
        name = func.__name__
        _TOOL_PERMISSIONS[name] = permission
        wrapped = guarded(func)
        options = dict(kwargs)
        hints = OVERWRITE_ANNOTATIONS if overwrites else PERMISSION_ANNOTATIONS[permission]
        options.setdefault("annotations", ToolAnnotations.model_validate(hints))
        options.setdefault("description", inspect.cleandoc(func.__doc__ or ""))
        options.setdefault("structured_output", False)
        _TOOL_SPECS[name] = _ToolSpec(wrapped, permission, options)
        config = get_config()
        if permission not in config.permissions:
            _DISABLED_TOOLS[name] = permission
        elif not in_toolset(name, config.toolset):
            _OUTSIDE_TOOLSET[name] = permission
        else:
            mcp.add_tool(wrapped, **options)
        return cast(F, wrapped)

    return decorator


async def catalogue(permissions: frozenset[str] | set[str], toolset: str = "full") -> list[MCPTool]:
    """The ``tools/list`` result of this package's tools for the given permission classes and tool set.

    Built on a scratch server from the definitions of every tool, so it does not depend on the
    running configuration (tests and the catalogue size report use it). Tools defined outside the
    package (e.g. by tests) are left out.
    """
    server = IntervalsFastMCP("catalogue")
    server.toolset = toolset
    for name, spec in _TOOL_SPECS.items():
        if (
            spec.permission in permissions and in_toolset(name, toolset)
            and spec.func.__module__.startswith("intervals_mcp_server.")
        ):
            server.add_tool(spec.func, **spec.options)
    return await server.list_tools()


@lru_cache(maxsize=8)
def _outside_pattern(toolset: str, names: frozenset[str]) -> re.Pattern[str] | None:
    outside = sorted((name for name in names if not in_toolset(name, toolset)), key=len, reverse=True)
    if not outside:
        return None
    return re.compile(r"\b(" + "|".join(map(re.escape, outside)) + r")\b(?! \(full tool set\))")


def mark_outside_toolset(text: str, toolset: str) -> str:
    """Mark every tool of this package that *toolset* leaves out with "(full tool set)" in *text*."""
    if toolset == "full" or not text:
        return text
    names = frozenset(name for name, spec in _TOOL_SPECS.items() if spec.func.__module__.startswith("intervals_mcp_server."))
    pattern = _outside_pattern(toolset, names)
    return pattern.sub(r"\1 (full tool set)", text) if pattern else text


def tool_permissions() -> dict[str, str]:
    """Permission class per defined tool name."""
    return dict(_TOOL_PERMISSIONS)


def disabled_tools() -> dict[str, str]:
    """Tools hidden from clients because their permission class is not enabled."""
    return dict(_DISABLED_TOOLS)


def tools_outside_toolset() -> dict[str, str]:
    """Tools of an enabled class hidden from clients because MCP_TOOLSET does not include them."""
    return dict(_OUTSIDE_TOOLSET)
