# pylint: disable=missing-function-docstring
"""Sign-in options of the OAuth server: Intervals.icu API key (zero-config default) and an
optional TOTP second factor for the password and API-key sign-ins."""

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from intervals_mcp_server import auth
from intervals_mcp_server.auth import (
    SingleUserOAuthProvider,
    auth_settings,
    auth_status_from_env,
    install_login_routes,
    oauth_config_from_env,
)
from intervals_mcp_server.auth_totp import generate_secret, match_counter, normalize_secret, provisioning_uri, totp
from intervals_mcp_server.http_app import build_http_app
from intervals_mcp_server.mcp_instance import IntervalsFastMCP

ISSUER = "http://localhost"
API_KEY = "test-api-key-7f3c2a9b1e"
REDIRECT = "https://chatgpt.com/connector/oauth/abc123"
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")  # RFC 6238 test key


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "MCP_AUTH": "oauth",
        "MCP_PUBLIC_URL": ISSUER,
        "OAUTH_STATE_FILE": str(tmp_path / "state.json"),
        "API_KEY": API_KEY,
        "ATHLETE_ID": "i1",
    }
    env.update(extra)
    return env


def make_client(env: dict[str, str], clock: Clock | None = None) -> tuple[SingleUserOAuthProvider, TestClient]:
    config = oauth_config_from_env(env)
    # The SDK checks code expiry against the real clock, so the default is real time.
    provider = SingleUserOAuthProvider(config, clock=clock or time.time)
    mcp = IntervalsFastMCP("login", auth_server_provider=provider, auth=auth_settings(config))
    install_login_routes(mcp, provider)
    app = build_http_app(mcp, "http+sse", provider=provider)
    return provider, TestClient(app, base_url="http://127.0.0.1:8000", follow_redirects=False)


def start(client: TestClient) -> tuple[str, str, str]:
    """Register, authorize; return (client_id, verifier, request_id)."""
    registration = client.post("/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"})
    assert registration.status_code == 201, registration.text
    client_id = registration.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    response = client.get(
        "/authorize",
        params={"client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
                "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"},
    )
    assert response.status_code == 302, response.text
    return client_id, verifier, parse_qs(urlsplit(response.headers["location"]).query)["request"][0]


def query(response: Any) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_default_sign_in_method_order(tmp_path):
    assert oauth_config_from_env(make_env(tmp_path)).login_methods == ("apikey",)
    assert oauth_config_from_env(make_env(tmp_path, OAUTH_PASSWORD="pw")).login_methods == ("password",)
    with_app = make_env(tmp_path, INTERVALS_OAUTH_CLIENT_ID="1", INTERVALS_OAUTH_CLIENT_SECRET="s")
    assert oauth_config_from_env(with_app).login_methods == ("intervals",)
    combined = oauth_config_from_env(make_env(tmp_path, OAUTH_LOGIN="apikey,password", OAUTH_PASSWORD="pw"))
    assert combined.login_methods == ("apikey", "password")


def test_api_key_sign_in_needs_the_api_key(tmp_path):
    env = make_env(tmp_path)
    env.pop("API_KEY")
    with pytest.raises(ValueError) as error:
        oauth_config_from_env(env)
    assert "API_KEY" in str(error.value) and "OAUTH_PASSWORD" in str(error.value)


def test_config_keeps_only_a_digest_of_the_api_key(tmp_path):
    config = oauth_config_from_env(make_env(tmp_path))
    assert API_KEY not in repr(config)
    assert config.verify_api_key(API_KEY) and config.verify_api_key(f"  {API_KEY} ")
    assert not config.verify_api_key("wrong") and not config.verify_api_key("")
    password_only = oauth_config_from_env(make_env(tmp_path, OAUTH_LOGIN="password", OAUTH_PASSWORD="pw"))
    assert not password_only.verify_api_key(API_KEY)


def test_status_reports_methods_and_second_factor(tmp_path):
    status = auth_status_from_env(make_env(tmp_path, OAUTH_TOTP_SECRET=generate_secret()))
    assert status["login"] == ["apikey"] and status["second_factor"] == "totp"
    assert API_KEY not in json.dumps(status)
    assert auth_status_from_env(make_env(tmp_path))["second_factor"] == "none"


# --------------------------------------------------------------------------- #
# Sign-in with the API key
# --------------------------------------------------------------------------- #


def test_api_key_sign_in_flow(tmp_path):
    _, client = make_client(make_env(tmp_path, MCP_PERMISSIONS="read,write"))
    client_id, verifier, request_id = start(client)
    page = client.get("/oauth/login", params={"request": request_id}).text
    assert 'name="api_key"' in page and "Sign in with API key" in page
    assert 'name="password"' not in page and 'name="totp"' not in page and API_KEY not in page
    response = client.post("/oauth/login", data={"request": request_id, "action": "apikey", "api_key": API_KEY, "grant": ["read", "write"]})
    assert response.status_code == 302 and response.headers["location"].startswith(REDIRECT)
    token = client.post("/token", data={"grant_type": "authorization_code", "code": query(response)["code"],
                                        "redirect_uri": REDIRECT, "client_id": client_id, "code_verifier": verifier})
    assert token.status_code == 200 and token.json()["scope"] == "mcp intervals:read intervals:write"


def test_wrong_api_key_is_rejected_and_rate_limited(tmp_path):
    _, client = make_client(make_env(tmp_path, OAUTH_LOGIN_RATE_LIMIT="3"))
    _, _, request_id = start(client)
    for _ in range(3):
        response = client.post("/oauth/login", data={"request": request_id, "action": "apikey", "api_key": "nope"})
        assert response.status_code == 401 and "Invalid credentials." in response.text
    blocked = client.post("/oauth/login", data={"request": request_id, "action": "apikey", "api_key": API_KEY})
    assert blocked.status_code == 429


def test_password_action_refused_when_only_api_key_is_enabled(tmp_path):
    _, client = make_client(make_env(tmp_path, OAUTH_PASSWORD_HASH=auth.hash_password("pw", 1), OAUTH_LOGIN="apikey"))
    _, _, request_id = start(client)
    response = client.post("/oauth/login", data={"request": request_id, "action": "password", "username": "athlete", "password": "pw"})
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# TOTP
# --------------------------------------------------------------------------- #


def test_totp_rfc6238_vector_and_window():
    # RFC 6238 appendix B (SHA1): T = 59 -> 94287082 (8 digits)
    assert totp(RFC_SECRET, 59, digits=8) == "94287082"
    assert totp(RFC_SECRET, 1111111109, digits=8) == "07081804"
    code = totp(RFC_SECRET, 1_000_000)
    assert match_counter(RFC_SECRET, code, 1_000_000) == 1_000_000 // 30
    assert match_counter(RFC_SECRET, code, 1_000_000 + 30) == 1_000_000 // 30  # one step late is fine
    assert match_counter(RFC_SECRET, code, 1_000_000 + 90) is None  # three steps late is not
    assert match_counter(RFC_SECRET, "12345", 1_000_000) is None
    assert match_counter(RFC_SECRET, f"{code[:3]} {code[3:]}", 1_000_000) is not None


def test_totp_secret_validation_and_uri():
    secret = generate_secret()
    assert normalize_secret(secret.lower()) == secret and len(secret) == 32
    with pytest.raises(ValueError, match="base32"):
        normalize_secret("not base32!")
    with pytest.raises(ValueError, match="too short"):
        normalize_secret("ABCDEFGH")
    uri = provisioning_uri(secret, "futureweb")
    assert uri.startswith("otpauth://totp/Intervals%20MCP%3Afutureweb?secret=") and "issuer=Intervals%20MCP" in uri


def test_sign_in_with_second_factor_and_replay_protection(tmp_path):
    secret = generate_secret()
    clock = Clock()
    _, client = make_client(make_env(tmp_path, OAUTH_TOTP_SECRET=secret), clock)
    _, _, request_id = start(client)
    page = client.get("/oauth/login", params={"request": request_id}).text
    assert 'name="totp"' in page and "Authenticator code" in page
    missing = client.post("/oauth/login", data={"request": request_id, "action": "apikey", "api_key": API_KEY})
    assert missing.status_code == 401 and "authenticator code" in missing.text
    wrong_key = client.post("/oauth/login", data={"request": request_id, "action": "apikey", "api_key": "x", "totp": totp(secret, clock.now)})
    assert wrong_key.status_code == 401
    clock.now += 30  # the previous code was consumed by the failed attempt's check; use the next step
    ok = client.post("/oauth/login", data={"request": request_id, "action": "apikey", "api_key": API_KEY, "totp": totp(secret, clock.now)})
    assert ok.status_code == 302
    _, _, second_request = start(client)
    replay = client.post("/oauth/login", data={"request": second_request, "action": "apikey", "api_key": API_KEY, "totp": totp(secret, clock.now)})
    assert replay.status_code == 401


def test_password_sign_in_also_needs_the_code(tmp_path):
    secret = generate_secret()
    clock = Clock()
    env = make_env(tmp_path, OAUTH_LOGIN="password", OAUTH_PASSWORD="pw", OAUTH_TOTP_SECRET=secret)
    _, client = make_client(env, clock)
    _, _, request_id = start(client)
    assert client.post("/oauth/login", data={"request": request_id, "action": "password", "username": "athlete", "password": "pw"}).status_code == 401
    ok = client.post("/oauth/login", data={"request": request_id, "action": "password", "username": "athlete", "password": "pw", "totp": totp(secret, clock.now)})
    assert ok.status_code == 302


def test_cli_totp_secret(capsys):
    assert auth.main(["totp-secret", "--account", "coach"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("OAUTH_TOTP_SECRET=") and len(lines[0].split("=", 1)[1]) == 32
    assert lines[1].startswith("otpauth://totp/Intervals%20MCP%3Acoach?secret=")
