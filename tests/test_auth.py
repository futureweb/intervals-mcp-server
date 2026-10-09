"""Tests for the optional single-user OAuth 2.1 authorization server (synthetic data only).

The whole flow a remote client such as ChatGPT performs is exercised against a
``FastMCP`` app through ``starlette.testclient.TestClient``: metadata discovery,
dynamic client registration, PKCE authorization, the login page, code exchange,
bearer access, refresh-token rotation, revocation and persistence.

Note on the SSE endpoint: ``GET /sse`` never completes (it is an infinite event
stream) and the Starlette ``TestClient`` only reports a disconnect to the app
once the response has finished, so an authenticated ``GET /sse`` would block
forever.  The bearer check is therefore verified with ``GET /sse`` *without* a
token (rejected before the stream starts), with ``POST /messages/`` (which is
wrapped by the same ``RequireAuthMiddleware``) and with a custom route that
reports the identity the SDK's bearer middleware resolved.
"""

# pylint: disable=redefined-outer-name

import asyncio
import base64
import hashlib
import io
import json
import os
import secrets
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AuthorizationParams
from mcp.server.fastmcp import FastMCP
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.testclient import TestClient

from intervals_mcp_server import auth
from tests.oauth_helpers import submit_consent
from intervals_mcp_server.auth import (
    SingleUserOAuthProvider,
    auth_status_from_env,
    hash_password,
    install_login_routes,
    oauth_config_from_env,
    oauth_from_env,
    verify_password_hash,
)

PASSWORD = "correct horse battery staple"
ISSUER = "http://localhost"
REDIRECT_URI = "https://chatgpt.com/connector/oauth/test-callback"
CHATGPT_REGISTRATION: dict[str, Any] = {
    "client_name": "ChatGPT",
    "redirect_uris": [REDIRECT_URI],
    "token_endpoint_auth_method": "none",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
}


# --------------------------------------------------------------------------- #
# Helpers and fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def oauth_env(tmp_path) -> dict[str, str]:
    """A complete OAuth configuration with a per-test state file."""
    return {
        "MCP_AUTH": "oauth",
        "MCP_PUBLIC_URL": ISSUER,
        "OAUTH_PASSWORD": PASSWORD,
        "OAUTH_STATE_FILE": str(tmp_path / "oauth_state.json"),
    }


def build_app(env: Mapping[str, str]) -> tuple[FastMCP[Any], TestClient]:
    """Create a FastMCP server with OAuth from *env*, one tool and a whoami route."""
    kwargs = oauth_from_env(env)
    mcp: FastMCP[Any] = FastMCP("test", **kwargs)
    install_login_routes(mcp, kwargs["auth_server_provider"])

    @mcp.tool()
    def ping() -> str:
        """Dummy tool so the server is not empty."""
        return "pong"

    @mcp.custom_route("/whoami", methods=["GET"])
    async def whoami(request: Request) -> Response:
        user = request.user
        if isinstance(user, AuthenticatedUser):
            return JSONResponse({"client_id": user.username, "scopes": list(user.scopes)})
        return JSONResponse({"client_id": None, "scopes": []})

    assert ping() == "pong"
    return mcp, TestClient(mcp.sse_app(), base_url="http://127.0.0.1:8000", follow_redirects=False)


@pytest.fixture
def client(oauth_env) -> TestClient:
    """TestClient for the SSE app with OAuth enabled."""
    return build_app(oauth_env)[1]


def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, S256 code_challenge)."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def register(client: TestClient, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dynamically register a client and return the registration response."""
    response = client.post("/register", json=metadata or CHATGPT_REGISTRATION)
    assert response.status_code == 201, response.text
    return response.json()


def start_authorization(
    client: TestClient, client_id: str, challenge: str, state: str = "xyz"
) -> str:
    """Call /authorize and return the login request id from the redirect."""
    response = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "mcp",
            "resource": ISSUER,
        },
    )
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert location.path == "/oauth/login"
    assert response.headers["location"].startswith(f"{ISSUER}/oauth/login?request=")
    return parse_qs(location.query)["request"][0]


def login(
    client: TestClient, request_id: str, password: str, username: str = "athlete"
) -> Any:
    """Submit the login form."""
    return submit_consent(
        client,
        {"request": request_id, "username": username, "password": password},
    )


def exchange_code(
    client: TestClient, client_id: str, code: str, verifier: str
) -> Any:
    """Exchange an authorization code for tokens."""
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": ISSUER,
        },
    )


def obtain_tokens(client: TestClient, password: str = PASSWORD) -> tuple[str, dict[str, Any]]:
    """Run register -> authorize -> login -> token and return (client_id, token response)."""
    client_id = register(client)["client_id"]
    verifier, challenge = pkce_pair()
    request_id = start_authorization(client, client_id, challenge)
    redirect = login(client, request_id, password)
    assert redirect.status_code == 302, redirect.text
    query = parse_qs(urlsplit(redirect.headers["location"]).query)
    tokens = exchange_code(client, client_id, query["code"][0], verifier)
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


def bearer(token: str) -> dict[str, str]:
    """Authorization header for *token*."""
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# 1-2: discovery and unauthenticated access
# --------------------------------------------------------------------------- #


def test_authorization_server_metadata(client):
    """RFC 8414 metadata advertises every endpoint, the scope and PKCE S256."""
    response = client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["issuer"] == f"{ISSUER}/"
    assert metadata["authorization_endpoint"] == f"{ISSUER}/authorize"
    assert metadata["token_endpoint"] == f"{ISSUER}/token"
    assert metadata["registration_endpoint"] == f"{ISSUER}/register"
    assert metadata["revocation_endpoint"] == f"{ISSUER}/revoke"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["scopes_supported"] == ["mcp", "intervals:read"]
    assert set(metadata["grant_types_supported"]) == {"authorization_code", "refresh_token"}


def test_protected_resource_metadata(client):
    """RFC 9728 metadata points clients at this server as the authorization server."""
    response = client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["resource"] == f"{ISSUER}/"
    assert metadata["authorization_servers"] == [f"{ISSUER}/"]
    assert metadata["scopes_supported"] == ["mcp"]


def test_sse_without_token_is_rejected(client):
    """GET /sse without a bearer token returns 401 and points at the resource metadata."""
    response = client.get("/sse")
    assert response.status_code == 401
    www_authenticate = response.headers["www-authenticate"]
    assert www_authenticate.startswith("Bearer ")
    assert f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"' in www_authenticate
    assert client.post("/messages/?session_id=abc", json={}).status_code == 401
    assert client.get("/sse", headers=bearer("not-a-token")).status_code == 401


# --------------------------------------------------------------------------- #
# 3: dynamic client registration
# --------------------------------------------------------------------------- #


def test_public_client_registration(client):
    """A ChatGPT-like public client gets a client_id and no secret."""
    info = register(client)
    assert info["client_id"]
    assert "client_secret" not in info
    assert info["token_endpoint_auth_method"] == "none"
    assert info["redirect_uris"] == [REDIRECT_URI]
    assert info["scope"] == "mcp intervals:read"


def test_confidential_client_registration(client):
    """Clients that want a secret get one; the SDK then enforces it on /token."""
    metadata = {**CHATGPT_REGISTRATION, "token_endpoint_auth_method": "client_secret_post"}
    info = register(client, metadata)
    assert info["client_secret"]


@pytest.mark.parametrize(
    "redirect_uri",
    ["http://evil.example/callback", "myapp://callback", "ftp://example.com/cb"],
)
def test_registration_rejects_insecure_redirect_uris(client, redirect_uri):
    """Only https and loopback-http redirect URIs are accepted."""
    response = client.post("/register", json={**CHATGPT_REGISTRATION, "redirect_uris": [redirect_uri]})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(
    "redirect_uri", ["http://localhost:6274/oauth/callback", "http://127.0.0.1:8123/cb"]
)
def test_registration_accepts_loopback_redirect_uris(client, redirect_uri):
    """Local clients (MCP Inspector, Claude Code) may use loopback http redirects."""
    info = register(client, {**CHATGPT_REGISTRATION, "redirect_uris": [redirect_uri]})
    assert info["redirect_uris"] == [redirect_uri]


# --------------------------------------------------------------------------- #
# 4: authorization request and login page
# --------------------------------------------------------------------------- #


def test_authorize_redirects_to_login_page(client):
    """/authorize parks the request and redirects to /oauth/login?request=<id>."""
    client_id = register(client)["client_id"]
    request_id = start_authorization(client, client_id, pkce_pair()[1])
    assert len(request_id) >= 32


def test_login_page_renders_form(client):
    """The login page is a self-contained form carrying the request id and the username."""
    client_id = register(client)["client_id"]
    request_id = start_authorization(client, client_id, pkce_pair()[1])
    response = client.get("/oauth/login", params={"request": request_id})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "content-security-policy" in response.headers
    body = response.text
    assert '<form method="post"' in body
    assert f'name="request" value="{request_id}"' in body
    assert 'name="username" value="athlete"' in body
    assert 'type="password" id="password" name="password"' in body
    assert 'name="grant" value="read"' in body and "Deny" in body
    assert "<script" not in body and "http://" not in body and "https://" not in body


def test_login_page_rejects_unknown_request(client):
    """An unknown or missing request id renders an error page instead of the form."""
    assert client.get("/oauth/login", params={"request": "nope"}).status_code == 400
    assert client.get("/oauth/login").status_code == 400
    response = login(client, "nope", PASSWORD)
    assert response.status_code == 400
    assert "<form" not in response.text


def test_wrong_password_is_rejected_and_rate_limited(client):
    """Wrong credentials yield 401 with a generic error; the sixth attempt is throttled."""
    client_id = register(client)["client_id"]
    request_id = start_authorization(client, client_id, pkce_pair()[1])
    for _ in range(5):
        response = login(client, request_id, "wrong password")
        assert response.status_code == 401
        assert "Invalid credentials." in response.text
        assert "<form" in response.text
    throttled = login(client, request_id, PASSWORD)
    assert throttled.status_code == 429
    assert "retry-after" in throttled.headers


def test_wrong_username_is_rejected(client):
    """The user name must match OAUTH_USERNAME as well."""
    client_id = register(client)["client_id"]
    request_id = start_authorization(client, client_id, pkce_pair()[1])
    assert login(client, request_id, PASSWORD, username="admin").status_code == 401


def test_successful_login_redirects_with_code_and_state(client):
    """Correct credentials redirect to the client's redirect_uri with code and state."""
    client_id = register(client)["client_id"]
    request_id = start_authorization(client, client_id, pkce_pair()[1], state="xyz")
    response = login(client, request_id, PASSWORD)
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT_URI
    query = parse_qs(location.query)
    assert query["state"] == ["xyz"]
    assert len(query["code"][0]) >= 32
    # the request id is single use
    assert login(client, request_id, PASSWORD).status_code == 400
    assert client.get("/oauth/login", params={"request": request_id}).status_code == 400


# --------------------------------------------------------------------------- #
# 5: token exchange
# --------------------------------------------------------------------------- #


def test_code_exchange_issues_tokens(client):
    """The authorization code plus PKCE verifier yields access and refresh tokens."""
    _, tokens = obtain_tokens(client)
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 3600
    assert tokens["scope"] == "mcp intervals:read"
    assert len(tokens["access_token"]) >= 32
    assert len(tokens["refresh_token"]) >= 32
    assert tokens["access_token"] != tokens["refresh_token"]


def test_code_cannot_be_exchanged_twice(client):
    """A second exchange of the same code fails and revokes the tokens it produced."""
    client_id = register(client)["client_id"]
    verifier, challenge = pkce_pair()
    request_id = start_authorization(client, client_id, challenge)
    code = parse_qs(urlsplit(login(client, request_id, PASSWORD).headers["location"]).query)["code"][0]
    first = exchange_code(client, client_id, code, verifier)
    assert first.status_code == 200
    access_token = first.json()["access_token"]
    assert client.get("/whoami", headers=bearer(access_token)).json()["client_id"] == client_id

    second = exchange_code(client, client_id, code, verifier)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"
    assert client.get("/whoami", headers=bearer(access_token)).json()["client_id"] is None


def test_wrong_code_verifier_is_rejected(client):
    """The SDK verifies PKCE: a wrong verifier does not produce tokens."""
    client_id = register(client)["client_id"]
    _, challenge = pkce_pair()
    request_id = start_authorization(client, client_id, challenge)
    code = parse_qs(urlsplit(login(client, request_id, PASSWORD).headers["location"]).query)["code"][0]
    response = exchange_code(client, client_id, code, "not-the-verifier")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_code_belongs_to_its_client(client):
    """A code issued to one client cannot be exchanged by another registered client."""
    client_id = register(client)["client_id"]
    other_id = register(client)["client_id"]
    verifier, challenge = pkce_pair()
    request_id = start_authorization(client, client_id, challenge)
    code = parse_qs(urlsplit(login(client, request_id, PASSWORD).headers["location"]).query)["code"][0]
    assert exchange_code(client, other_id, code, verifier).status_code == 400


# --------------------------------------------------------------------------- #
# 6: bearer access
# --------------------------------------------------------------------------- #


def test_bearer_token_grants_access(client):
    """A valid access token passes the SDK's bearer middleware on the MCP routes."""
    client_id, tokens = obtain_tokens(client)
    headers = bearer(tokens["access_token"])
    # /messages/ is protected by the same RequireAuthMiddleware as /sse; once the token
    # is accepted the SSE transport itself complains about the missing session.
    response = client.post("/messages/", headers=headers, json={})
    assert response.status_code == 400
    assert "session_id" in response.text
    identity = client.get("/whoami", headers=headers).json()
    assert identity == {"client_id": client_id, "scopes": ["mcp", "intervals:read"]}
    assert client.get("/whoami").json()["client_id"] is None
    assert client.get("/whoami", headers=bearer(tokens["refresh_token"])).json()["client_id"] is None


def test_streamable_http_initialize_with_bearer(oauth_env):
    """The Streamable HTTP transport (/mcp) accepts the token end to end."""
    kwargs = oauth_from_env(oauth_env)
    mcp = FastMCP("test", json_response=True, stateless_http=True, **kwargs)
    install_login_routes(mcp, kwargs["auth_server_provider"])
    with TestClient(mcp.streamable_http_app(), base_url="http://127.0.0.1:8000", follow_redirects=False) as http:
        _, tokens = obtain_tokens(http)
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
        headers = {"Accept": "application/json, text/event-stream"}
        assert http.post("/mcp", json=initialize, headers=headers).status_code == 401
        response = http.post("/mcp", json=initialize, headers={**headers, **bearer(tokens["access_token"])})
        assert response.status_code == 200, response.text
        assert response.json()["result"]["serverInfo"]["name"] == "test"


# --------------------------------------------------------------------------- #
# 7-8: refresh and revocation
# --------------------------------------------------------------------------- #


def test_refresh_token_rotation(oauth_env):
    """The refresh grant returns new tokens and the old refresh token stops working.

    Without a grace period the replay is reuse: it fails and revokes the grant.
    """
    client = build_app({**oauth_env, "OAUTH_REFRESH_REUSE_GRACE": "0"})[1]
    client_id, tokens = obtain_tokens(client)
    response = client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
    )
    assert response.status_code == 200, response.text
    rotated = response.json()
    assert rotated["access_token"] != tokens["access_token"]
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert client.get("/whoami", headers=bearer(rotated["access_token"])).json()["client_id"] == client_id

    replay = client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    # the reuse revoked the grant: the rotated tokens stopped working too
    assert client.get("/whoami", headers=bearer(rotated["access_token"])).json()["client_id"] is None


def test_revocation(client):
    """Revoking either token invalidates the whole grant; unknown tokens still give 200."""
    client_id, tokens = obtain_tokens(client)
    # The SDK's revocation model requires the client_secret field to be present even for
    # public clients, so an empty value is sent.
    form = {"client_id": client_id, "client_secret": ""}
    response = client.post("/revoke", data={**form, "token": tokens["refresh_token"]})
    assert response.status_code == 200
    assert client.get("/whoami", headers=bearer(tokens["access_token"])).json()["client_id"] is None
    refresh = client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
    )
    assert refresh.status_code == 400

    client_id, tokens = obtain_tokens(client)  # registers a fresh client
    form = {"client_id": client_id, "client_secret": ""}
    assert client.post("/revoke", data={**form, "token": tokens["access_token"]}).status_code == 200
    assert client.get("/whoami", headers=bearer(tokens["access_token"])).json()["client_id"] is None
    assert client.post("/revoke", data={**form, "token": "unknown"}).status_code == 200


# --------------------------------------------------------------------------- #
# 9: persistence
# --------------------------------------------------------------------------- #


def test_state_file_persists_clients_and_refresh_tokens(oauth_env):
    """A new provider on the same state file knows the client and the refresh token only."""
    first_client = build_app(oauth_env)[1]
    client_id, tokens = obtain_tokens(first_client)

    path = oauth_env["OAUTH_STATE_FILE"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    raw = Path(path).read_text(encoding="utf-8")
    assert client_id in raw
    assert tokens["access_token"] not in raw
    assert tokens["refresh_token"] not in raw  # only a digest is stored
    assert PASSWORD not in raw

    provider = SingleUserOAuthProvider(oauth_config_from_env(oauth_env))
    stored = asyncio.run(provider.get_client(client_id))
    assert stored is not None and stored.client_id == client_id
    assert asyncio.run(provider.load_refresh_token(stored, tokens["refresh_token"])) is not None
    assert asyncio.run(provider.load_access_token(tokens["access_token"])) is None

    # a "restarted" server accepts the refresh token over HTTP
    second_client = build_app(oauth_env)[1]
    assert second_client.get("/whoami", headers=bearer(tokens["access_token"])).json()["client_id"] is None
    response = second_client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
    )
    assert response.status_code == 200, response.text
    assert second_client.get("/whoami", headers=bearer(response.json()["access_token"])).json()["client_id"] == client_id


def test_corrupt_state_file_is_reported(oauth_env):
    """A state file that is not JSON raises a clear error instead of being overwritten."""
    with open(oauth_env["OAUTH_STATE_FILE"], "w", encoding="utf-8") as handle:
        handle.write("{not json")
    with pytest.raises(ValueError, match="OAUTH_STATE_FILE"):
        oauth_from_env(oauth_env)


# --------------------------------------------------------------------------- #
# 10: password hashing
# --------------------------------------------------------------------------- #


def test_hash_password_round_trip():
    """hash_password produces a pbkdf2_sha256 line that verify_password_hash accepts."""
    encoded = hash_password("s3cret", iterations=1000)
    assert encoded.startswith("pbkdf2_sha256$1000$")
    assert len(encoded.split("$")) == 4
    assert verify_password_hash("s3cret", encoded)
    assert not verify_password_hash("S3cret", encoded)
    assert not verify_password_hash("s3cret", "garbage")
    assert hash_password("s3cret", iterations=1000) != encoded  # fresh salt every time
    with pytest.raises(ValueError):
        hash_password("")


def test_login_with_password_hash_env(oauth_env):
    """OAUTH_PASSWORD_HASH works for the login form exactly like the plain password."""
    env = {k: v for k, v in oauth_env.items() if k != "OAUTH_PASSWORD"}
    env["OAUTH_PASSWORD_HASH"] = hash_password(PASSWORD, iterations=2000)
    env["OAUTH_USERNAME"] = "coach"
    hashed_client = build_app(env)[1]
    client_id = register(hashed_client)["client_id"]
    request_id = start_authorization(hashed_client, client_id, pkce_pair()[1])
    assert login(hashed_client, request_id, "wrong", username="coach").status_code == 401
    assert login(hashed_client, request_id, PASSWORD, username="athlete").status_code == 401
    assert login(hashed_client, request_id, PASSWORD, username="coach").status_code == 302


def test_cli_hash_password(capsys, monkeypatch):
    """The hash-password command prints a verifiable hash from --password or stdin."""
    assert auth.main(["hash-password", "--password", "cli-secret", "--iterations", "1000"]) == 0
    line = capsys.readouterr().out.strip()
    assert verify_password_hash("cli-secret", line)

    monkeypatch.setattr(sys, "stdin", io.StringIO("piped secret\n"))
    assert auth.main(["hash-password", "--iterations", "1000"]) == 0
    assert verify_password_hash("piped secret", capsys.readouterr().out.strip())

    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    with pytest.raises(SystemExit):
        auth.main(["hash-password"])


# --------------------------------------------------------------------------- #
# 11-12: environment handling and status
# --------------------------------------------------------------------------- #


def test_oauth_from_env_disabled_by_default():
    """Without MCP_AUTH=oauth no auth arguments are produced."""
    assert not oauth_from_env({})
    assert not oauth_from_env({"MCP_AUTH": "none", "OAUTH_PASSWORD": "x"})
    assert not oauth_from_env({"MCP_AUTH": "  "})


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"MCP_AUTH": "oauth"}, "MCP_PUBLIC_URL"),
        ({"MCP_AUTH": "oauth", "MCP_PUBLIC_URL": "https://mcp.example.com"}, "OAUTH_PASSWORD"),
        ({"MCP_AUTH": "oauth", "MCP_PUBLIC_URL": "http://mcp.example.com", "OAUTH_PASSWORD": "x"}, "https"),
        ({"MCP_AUTH": "oauth", "MCP_PUBLIC_URL": "https://mcp.example.com?x=1", "OAUTH_PASSWORD": "x"}, "query"),
        ({"MCP_AUTH": "oauth", "MCP_PUBLIC_URL": "https://a", "OAUTH_PASSWORD": "x", "OAUTH_PASSWORD_HASH": "y"}, "not both"),
        ({"MCP_AUTH": "oauth", "MCP_PUBLIC_URL": "https://a", "OAUTH_PASSWORD_HASH": "md5$1$a$b"}, "pbkdf2_sha256"),
        ({"MCP_AUTH": "oauth", "MCP_PUBLIC_URL": "https://a", "OAUTH_PASSWORD": "x", "OAUTH_ACCESS_TOKEN_TTL": "0"}, "OAUTH_ACCESS_TOKEN_TTL"),
        ({"MCP_AUTH": "oauth", "MCP_PUBLIC_URL": "https://a", "OAUTH_PASSWORD": "x", "OAUTH_LOGIN_RATE_LIMIT": "x"}, "OAUTH_LOGIN_RATE_LIMIT"),
        ({"MCP_AUTH": "basic"}, "MCP_AUTH"),
    ],
)
def test_half_configuration_raises(env, message, tmp_path):
    """Incomplete or inconsistent settings raise a ValueError naming the variable."""
    env = {"OAUTH_STATE_FILE": str(tmp_path / "state.json"), **env}
    with pytest.raises(ValueError, match=message):
        oauth_from_env(env)


def test_oauth_from_env_reads_process_environment(monkeypatch, tmp_path):
    """Without an explicit mapping the process environment is used."""
    monkeypatch.setenv("MCP_AUTH", "oauth")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://mcp.example.com/secret-path")
    monkeypatch.setenv("OAUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("OAUTH_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("OAUTH_ACCESS_TOKEN_TTL", "120")
    kwargs = oauth_from_env()
    provider = kwargs["auth_server_provider"]
    assert isinstance(provider, SingleUserOAuthProvider)
    assert provider.login_url == "https://mcp.example.com/secret-path/oauth/login"
    assert provider.config.access_token_ttl == 120
    settings = kwargs["auth"]
    assert str(settings.issuer_url) == "https://mcp.example.com/secret-path"
    assert str(settings.resource_server_url) == "https://mcp.example.com/secret-path"
    assert settings.required_scopes == ["mcp"]
    assert settings.client_registration_options.enabled
    assert settings.revocation_options.enabled


def test_auth_status_never_contains_password(oauth_env):
    """The status dictionary describes the mode without leaking secrets."""
    status = auth_status_from_env(oauth_env, include_private=True)
    assert status["mode"] == "oauth"
    assert status["issuer"] == ISSUER
    assert status["state_file"] == oauth_env["OAUTH_STATE_FILE"]
    assert status["password_source"] == "plain"
    assert PASSWORD not in json.dumps(status)
    assert auth_status_from_env({}, include_private=True) == {"mode": "none", "issuer": None, "state_file": None}
    hashed = auth_status_from_env({"MCP_AUTH": "oauth", "OAUTH_PASSWORD_HASH": hash_password("x", 1)}, include_private=True)
    assert hashed["password_source"] == "hash" and "x" not in json.dumps(hashed).replace('"', "")


def test_auth_status_for_clients_omits_deployment_details(oauth_env):
    """SEC-11: the status an MCP client can read names no user name, state file or athlete list."""
    env = {**oauth_env, "OAUTH_USERNAME": "coach", "OAUTH_ALLOWED_ATHLETES": "i42,i43"}
    status = auth_status_from_env(env)
    assert status["mode"] == "oauth" and status["login"] == ["password"]
    for private in ("state_file", "username", "password_source", "allowed_athletes"):
        assert private not in status
    text = json.dumps(status)
    assert "coach" not in text and "42" not in text and oauth_env["OAUTH_STATE_FILE"] not in text
    assert auth_status_from_env({}) == {"mode": "none", "issuer": None}


# --------------------------------------------------------------------------- #
# Provider-level expiry behaviour with a fake clock
# --------------------------------------------------------------------------- #


def test_codes_and_login_requests_expire(oauth_env):
    """Pending logins expire after 10 minutes and codes after 5 minutes."""
    now = [1_000_000.0]
    provider = SingleUserOAuthProvider(oauth_config_from_env(oauth_env), clock=lambda: now[0])
    client_info = asyncio.run(provider.get_client("missing"))
    assert client_info is None

    from mcp.shared.auth import OAuthClientInformationFull  # pylint: disable=import-outside-toplevel

    registered = OAuthClientInformationFull(
        client_id="c1", client_id_issued_at=1, redirect_uris=[AnyUrl(REDIRECT_URI)],
        token_endpoint_auth_method="none", scope="mcp",
    )
    asyncio.run(provider.register_client(registered))
    params = AuthorizationParams(
        state="s", scopes=["mcp"], code_challenge="challenge", redirect_uri=AnyUrl(REDIRECT_URI),
        redirect_uri_provided_explicitly=True, resource=None,
    )
    request_id = parse_qs(urlsplit(asyncio.run(provider.authorize(registered, params))).query)["request"][0]
    now[0] += 601
    assert provider.pending_login(request_id) is None

    request_id = parse_qs(urlsplit(asyncio.run(provider.authorize(registered, params))).query)["request"][0]
    code = parse_qs(urlsplit(provider.complete_login(request_id, "ip")).query)["code"][0]
    assert asyncio.run(provider.load_authorization_code(registered, code)) is not None
    now[0] += 301
    assert asyncio.run(provider.load_authorization_code(registered, code)) is None


# --------------------------------------------------------------------------- #
# Hardening: scope narrowing on refresh, registration limits, log values
# --------------------------------------------------------------------------- #


def obtain_granted_tokens(client: TestClient, grants: list[str]) -> tuple[str, dict[str, Any]]:
    """Like obtain_tokens, but tick the given permission classes on the consent page."""
    client_id = register(client)["client_id"]
    verifier, challenge = pkce_pair()
    request_id = start_authorization(client, client_id, challenge)
    redirect = submit_consent(
        client,
        {"request": request_id, "username": "athlete", "password": PASSWORD, "grant": grants},
    )
    assert redirect.status_code == 302, redirect.text
    query = parse_qs(urlsplit(redirect.headers["location"]).query)
    tokens = exchange_code(client, client_id, query["code"][0], verifier)
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


def refresh(client: TestClient, client_id: str, refresh_token: str, scope: str | None = None) -> dict[str, Any]:
    """Run the refresh grant, optionally narrowing the scope."""
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    if scope is not None:
        data["scope"] = scope
    response = client.post("/token", data=data)
    assert response.status_code == 200, response.text
    return response.json()


def test_refresh_with_bare_mcp_scope_keeps_the_granted_permissions(oauth_env):
    """Narrowing to ``mcp`` on refresh must not turn a read-only grant into MCP_PERMISSIONS."""
    client = build_app({**oauth_env, "MCP_PERMISSIONS": "read,write,destructive"})[1]
    client_id, tokens = obtain_granted_tokens(client, [])
    scopes = client.get("/whoami", headers=bearer(tokens["access_token"])).json()["scopes"]
    assert sorted(scopes) == ["intervals:read", "mcp"]

    narrowed = refresh(client, client_id, tokens["refresh_token"], scope="mcp")
    assert sorted(client.get("/whoami", headers=bearer(narrowed["access_token"])).json()["scopes"]) == [
        "intervals:read",
        "mcp",
    ]
    again = refresh(client, client_id, narrowed["refresh_token"])
    assert sorted(client.get("/whoami", headers=bearer(again["access_token"])).json()["scopes"]) == [
        "intervals:read",
        "mcp",
    ]


def test_refresh_can_still_narrow_permission_scopes(oauth_env):
    """A client may drop write on refresh; it can never add a class it was not granted."""
    client = build_app({**oauth_env, "MCP_PERMISSIONS": "read,write"})[1]
    client_id, tokens = obtain_granted_tokens(client, ["write"])
    assert "intervals:write" in client.get("/whoami", headers=bearer(tokens["access_token"])).json()["scopes"]
    narrowed = refresh(client, client_id, tokens["refresh_token"], scope="mcp intervals:read")
    assert sorted(client.get("/whoami", headers=bearer(narrowed["access_token"])).json()["scopes"]) == [
        "intervals:read",
        "mcp",
    ]
    widened = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": narrowed["refresh_token"],
            "client_id": client_id,
            "scope": "mcp intervals:read intervals:write",
        },
    )
    assert widened.status_code == 400


@pytest.mark.parametrize(
    "extra",
    [
        {"client_name": "A" * 101},
        {"client_name": "ChatGPT\nforged log line"},
        {"redirect_uris": [REDIRECT_URI] * 11},
        {"contacts": ["x" * 9000 + "@example.com"]},
    ],
    ids=["long-name", "control-chars", "many-redirects", "oversized"],
)
def test_registration_rejects_oversized_or_unprintable_metadata(client, extra):
    """/register is unauthenticated: names, redirect lists and total size are bounded."""
    response = client.post("/register", json={**CHATGPT_REGISTRATION, **extra})
    assert response.status_code == 400, response.text
    assert response.json()["error"] in ("invalid_client_metadata", "invalid_redirect_uri")
    assert register(client)["client_name"] == "ChatGPT"


def test_log_safe_clips_and_escapes():
    """Client-supplied values are escaped and clipped before they reach a log line."""
    from intervals_mcp_server.auth_clients import log_safe  # pylint: disable=import-outside-toplevel

    assert log_safe("ChatGPT") == "ChatGPT"
    assert log_safe("a\nb") == "a\\nb"
    clipped = log_safe("x" * 1000)
    assert clipped.startswith("x" * 120) and clipped.endswith("(+880 chars)") and len(clipped) < 140
