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
* verification of ``private_key_jwt`` client assertions at ``/token`` and ``/revoke``
  (and, by default, refusal of token requests without one from clients whose metadata
  document declares ``private_key_jwt``),
* the client address for ``/authorize`` and ``/register``, whose SDK handlers do not hand
  the request to the provider (per-address limits of pending sign-ins and registrations).
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

from intervals_mcp_server.auth import SingleUserOAuthProvider, reset_request_client_key, set_request_client_key
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


class ClientKeyMiddleware:  # pylint: disable=too-few-public-methods
    """Make the client address of the request available to the provider (rate-limit key)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        token = set_request_client_key(client[0] if client else None)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_client_key(token)


class PublicClientRevocationMiddleware:  # pylint: disable=too-few-public-methods
    """Let public clients use ``/revoke`` without sending ``client_secret``.

    The MCP SDK's revocation request model declares ``client_secret: str | None`` without a
    default, which makes the field mandatory, so a public client (``token_endpoint_auth_method
    none``, e.g. ChatGPT) gets ``400 invalid_request``. An absent field is added as an empty
    value; the SDK then authenticates exactly as before (a client with a secret still needs it).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > 64 * 1024:
                break
        fields = parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)
        if "client_secret" not in {name for name, _ in fields}:
            body = urlencode(fields + [("client_secret", "")]).encode("utf-8")
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


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
    """Replace the SDK route for *path* with *new* (keeping the SDK's path).

    With an issuer that has a path (``https://host/prefix``) the SDK serves the metadata
    at ``<path>/prefix`` (RFC 8414 / RFC 9728 path insertion); that route is replaced, so
    there is exactly one document and it is the one the SDK advertises.
    """
    for index, route in enumerate(routes):
        route_path = getattr(route, "path", None)
        if isinstance(route_path, str) and (route_path == path or route_path.startswith(path + "/")):
            routes[index] = Route(route_path, endpoint=new.endpoint, methods=list(new.methods or []))
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
    _wrap_route(routes, "/revoke", PublicClientRevocationMiddleware)
    for path in ("/authorize", "/register"):
        _wrap_route(routes, path, ClientKeyMiddleware)
    if config.private_key_jwt and provider.metadata_clients.enabled:
        audiences = {
            config.issuer,
            config.metadata_issuer,
            config.issuer + "/token",
            config.issuer + "/revoke",
        }
        verifier = ClientAssertionVerifier(provider.metadata_clients, audiences)
        _wrap_route(
            routes,
            "/token",
            lambda inner: ClientAssertionMiddleware(inner, verifier, require_declared=config.require_private_key_jwt),
        )
        _wrap_route(routes, "/revoke", lambda inner: ClientAssertionMiddleware(inner, verifier))


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
