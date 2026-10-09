# pylint: disable=missing-function-docstring,too-few-public-methods
"""Tests for the OAuth extensions: sign-in with Intervals.icu, consent with permission
scopes, client metadata documents with private_key_jwt, RFC 9207 ``iss``, redirect host
allowlist, audience checks and the combined streamable HTTP + SSE app.

No network access: client metadata documents, JWKS and the Intervals.icu token endpoint
are replaced by in-memory fakes.
"""

import asyncio
import base64
import hashlib
import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from intervals_mcp_server import mcp_instance
from intervals_mcp_server.auth import (
    SingleUserOAuthProvider,
    auth_settings,
    granted_classes,
    install_login_routes,
    normalize_athlete_id,
    oauth_config_from_env,
)
from intervals_mcp_server.auth_clients import ClientAssertionVerifier, ClientMetadataResolver
from intervals_mcp_server.http_app import add_issuer, build_http_app
from intervals_mcp_server.mcp_instance import IntervalsFastMCP

ISSUER = "http://localhost"
CHATGPT_ID = "https://chatgpt.com/oauth/client.json"
CHATGPT_JWKS = "https://chatgpt.com/oauth/jwks.json"
STABLE_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
DCR_REDIRECT = "https://chatgpt.com/connector/oauth/abc123"
PASSWORD = "correct horse battery staple"

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
PUBLIC_JWK.update({"kid": "test-key", "use": "sig", "alg": "RS256"})

CHATGPT_DOC = {
    "client_id": CHATGPT_ID,
    "client_uri": "https://chatgpt.com/",
    "redirect_uris": [STABLE_REDIRECT],
    "token_endpoint_auth_method": "private_key_jwt",
    "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "client_name": "ChatGPT",
    "token_endpoint_auth_signing_alg": "RS256",
    "jwks_uri": CHATGPT_JWKS,
}


class FakeWeb:
    """In-memory replacement for the HTTPS fetches of the resolver and verifier."""

    def __init__(self, documents: dict[str, Any] | None = None) -> None:
        self.documents = {CHATGPT_ID: CHATGPT_DOC, CHATGPT_JWKS: {"keys": [PUBLIC_JWK]}}
        self.documents.update(documents or {})
        self.calls: list[str] = []

    async def __call__(self, url: str) -> tuple[int, dict[str, str], bytes]:
        self.calls.append(url)
        if url not in self.documents:
            return 404, {}, b"{}"
        return 200, {"cache-control": "max-age=300"}, json.dumps(self.documents[url]).encode()


class FakeIntervals:
    """Replacement for the Intervals.icu token endpoint."""

    def __init__(self, athlete_id: str = "219504") -> None:
        self.athlete_id = athlete_id
        self.codes: list[str] = []

    async def __call__(self, code: str) -> dict[str, Any]:
        self.codes.append(code)
        return {
            "token_type": "Bearer",
            "access_token": "upstream-secret-token",
            "scope": "ACTIVITY:READ",
            "athlete": {"id": self.athlete_id, "name": "Test Athlete"},
        }


def make_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "MCP_AUTH": "oauth",
        "MCP_PUBLIC_URL": ISSUER,
        "OAUTH_PASSWORD": PASSWORD,
        "OAUTH_STATE_FILE": str(tmp_path / "state.json"),
        "MCP_PERMISSIONS": "read,write",
        "ATHLETE_ID": "i219504",
    }
    env.update(extra)
    return env


def make_app(env: dict[str, str], web: FakeWeb | None = None, intervals: FakeIntervals | None = None, transport: str = "http+sse"):
    config = oauth_config_from_env(env)
    provider = SingleUserOAuthProvider(config, fetch=web or FakeWeb(), intervals_exchange=intervals or FakeIntervals())
    mcp = IntervalsFastMCP("test", auth_server_provider=provider, auth=auth_settings(config))

    @mcp.tool()
    def ping() -> str:
        """Dummy tool."""
        return "pong"

    install_login_routes(mcp, provider)
    app = build_http_app(mcp, transport, provider=provider)
    return mcp, provider, TestClient(app, base_url="http://127.0.0.1:8000", follow_redirects=False)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorize(client: TestClient, client_id: str, redirect_uri: str, challenge: str, **extra: str):
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st4te",
        "resource": ISSUER + "/",
    }
    params.update(extra)
    return client.get("/authorize", params=params)


def request_id_from(response) -> str:
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert location.path == "/oauth/login"
    return parse_qs(location.query)["request"][0]


def query_of(response) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}


def client_assertion(audience: str = ISSUER + "/token", **overrides: Any) -> str:
    now = int(time.time())
    claims = {"iss": CHATGPT_ID, "sub": CHATGPT_ID, "aud": audience, "iat": now, "exp": now + 60, "jti": uuid.uuid4().hex}
    claims.update(overrides)
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "test-key"})


def register(client: TestClient, redirect_uri: str = DCR_REDIRECT) -> str:
    response = client.post(
        "/register",
        json={"client_name": "ChatGPT", "redirect_uris": [redirect_uri], "token_endpoint_auth_method": "none"},
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #


def test_metadata_advertises_cimd_iss_and_permission_scopes(tmp_path):
    _, _, client = make_app(make_env(tmp_path))
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["client_id_metadata_document_supported"] is True
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert metadata["token_endpoint_auth_methods_supported"] == ["none", "client_secret_post", "private_key_jwt"]
    assert "RS256" in metadata["token_endpoint_auth_signing_alg_values_supported"]
    assert metadata["scopes_supported"] == ["mcp", "intervals:read", "intervals:write"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["registration_endpoint"] == ISSUER + "/register"
    resource = client.get("/.well-known/oauth-protected-resource").json()
    assert resource["scopes_supported"] == ["mcp", "intervals:read", "intervals:write"]
    assert resource["authorization_servers"] == [metadata["issuer"]]


def test_metadata_without_cimd_and_registration(tmp_path):
    env = make_env(tmp_path, OAUTH_CLIENT_HOSTS="none", OAUTH_DYNAMIC_REGISTRATION="false")
    _, _, client = make_app(env)
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["client_id_metadata_document_supported"] is False
    assert "registration_endpoint" not in metadata
    assert "private_key_jwt" not in metadata["token_endpoint_auth_methods_supported"]
    assert client.post("/register", json={"redirect_uris": [DCR_REDIRECT]}).status_code in (404, 405)


# --------------------------------------------------------------------------- #
# Client metadata documents + private_key_jwt
# --------------------------------------------------------------------------- #


def test_cimd_client_full_flow_with_private_key_jwt(tmp_path):
    web = FakeWeb()
    _, _, client = make_app(make_env(tmp_path), web=web)
    verifier, challenge = pkce_pair()
    request_id = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge, scope="mcp intervals:read intervals:write"))
    page = client.get("/oauth/login", params={"request": request_id}).text
    assert "ChatGPT" in page and "verified: chatgpt.com" in page
    assert 'value="write"' in page and 'value="destructive"' not in page

    redirect = client.post(
        "/oauth/login",
        data={"request": request_id, "action": "password", "username": "athlete", "password": PASSWORD, "grant": ["read", "write"]},
    )
    assert redirect.status_code == 302
    assert redirect.headers["location"].startswith(STABLE_REDIRECT + "?")
    query = query_of(redirect)
    assert query["state"] == "st4te" and query["iss"] == ISSUER + "/"

    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"],
            "redirect_uri": STABLE_REDIRECT,
            "code_verifier": verifier,
            "client_id": CHATGPT_ID,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion(),
            "resource": ISSUER + "/",
        },
    )
    assert token.status_code == 200, token.text
    assert token.json()["scope"] == "mcp intervals:read intervals:write"
    assert CHATGPT_ID in web.calls and CHATGPT_JWKS in web.calls


def test_private_key_jwt_rejections(tmp_path):
    _, provider, client = make_app(make_env(tmp_path))
    verifier_obj = ClientAssertionVerifier(provider.metadata_clients, [ISSUER + "/token"])

    def run(assertion: str, hint: str | None = CHATGPT_ID) -> str:
        return asyncio.run(verifier_obj.verify(assertion, hint))

    assert run(client_assertion()) == CHATGPT_ID
    reused = client_assertion(jti="same")
    assert run(reused) == CHATGPT_ID
    with pytest.raises(ValueError, match="already used"):
        run(reused)
    with pytest.raises(ValueError, match="rejected"):
        run(client_assertion(audience="https://elsewhere.example/token"))
    with pytest.raises(ValueError, match="rejected"):
        run(client_assertion(exp=int(time.time()) - 300))
    with pytest.raises(ValueError, match="too long"):
        run(client_assertion(exp=int(time.time()) + 3600))
    with pytest.raises(ValueError, match="iss and sub"):
        run(client_assertion(sub="someone-else"))
    with pytest.raises(ValueError, match="does not belong"):
        run(client_assertion(), hint="https://claude.ai/oauth/client.json")
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {"iss": CHATGPT_ID, "sub": CHATGPT_ID, "aud": ISSUER + "/token", "exp": int(time.time()) + 60},
        other_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    with pytest.raises(ValueError, match="rejected"):
        run(forged)
    # Through the token endpoint a bad assertion is invalid_client.
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": "x",
            "code_verifier": "y",
            "client_id": CHATGPT_ID,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": forged,
        },
    )
    assert response.status_code == 401 and response.json()["error"] == "invalid_client"


def test_cimd_public_client_without_assertion(tmp_path):
    """ChatGPT may also authenticate as a public client; PKCE protects the code."""
    _, _, client = make_app(make_env(tmp_path))
    verifier, challenge = pkce_pair()
    request_id = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge))
    redirect = client.post("/oauth/login", data={"request": request_id, "action": "password", "username": "athlete", "password": PASSWORD})
    code = query_of(redirect)["code"]
    token = client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": STABLE_REDIRECT, "code_verifier": verifier, "client_id": CHATGPT_ID},
    )
    assert token.status_code == 200, token.text
    assert token.json()["scope"] == "mcp intervals:read"


def test_cimd_document_validation():
    bad_doc = dict(CHATGPT_DOC, client_id="https://chatgpt.com/other.json")
    resolver = ClientMetadataResolver(["chatgpt.com"], "mcp", fetch=FakeWeb({CHATGPT_ID: bad_doc}))
    assert asyncio.run(resolver.get(CHATGPT_ID)) is None
    resolver = ClientMetadataResolver(["chatgpt.com"], "mcp", fetch=FakeWeb())
    assert asyncio.run(resolver.get("https://evil.example/client.json")) is None
    assert not resolver.url_allowed("https://chatgpt.com/")
    assert not resolver.url_allowed("https://chatgpt.com:8443/oauth/client.json")
    assert not resolver.url_allowed("http://chatgpt.com/oauth/client.json")
    client_info = asyncio.run(resolver.get(CHATGPT_ID))
    assert client_info is not None and client_info.client_name == "ChatGPT"
    assert [str(u) for u in client_info.redirect_uris or []] == [STABLE_REDIRECT]
    wrong_jwks = dict(CHATGPT_DOC, jwks_uri="https://keys.example/jwks.json")
    resolver = ClientMetadataResolver(["chatgpt.com"], "mcp", fetch=FakeWeb({CHATGPT_ID: wrong_jwks}))
    assert asyncio.run(resolver.get(CHATGPT_ID)) is None


def test_cimd_client_cannot_use_unlisted_redirect(tmp_path):
    _, _, client = make_app(make_env(tmp_path))
    response = authorize(client, CHATGPT_ID, "https://chatgpt.com/connector/oauth/other", pkce_pair()[1])
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# iss, deny, audience, redirect hosts
# --------------------------------------------------------------------------- #


def test_error_redirects_from_the_sdk_carry_iss(tmp_path):
    _, _, client = make_app(make_env(tmp_path))
    client_id = register(client)
    response = authorize(client, client_id, DCR_REDIRECT, pkce_pair()[1], scope="mcp intervals:admin")
    assert response.status_code == 302
    query = query_of(response)
    assert query["error"] == "invalid_scope" and query["iss"] == ISSUER + "/"


def test_foreign_resource_is_rejected_with_iss(tmp_path):
    _, _, client = make_app(make_env(tmp_path))
    client_id = register(client)
    response = authorize(client, client_id, DCR_REDIRECT, pkce_pair()[1], resource="https://evil.example/mcp")
    query = query_of(response)
    assert query["error"] == "invalid_request" and query["iss"] == ISSUER + "/"


def test_deny_redirects_with_access_denied_and_iss(tmp_path):
    _, _, client = make_app(make_env(tmp_path))
    client_id = register(client)
    request_id = request_id_from(authorize(client, client_id, DCR_REDIRECT, pkce_pair()[1]))
    response = client.post("/oauth/login", data={"request": request_id, "action": "deny"})
    query = query_of(response)
    assert query == {
        "error": "access_denied",
        "error_description": "The athlete denied the request",
        "state": "st4te",
        "iss": ISSUER + "/",
    }
    assert client.get("/oauth/login", params={"request": request_id}).status_code == 400


def test_registration_redirect_host_allowlist(tmp_path):
    _, _, client = make_app(make_env(tmp_path))
    rejected = client.post("/register", json={"redirect_uris": ["https://evil.example/cb"], "token_endpoint_auth_method": "none"})
    assert rejected.status_code == 400 and "allowed host" in rejected.json()["error_description"]
    assert register(client, "http://127.0.0.1:3000/callback")
    _, _, open_client = make_app(make_env(tmp_path / "open", OAUTH_REDIRECT_HOSTS="*"))
    assert register(open_client, "https://evil.example/cb")


def test_audience_is_checked_on_access_tokens(tmp_path):
    _, provider, _ = make_app(make_env(tmp_path))
    tokens = provider._issue_tokens("c1", ["mcp"], "g1", "https://elsewhere.example/")  # pylint: disable=protected-access
    assert asyncio.run(provider.load_access_token(tokens.access_token)) is None
    tokens = provider._issue_tokens("c1", ["mcp"], "g2", ISSUER + "/mcp")  # pylint: disable=protected-access
    assert asyncio.run(provider.load_access_token(tokens.access_token)) is not None


def test_add_issuer_only_touches_authorization_responses():
    assert add_issuer("https://c.example/cb?code=1&state=s", "https://i/") == "https://c.example/cb?code=1&state=s&iss=https%3A%2F%2Fi%2F"
    assert add_issuer("https://c.example/cb?error=x", "https://i/").endswith("iss=https%3A%2F%2Fi%2F")
    assert add_issuer("https://i/oauth/login?request=1", "https://i/") == "https://i/oauth/login?request=1"
    assert add_issuer("https://c.example/cb?code=1&iss=keep", "https://i/") == "https://c.example/cb?code=1&iss=keep"


# --------------------------------------------------------------------------- #
# Sign-in with Intervals.icu
# --------------------------------------------------------------------------- #


def intervals_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = make_env(tmp_path, OAUTH_LOGIN="intervals", INTERVALS_OAUTH_CLIENT_ID="77", INTERVALS_OAUTH_CLIENT_SECRET="s3cret", **extra)
    env.pop("OAUTH_PASSWORD")
    return env


def start_intervals(client: TestClient, grant: list[str] | None = None) -> tuple[str, str, dict[str, str]]:
    client_id = register(client)
    verifier, challenge = pkce_pair()
    request_id = request_id_from(authorize(client, client_id, DCR_REDIRECT, challenge))
    page = client.get("/oauth/login", params={"request": request_id}).text
    assert "Continue with Intervals.icu" in page and 'type="password"' not in page
    response = client.post("/oauth/login", data={"request": request_id, "action": "intervals", "grant": grant or ["read"]})
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == "https://intervals.icu/oauth/authorize"
    params = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert params["client_id"] == "77" and params["scope"] == "ACTIVITY:READ"
    assert params["redirect_uri"] == ISSUER + "/oauth/intervals/callback"
    assert "intervals_mcp_login" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]
    return client_id, verifier, params


def test_intervals_sign_in_happy_path(tmp_path):
    intervals = FakeIntervals("219504")
    _, _, client = make_app(intervals_env(tmp_path), intervals=intervals)
    client_id, verifier, params = start_intervals(client, ["read", "write"])
    callback = client.get("/oauth/intervals/callback", params={"code": "up-code", "state": params["state"]})
    assert callback.status_code == 302, callback.text
    assert callback.headers["location"].startswith(DCR_REDIRECT + "?")
    query = query_of(callback)
    assert query["state"] == "st4te" and query["iss"] == ISSUER + "/"
    assert intervals.codes == ["up-code"]
    token = client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": query["code"], "redirect_uri": DCR_REDIRECT, "code_verifier": verifier, "client_id": client_id},
    )
    assert token.status_code == 200 and token.json()["scope"] == "mcp intervals:read intervals:write"
    # The Intervals.icu token is never persisted.
    assert "upstream-secret-token" not in (tmp_path / "state.json").read_text()
    # The state is single use.
    assert client.get("/oauth/intervals/callback", params={"code": "again", "state": params["state"]}).status_code == 400


def test_intervals_sign_in_rejects_other_athletes(tmp_path):
    _, _, client = make_app(intervals_env(tmp_path), intervals=FakeIntervals("999"))
    _, _, params = start_intervals(client)
    callback = client.get("/oauth/intervals/callback", params={"code": "c", "state": params["state"]})
    assert callback.status_code == 403
    assert "not allowed" in callback.text and "location" not in callback.headers


def test_intervals_sign_in_needs_the_same_browser(tmp_path):
    _, _, client = make_app(intervals_env(tmp_path))
    _, _, params = start_intervals(client)
    client.cookies.clear()
    callback = client.get("/oauth/intervals/callback", params={"code": "c", "state": params["state"]})
    assert callback.status_code == 400 and "different browser" in callback.text


def test_intervals_sign_in_declined(tmp_path):
    _, _, client = make_app(intervals_env(tmp_path))
    _, _, params = start_intervals(client)
    callback = client.get("/oauth/intervals/callback", params={"error": "access_denied", "state": params["state"]})
    query = query_of(callback)
    assert query["error"] == "access_denied" and query["iss"] == ISSUER + "/"


def test_password_is_refused_when_only_intervals_sign_in_is_enabled(tmp_path):
    _, _, client = make_app(intervals_env(tmp_path, OAUTH_PASSWORD=PASSWORD))
    client_id = register(client)
    request_id = request_id_from(authorize(client, client_id, DCR_REDIRECT, pkce_pair()[1]))
    response = client.post("/oauth/login", data={"request": request_id, "action": "password", "username": "athlete", "password": PASSWORD})
    assert response.status_code == 400


def test_intervals_configuration_validation(tmp_path):
    with pytest.raises(ValueError, match="INTERVALS_OAUTH_CLIENT_ID"):
        oauth_config_from_env(make_env(tmp_path, OAUTH_LOGIN="intervals"))
    env = intervals_env(tmp_path)
    env.pop("ATHLETE_ID")
    with pytest.raises(ValueError, match="OAUTH_ALLOWED_ATHLETES"):
        oauth_config_from_env(env)
    config = oauth_config_from_env(intervals_env(tmp_path, OAUTH_ALLOWED_ATHLETES="i1, I2 ,3"))
    assert config.allowed_athletes == frozenset({"1", "2", "3"})
    assert config.login_methods == ("intervals",)
    assert oauth_config_from_env(make_env(tmp_path, INTERVALS_OAUTH_CLIENT_ID="1", INTERVALS_OAUTH_CLIENT_SECRET="x")).login_methods == ("intervals",)
    with pytest.raises(ValueError, match="does not accept"):
        oauth_config_from_env(make_env(tmp_path, OAUTH_CLIENT_HOSTS="*"))
    with pytest.raises(ValueError, match="OAUTH_LOGIN"):
        oauth_config_from_env(make_env(tmp_path, OAUTH_LOGIN="github"))
    assert normalize_athlete_id("i219504") == normalize_athlete_id(219504) == "219504"


# --------------------------------------------------------------------------- #
# Permission scopes on tool calls
# --------------------------------------------------------------------------- #


def test_granted_classes():
    assert granted_classes(["mcp"]) is None
    assert granted_classes(None) is None
    assert granted_classes(["mcp", "intervals:read", "intervals:write"]) == {"read", "write"}


def test_tool_scopes_filter_list_and_call(monkeypatch):
    server = IntervalsFastMCP("scopes")

    @server.tool()
    def read_tool() -> str:
        """Read."""
        return "r"

    @server.tool()
    def write_tool() -> str:
        """Write."""
        return "w"

    monkeypatch.setitem(mcp_instance._TOOL_PERMISSIONS, "read_tool", "read")  # pylint: disable=protected-access
    monkeypatch.setitem(mcp_instance._TOOL_PERMISSIONS, "write_tool", "write")  # pylint: disable=protected-access

    async def as_token(scopes: list[str] | None):
        token = None
        if scopes is not None:
            token = auth_context_var.set(AuthenticatedUser(AccessToken(token="t", client_id="c", scopes=scopes)))
        try:
            names = sorted(t.name for t in await server.list_tools())
            try:
                await server.call_tool("write_tool", {})
                write = "ok"
            except ToolError as exc:
                write = str(exc)
            return names, write
        finally:
            if token is not None:
                auth_context_var.reset(token)

    names, write = asyncio.run(as_token(None))
    assert names == ["read_tool", "write_tool"] and write == "ok"
    names, write = asyncio.run(as_token(["mcp"]))
    assert names == ["read_tool", "write_tool"] and write == "ok"
    names, write = asyncio.run(as_token(["mcp", "intervals:read"]))
    assert names == ["read_tool"]
    assert "needs the 'write' permission" in write
    names, write = asyncio.run(as_token(["mcp", "intervals:read", "intervals:write"]))
    assert names == ["read_tool", "write_tool"] and write == "ok"


# --------------------------------------------------------------------------- #
# Combined transports
# --------------------------------------------------------------------------- #


def test_combined_app_protects_both_transports(tmp_path):
    _, _, client = make_app(make_env(tmp_path))
    paths = {getattr(r, "path", None) for r in client.app.router.routes}  # type: ignore[attr-defined]
    assert {"/mcp", "/sse", "/messages", "/authorize", "/oauth/login", "/oauth/intervals/callback"} <= paths
    for path, method in (("/mcp", "post"), ("/sse", "get"), ("/messages/", "post")):
        response = getattr(client, method)(path)
        assert response.status_code == 401, path
        assert "resource_metadata" in response.headers.get("www-authenticate", "")


def test_combined_app_serves_mcp_with_bearer(tmp_path):
    _, provider, client = make_app(make_env(tmp_path))
    tokens = provider._issue_tokens("c1", ["mcp", "intervals:read"], "g", ISSUER + "/")  # pylint: disable=protected-access
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    with TestClient(client.app, base_url="http://127.0.0.1:8000", follow_redirects=False) as http:
        response = http.post("/mcp", headers=headers, json=init)
        assert response.status_code == 200, response.text
        assert "serverInfo" in response.text


def test_build_http_app_rejects_unknown_transport(tmp_path):
    with pytest.raises(ValueError, match="transport"):
        make_app(make_env(tmp_path), transport="websocket")


def test_public_client_can_revoke_without_client_secret(tmp_path):
    """The SDK requires client_secret on /revoke; public clients must not need it."""
    _, provider, client = make_app(make_env(tmp_path))
    client_id = register(client)
    tokens = provider._issue_tokens(client_id, ["mcp"], "grant", ISSUER + "/")  # pylint: disable=protected-access
    assert asyncio.run(provider.load_access_token(tokens.access_token)) is not None
    response = client.post("/revoke", data={"token": tokens.refresh_token, "client_id": client_id})
    assert response.status_code == 200, response.text
    assert asyncio.run(provider.load_access_token(tokens.access_token)) is None
    confidential = client.post(
        "/register",
        json={"redirect_uris": [DCR_REDIRECT], "token_endpoint_auth_method": "client_secret_post"},
    ).json()
    other = provider._issue_tokens(confidential["client_id"], ["mcp"], "g2", None)  # pylint: disable=protected-access
    refused = client.post("/revoke", data={"token": other.refresh_token, "client_id": confidential["client_id"]})
    assert refused.status_code == 401
    assert asyncio.run(provider.load_access_token(other.access_token)) is not None


def test_transport_security_allows_the_public_host(tmp_path):
    """Behind a reverse proxy that keeps the Host header, the public host must be accepted."""
    from intervals_mcp_server.mcp_instance import transport_security_from_env  # pylint: disable=import-outside-toplevel

    assert transport_security_from_env({}) is None
    security = transport_security_from_env({"MCP_PUBLIC_URL": "https://mcp.example.com", "FASTMCP_ALLOWED_HOSTS": "legacy.example.com"})
    assert security is not None and security.enable_dns_rebinding_protection
    assert set(security.allowed_hosts) >= {"127.0.0.1:*", "mcp.example.com", "legacy.example.com"}
    assert set(security.allowed_origins) >= {"http://127.0.0.1:*", "https://mcp.example.com"}
    off = transport_security_from_env({"FASTMCP_ALLOWED_HOSTS": "*"})
    assert off is not None and not off.enable_dns_rebinding_protection

    config = oauth_config_from_env(make_env(tmp_path))
    provider = SingleUserOAuthProvider(config, fetch=FakeWeb(), intervals_exchange=FakeIntervals())
    mcp = IntervalsFastMCP(
        "hosts", auth_server_provider=provider, auth=auth_settings(config),
        transport_security=transport_security_from_env({"MCP_PUBLIC_URL": "https://mcp.example.com"}),
    )
    install_login_routes(mcp, provider)
    app = build_http_app(mcp, "http+sse", provider=provider)
    with TestClient(app, base_url="https://mcp.example.com", follow_redirects=False) as public:
        assert public.post("/mcp", json={}).status_code == 401  # host accepted, token missing
        tokens = provider._issue_tokens("c", ["mcp"], "g", None)  # pylint: disable=protected-access
        headers = {"Authorization": f"Bearer {tokens.access_token}", "Accept": "application/json, text/event-stream"}
        assert public.post("/mcp", json={}, headers=headers | {"Host": "evil.example.com"}).status_code == 421


def test_tools_carry_annotations_per_permission_class():
    """Read tools are marked read-only, deletions and admin tools destructive (client confirmation)."""
    from intervals_mcp_server.mcp_instance import PERMISSION_ANNOTATIONS, mcp, tool_permissions  # pylint: disable=import-outside-toplevel

    registered = {t.name: t for t in asyncio.run(mcp.list_tools())}
    classes = tool_permissions()
    assert registered, "no tools registered"
    for name, info in registered.items():
        expected = PERMISSION_ANNOTATIONS[classes.get(name, "read")]
        annotations = info.annotations
        assert annotations is not None, name
        assert annotations.readOnlyHint == expected["readOnlyHint"], name
        assert annotations.openWorldHint is False, name
    assert registered["get_activity_details"].annotations.readOnlyHint is True
    assert PERMISSION_ANNOTATIONS["destructive"]["destructiveHint"] is True
    assert PERMISSION_ANNOTATIONS["write"]["destructiveHint"] is False
