"""ASGI app for the HTTP transports: SSE, streamable HTTP or both, plus OAuth refinements.

FastMCP builds one Starlette app per transport.  :func:`build_http_app` returns the app
for ``MCP_TRANSPORT`` and, with ``http+sse``, serves both transports from one process
(``/mcp`` for streamable HTTP clients such as ChatGPT, ``/sse`` + ``/messages/`` for SSE
clients) behind the same authentication.

When the built-in OAuth server is enabled it also adds what the MCP SDK does not provide:

* authorization server metadata with ``client_id_metadata_document_supported``,
  ``authorization_response_iss_parameter_supported`` (RFC 9207), the public-client and
  ``private_key_jwt`` token endpoint methods and the permission scopes,
* protected resource metadata listing the permission scopes,
* an ``iss`` parameter on every authorization response of ``/authorize`` (also the error
  redirects produced by the SDK), so clients can use a stable redirect URI,
* verification of ``private_key_jwt`` client assertions at ``/token`` and ``/revoke``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from intervals_mcp_server.auth import SingleUserOAuthProvider
from intervals_mcp_server.auth_clients import (
    ASSERTION_ALGORITHMS,
    ClientAssertionMiddleware,
    ClientAssertionVerifier,
)

__all__ = ["HTTP_TRANSPORTS", "authorization_server_metadata", "build_http_app"]

HTTP_TRANSPORTS = ("sse", "streamable-http", "http+sse")
AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
PRM_PATH = "/.well-known/oauth-protected-resource"


class IssuerParameterMiddleware:  # pylint: disable=too-few-public-methods
    """Add ``iss`` (RFC 9207) to redirects of ``/authorize`` that carry ``code`` or ``error``."""

    def __init__(self, app: ASGIApp, issuer: str) -> None:
        self.app = app
        self.issuer = issuer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_iss(message: Message) -> None:
            if message["type"] == "http.response.start" and message.get("status") in (302, 303, 307):
                message = dict(message)
                message["headers"] = [
                    (name, add_issuer(value.decode("latin-1"), self.issuer).encode("latin-1"))
                    if name.lower() == b"location"
                    else (name, value)
                    for name, value in message.get("headers", [])
                ]
            await send(message)

        await self.app(scope, receive, send_with_iss)


def add_issuer(location: str, issuer: str) -> str:
    """Append ``iss`` to an authorization response URL that has ``code`` or ``error``."""
    parts = urlsplit(location)
    query = parse_qsl(parts.query, keep_blank_values=True)
    names = {name for name, _ in query}
    if not names & {"code", "error"} or "iss" in names:
        return location
    return urlunsplit(parts._replace(query=urlencode(query + [("iss", issuer)])))


def authorization_server_metadata(provider: SingleUserOAuthProvider, settings: AuthSettings) -> dict[str, Any]:
    """RFC 8414 metadata of the SDK plus CIMD, RFC 9207 and the token endpoint methods."""
    config = provider.config
    metadata = build_metadata(
        settings.issuer_url,
        settings.service_documentation_url,
        settings.client_registration_options or ClientRegistrationOptions(),
        settings.revocation_options or RevocationOptions(),
    ).model_dump(mode="json", exclude_none=True)
    methods = ["none", "client_secret_post"]
    signed = config.private_key_jwt and provider.metadata_clients.enabled
    if signed:
        methods.append("private_key_jwt")
        metadata["token_endpoint_auth_signing_alg_values_supported"] = ASSERTION_ALGORITHMS
    metadata["token_endpoint_auth_methods_supported"] = methods
    metadata["revocation_endpoint_auth_methods_supported"] = methods if "revocation_endpoint" in metadata else None
    metadata["scopes_supported"] = config.scopes_supported
    metadata["client_id_metadata_document_supported"] = provider.metadata_clients.enabled
    metadata["authorization_response_iss_parameter_supported"] = True
    return {key: value for key, value in metadata.items() if value is not None}


def _json_route(path: str, payload: dict[str, Any]) -> Route:
    body = json.dumps(payload)

    async def handler(_request: Request) -> Response:
        return JSONResponse(json.loads(body), headers={"Cache-Control": "public, max-age=3600"})

    return Route(path, endpoint=cors_middleware(handler, ["GET", "OPTIONS"]), methods=["GET", "OPTIONS"])


def _replace_route(routes: list[BaseRoute], path: str, new: Route) -> None:
    for index, route in enumerate(routes):
        if getattr(route, "path", None) == path:
            routes[index] = new
            return
    routes.append(new)


def _wrap_route(routes: list[BaseRoute], path: str, wrapper: Any) -> None:
    for route in routes:
        if isinstance(route, Route) and route.path == path:
            route.app = wrapper(route.app)


def _apply_oauth(app: Starlette, provider: SingleUserOAuthProvider, settings: AuthSettings) -> None:
    config = provider.config
    routes = app.router.routes
    _replace_route(routes, AS_METADATA_PATH, _json_route(AS_METADATA_PATH, authorization_server_metadata(provider, settings)))
    resource = {
        "resource": str(settings.resource_server_url or settings.issuer_url),
        "authorization_servers": [config.metadata_issuer],
        "scopes_supported": config.scopes_supported,
        "bearer_methods_supported": ["header"],
        "resource_name": "Futureweb Intervals MCP",
    }
    _replace_route(routes, PRM_PATH, _json_route(PRM_PATH, resource))
    _wrap_route(routes, "/authorize", lambda inner: IssuerParameterMiddleware(inner, config.metadata_issuer))
    if config.private_key_jwt and provider.metadata_clients.enabled:
        audiences = {
            config.issuer,
            config.metadata_issuer,
            config.issuer + "/token",
            config.issuer + "/revoke",
        }
        verifier = ClientAssertionVerifier(provider.metadata_clients, audiences)
        for path in ("/token", "/revoke"):
            _wrap_route(routes, path, lambda inner: ClientAssertionMiddleware(inner, verifier))


def build_http_app(
    mcp: FastMCP[Any],
    transport: str,
    provider: SingleUserOAuthProvider | None = None,
    mount_path: str | None = None,
) -> Starlette:
    """Starlette app for *transport* (``sse``, ``streamable-http`` or ``http+sse``)."""
    if transport not in HTTP_TRANSPORTS:
        raise ValueError(f"transport must be one of {', '.join(HTTP_TRANSPORTS)}, got {transport!r}")
    if transport == "sse":
        app = mcp.sse_app(mount_path)
    else:
        app = mcp.streamable_http_app()
        if transport == "http+sse":
            present = {getattr(route, "path", None) for route in app.router.routes}
            for route in mcp.sse_app(mount_path).router.routes:
                if getattr(route, "path", None) not in present:
                    app.router.routes.append(route)
    settings = mcp.settings.auth
    if provider is not None and settings is not None:
        _apply_oauth(app, provider, settings)
    return app
