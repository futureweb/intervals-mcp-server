# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name,too-many-lines
"""Multi-user mode (MCP_TENANCY=multi): isolation between athletes, credentials, lifecycle.

Synthetic athletes only: the owner i219504 (server API key), ALPHA i1001 and BRAVO i1002
(their own Intervals.icu OAuth tokens). The Intervals.icu API is a MockTransport keyed by the
credential: a request for an athlete path with another athlete's credential is recorded as a
violation and fails the test, like any Basic-auth (API key) request for a non-owner.
"""

import asyncio
import base64
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.shared.auth import OAuthClientInformationFull

import intervals_mcp_server.server  # noqa: F401  # registers every tool on the shared FastMCP instance
from intervals_mcp_server import auth
from intervals_mcp_server.api import client as api_client
from intervals_mcp_server.auth import CredentialError, LoginError, SingleUserOAuthProvider, oauth_config_from_env, oauth_from_env
from intervals_mcp_server.auth_grants import grant_context, grants_main
from intervals_mcp_server.auth_totp import generate_secret
from intervals_mcp_server.mcp_instance import mcp
from intervals_mcp_server.tenancy import (
    BUDGETS,
    Credential,
    RequestBudgets,
    athlete_argument,
    intervals_scopes_for,
    required_scope,
    scope_satisfied,
    use_credential,
)
from intervals_mcp_server.token_vault import TokenVault, VaultError, generate_key, vault_from_env
from intervals_mcp_server.tools import athlete as athlete_tools
from intervals_mcp_server.tools import custom_items, gear
from intervals_mcp_server.utils.cache import cache_key
from tests.test_oauth_extensions import FakeWeb
from tests.test_oauth_hardening import VERSION_1_STATE, Clock, dcr_client, refresh_with, write_state

OWNER, ALPHA, BRAVO = "i219504", "i1001", "i1002"
OWNER_KEY = "owner-api-key-0123456789"
TOKENS = {"tok-alpha-secret-0001": ALPHA, "tok-bravo-secret-0002": BRAVO}
TAGS = {OWNER: "OWNERDATA", ALPHA: "ALPHADATA", BRAVO: "BRAVODATA"}
ISSUER = "https://intervals-mcp.example.com"
REDIRECT = "https://chatgpt.com/connector/oauth/test-callback"
KEY = generate_key()
TOTP = generate_secret()


def multi_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "MCP_AUTH": "oauth",
        "MCP_TENANCY": "multi",
        "MCP_PUBLIC_URL": ISSUER,
        "OAUTH_LOGIN": "intervals,password",
        "OAUTH_PASSWORD": "owner-password-for-tests",
        "OAUTH_TOTP_SECRET": TOTP,  # the owner's password sign-in needs a second factor in multi-user mode
        "INTERVALS_OAUTH_CLIENT_ID": "1304",
        "INTERVALS_OAUTH_CLIENT_SECRET": "app-secret",
        "OAUTH_ALLOWED_ATHLETES": f"{OWNER},{ALPHA},{BRAVO}",
        "OAUTH_STATE_FILE": str(tmp_path / "state.json"),
        "OAUTH_TOKEN_KEY": KEY,
        "MCP_PERMISSIONS": "read,write",
        "ATHLETE_ID": OWNER,
        "API_KEY": OWNER_KEY,
    }
    env.update(extra)
    return env


class FakeIntervalsOAuth:  # pylint: disable=too-few-public-methods
    """Intervals.icu token endpoint: the code names the athlete who signed in."""

    def __init__(self) -> None:
        self.codes: list[str] = []

    async def __call__(self, code: str) -> dict[str, Any]:
        self.codes.append(code)
        athlete = code.split(":", 1)[1]
        token = next((t for t, a in TOKENS.items() if a == athlete), f"tok-{athlete}-unused")
        # No "scope" in the answer: the server then keeps the scope it requested.
        return {"token_type": "Bearer", "access_token": token, "athlete": {"id": athlete.lstrip("i"), "name": "Synthetic"}}


def make_provider(tmp_path: Path, clock: Any = time.time, **extra: str) -> SingleUserOAuthProvider:
    return SingleUserOAuthProvider(
        oauth_config_from_env(multi_env(tmp_path, **extra)), clock=clock, fetch=FakeWeb(), intervals_exchange=FakeIntervalsOAuth()
    )


def _client(provider: SingleUserOAuthProvider, client_id: str = "c1") -> OAuthClientInformationFull:
    client = dcr_client(client_id)
    asyncio.run(provider.register_client(client))
    return client


def _request(provider: SingleUserOAuthProvider, client: OAuthClientInformationFull) -> str:
    from tests.test_oauth_hardening import authorize_as  # pylint: disable=import-outside-toplevel

    return authorize_as(provider, client, "198.51.100.7")


def _exchange(provider: SingleUserOAuthProvider, client: OAuthClientInformationFull, location: str) -> Any:
    code = parse_qs(urlsplit(location).query)["code"][0]
    loaded = asyncio.run(provider.load_authorization_code(client, code))
    assert loaded is not None
    return asyncio.run(provider.exchange_authorization_code(client, loaded))


def sign_in_intervals(
    provider: SingleUserOAuthProvider, client: OAuthClientInformationFull, athlete: str, granted: tuple[str, ...] = ("read",)
) -> Any:
    request_id = _request(provider, client)
    location, browser = provider.begin_intervals_login(request_id, granted, "198.51.100.7")
    state = parse_qs(urlsplit(location).query)["state"][0]
    redirect = asyncio.run(provider.finish_intervals_login(state, browser, f"code:{athlete}", None, "198.51.100.7"))
    return _exchange(provider, client, redirect)


def sign_in_password(provider: SingleUserOAuthProvider, client: OAuthClientInformationFull) -> Any:
    request_id = _request(provider, client)
    return _exchange(provider, client, provider.complete_login(request_id, "198.51.100.7", ("read",), method="password"))


# --------------------------------------------------------------------------- #
# A fake Intervals.icu API that checks every credential
# --------------------------------------------------------------------------- #


class FakeApi:
    """Answers with data tagged per athlete and records every request and violation."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []  # (athlete of the credential, method, path)
        self.violations: list[str] = []
        self.coach_mode = False  # answer another athlete's activity like a coach account would
        self.revoked: set[str] = set()

    def who(self, request: httpx.Request) -> str | None:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return TOKENS.get(header[7:])
        if header.startswith("Basic "):
            user, _, key = base64.b64decode(header[6:]).decode().partition(":")
            return OWNER if user == "API_KEY" and key == OWNER_KEY else None
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:  # pylint: disable=too-many-return-statements
        header = request.headers.get("Authorization", "")
        who = self.who(request)
        path = request.url.path.removeprefix("/api/v1")
        self.requests.append((who or "?", request.method, path))
        if header.startswith("Bearer ") and header[7:] in self.revoked:
            return httpx.Response(401, json={"error": "invalid token"})
        if who is None:
            self.violations.append(f"unknown credential for {path}")
            return httpx.Response(401, json={"error": "unauthorized"})
        if header.startswith("Basic ") and who != OWNER:
            self.violations.append(f"API key used for {who}")
        parts = [p for p in path.split("/") if p]
        if parts[0] == "athlete":
            target = who if parts[1] == "0" else parts[1]
            if target != who:
                self.violations.append(f"{who} credential used for athlete {target}: {path}")
                return httpx.Response(403, json={"error": "forbidden"})
            return httpx.Response(200, json=self.athlete_data(who, parts[2:]))
        if parts[0] == "activity":
            owner = next((a for a, tag in TAGS.items() if parts[1] == f"act-{tag}"), None)
            if owner != who and not self.coach_mode:
                self.violations.append(f"{who} credential used for the activity of {owner}")
                return httpx.Response(403, json={"error": "forbidden"})
            if len(parts) == 2:
                return httpx.Response(200, json=self.activity(owner or who))
            return httpx.Response(200, json=[])
        self.violations.append(f"unexpected path {path}")
        return httpx.Response(404, json={"error": "not found"})

    @staticmethod
    def activity(athlete: str) -> dict[str, Any]:
        tag = TAGS[athlete]
        return {
            "id": f"act-{tag}", "name": f"{tag} long ride", "type": "Ride", "icu_athlete_id": athlete,
            "start_date_local": "2026-10-01T08:00:00", "moving_time": 3600, "distance": 30000.0,
        }

    def athlete_data(self, athlete: str, rest: list[str]) -> Any:  # pylint: disable=too-many-return-statements
        tag = TAGS[athlete]
        segment = rest[0].split(".", 1)[0] if rest else ""
        if segment in ("", "profile"):
            return {"id": athlete, "name": f"{tag} name", "timezone": "UTC", "sportSettings": [{"id": 1, "types": ["Ride"], "ftp": 250}]}
        if segment == "sport-settings":
            return [{"id": 1, "types": ["Ride"], "ftp": 250}]
        if segment == "activities":
            return [self.activity(athlete)]
        if segment == "wellness":
            return [{"id": "2026-10-01", "weight": 70.5, "comments": f"{tag} wellness note"}]
        if segment == "events":
            return [{"id": 77, "name": f"{tag} planned workout", "start_date_local": "2026-10-02T00:00:00", "category": "WORKOUT"}]
        return []


@pytest.fixture
def fake_api(monkeypatch) -> Iterator[FakeApi]:
    fake = FakeApi()
    shared = httpx.AsyncClient(transport=httpx.MockTransport(fake))

    async def get_client() -> httpx.AsyncClient:
        return shared

    monkeypatch.setattr(api_client, "_get_httpx_client", get_client)
    for cache in (athlete_tools._ATHLETE_CACHE, athlete_tools._SPORT_SETTINGS_CACHE, athlete_tools._TIMEZONE_CACHE,
                  athlete_tools._TIMEZONE_FAILURES, custom_items._CUSTOM_ITEMS_CACHE, gear._GEAR_RAW_CACHE):
        cache.clear()
    BUDGETS.reset()
    yield fake
    assert not fake.violations, fake.violations


@pytest.fixture
def multi(tmp_path, monkeypatch, fake_api):
    """A multi-user provider wired into the shared FastMCP instance, with three connected athletes."""
    for name, value in multi_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    provider = make_provider(tmp_path)
    monkeypatch.setattr(mcp, "credential_source", provider)
    client = _client(provider)
    tokens = {
        ALPHA: sign_in_intervals(provider, client, ALPHA),
        BRAVO: sign_in_intervals(provider, client, BRAVO, ("read", "write")),
        OWNER: sign_in_password(provider, client),
    }
    return provider, tokens, fake_api


async def acall_as(provider: SingleUserOAuthProvider, access_token: str, tool: str, arguments: dict[str, Any] | None = None) -> str:
    """tools/call as the connection holding *access_token* (the bearer middleware's auth context)."""
    loaded = await provider.load_access_token(access_token)
    assert loaded is not None
    marker = auth_context_var.set(AuthenticatedUser(loaded))
    try:
        result: Any = await mcp.call_tool(tool, arguments or {})
    finally:
        auth_context_var.reset(marker)
    if isinstance(result, tuple):
        result = result[0]
    return "\n".join(getattr(item, "text", "") for item in result)


def call_as(provider: SingleUserOAuthProvider, access_token: str, tool: str, arguments: dict[str, Any] | None = None) -> str:
    return asyncio.run(acall_as(provider, access_token, tool, arguments))


def credential_of(provider: SingleUserOAuthProvider, access_token: str) -> Credential:
    return asyncio.run(provider.connection_credential(access_token))


# --------------------------------------------------------------------------- #
# Scopes
# --------------------------------------------------------------------------- #


def test_scopes_follow_the_granted_permission_classes():
    # CHATS (activity comments, but also private chats) only on the athlete's explicit choice.
    assert intervals_scopes_for(["read"]) == "ACTIVITY:READ,WELLNESS:READ,CALENDAR:READ,LIBRARY:READ,SETTINGS:READ"
    assert intervals_scopes_for(["read"], include_chats=True).endswith("SETTINGS:READ,CHATS:READ")
    assert intervals_scopes_for(["read", "write"]) == "ACTIVITY:WRITE,WELLNESS:WRITE,CALENDAR:WRITE,LIBRARY:WRITE,SETTINGS:READ"
    assert intervals_scopes_for(["read", "write"], include_chats=True).endswith(",CHATS:WRITE")
    assert intervals_scopes_for(["read", "write", "destructive", "admin"]).endswith("LIBRARY:WRITE,SETTINGS:WRITE")
    assert "CHATS" not in intervals_scopes_for(["read", "write"], exclude_areas=["chats"], include_chats=True)
    assert required_scope("GET", "/athlete/i1/activities") == "ACTIVITY:READ"
    assert required_scope("PUT", "/athlete/i1/wellness/2026-10-01") == "WELLNESS:WRITE"
    assert required_scope("GET", "/athlete/i1/events.csv") == "CALENDAR:READ"
    assert required_scope("POST", "/athlete/i1/workouts") == "LIBRARY:WRITE"
    assert required_scope("PUT", "/athlete/i1/sport-settings/Ride") == "SETTINGS:WRITE"
    assert required_scope("GET", "/athlete/i1") == "SETTINGS:READ"
    assert required_scope("GET", "/activity/i9/streams") == "ACTIVITY:READ"
    assert required_scope("POST", "/activity/i9/messages") == "CHATS:WRITE"
    assert required_scope("GET", "/athlete/i1/something-new") is None
    assert scope_satisfied({"CALENDAR:WRITE"}, "CALENDAR:READ") and not scope_satisfied({"CALENDAR:READ"}, "CALENDAR:WRITE")


def test_authorize_url_requests_the_scopes_of_the_consent(tmp_path):
    provider = make_provider(tmp_path)
    client = _client(provider)
    location, _ = provider.begin_intervals_login(_request(provider, client), ("read",), "k")
    assert parse_qs(urlsplit(location).query)["scope"] == [intervals_scopes_for(["read"])]
    location, _ = provider.begin_intervals_login(_request(provider, client), ("read", "write"), "k")
    assert parse_qs(urlsplit(location).query)["scope"] == [intervals_scopes_for(["read", "write"])]
    assert location.startswith("https://intervals.icu/oauth/authorize?")
    # The comments checkbox is ignored unless the server offers it (INTERVALS_OAUTH_OFFER_CHATS).
    location, _ = provider.begin_intervals_login(_request(provider, client), ("read",), "k", chats=True)
    assert "CHATS" not in parse_qs(urlsplit(location).query)["scope"][0]
    offering = make_provider(tmp_path, INTERVALS_OAUTH_OFFER_CHATS="true")
    client = _client(offering, "c2")
    location, _ = offering.begin_intervals_login(_request(offering, client), ("read",), "k", chats=True)
    assert parse_qs(urlsplit(location).query)["scope"][0].endswith(",CHATS:READ")


def test_single_user_mode_keeps_the_configured_identity_scope(tmp_path):
    env = multi_env(tmp_path, MCP_TENANCY="single", INTERVALS_OAUTH_SCOPE="ACTIVITY:READ", OAUTH_ALLOWED_ATHLETES=OWNER)
    provider = SingleUserOAuthProvider(oauth_config_from_env(env), fetch=FakeWeb())
    client = _client(provider)
    location, _ = provider.begin_intervals_login(_request(provider, client), ("read", "write"), "k")
    assert parse_qs(urlsplit(location).query)["scope"] == ["ACTIVITY:READ"]


def test_missing_intervals_scope_is_refused_before_the_request(multi):
    provider, tokens, fake = multi
    credential = credential_of(provider, tokens[ALPHA].access_token)
    narrowed = Credential(credential.athlete_id, "bearer", credential.secret, credential.grant_id, frozenset({"ACTIVITY:READ"}))
    before = len(fake.requests)
    with use_credential(narrowed):
        result = asyncio.run(api_client.make_intervals_request(f"/athlete/{ALPHA}/events"))
    assert result["error"] and "CALENDAR:READ" in result["message"] and "reconnect" in result["message"]
    assert len(fake.requests) == before


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"OAUTH_TOKEN_KEY": ""}, "OAUTH_TOKEN_KEY or OAUTH_TOKEN_KEY_FILE"),
        ({"OAUTH_TOKEN_KEY": "c2hvcnQ="}, "32 bytes"),
        ({"OAUTH_LOGIN": "password"}, "needs the Intervals.icu sign-in"),
        ({"API_KEY": ""}, "needs ATHLETE_ID and API_KEY"),
        ({"OAUTH_ALLOWED_ATHLETES": "*"}, "OAUTH_ALLOW_ANY_ATHLETE=true"),
        ({"MCP_TENANCY": "multiple"}, "MCP_TENANCY must be"),
        ({"INTERVALS_OAUTH_EXCLUDE_AREAS": "GEAR"}, "unknown area"),
        ({"OAUTH_TOTP_SECRET": ""}, "needs OAUTH_TOTP_SECRET"),
        ({"OAUTH_MAX_GRANTS_PER_ATHLETE": "0"}, "OAUTH_MAX_GRANTS_PER_ATHLETE"),
    ],
)
def test_multi_user_configuration_is_validated(tmp_path, extra, message):
    with pytest.raises(ValueError, match=message):
        oauth_config_from_env(multi_env(tmp_path, **extra))


def test_any_athlete_needs_the_explicit_opt_in(tmp_path):
    config = oauth_config_from_env(multi_env(tmp_path, OAUTH_ALLOWED_ATHLETES="*", OAUTH_ALLOW_ANY_ATHLETE="true"))
    assert config.allow_any_athlete and config.athlete_allowed("i424242")
    with pytest.raises(ValueError, match="only accepted with MCP_TENANCY=multi"):
        oauth_config_from_env(multi_env(tmp_path, MCP_TENANCY="single", OAUTH_ALLOWED_ATHLETES="*", OAUTH_ALLOW_ANY_ATHLETE="true"))


def test_single_user_mode_refuses_other_athletes_on_the_allowlist(tmp_path):
    """R30-2: in the single-user mode every allowed athlete sees the owner's data."""
    with pytest.raises(ValueError, match="besides ATHLETE_ID"):
        oauth_config_from_env(multi_env(tmp_path, MCP_TENANCY="single"))
    config = oauth_config_from_env(multi_env(tmp_path, MCP_TENANCY="single", OAUTH_ALLOWED_ATHLETES=f"{OWNER},{ALPHA}",
                                             OAUTH_OWNER_ACCOUNTS=ALPHA))
    assert config.owner_accounts == frozenset({"1001"})
    assert oauth_config_from_env(multi_env(tmp_path, MCP_TENANCY="single", OAUTH_ALLOWED_ATHLETES=OWNER)).allowed_athletes


def test_multi_user_mode_requires_oauth(tmp_path):
    with pytest.raises(ValueError, match="requires MCP_AUTH=oauth"):
        oauth_from_env(multi_env(tmp_path, MCP_AUTH="none"))


def test_key_file_must_be_private(tmp_path):
    key_file = tmp_path / "token.key"
    key_file.write_text(KEY + "\n")
    key_file.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        vault_from_env({"OAUTH_TOKEN_KEY_FILE": str(key_file)})
    key_file.chmod(0o600)
    vault = vault_from_env({"OAUTH_TOKEN_KEY_FILE": str(key_file)})
    assert vault is not None and vault.key_count == 1 and KEY not in repr(vault)


def test_token_key_cli_creates_a_private_key_file(tmp_path, capsys):
    target = tmp_path / "new.key"
    assert auth.main(["token-key", "--file", str(target)]) == 0
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert vault_from_env({"OAUTH_TOKEN_KEY_FILE": str(target)}) is not None
    assert str(target) in capsys.readouterr().out
    assert auth.main(["token-key"]) == 0
    assert capsys.readouterr().out.startswith("OAUTH_TOKEN_KEY=")


def test_vault_binds_tokens_to_grant_and_rotates_keys():
    old, new = generate_key(), generate_key()
    keys = [base64.urlsafe_b64decode(k) for k in (old, new)]
    sealed = TokenVault([keys[0]]).seal({"access_token": "t"}, "grant=a")
    rotated = TokenVault([keys[1], keys[0]])
    assert rotated.open(sealed, "grant=a") == {"access_token": "t"}
    with pytest.raises(VaultError):
        rotated.open(sealed, "grant=b")
    with pytest.raises(VaultError, match="key"):
        TokenVault([keys[1]]).open(sealed, "grant=a")


# --------------------------------------------------------------------------- #
# Credentials per connection
# --------------------------------------------------------------------------- #


def test_each_connection_gets_its_own_credential(multi):
    provider, tokens, _ = multi
    alpha = credential_of(provider, tokens[ALPHA].access_token)
    bravo = credential_of(provider, tokens[BRAVO].access_token)
    owner = credential_of(provider, tokens[OWNER].access_token)
    assert (alpha.athlete_id, alpha.kind, alpha.secret) == (ALPHA, "bearer", "tok-alpha-secret-0001")
    assert (bravo.athlete_id, bravo.kind, bravo.secret) == (BRAVO, "bearer", "tok-bravo-secret-0002")
    assert (owner.athlete_id, owner.kind, owner.secret, owner.owner) == (OWNER, "apikey", OWNER_KEY, True)
    assert "tok-alpha" not in repr(alpha) and OWNER_KEY not in repr(owner)
    assert alpha.partition != bravo.partition


def test_owner_signing_in_with_intervals_uses_the_api_key_and_stores_no_token(tmp_path):
    provider = make_provider(tmp_path)
    token = sign_in_intervals(provider, _client(provider), OWNER)
    credential = credential_of(provider, token.access_token)
    assert credential.kind == "apikey" and credential.athlete_id == OWNER
    grant = next(iter(json.loads(provider.config.state_file.read_text())["grants"].values()))
    assert grant["kind"] == "owner" and "credential" not in grant


def test_owner_without_api_key_connects_with_its_own_token(tmp_path):
    provider = make_provider(tmp_path, OAUTH_LOGIN="intervals", API_KEY="")
    token = sign_in_intervals(provider, _client(provider), OWNER)
    credential = credential_of(provider, token.access_token)
    assert (credential.kind, credential.athlete_id, credential.owner) == ("bearer", OWNER, True)
    with pytest.raises(LoginError):
        provider.complete_login(_request(provider, _client(provider, "c2")), "k", ("read",), method="password")


def test_athletes_outside_the_allowlist_cannot_connect(tmp_path):
    provider = make_provider(tmp_path, OAUTH_ALLOWED_ATHLETES=f"{OWNER},{ALPHA}")
    with pytest.raises(LoginError, match="not allowed"):
        sign_in_intervals(provider, _client(provider), BRAVO)


def test_tokens_are_encrypted_at_rest(multi):
    provider, _, _ = multi
    text = provider.config.state_file.read_text()
    data = json.loads(text)
    assert data["version"] == 2
    for secret in (*TOKENS, OWNER_KEY, KEY):
        assert secret not in text
    athlete_grants = {gid: g for gid, g in data["grants"].items() if g["kind"] == "athlete"}
    assert {g["athlete_id"] for g in athlete_grants.values()} == {ALPHA, BRAVO}
    assert all(g["credential"].startswith("v1.") for g in athlete_grants.values())
    assert oct(provider.config.state_file.stat().st_mode & 0o777) == "0o600"


def test_a_sealed_token_moved_to_another_grant_does_not_open(multi):
    provider, tokens, _ = multi
    alpha_grant = credential_of(provider, tokens[ALPHA].access_token).grant_id
    bravo_grant = credential_of(provider, tokens[BRAVO].access_token).grant_id
    assert alpha_grant and bravo_grant
    provider._grants[bravo_grant].sealed = provider._grants[alpha_grant].sealed
    with pytest.raises(CredentialError, match="cannot be read"):
        credential_of(provider, tokens[BRAVO].access_token)
    vault = provider.config.vault
    assert vault is not None
    assert vault.open(provider._grants[alpha_grant].sealed or "", grant_context(alpha_grant, ALPHA))["access_token"] == "tok-alpha-secret-0001"


def test_sessions_are_bound_to_the_grant(multi):
    provider, tokens, _ = multi
    alpha = asyncio.run(provider.load_access_token(tokens[ALPHA].access_token))
    bravo = asyncio.run(provider.load_access_token(tokens[BRAVO].access_token))
    assert alpha is not None and bravo is not None
    assert alpha.client_id == bravo.client_id and alpha.subject != bravo.subject


# --------------------------------------------------------------------------- #
# Isolation over tool calls
# --------------------------------------------------------------------------- #

TOOLS = [
    ("get_athlete_profile", {}),
    ("get_activities", {"start_date": "2026-09-01", "end_date": "2026-10-05"}),
    ("get_wellness_data", {"start_date": "2026-10-01", "end_date": "2026-10-01"}),
    ("get_events", {"start_date": "2026-10-01", "end_date": "2026-10-05"}),
    ("get_server_status", {"output_format": "json"}),
]


def test_two_athletes_in_parallel_never_see_each_other(multi):
    provider, tokens, fake = multi

    async def run() -> dict[str, list[str]]:
        async def one(athlete: str) -> list[str]:
            # Every tool of the three connections runs concurrently in one event loop.
            return list(await asyncio.gather(*(acall_as(provider, tokens[athlete].access_token, n, a) for n, a in TOOLS)))

        alpha, bravo, owner = await asyncio.gather(one(ALPHA), one(BRAVO), one(OWNER))
        return {ALPHA: alpha, BRAVO: bravo, OWNER: owner}

    results = asyncio.run(run())
    for athlete, texts in results.items():
        joined = "\n".join(texts)
        assert TAGS[athlete] in joined, (athlete, joined[:500])
        for other, tag in TAGS.items():
            if other != athlete:
                assert tag not in joined, (athlete, other)
                assert other not in joined, (athlete, other)
    assert {who for who, _, _ in fake.requests} == {ALPHA, BRAVO, OWNER}
    status = json.loads(results[ALPHA][-1])
    assert status["tenancy"] == "multi" and status["athlete_id"] == ALPHA
    assert status["connection"]["credential"].startswith("Intervals.icu sign-in") and status["api_key_configured"] is None


def test_athlete_id_spoofing_is_refused_before_any_request(multi):
    provider, tokens, fake = multi
    before = len(fake.requests)
    for spoof in (BRAVO, "1002", "I1002", OWNER, "219504"):
        text = call_as(provider, tokens[ALPHA].access_token, "get_activities", {"athlete_id": spoof})
        assert "not the athlete of this connection" in text and "BRAVODATA" not in text and "OWNERDATA" not in text
    assert len(fake.requests) == before
    for alias in ("0", "i0", "1001", ALPHA):
        text = call_as(provider, tokens[ALPHA].access_token, "get_wellness_data",
                       {"athlete_id": alias, "start_date": "2026-10-01", "end_date": "2026-10-01"})
        assert "ALPHADATA" in text
    assert {who for who, _, _ in fake.requests[before:]} == {ALPHA}


def test_athlete_argument_rules():
    credential = Credential(ALPHA, "bearer", "t", "g", None)
    assert athlete_argument(None, credential) == (ALPHA, None)
    assert athlete_argument(" ", credential) == (ALPHA, None)
    assert athlete_argument("0", credential) == (ALPHA, None)
    assert athlete_argument("1001", credential) == (ALPHA, None)
    assert athlete_argument("i10011", credential)[1] is not None


def test_api_client_refuses_other_athletes_and_foreign_endpoints(multi):
    provider, tokens, fake = multi
    before = len(fake.requests)
    with use_credential(credential_of(provider, tokens[ALPHA].access_token)):
        for url in (f"/athlete/{BRAVO}/activities", f"/athlete/{OWNER}", "/athletes", "/chats/1/messages"):
            result = asyncio.run(api_client.make_intervals_request(url))
            assert result["error"], url
        assert asyncio.run(api_client.make_intervals_request("/athlete/0/wellness"))[0]["comments"] == "ALPHADATA wellness note"
        passed_key = asyncio.run(api_client.make_intervals_request(f"/athlete/{ALPHA}", api_key=OWNER_KEY))
        assert passed_key["error"]
    assert len(fake.requests) == before + 1


def test_requests_without_a_connection_are_refused(multi, monkeypatch):
    _, _, fake = multi
    from intervals_mcp_server.tools.activities import get_activities  # pylint: disable=import-outside-toplevel

    before = len(fake.requests)
    assert asyncio.run(api_client.make_intervals_request(f"/athlete/{OWNER}/activities"))["error"]
    assert "signed-in connection" in asyncio.run(get_activities())
    assert len(fake.requests) == before
    monkeypatch.setattr(mcp, "credential_source", None)
    with pytest.raises(Exception, match="multi-user mode"):
        asyncio.run(mcp.call_tool("get_activities", {}))


def test_another_athletes_activity_is_never_returned(multi):
    provider, tokens, fake = multi
    fake.coach_mode = True  # Intervals.icu would answer (e.g. a coach account); the server must not pass it on
    text = call_as(provider, tokens[ALPHA].access_token, "get_activity_details", {"activity_id": "act-BRAVODATA"})
    assert "BRAVODATA" not in text and "another athlete" in text


def test_caches_are_partitioned_per_connection(multi):
    provider, tokens, fake = multi
    alpha = credential_of(provider, tokens[ALPHA].access_token)
    bravo = credential_of(provider, tokens[BRAVO].access_token)
    owner = credential_of(provider, tokens[OWNER].access_token)
    keys = set()
    for credential in (alpha, bravo, owner):
        with use_credential(credential):
            keys.add(cache_key(ALPHA))
    assert len(keys) == 3
    assert "ALPHADATA" in call_as(provider, tokens[ALPHA].access_token, "get_athlete_profile")
    assert "ALPHADATA" in call_as(provider, tokens[ALPHA].access_token, "get_athlete_profile")
    assert [r for r in fake.requests if r[2] == f"/athlete/{ALPHA}"] == [(ALPHA, "GET", f"/athlete/{ALPHA}")]  # cached
    # An entry under BRAVO's id in ALPHA's partition is never served to BRAVO.
    with use_credential(alpha):
        athlete_tools._ATHLETE_CACHE.set(cache_key(BRAVO), {"id": BRAVO, "name": "ALPHADATA forged"})
    text = call_as(provider, tokens[BRAVO].access_token, "get_athlete_profile")
    assert "ALPHADATA" not in text and "BRAVODATA" in text
    assert (BRAVO, "GET", f"/athlete/{BRAVO}") in fake.requests


def test_rejected_tokens_ask_to_reconnect(multi):
    provider, tokens, fake = multi
    fake.revoked.add("tok-alpha-secret-0001")
    text = call_as(provider, tokens[ALPHA].access_token, "get_wellness_data", {"start_date": "2026-10-01", "end_date": "2026-10-01"})
    assert "401" in text and "reconnect" in text and "tok-alpha" not in text


def test_daily_budget_per_athlete(multi, monkeypatch):
    provider, tokens, _ = multi
    monkeypatch.setenv("MCP_ATHLETE_DAILY_REQUESTS", "2")
    alpha = credential_of(provider, tokens[ALPHA].access_token)
    owner = credential_of(provider, tokens[OWNER].access_token)
    with use_credential(alpha):
        results = [asyncio.run(api_client.make_intervals_request(f"/athlete/{ALPHA}/wellness")) for _ in range(3)]
    assert not isinstance(results[1], dict) and results[2]["limit_reached"] and "daily budget" in results[2]["message"]
    with use_credential(credential_of(provider, tokens[BRAVO].access_token)):
        assert isinstance(asyncio.run(api_client.make_intervals_request(f"/athlete/{BRAVO}/wellness")), list)
    with use_credential(owner):  # the owner's API key is not an OAuth token of the shared app
        for _ in range(3):
            assert isinstance(asyncio.run(api_client.make_intervals_request(f"/athlete/{OWNER}/wellness")), list)


def test_shared_window_budget(multi, monkeypatch):
    provider, tokens, _ = multi
    monkeypatch.setenv("MCP_APP_REQUESTS_PER_15MIN", "3")
    monkeypatch.setenv("MCP_ATHLETE_SHARE_PERCENT", "100")
    monkeypatch.setenv("MCP_OWNER_RESERVED_PERCENT", "0")
    for athlete in (ALPHA, BRAVO, ALPHA):
        with use_credential(credential_of(provider, tokens[athlete].access_token)):
            assert isinstance(asyncio.run(api_client.make_intervals_request(f"/athlete/{athlete}/wellness")), list)
    with use_credential(credential_of(provider, tokens[BRAVO].access_token)):
        refused = asyncio.run(api_client.make_intervals_request(f"/athlete/{BRAVO}/wellness"))
    assert refused["limit_reached"] and "15 minutes" in refused["message"]


def test_write_tools_use_the_connections_token(multi, monkeypatch):
    provider, tokens, fake = multi
    from intervals_mcp_server.tools.wellness import update_wellness  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(fake, "athlete_data", lambda athlete, rest: {"id": "2026-10-01", "mood": 2})
    with use_credential(credential_of(provider, tokens[BRAVO].access_token)):
        refused = asyncio.run(update_wellness(athlete_id=ALPHA, date="2026-10-01", mood=2))
        assert "not the athlete of this connection" in refused
        asyncio.run(update_wellness(date="2026-10-01", mood=2))
    writes = [r for r in fake.requests if r[1] == "PUT"]
    assert writes and all(who == BRAVO and BRAVO in path for who, _, path in writes)


# --------------------------------------------------------------------------- #
# Lifecycle: revoke, retention, CLI, state file migration
# --------------------------------------------------------------------------- #


def test_revoking_the_grant_deletes_the_stored_token(multi):
    provider, tokens, _ = multi
    grant_id = credential_of(provider, tokens[ALPHA].access_token).grant_id
    access = asyncio.run(provider.load_access_token(tokens[ALPHA].access_token))
    assert access is not None
    sealed = provider._grants[grant_id or ""].sealed
    asyncio.run(provider.revoke_token(access))
    text = provider.config.state_file.read_text()
    assert grant_id not in json.loads(text)["grants"] and sealed and sealed not in text
    assert asyncio.run(provider.load_access_token(tokens[ALPHA].access_token)) is None
    with pytest.raises(CredentialError):
        credential_of(provider, tokens[ALPHA].access_token)
    assert credential_of(provider, tokens[BRAVO].access_token).athlete_id == BRAVO


def test_refresh_keeps_the_grant_and_its_token(multi):
    provider, tokens, _ = multi
    client = asyncio.run(provider.get_client("c1"))
    assert client is not None
    refreshed = refresh_with(provider, client, tokens[ALPHA].refresh_token)
    assert credential_of(provider, refreshed.access_token).secret == "tok-alpha-secret-0001"


def test_retention_drops_unused_tokens(tmp_path):
    clock = Clock()
    provider = make_provider(tmp_path, clock=clock, OAUTH_TOKEN_RETENTION_DAYS="2", OAUTH_REFRESH_TOKEN_TTL=str(30 * 86400))
    client = _client(provider)
    alpha = sign_in_intervals(provider, client, ALPHA)
    clock.now += 3 * 86400
    bravo = sign_in_intervals(provider, client, BRAVO)  # any later write applies the retention
    text = provider.config.state_file.read_text()
    assert [g["athlete_id"] for g in json.loads(text)["grants"].values()] == [BRAVO]
    assert asyncio.run(provider.load_access_token(alpha.access_token)) is None
    assert refresh_with(provider, client, alpha.refresh_token) is None
    assert credential_of(provider, bravo.access_token).athlete_id == BRAVO


def test_a_new_sign_in_updates_the_scopes_of_the_athletes_other_grants(tmp_path):
    provider = make_provider(tmp_path)
    client = _client(provider)
    first = sign_in_intervals(provider, client, ALPHA, ("read", "write"))

    async def read_only(_code: str) -> dict[str, Any]:
        return {"access_token": "tok-alpha-secret-0001", "scope": "ACTIVITY:READ", "athlete": {"id": "1001"}}

    provider._intervals_exchange = read_only
    sign_in_intervals(provider, client, ALPHA, ("read",))
    assert credential_of(provider, first.access_token).intervals_scopes == frozenset({"ACTIVITY:READ"})


def test_grants_cli_lists_and_removes_without_showing_tokens(multi, capsys, monkeypatch):
    provider, tokens, _ = multi
    env = {"OAUTH_STATE_FILE": str(provider.config.state_file)}
    assert grants_main(["list"], env) == 0
    listing = capsys.readouterr().out
    assert ALPHA in listing and BRAVO in listing and "owner" in listing
    for secret in (*TOKENS, OWNER_KEY, "v1."):
        assert secret not in listing
    assert grants_main(["list", "--json"], env) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {row["athlete_id"] for row in rows if row["token_stored"]} == {ALPHA, BRAVO}
    assert grants_main(["remove", "1002"], env) == 0
    data = json.loads(provider.config.state_file.read_text())
    assert {g["athlete_id"] for g in data["grants"].values()} == {ALPHA, OWNER}
    # The running server notices the change: BRAVO's connection is gone, the others keep working.
    assert asyncio.run(provider.load_access_token(tokens[BRAVO].access_token)) is None
    assert credential_of(provider, tokens[ALPHA].access_token).athlete_id == ALPHA
    asyncio.run(provider.register_client(dcr_client("c9")))  # the server's next write keeps the removal
    assert BRAVO not in {g["athlete_id"] for g in json.loads(provider.config.state_file.read_text())["grants"].values()}
    assert grants_main(["remove", "--grant", "nope"], env) == 1
    monkeypatch.setattr(time, "time", lambda: 4_100_000_000.0)
    assert grants_main(["prune", "--days", "1"], env) == 0
    assert not [g for g in json.loads(provider.config.state_file.read_text())["grants"].values() if g["kind"] == "athlete"]


def _chatgpt(provider: SingleUserOAuthProvider) -> OAuthClientInformationFull:
    from tests.test_oauth_extensions import CHATGPT_ID  # pylint: disable=import-outside-toplevel

    client = asyncio.run(provider.get_client(CHATGPT_ID))
    assert client is not None
    return client


def test_version_1_grant_keeps_working_in_single_user_mode(tmp_path):
    write_state(tmp_path, VERSION_1_STATE)
    env = multi_env(tmp_path, MCP_TENANCY="single", OAUTH_ALLOWED_ATHLETES=OWNER)
    provider = SingleUserOAuthProvider(oauth_config_from_env(env), fetch=FakeWeb())
    assert refresh_with(provider, _chatgpt(provider), "cimd-refresh-token") is not None
    saved = json.loads(provider.config.state_file.read_text())
    assert saved["version"] == 1 and set(saved) == {"version", "clients", "refresh_tokens"}
    assert all("athlete_id" not in r for r in saved["refresh_tokens"].values())  # unknown stays unknown


def test_version_1_grants_are_refused_in_multi_user_mode_until_adopted(tmp_path, capsys):
    """R30-1/R30-2: connections of the single-user mode without a recorded athlete are never the
    owner's by default; `grants adopt-legacy --owner` adopts them once (also on a running server)."""
    write_state(tmp_path, VERSION_1_STATE)
    provider = make_provider(tmp_path)
    assert refresh_with(provider, _chatgpt(provider), "cimd-refresh-token") is None
    assert provider.grant_overview()["legacy_grants"] == 2
    env = {"OAUTH_STATE_FILE": str(provider.config.state_file), "ATHLETE_ID": OWNER}
    assert grants_main(["adopt-legacy", "--owner"], env) == 0
    assert "Adopted 2" in capsys.readouterr().out
    rotated = refresh_with(provider, _chatgpt(provider), "cimd-refresh-token")  # the running server reloads
    assert rotated is not None and rotated.scope == "mcp intervals:read intervals:write"
    credential = credential_of(provider, rotated.access_token)
    assert (credential.athlete_id, credential.kind, credential.secret) == (OWNER, "apikey", OWNER_KEY)
    saved = json.loads(provider.config.state_file.read_text())
    assert saved["version"] == 2 and {g["kind"] for g in saved["grants"].values()} == {"owner"}
    assert len(saved["grants"]) == 2 and all(r["athlete_id"] == OWNER for r in saved["refresh_tokens"].values())
    # Without an owner API key owner grants cannot be served.
    provider = make_provider(tmp_path, OAUTH_LOGIN="intervals", API_KEY="")
    assert not provider._tokens.refresh


def test_adopting_before_the_switch_keeps_the_live_grant_working(tmp_path):
    """The production path: adopt while still in single-user mode, then switch; ChatGPT keeps working."""
    write_state(tmp_path, VERSION_1_STATE)
    single = SingleUserOAuthProvider(oauth_config_from_env(multi_env(tmp_path, MCP_TENANCY="single", OAUTH_ALLOWED_ATHLETES=OWNER)),
                                     fetch=FakeWeb())
    assert grants_main(["adopt-legacy", "--owner"], {"OAUTH_STATE_FILE": str(tmp_path / "state.json"), "ATHLETE_ID": OWNER}) == 0
    rotated = refresh_with(single, _chatgpt(single), "cimd-refresh-token")
    assert rotated is not None
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["version"] == 1 and all(r["athlete_id"] == OWNER for r in saved["refresh_tokens"].values())
    multi_provider = make_provider(tmp_path)  # the switch: owner records become owner grants, once
    again = refresh_with(multi_provider, _chatgpt(multi_provider), rotated.refresh_token)
    assert again is not None and credential_of(multi_provider, again.access_token).kind == "apikey"


def test_single_user_sign_ins_record_the_athlete(tmp_path):
    """From this release on the single-user mode records who signed in; the owner's connections then
    become owner grants in multi-user mode without adoption."""
    single = SingleUserOAuthProvider(
        oauth_config_from_env(multi_env(tmp_path, MCP_TENANCY="single", OAUTH_ALLOWED_ATHLETES=OWNER)), fetch=FakeWeb(),
        intervals_exchange=FakeIntervalsOAuth(),
    )
    client = _client(single)
    by_password = sign_in_password(single, client)
    by_intervals = sign_in_intervals(single, client, OWNER)
    records = json.loads(single.config.state_file.read_text())["refresh_tokens"].values()
    assert sorted((r["athlete_id"], r["method"]) for r in records) == [(OWNER, "intervals"), (OWNER, "password")]
    multi_provider = make_provider(tmp_path)
    client = asyncio.run(multi_provider.get_client("c1"))
    assert client is not None
    for token in (by_password, by_intervals):
        rotated = refresh_with(multi_provider, client, token.refresh_token)
        assert rotated is not None and credential_of(multi_provider, rotated.access_token).kind == "apikey"


def test_friend_connected_in_single_mode_is_not_the_owner_after_the_switch(tmp_path, monkeypatch, fake_api):
    """Reviewer reproduction R30-2: a friend who signed in in single mode (only possible as an owner account
    now) never gets the owner's API key after the switch to multi-user mode."""
    single_env = multi_env(tmp_path, MCP_TENANCY="single", OAUTH_ALLOWED_ATHLETES=f"{OWNER},{ALPHA}", OAUTH_OWNER_ACCOUNTS=ALPHA)
    single = SingleUserOAuthProvider(oauth_config_from_env(single_env), fetch=FakeWeb(), intervals_exchange=FakeIntervalsOAuth())
    client = _client(single)
    friend = sign_in_intervals(single, client, ALPHA)
    for name, value in multi_env(tmp_path, OAUTH_ALLOWED_ATHLETES=f"{OWNER},{ALPHA}").items():
        monkeypatch.setenv(name, value)
    multi_provider = make_provider(tmp_path, OAUTH_ALLOWED_ATHLETES=f"{OWNER},{ALPHA}")
    monkeypatch.setattr(mcp, "credential_source", multi_provider)
    client = asyncio.run(multi_provider.get_client(client.client_id)) or client
    assert refresh_with(multi_provider, client, friend.refresh_token) is None
    assert grants_main(["adopt-legacy", "--owner"], {"OAUTH_STATE_FILE": str(tmp_path / "state.json"), "ATHLETE_ID": OWNER}) == 0
    assert refresh_with(multi_provider, client, friend.refresh_token) is None  # never adopted as the owner's
    assert not fake_api.requests


@pytest.mark.parametrize("damage", ["delete-record", "grants-empty", "grants-null"])
def test_missing_grant_record_fails_closed(multi, damage):
    """Reviewer reproduction R30-1: a format-2 file without the athlete's grant record never turns the
    connection into the owner's."""
    provider, tokens, fake = multi
    path = provider.config.state_file
    data = json.loads(path.read_text())
    alpha_grant = credential_of(provider, tokens[ALPHA].access_token).grant_id
    if damage == "delete-record":
        del data["grants"][alpha_grant]
    elif damage == "grants-empty":
        data["grants"] = {}
    else:
        data["grants"] = None
    time.sleep(0.01)
    path.write_text(json.dumps(data))
    if damage == "grants-null":
        # A damaged file is not loaded (the running server keeps its state, a new start stops with a message).
        with pytest.raises(ValueError, match="'grants'"):
            make_provider(path.parent)
        assert credential_of(provider, tokens[ALPHA].access_token).kind == "bearer"
        return
    assert asyncio.run(provider.load_access_token(tokens[ALPHA].access_token)) is None
    with pytest.raises(CredentialError):
        credential_of(provider, tokens[ALPHA].access_token)
    client = asyncio.run(provider.get_client("c1"))
    assert client is not None and refresh_with(provider, client, tokens[ALPHA].refresh_token) is None
    restarted = make_provider(path.parent)
    assert asyncio.run(restarted.get_client("c1")) is not None
    assert refresh_with(restarted, client, tokens[ALPHA].refresh_token) is None
    assert not [r for r in fake.requests if r[0] == OWNER]


@pytest.mark.usefixtures("fake_api")
def test_unreadable_client_keeps_the_grant_record(tmp_path, monkeypatch):  # pylint: disable=too-many-locals
    """Reviewer reproduction R30-1 (no tampering): while a client registration is unreadable its tokens are
    kept with their grant record; once readable again the friend is still the friend, never the owner."""
    for name, value in multi_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    provider = make_provider(tmp_path)
    friend_client, owner_client = _client(provider, "friend-dcr"), _client(provider, "owner-dcr")
    friend = sign_in_intervals(provider, friend_client, ALPHA)
    path = provider.config.state_file
    good = json.loads(path.read_text())
    broken = json.loads(json.dumps(good))
    broken["clients"]["friend-dcr"]["redirect_uris"] = "not-a-list"
    path.write_text(json.dumps(broken))
    restarted = make_provider(tmp_path)
    sign_in_password(restarted, asyncio.run(restarted.get_client("owner-dcr")) or owner_client)  # any later write
    after = json.loads(path.read_text())
    assert ALPHA in {g["athlete_id"] for g in after["grants"].values()}
    after["clients"]["friend-dcr"] = good["clients"]["friend-dcr"]
    path.write_text(json.dumps(after))
    fixed = make_provider(tmp_path)
    monkeypatch.setattr(mcp, "credential_source", fixed)
    client = asyncio.run(fixed.get_client("friend-dcr"))
    assert client is not None
    rotated = refresh_with(fixed, client, friend.refresh_token)
    assert rotated is not None
    credential = credential_of(fixed, rotated.access_token)
    assert (credential.kind, credential.athlete_id) == ("bearer", ALPHA)
    text = call_as(fixed, rotated.access_token, "get_activities", {})
    assert "ALPHADATA" in text and "OWNERDATA" not in text


def test_switch_to_single_with_an_unreadable_client_drops_the_friend(tmp_path, monkeypatch):
    """Reviewer reproduction R30-1 (multi -> single): the friend's token is not written as a single-user token."""
    for name, value in multi_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    provider = make_provider(tmp_path)
    friend_client, owner_client = _client(provider, "friend-dcr"), _client(provider, "owner-dcr")
    friend = sign_in_intervals(provider, friend_client, ALPHA)
    path = provider.config.state_file
    data = json.loads(path.read_text())
    good_client = json.loads(json.dumps(data["clients"]["friend-dcr"]))
    data["clients"]["friend-dcr"]["redirect_uris"] = "not-a-list"
    path.write_text(json.dumps(data))
    single_env = multi_env(tmp_path, MCP_TENANCY="single", OAUTH_TOKEN_KEY="", OAUTH_ALLOWED_ATHLETES=OWNER)
    single = SingleUserOAuthProvider(oauth_config_from_env(single_env), fetch=FakeWeb(), intervals_exchange=FakeIntervalsOAuth())
    sign_in_password(single, asyncio.run(single.get_client("owner-dcr")) or owner_client)
    saved = json.loads(path.read_text())
    assert saved["version"] == 1 and not any(r["client_id"] == "friend-dcr" for r in saved["refresh_tokens"].values())
    saved["clients"]["friend-dcr"] = good_client
    path.write_text(json.dumps(saved))
    single = SingleUserOAuthProvider(oauth_config_from_env(single_env), fetch=FakeWeb(), intervals_exchange=FakeIntervalsOAuth())
    client = asyncio.run(single.get_client("friend-dcr"))
    assert client is not None and refresh_with(single, client, friend.refresh_token) is None


def test_switching_back_to_single_user_mode_drops_other_athletes(multi):
    provider, tokens, _ = multi
    env = multi_env(provider.config.state_file.parent, MCP_TENANCY="single", OAUTH_ALLOWED_ATHLETES=OWNER)
    single = SingleUserOAuthProvider(oauth_config_from_env(env), fetch=FakeWeb())
    assert asyncio.run(single.load_access_token(tokens[ALPHA].access_token)) is None
    client = asyncio.run(single.get_client("c1"))
    assert client is not None
    assert refresh_with(single, client, tokens[ALPHA].refresh_token) is None
    assert refresh_with(single, client, tokens[OWNER].refresh_token) is not None
    text = single.config.state_file.read_text()
    saved = json.loads(text)
    assert saved["version"] == 1 and "grants" not in saved and len(saved["refresh_tokens"]) == 1
    assert next(iter(saved["refresh_tokens"].values()))["athlete_id"] == OWNER
    assert "v1." not in text


def test_older_releases_refuse_the_multi_user_state_file(multi):
    provider, _, _ = multi
    data = json.loads(provider.config.state_file.read_text())
    assert data["version"] == 2  # version 1 readers raise "written by a newer version"


def test_single_user_mode_is_unchanged_for_tool_calls(monkeypatch, fake_api):
    monkeypatch.setenv("MCP_TENANCY", "single")
    monkeypatch.setattr(mcp, "credential_source", None)
    monkeypatch.setattr(intervals_mcp_server.server.config, "athlete_id", OWNER)
    monkeypatch.setattr(intervals_mcp_server.server.config, "api_key", OWNER_KEY)
    from intervals_mcp_server.tools.wellness import get_wellness_data  # pylint: disable=import-outside-toplevel

    text = asyncio.run(get_wellness_data(start_date="2026-10-01", end_date="2026-10-01"))
    assert "OWNERDATA" in text and fake_api.requests[-1][0] == OWNER


def test_doctor_reports_multi_user_problems(tmp_path, monkeypatch):
    from intervals_mcp_server.cli import configuration_problems  # pylint: disable=import-outside-toplevel

    env = multi_env(tmp_path, MCP_TRANSPORT="streamable-http", ATHLETE_TIMEZONE="Europe/Vienna", MCP_ATHLETE_DAILY_REQUESTS="-1")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    write_state(tmp_path, VERSION_1_STATE)
    errors, warnings = configuration_problems(env)
    assert any("MCP_ATHLETE_DAILY_REQUESTS" in e for e in errors)
    assert any("ATHLETE_TIMEZONE applies to every athlete" in w for w in warnings)
    assert any("grants remove --legacy" in w for w in warnings)
    _, warnings = configuration_problems({**env, "MCP_ATHLETE_DAILY_REQUESTS": "", "MCP_ATHLETE_SHARE_PERCENT": "0",
                                          "MCP_OWNER_RESERVED_PERCENT": "100"})
    assert any("one request per 15 minutes" in w for w in warnings)
    assert any("every other athlete is refused" in w for w in warnings)
    errors, _ = configuration_problems({**env, "OAUTH_TOKEN_KEY": "", "MCP_ATHLETE_DAILY_REQUESTS": ""})
    assert any("OAUTH_TOKEN_KEY" in e for e in errors)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    errors, _ = configuration_problems({**env, "MCP_TRANSPORT": "stdio", "MCP_ATHLETE_DAILY_REQUESTS": ""})
    assert any("network transport" in e for e in errors)


def test_single_user_doctor_reports_other_allowed_athletes(tmp_path):
    from intervals_mcp_server.cli import configuration_problems  # pylint: disable=import-outside-toplevel

    env = multi_env(tmp_path, MCP_TENANCY="single", MCP_TRANSPORT="streamable-http")
    errors, _ = configuration_problems(env)
    assert any("sees the owner's data" in e for e in errors)


def test_no_token_reaches_the_logs(multi, caplog):
    provider, tokens, fake = multi
    caplog.set_level("DEBUG")
    fake.revoked.add("tok-bravo-secret-0002")
    call_as(provider, tokens[BRAVO].access_token, "get_activities", {"start_date": "2026-09-01", "end_date": "2026-10-05"})
    call_as(provider, tokens[ALPHA].access_token, "get_activities", {"athlete_id": BRAVO})
    for secret in (*TOKENS, OWNER_KEY, KEY):
        assert secret not in caplog.text


def test_expiring_tokens_are_refreshed_if_intervals_issues_refresh_tokens(tmp_path):
    clock = Clock()
    refreshed: list[str] = []

    async def exchange(code: str) -> dict[str, Any]:
        return {"access_token": "tok-old-0001", "refresh_token": "refresh-old", "expires_in": 3600,
                "scope": "ACTIVITY:READ", "athlete": {"id": code.split(":", 1)[1]}}

    async def renew(refresh_token: str) -> dict[str, Any]:
        refreshed.append(refresh_token)
        return {"access_token": "tok-new-0002", "expires_in": 3600}

    config = oauth_config_from_env(multi_env(tmp_path, OAUTH_ACCESS_TOKEN_TTL=str(10 * 3600)))
    provider = SingleUserOAuthProvider(config, clock=clock, fetch=FakeWeb(), intervals_exchange=exchange, intervals_refresh=renew)
    token = sign_in_intervals(provider, _client(provider), ALPHA)
    assert credential_of(provider, token.access_token).secret == "tok-old-0001" and not refreshed
    clock.now += 3600
    assert credential_of(provider, token.access_token).secret == "tok-new-0002"
    assert refreshed == ["refresh-old"]
    text = provider.config.state_file.read_text()
    assert "tok-new-0002" not in text and "refresh-old" not in text
    clock.now += 1800  # the renewed token (and the kept refresh token) are used from the state file
    reloaded = SingleUserOAuthProvider(config, clock=clock, fetch=FakeWeb(), intervals_exchange=exchange, intervals_refresh=renew)
    grant_id = next(iter(reloaded._grants))
    vault = reloaded.config.vault
    assert vault is not None
    payload = vault.open(reloaded._grants[grant_id].sealed or "", grant_context(grant_id, ALPHA))
    assert payload["access_token"] == "tok-new-0002" and payload["refresh_token"] == "refresh-old"


def test_expired_token_without_refresh_token_asks_to_reconnect(tmp_path):
    clock = Clock()

    async def exchange(code: str) -> dict[str, Any]:
        return {"access_token": "tok-short", "expires_in": 600, "athlete": {"id": code.split(":", 1)[1]}}

    config = oauth_config_from_env(multi_env(tmp_path))
    provider = SingleUserOAuthProvider(config, clock=clock, fetch=FakeWeb(), intervals_exchange=exchange)
    token = sign_in_intervals(provider, _client(provider), ALPHA)
    clock.now += 900
    with pytest.raises(CredentialError, match="reconnect"):
        credential_of(provider, token.access_token)


def test_legacy_grants_can_be_removed(tmp_path, capsys):
    write_state(tmp_path, VERSION_1_STATE)
    env = {"OAUTH_STATE_FILE": str(tmp_path / "state.json")}
    assert grants_main(["list"], env) == 0
    assert capsys.readouterr().out.count("legacy") == 2
    assert grants_main(["remove", "--legacy"], env) == 0
    assert json.loads((tmp_path / "state.json").read_text())["refresh_tokens"] == {}


def test_main_cli_forwards_the_grants_and_token_key_commands(tmp_path, monkeypatch, capsys):
    from intervals_mcp_server.cli import main  # pylint: disable=import-outside-toplevel

    monkeypatch.setenv("OAUTH_STATE_FILE", str(tmp_path / "missing.json"))
    assert main(["grants", "list"]) == 0
    assert "No connections" in capsys.readouterr().out
    assert main(["token-key"]) == 0
    assert capsys.readouterr().out.startswith("OAUTH_TOKEN_KEY=")


# --------------------------------------------------------------------------- #
# Review follow-ups: fair share, grant cap, activity ownership, comments, key rotation
# --------------------------------------------------------------------------- #


def test_fair_share_of_the_shared_window(monkeypatch):
    """R30-3: one athlete takes at most its share of the 15-minute budget, the owner keeps a reserve."""
    for name in ("MCP_ATHLETE_DAILY_REQUESTS", "MCP_ATHLETE_SHARE_PERCENT", "MCP_OWNER_RESERVED_PERCENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCP_APP_REQUESTS_PER_15MIN", "100")
    budgets = RequestBudgets(clock=lambda: 1_760_000_000.0)
    alpha, bravo, charlie = (Credential(a, "bearer", "t", f"g{a}", None) for a in (ALPHA, BRAVO, "i1003"))
    owner_token = Credential(OWNER, "bearer", "t", "g0", None, owner=True)
    assert all(budgets.admit(alpha) is None for _ in range(25))  # default share 25 %
    refused = budgets.admit(alpha)
    assert refused is not None and "for one athlete (25 of the 100" in refused
    assert all(budgets.admit(bravo) is None for _ in range(25))
    assert all(budgets.admit(charlie) is None for _ in range(25))  # a third friend still gets its share
    dave = Credential("i1004", "bearer", "t", "g4", None)
    assert all(budgets.admit(dave) is None for _ in range(5))
    assert "reserved for the server owner" in (budgets.admit(dave) or "")  # friends together: 80 of 100
    assert all(budgets.admit(owner_token) is None for _ in range(20))  # the owner's reserve
    assert "for all connected athletes together" in (budgets.admit(owner_token) or "")


def test_retries_count_against_the_budgets(multi, monkeypatch):
    provider, tokens, fake = multi

    def busy(_athlete: str, _rest: list[str]) -> Any:
        raise RuntimeError("unused")

    original = fake.__call__

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wellness"):
            fake.requests.append((ALPHA, request.method, request.url.path))
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow down"})
        return original(request)

    monkeypatch.setattr(api_client, "_get_httpx_client", _client_with(answer))
    del busy
    with use_credential(credential_of(provider, tokens[ALPHA].access_token)):
        result = asyncio.run(api_client.make_intervals_request(f"/athlete/{ALPHA}/wellness"))
    assert result["status_code"] == 429
    assert BUDGETS.used_today(ALPHA) == api_client.MAX_ATTEMPTS


def _client_with(handler: Any) -> Any:
    shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def get_client() -> httpx.AsyncClient:
        return shared

    return get_client


def test_owner_grants_are_never_evicted(tmp_path, monkeypatch):
    """The owner's connections (e.g. the long-lived ChatGPT grant) are exempt from both caps."""
    clock = Clock()
    provider = make_provider(tmp_path, clock=clock, OAUTH_MAX_GRANTS_PER_ATHLETE="2")
    client = _client(provider)
    owner_tokens = []
    for _ in range(4):
        clock.now += 60
        owner_tokens.append(sign_in_password(provider, client))
    assert all(asyncio.run(provider.load_access_token(t.access_token)) is not None for t in owner_tokens)
    monkeypatch.setattr(auth, "MAX_GRANT_RECORDS", 5)
    friends = []
    for athlete in (ALPHA, BRAVO, ALPHA):
        clock.now += 60
        friends.append(sign_in_intervals(provider, client, athlete))
    assert all(asyncio.run(provider.load_access_token(t.access_token)) is not None for t in owner_tokens)
    assert asyncio.run(provider.load_access_token(friends[0].access_token)) is None  # the total cap took a friend's


def test_single_user_mode_has_no_grant_cap(tmp_path):
    single = SingleUserOAuthProvider(
        oauth_config_from_env(multi_env(tmp_path, MCP_TENANCY="single", OAUTH_ALLOWED_ATHLETES=OWNER,
                                        OAUTH_MAX_GRANTS_PER_ATHLETE="2")), fetch=FakeWeb(),
    )
    client = _client(single)
    tokens = [sign_in_password(single, client) for _ in range(4)]
    assert all(asyncio.run(single.load_access_token(t.access_token)) is not None for t in tokens)
    assert len(json.loads(single.config.state_file.read_text())["refresh_tokens"]) == 4


def test_grants_per_athlete_are_limited(tmp_path):
    """R30-4: an athlete keeps at most OAUTH_MAX_GRANTS_PER_ATHLETE grants; the least recently used go."""
    clock = Clock()
    provider = make_provider(tmp_path, clock=clock, OAUTH_MAX_GRANTS_PER_ATHLETE="3")
    client = _client(provider)
    tokens = []
    for _ in range(5):
        clock.now += 60
        tokens.append(sign_in_intervals(provider, client, ALPHA))
    grants = json.loads(provider.config.state_file.read_text())["grants"]
    assert len(grants) == 3
    assert asyncio.run(provider.load_access_token(tokens[0].access_token)) is None
    assert asyncio.run(provider.load_access_token(tokens[4].access_token)) is not None
    owner = sign_in_password(provider, client)  # other athletes are not affected
    assert credential_of(provider, owner.access_token).kind == "apikey"


def test_activity_sub_resources_of_another_athlete_are_refused(multi):
    """R30-5: streams, intervals, messages and writes of another athlete's activity are never requested."""
    provider, tokens, fake = multi
    fake.coach_mode = True  # Intervals.icu would answer them (coach token, shared activity)
    for tool in ("get_activity_streams", "get_activity_intervals", "get_activity_messages"):
        before = len(fake.requests)
        text = call_as(provider, tokens[ALPHA].access_token, tool, {"activity_id": "act-BRAVODATA"})
        sent = [path for _, _, path in fake.requests[before:]]
        assert sent in ([], ["/activity/act-BRAVODATA"]), (tool, sent)
        assert "BRAVODATA long ride" not in text


def test_own_activity_sub_resources_look_the_owner_up_once(multi):
    provider, tokens, fake = multi
    for _ in range(2):
        call_as(provider, tokens[ALPHA].access_token, "get_activity_intervals", {"activity_id": "act-ALPHADATA"})
    lookups = [path for _, method, path in fake.requests if path == "/activity/act-ALPHADATA"]
    assert len(lookups) <= 2  # the tool's own read plus at most one owner lookup, then cached
    assert any(path.startswith("/activity/act-ALPHADATA/") for _, _, path in fake.requests)


def test_activity_comments_need_the_optional_permission(multi):
    """User decision: CHATS is off by default; the comment tools say how to get it."""
    provider, tokens, fake = multi
    before = len(fake.requests)
    text = call_as(provider, tokens[ALPHA].access_token, "get_activity_messages", {"activity_id": "act-ALPHADATA"})
    assert "Activity comments" in text and "CHATS:READ" in text and len(fake.requests) == before


@pytest.mark.usefixtures("fake_api")
def test_comments_work_when_offered_and_chosen(tmp_path, monkeypatch):  # pylint: disable=too-many-locals
    for name, value in multi_env(tmp_path, INTERVALS_OAUTH_OFFER_CHATS="true").items():
        monkeypatch.setenv(name, value)
    provider = make_provider(tmp_path, INTERVALS_OAUTH_OFFER_CHATS="true")
    monkeypatch.setattr(mcp, "credential_source", provider)
    client = _client(provider)
    request_id = _request(provider, client)
    location, browser = provider.begin_intervals_login(request_id, ("read",), "k", chats=True)
    state = parse_qs(urlsplit(location).query)["state"][0]

    async def with_chats(code: str) -> dict[str, Any]:
        return {"access_token": "tok-alpha-secret-0001", "scope": parse_qs(urlsplit(location).query)["scope"][0],
                "athlete": {"id": code.split(":", 1)[1]}}

    provider._intervals_exchange = with_chats
    token = _exchange(provider, client, asyncio.run(provider.finish_intervals_login(state, browser, f"code:{ALPHA}", None, "k")))
    assert "CHATS:READ" in (credential_of(provider, token.access_token).intervals_scopes or set())
    text = call_as(provider, token.access_token, "get_activity_messages", {"activity_id": "act-ALPHADATA"})
    assert "permission" not in text


def test_consent_page_offers_comments_only_when_enabled(tmp_path):
    from intervals_mcp_server.auth_pages import _consent_body  # pylint: disable=import-outside-toplevel

    for offered in (False, True):
        provider = make_provider(tmp_path / str(offered), INTERVALS_OAUTH_OFFER_CHATS=str(offered).lower())
        page = _consent_body(provider, _request(provider, _client(provider)), "athlete", None, "form-token")
        assert ('name="chats"' in page) is offered
        assert "revokes the connection on disconnect" in page and "30 days without use" in page


def test_key_rotation_reseals_tokens_at_their_next_use(tmp_path):
    """R30-7: a token opened with an older key is sealed again with the first key."""
    old_key, new_key = KEY, generate_key()
    provider = make_provider(tmp_path)
    token = sign_in_intervals(provider, _client(provider), ALPHA)
    rotated = make_provider(tmp_path, OAUTH_TOKEN_KEY=f"{new_key},{old_key}")
    assert rotated.grant_overview()["old_key_tokens"] == 1
    client = asyncio.run(rotated.get_client("c1"))
    assert client is not None
    fresh = refresh_with(rotated, client, token.refresh_token)
    assert credential_of(rotated, fresh.access_token).secret == "tok-alpha-secret-0001"
    only_new = make_provider(tmp_path, OAUTH_TOKEN_KEY=new_key)
    assert only_new.grant_overview() | {"old_key_tokens": 0, "unreadable_tokens": 0} == only_new.grant_overview()


def test_status_shows_friends_no_sign_in_details(multi):
    provider, tokens, _ = multi
    friend = json.loads(call_as(provider, tokens[ALPHA].access_token, "get_server_status", {"output_format": "json"}))
    owner = json.loads(call_as(provider, tokens[OWNER].access_token, "get_server_status", {"output_format": "json"}))
    assert friend["auth"] == {"mode": "oauth"}
    assert "login" in owner["auth"] and owner["connection"]["credential"].startswith("server API key")


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to change file owners")
def test_cli_run_as_root_keeps_the_owner_of_the_state_file(multi):
    """R30-8: `docker exec` as root must not hand the state file to root."""
    provider, _, _ = multi
    path = provider.config.state_file
    os.chown(path, 4321, 4321)
    assert grants_main(["remove", "1002"], {"OAUTH_STATE_FILE": str(path)}) == 0
    assert (path.stat().st_uid, path.stat().st_gid) == (4321, 4321)
    lock = path.with_name(path.name + ".lock")
    assert lock.exists()


# --------------------------------------------------------------------------- #
# R30-15: the grants CLI never follows links planted next to the state file
# --------------------------------------------------------------------------- #


def _victim(tmp_path: Path) -> Path:
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch")
    victim.chmod(0o600)
    return victim


def _untouched(victim: Path, before: os.stat_result) -> bool:
    after = victim.stat()
    return victim.read_text() == "do not touch" and (after.st_uid, after.st_gid, after.st_mode) == (
        before.st_uid, before.st_gid, before.st_mode)


@pytest.mark.parametrize("command", [["remove", "1002"], ["remove", "--grant", "none"], ["prune", "--days", "1"]])
def test_symlinked_lock_file_is_refused(multi, tmp_path, command, capsys):
    provider, _, _ = multi
    state = provider.config.state_file
    lock = state.with_name(state.name + ".lock")
    lock.unlink(missing_ok=True)
    victim = _victim(tmp_path)
    before = victim.stat()
    lock.symlink_to(victim)
    env = {"OAUTH_STATE_FILE": str(state), "ATHLETE_ID": OWNER}
    assert grants_main(command, env) == 2
    assert "grants:" in capsys.readouterr().err
    assert _untouched(victim, before)
    # The server refuses to write through it as well.
    with pytest.raises(OSError):
        asyncio.run(provider.register_client(dcr_client("c-link")))
    assert _untouched(victim, before)


def test_hard_linked_lock_file_is_refused(multi, tmp_path):
    provider, _, _ = multi
    state = provider.config.state_file
    lock = state.with_name(state.name + ".lock")
    lock.unlink(missing_ok=True)
    victim = _victim(tmp_path)
    before = victim.stat()
    os.link(victim, lock)
    assert grants_main(["adopt-legacy", "--owner"], {"OAUTH_STATE_FILE": str(state), "ATHLETE_ID": OWNER}) == 2
    assert _untouched(victim, before)


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to create files of other users")
def test_foreign_owned_lock_file_is_refused(multi):
    provider, _, _ = multi
    state = provider.config.state_file
    lock = state.with_name(state.name + ".lock")
    lock.touch()
    os.chown(lock, 4321, 4321)  # neither root nor the owner of the state file
    assert grants_main(["remove", "1002"], {"OAUTH_STATE_FILE": str(state)}) == 2
    assert (lock.stat().st_uid, json.loads(state.read_text())["version"]) == (4321, 2)


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root")
def test_root_refuses_a_state_directory_of_another_user(tmp_path, capsys):
    directory = tmp_path / "state"
    directory.mkdir()
    state = directory / "state.json"
    state.write_text(json.dumps({"version": 1, "clients": {}, "refresh_tokens": {}}))
    os.chown(directory, 4321, 4321)
    os.chown(state, 4321, 4321)
    env = {"OAUTH_STATE_FILE": str(state)}
    assert grants_main(["remove", "--grant", "x"], env) == 2
    assert "refusing to run as root" in capsys.readouterr().err
    assert grants_main(["remove", "--grant", "x", "--allow-root"], env) == 1  # nothing to remove, but it ran
    lock = directory / "state.json.lock"
    assert lock.stat().st_uid == 4321  # created by this run: given to the state file's owner
