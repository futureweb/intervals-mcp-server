# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name
"""Regression tests for the OAuth hardening from the security and operations review
(SEC-4..SEC-10, OPS-2..OPS-5, OPS-10, OPS-14).

Synthetic data only; client metadata documents and the Intervals.icu token endpoint are
in-memory fakes, and no request leaves the process.
"""

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp.server.auth.provider import AuthorizationParams, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.testclient import TestClient

from intervals_mcp_server import auth
from intervals_mcp_server.auth import (
    LoginError,
    SingleUserOAuthProvider,
    client_key,
    oauth_config_from_env,
    set_request_client_key,
)
from intervals_mcp_server.auth_clients import ClientMetadataResolver, allowlist_match, normalize_allow_entry
from intervals_mcp_server.http_app import PRM_PATH
from tests.oauth_helpers import consent_token, submit_consent
from tests.test_oauth_extensions import (
    CHATGPT_DOC,
    CHATGPT_ID,
    DCR_REDIRECT,
    PASSWORD,
    STABLE_REDIRECT,
    FakeIntervals,
    FakeWeb,
    authorize,
    make_app,
    make_env,
    pkce_pair,
    request_id_from,
)
REDIRECT = "https://chatgpt.com/connector/oauth/test-callback"


class Clock:  # pylint: disable=too-few-public-methods
    """A clock the test moves forward."""

    def __init__(self, now: float | None = None) -> None:
        self.now = time.time() if now is None else now

    def __call__(self) -> float:
        return self.now


def provider_for(tmp_path: Path, clock: Clock | None = None, **extra: str) -> SingleUserOAuthProvider:
    return SingleUserOAuthProvider(oauth_config_from_env(make_env(tmp_path, **extra)), clock=clock or time.time, fetch=FakeWeb())


def dcr_client(client_id: str = "c1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_id_issued_at=int(time.time()),
        redirect_uris=[AnyUrl(REDIRECT)],
        token_endpoint_auth_method="none",
        scope="mcp intervals:read intervals:write",
    )


def auth_params(state: str = "s") -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=["mcp", "intervals:read"],
        code_challenge="challenge",
        redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


def authorize_as(provider: SingleUserOAuthProvider, client: OAuthClientInformationFull, host: str) -> str:
    """/authorize from client address *host*; returns the pending request id."""

    async def run() -> str:
        set_request_client_key(host)
        return await provider.authorize(client, auth_params())

    return parse_qs(urlsplit(asyncio.run(run())).query)["request"][0]


def issue_tokens(provider: SingleUserOAuthProvider, client: OAuthClientInformationFull) -> Any:
    request_id = authorize_as(provider, client, "198.51.100.1")
    code = parse_qs(urlsplit(provider.complete_login(request_id, "198.51.100.1", ("read",))).query)["code"][0]
    loaded = asyncio.run(provider.load_authorization_code(client, code))
    assert loaded is not None
    return asyncio.run(provider.exchange_authorization_code(client, loaded))


def refresh_with(provider: SingleUserOAuthProvider, client: OAuthClientInformationFull, token: str) -> Any:
    async def run() -> Any:
        loaded = await provider.load_refresh_token(client, token)
        if loaded is None:
            return None
        return await provider.exchange_refresh_token(client, loaded, loaded.scopes)

    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# SEC-4: consent form bound to the browser (CSRF)
# --------------------------------------------------------------------------- #


def test_consent_post_from_another_site_is_refused(tmp_path):
    """The attack from the review: a cross-site page auto-submits the consent with all permissions."""
    env = make_env(tmp_path, MCP_PERMISSIONS="read,write,destructive,admin", OAUTH_LOGIN="intervals,password",
                   INTERVALS_OAUTH_CLIENT_ID="1", INTERVALS_OAUTH_CLIENT_SECRET="s")
    _, provider, attacker = make_app(env)
    _, challenge = pkce_pair()
    request_id = request_id_from(authorize(attacker, CHATGPT_ID, STABLE_REDIRECT, challenge))
    token = consent_token(attacker, request_id)  # the attacker can load the page with their own cookie
    victim = TestClient(attacker.app, base_url="http://127.0.0.1:8000", follow_redirects=False)
    form = {"request": request_id, "action": "intervals", "grant": ["write", "destructive", "admin"], "csrf": token}

    cross_site = victim.post("/oauth/login", data=form, headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"})
    assert cross_site.status_code == 403 and "location" not in cross_site.headers
    # Without the headers (older browser): the victim's browser has no consent cookie for the token.
    no_cookie = victim.post("/oauth/login", data=form)
    assert no_cookie.status_code == 403 and "location" not in no_cookie.headers
    assert "Please confirm again" in no_cookie.text
    garbage = attacker.post("/oauth/login", data={**form, "csrf": "ünïcode"})
    assert garbage.status_code == 403
    assert not provider._upstream, "no Intervals.icu sign-in may start for a forged form"
    # The request is still pending: the athlete can still use the page normally.
    assert provider.pending_login(request_id) is not None


def test_consent_post_checks_origin_and_sec_fetch_site(tmp_path):
    _, _, client = make_app(make_env(tmp_path))
    _, challenge = pkce_pair()

    def attempt(headers: dict[str, str]) -> int:
        request_id = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge))
        data = {"request": request_id, "action": "password", "username": "athlete", "password": PASSWORD,
                "csrf": consent_token(client, request_id)}
        return client.post("/oauth/login", data=data, headers=headers).status_code

    assert attempt({"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}) == 302
    assert attempt({"Origin": "http://localhost:80"}) == 302  # default port
    assert attempt({"Origin": "null", "Sec-Fetch-Site": "same-origin"}) == 302
    assert attempt({}) == 302  # non-browser client: the cookie-bound token still applies
    assert attempt({"Origin": "null"}) == 403
    assert attempt({"Origin": "https://localhost"}) == 403
    assert attempt({"Sec-Fetch-Site": "same-site"}) == 403
    assert attempt({"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"}) == 403


def test_consent_cookie_and_page_headers(tmp_path):
    """https deployments use a __Host- cookie; the page keeps working inside OAuth pop-ups."""
    env = make_env(tmp_path, MCP_PUBLIC_URL="https://mcp.example.com")
    _, _, plain = make_app(env)
    client = TestClient(plain.app, base_url="https://mcp.example.com", follow_redirects=False)
    _, challenge = pkce_pair()
    response = client.get(
        "/authorize",
        params={"client_id": CHATGPT_ID, "redirect_uri": STABLE_REDIRECT, "response_type": "code", "code_challenge": challenge,
                "code_challenge_method": "S256", "state": "st", "resource": "https://mcp.example.com"},
        headers={"Host": "mcp.example.com"},
    )
    assert response.status_code == 302, response.text
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["request"][0]
    page = client.get("/oauth/login", params={"request": request_id}, headers={"Host": "mcp.example.com"})
    cookie = page.headers["set-cookie"]
    assert cookie.startswith("__Host-intervals_mcp_csrf=") and "Secure" in cookie and "HttpOnly" in cookie
    assert "Path=/" in cookie and "Domain" not in cookie and "SameSite=lax" in cookie
    assert "form-action" not in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "same-origin"


def test_second_consent_tab_keeps_the_first_form_valid(tmp_path):
    """The consent cookie is reused, so two open consent pages do not invalidate each other."""
    _, _, client = make_app(make_env(tmp_path))
    _, challenge = pkce_pair()
    first = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge))
    second = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge))
    first_token = consent_token(client, first)
    consent_token(client, second)
    response = client.post("/oauth/login", data={"request": first, "action": "password", "username": "athlete",
                                                 "password": PASSWORD, "csrf": first_token})
    assert response.status_code == 302


# --------------------------------------------------------------------------- #
# SEC-5: body limit on the login route, registration rate limit
# --------------------------------------------------------------------------- #


def test_login_form_body_is_limited(tmp_path):
    _, _, client = make_app(make_env(tmp_path))
    _, challenge = pkce_pair()
    request_id = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge))
    huge = client.post("/oauth/login", data={"request": request_id, "username": "x" * 100_000})
    assert huge.status_code == 413
    multipart = client.post("/oauth/login", data={"request": request_id}, files={"f": ("a.txt", b"x")})
    assert multipart.status_code == 415
    many = "&".join(f"f{i}=1" for i in range(200))
    fields = client.post("/oauth/login", content=many, headers={"content-type": "application/x-www-form-urlencoded"})
    assert fields.status_code == 400


def test_registration_is_rate_limited_per_address(tmp_path):
    _, provider, client = make_app(make_env(tmp_path))
    body = {"client_name": "x", "redirect_uris": [DCR_REDIRECT], "token_endpoint_auth_method": "none"}
    for _ in range(auth.REGISTRATIONS_PER_KEY):
        assert client.post("/register", json=body).status_code == 201
    refused = client.post("/register", json=body)
    assert refused.status_code == 400 and "too many" in refused.json()["error_description"]
    assert len(provider._clients) == auth.REGISTRATIONS_PER_KEY


def test_client_in_the_middle_of_consent_is_not_evicted(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "MAX_CLIENTS", 3)
    provider = provider_for(tmp_path)
    clients = [dcr_client(f"c{i}") for i in range(3)]
    for registered in clients:
        asyncio.run(provider.register_client(registered))
    authorize_as(provider, clients[0], "198.51.100.1")  # c0 is the oldest but has a pending sign-in
    asyncio.run(provider.register_client(dcr_client("c3")))
    assert "c0" in provider._clients and "c1" not in provider._clients


# --------------------------------------------------------------------------- #
# SEC-6 / OPS-4: per-address caps of unauthenticated tables
# --------------------------------------------------------------------------- #


def test_authorize_flood_does_not_evict_another_address(tmp_path):
    provider = provider_for(tmp_path)
    registered = dcr_client()
    asyncio.run(provider.register_client(registered))
    victim = authorize_as(provider, registered, "198.51.100.7")
    for _ in range(auth.MAX_PENDING_LOGINS + 100):
        authorize_as(provider, registered, "203.0.113.9")
    assert provider.pending_login(victim) is not None
    assert sum(1 for p in provider._pending.values() if p.key == "203.0.113.9") == auth.MAX_PENDING_PER_KEY


def test_authorize_flood_from_many_addresses_keeps_the_single_victim(tmp_path):
    provider = provider_for(tmp_path)
    registered = dcr_client()
    asyncio.run(provider.register_client(registered))
    victim = authorize_as(provider, registered, "198.51.100.7")
    for i in range(auth.MAX_PENDING_LOGINS + 50):
        authorize_as(provider, registered, f"2001:db8:{i % 40:x}::{i}")  # 40 /64 networks, many hosts
    assert provider.pending_login(victim) is not None
    assert len(provider._pending) <= auth.MAX_PENDING_LOGINS


def test_client_key_groups_ipv6_networks():
    assert client_key("2001:db8:1:2::5") == client_key("2001:db8:1:2:ffff::1") == "2001:db8:1:2::/64"
    assert client_key("2001:db8:1:3::5") != client_key("2001:db8:1:2::5")
    assert client_key("::ffff:198.51.100.4") == "198.51.100.4"
    assert client_key("198.51.100.4") == "198.51.100.4"
    assert client_key(None) == "unknown" and client_key("testclient") == "testclient"


def test_rate_limiter_table_is_bounded(monkeypatch):
    monkeypatch.setattr(auth, "MAX_RATE_LIMIT_KEYS", 10)
    limiter = auth._LoginRateLimiter(5, 900, time.time)
    for i in range(100):
        limiter.record_failure(f"198.51.100.{i}")
    assert len(limiter) <= 10


def test_upstream_logins_per_request_and_address(tmp_path):
    env = make_env(tmp_path, OAUTH_LOGIN="intervals", INTERVALS_OAUTH_CLIENT_ID="1", INTERVALS_OAUTH_CLIENT_SECRET="s")
    provider = SingleUserOAuthProvider(oauth_config_from_env(env), fetch=FakeWeb())
    registered = dcr_client()
    asyncio.run(provider.register_client(registered))
    request_id = authorize_as(provider, registered, "198.51.100.1")
    for _ in range(5):
        provider.begin_intervals_login(request_id, ("read",), "198.51.100.1")
    assert len(provider._upstream) == 1  # only the latest attempt of a request counts


# --------------------------------------------------------------------------- #
# SEC-7: client metadata resolver
# --------------------------------------------------------------------------- #


def test_metadata_cache_and_fetches_are_bounded():
    web = FakeWeb()
    clock = Clock(1_000_000.0)
    resolver = ClientMetadataResolver(["chatgpt.com"], "mcp", fetch=web, clock=clock)
    assert asyncio.run(resolver.get(CHATGPT_ID)) is not None
    for i in range(300):
        clock.now += 1.0
        asyncio.run(resolver.get(f"https://chatgpt.com/random/{i}.json"))
    assert len(resolver._cache) <= 256
    assert len(web.calls) < 300 * 0.6, "cache misses share a fetch budget"
    assert CHATGPT_ID in resolver._cache, "a good document outlives the rejected ones"


def test_metadata_urls_with_query_or_control_characters_are_rejected():
    resolver = ClientMetadataResolver(["chatgpt.com"], "mcp", fetch=FakeWeb())
    for url in ("https://chatgpt.com/oauth/client.json?x=1", "https://chatgpt.com/a\x01b", "https://chatgpt.com/" + "a" * 600,
                "https://chatgpt.com/a b", "https://chatgpt.com:99999/x"):
        assert not resolver.url_allowed(url)
        assert asyncio.run(resolver.get(url)) is None


def test_invalid_client_id_gives_an_oauth_error_not_500(tmp_path):
    _, _, client = make_app(make_env(tmp_path))
    _, challenge = pkce_pair()
    response = client.get("/authorize", params={"client_id": "https://chatgpt.com/a\x01b", "redirect_uri": STABLE_REDIRECT,
                                                "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256"})
    assert response.status_code == 400
    token = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": "x", "client_id": "https://chatgpt.com/a\x01b"})
    assert token.status_code in (400, 401)


def test_metadata_fetch_errors_keep_the_last_good_document():
    clock = Clock(1_000_000.0)
    state = {"fail": False}
    web = FakeWeb()

    async def flaky(url: str) -> tuple[int, dict[str, str], bytes]:
        if state["fail"]:
            raise httpx.InvalidURL("broken")
        return await web(url)

    resolver = ClientMetadataResolver(["chatgpt.com"], "mcp", fetch=flaky, clock=clock)
    assert asyncio.run(resolver.get(CHATGPT_ID)) is not None
    state["fail"] = True
    clock.now += 3600
    assert asyncio.run(resolver.get(CHATGPT_ID)) is not None
    clock.now += 2 * 24 * 3600
    assert asyncio.run(resolver.get(CHATGPT_ID)) is None


def test_concurrent_metadata_requests_share_one_fetch():
    calls: list[str] = []

    async def slow(url: str) -> tuple[int, dict[str, str], bytes]:
        calls.append(url)
        await asyncio.sleep(0.05)
        return 200, {}, json.dumps(CHATGPT_DOC).encode()

    resolver = ClientMetadataResolver(["chatgpt.com"], "mcp", fetch=slow)

    async def run() -> list[Any]:
        return await asyncio.gather(*(resolver.get(CHATGPT_ID) for _ in range(5)))

    assert all(result is not None for result in asyncio.run(run()))
    assert calls == [CHATGPT_ID]


# --------------------------------------------------------------------------- #
# SEC-8: refresh token reuse detection with a grace period
# --------------------------------------------------------------------------- #


def test_refresh_retry_within_the_grace_period_works(tmp_path):
    """A client that lost the refresh response can retry with the old token."""
    clock = Clock()
    provider = provider_for(tmp_path, clock)
    registered = dcr_client()
    asyncio.run(provider.register_client(registered))
    first = issue_tokens(provider, registered)
    rotated = refresh_with(provider, registered, first.refresh_token)
    clock.now += 30
    retried = refresh_with(provider, registered, first.refresh_token)
    assert retried is not None and retried.refresh_token not in (rotated.refresh_token, first.refresh_token)
    # both answers work: the client keeps whichever it received
    assert refresh_with(provider, registered, rotated.refresh_token) is not None
    assert refresh_with(provider, registered, retried.refresh_token) is not None


def test_refresh_reuse_after_the_grace_period_revokes_the_grant(tmp_path):
    clock = Clock()
    provider = provider_for(tmp_path, clock)
    registered = dcr_client()
    asyncio.run(provider.register_client(registered))
    first = issue_tokens(provider, registered)
    attacker = refresh_with(provider, registered, first.refresh_token)  # e.g. a leaked token used first
    clock.now += auth.DEFAULT_REFRESH_REUSE_GRACE + 1
    assert refresh_with(provider, registered, first.refresh_token) is None
    assert refresh_with(provider, registered, attacker.refresh_token) is None
    assert asyncio.run(provider.load_access_token(attacker.access_token)) is None
    assert not provider._tokens.refresh
    saved = json.loads(provider.config.state_file.read_text())
    assert saved["refresh_tokens"] == {}


def test_refresh_reuse_by_another_client_does_not_revoke(tmp_path):
    clock = Clock()
    provider = provider_for(tmp_path, clock)
    registered, other = dcr_client("c1"), dcr_client("c2")
    for item in (registered, other):
        asyncio.run(provider.register_client(item))
    first = issue_tokens(provider, registered)
    rotated = refresh_with(provider, registered, first.refresh_token)
    clock.now += 3600
    assert refresh_with(provider, other, first.refresh_token) is None
    assert refresh_with(provider, registered, rotated.refresh_token) is not None


def test_revoking_with_a_just_rotated_token_revokes_the_grant(tmp_path):
    provider = provider_for(tmp_path)
    registered = dcr_client()
    asyncio.run(provider.register_client(registered))
    first = issue_tokens(provider, registered)
    rotated = refresh_with(provider, registered, first.refresh_token)
    loaded = asyncio.run(provider.load_refresh_token(registered, first.refresh_token))
    assert isinstance(loaded, RefreshToken)
    asyncio.run(provider.revoke_token(loaded))
    assert refresh_with(provider, registered, rotated.refresh_token) is None


# --------------------------------------------------------------------------- #
# SEC-9: allowlists with paths, no query strings
# --------------------------------------------------------------------------- #


def test_allow_entries_with_paths():
    assert normalize_allow_entry(" ChatGPT.com/oauth/Client.json ") == "chatgpt.com/oauth/Client.json"
    assert normalize_allow_entry("https://claude.ai") == "claude.ai"
    for bad in ("chatgpt.com/x?y=1", "", "/path", "a b.com", "user@chatgpt.com"):
        with pytest.raises(ValueError):
            normalize_allow_entry(bad)
    entries = ["chatgpt.com/oauth/client.json", "claude.ai/api/mcp/"]
    assert allowlist_match("chatgpt.com", "/oauth/client.json", entries)
    assert not allowlist_match("chatgpt.com", "/oauth/client.json.evil", entries)
    assert not allowlist_match("chatgpt.com", "/backend-api/x", entries)
    assert allowlist_match("claude.ai", "/api/mcp/auth_callback", entries)
    assert not allowlist_match("claude.ai", "/api/mcpx", entries)


def test_exact_client_id_allowlist_keeps_chatgpt(tmp_path):
    env = make_env(tmp_path, OAUTH_CLIENT_HOSTS="chatgpt.com/oauth/client.json,claude.ai")
    _, provider, client = make_app(env)
    _, challenge = pkce_pair()
    assert authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge).status_code == 302
    assert not provider.metadata_clients.url_allowed("https://chatgpt.com/share/abc.json")
    assert provider.metadata_clients.url_allowed("https://claude.ai/oauth/client.json")


def test_document_redirect_uris_must_stay_on_trusted_hosts():
    off_host = dict(CHATGPT_DOC, redirect_uris=["https://evil.example/cb"])
    resolver = ClientMetadataResolver(["chatgpt.com"], "mcp", fetch=FakeWeb({CHATGPT_ID: off_host}))
    assert asyncio.run(resolver.get(CHATGPT_ID)) is None
    loopback = dict(CHATGPT_DOC, redirect_uris=["http://127.0.0.1:3334/cb", STABLE_REDIRECT])
    resolver = ClientMetadataResolver(["chatgpt.com"], "mcp", fetch=FakeWeb({CHATGPT_ID: loopback}))
    assert asyncio.run(resolver.get(CHATGPT_ID)) is not None


def test_dcr_redirect_uris_without_query_and_with_path_prefixes(tmp_path):
    _, _, client = make_app(make_env(tmp_path))
    body = {"redirect_uris": [DCR_REDIRECT + "?next=https://evil.example"], "token_endpoint_auth_method": "none"}
    assert client.post("/register", json=body).status_code == 400
    for uri in ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback",
                "https://chatgpt.com/connector_platform_oauth_redirect", "http://localhost:33418/callback"):
        assert client.post("/register", json={"redirect_uris": [uri], "token_endpoint_auth_method": "none"}).status_code == 201

    _, _, strict = make_app(make_env(tmp_path / "strict", OAUTH_REDIRECT_HOSTS="claude.ai/api/mcp/auth_callback"))
    ok = strict.post("/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"], "token_endpoint_auth_method": "none"})
    assert ok.status_code == 201
    other_path = strict.post("/register", json={"redirect_uris": ["https://claude.ai/share/x"], "token_endpoint_auth_method": "none"})
    assert other_path.status_code == 400


# --------------------------------------------------------------------------- #
# SEC-10 / OPS-5: sign-in hardening, nothing CPU heavy on the event loop
# --------------------------------------------------------------------------- #


def test_password_hash_is_checked_off_the_event_loop(tmp_path, monkeypatch):
    _, provider, client = make_app(make_env(tmp_path))
    seen: list[bool] = []
    original = provider.verify_credentials

    def recording(username: str, password: str) -> bool:
        try:
            asyncio.get_running_loop()
            seen.append(False)
        except RuntimeError:
            seen.append(True)  # no running loop: a worker thread
        return original(username, password)

    monkeypatch.setattr(provider, "verify_credentials", recording)
    _, challenge = pkce_pair()
    request_id = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge))
    assert submit_consent(client, {"request": request_id, "action": "password", "username": "athlete", "password": PASSWORD}).status_code == 302
    assert seen == [True]


def test_state_file_is_written_off_the_event_loop(tmp_path, monkeypatch):
    provider = provider_for(tmp_path)
    threads: list[int] = []
    original = provider._write_state

    def recording(data: dict[str, Any]) -> None:
        threads.append(threading.get_ident())
        original(data)

    monkeypatch.setattr(provider, "_write_state", recording)

    async def register() -> int:
        await provider.register_client(dcr_client())
        return threading.get_ident()

    loop_thread = asyncio.run(register())
    assert threads and threads[0] != loop_thread


def test_global_failure_budget_pauses_password_sign_in(tmp_path):
    env = make_env(tmp_path, OAUTH_LOGIN_GLOBAL_RATE_LIMIT="3", OAUTH_LOGIN_RATE_LIMIT="100")
    _, _, client = make_app(env)
    _, challenge = pkce_pair()
    request_id = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge))
    for _ in range(3):
        assert submit_consent(client, {"request": request_id, "action": "password", "username": "athlete", "password": "no"}).status_code == 401
    paused = submit_consent(client, {"request": request_id, "action": "password", "username": "athlete", "password": PASSWORD})
    assert paused.status_code == 429


def test_totp_code_is_not_used_up_by_a_wrong_password(tmp_path):
    from intervals_mcp_server.auth_totp import generate_secret, totp  # pylint: disable=import-outside-toplevel

    secret = generate_secret()
    _, _, client = make_app(make_env(tmp_path, OAUTH_TOTP_SECRET=secret))
    _, challenge = pkce_pair()
    request_id = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge))
    code = totp(secret, time.time())
    wrong = submit_consent(client, {"request": request_id, "action": "password", "username": "athlete", "password": "typo", "totp": code})
    assert wrong.status_code == 401
    right = submit_consent(client, {"request": request_id, "action": "password", "username": "athlete", "password": PASSWORD, "totp": code})
    assert right.status_code == 302
    replay_id = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge))
    replay = submit_consent(client, {"request": replay_id, "action": "password", "username": "athlete", "password": PASSWORD, "totp": code})
    assert replay.status_code == 401


# --------------------------------------------------------------------------- #
# OPS-2: state file writability and consistency
# --------------------------------------------------------------------------- #


def test_unwritable_state_directory_fails_at_startup(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    env = make_env(tmp_path, OAUTH_STATE_FILE=str(blocker / "state.json"))
    with pytest.raises(ValueError, match="OAUTH_STATE_FILE directory .* is not writable"):
        SingleUserOAuthProvider(oauth_config_from_env(env))


def test_failed_write_keeps_memory_and_disk_consistent(tmp_path, monkeypatch):
    provider = provider_for(tmp_path)
    registered = dcr_client()
    asyncio.run(provider.register_client(registered))
    first = issue_tokens(provider, registered)
    before = provider.config.state_file.read_bytes()

    def disk_full(_data: dict[str, Any]) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(provider, "_write_state", disk_full)
    with pytest.raises(OSError):
        asyncio.run(provider.register_client(dcr_client("c2")))
    assert "c2" not in provider._clients
    with pytest.raises(OSError):
        refresh_with(provider, registered, first.refresh_token)
    # the refresh token survived the failed rotation; a code survives a failed exchange
    request_id = authorize_as(provider, registered, "198.51.100.1")
    code = parse_qs(urlsplit(provider.complete_login(request_id, "198.51.100.1")).query)["code"][0]
    loaded = asyncio.run(provider.load_authorization_code(registered, code))
    assert loaded is not None
    with pytest.raises(OSError):
        asyncio.run(provider.exchange_authorization_code(registered, loaded))
    assert provider.config.state_file.read_bytes() == before

    monkeypatch.undo()
    assert refresh_with(provider, registered, first.refresh_token) is not None
    assert asyncio.run(provider.exchange_authorization_code(registered, loaded)).access_token


# --------------------------------------------------------------------------- #
# OPS-3: loading the state file
# --------------------------------------------------------------------------- #

VERSION_1_STATE = {
    "version": 1,
    "clients": {
        "dcr-client-1": {
            "client_id": "dcr-client-1",
            "client_id_issued_at": 1760000000,
            "client_name": "Claude",
            "grant_types": ["authorization_code", "refresh_token"],
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "response_types": ["code"],
            "scope": "mcp intervals:read intervals:write",
            "token_endpoint_auth_method": "none",
        }
    },
    "refresh_tokens": {
        auth._digest("cimd-refresh-token"): {
            "client_id": CHATGPT_ID,
            "scopes": ["mcp", "intervals:read", "intervals:write"],
            "expires_at": 4_000_000_000,
            "grant_id": "grant-chatgpt",
            "resource": "https://intervals-mcp.example.com/",
        },
        auth._digest("dcr-refresh-token"): {
            "client_id": "dcr-client-1",
            "scopes": ["mcp", "intervals:read"],
            "expires_at": 4_000_000_000,
            "grant_id": "grant-claude",
        },
    },
}


def write_state(tmp_path: Path, payload: Any) -> Path:
    path = tmp_path / "state.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def test_version_1_state_file_loads_and_keeps_its_format(tmp_path):
    """The production format (version 1) loads unchanged; its refresh tokens keep working."""
    write_state(tmp_path, VERSION_1_STATE)
    env = make_env(tmp_path, MCP_PUBLIC_URL="https://intervals-mcp.example.com")
    provider = SingleUserOAuthProvider(oauth_config_from_env(env), fetch=FakeWeb())
    assert set(provider._clients) == {"dcr-client-1"} and len(provider._tokens.refresh) == 2
    chatgpt = asyncio.run(provider.get_client(CHATGPT_ID))
    assert chatgpt is not None
    rotated = refresh_with(provider, chatgpt, "cimd-refresh-token")
    assert rotated is not None and rotated.scope == "mcp intervals:read intervals:write"
    saved = json.loads(provider.config.state_file.read_text())
    assert saved["version"] == 1 and set(saved) == {"version", "clients", "refresh_tokens"}
    assert saved["clients"] == VERSION_1_STATE["clients"]
    entry = next(v for v in saved["refresh_tokens"].values() if v["grant_id"] == "grant-chatgpt")
    assert entry["resource"] == "https://intervals-mcp.example.com/" and entry["scopes"] == ["mcp", "intervals:read", "intervals:write"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("", "could not be read"),
        ("[]", "is not a JSON object"),
        ({"version": 1, "clients": []}, "'clients' is not an object"),
        ({"version": 1, "refresh_tokens": "x"}, "'refresh_tokens' is not an object"),
        ({"version": 2, "clients": {}}, "newer version"),
        ({"version": "1"}, "unknown format version"),
    ],
    ids=["empty", "list", "clients-list", "tokens-string", "newer", "version-string"],
)
def test_unusable_state_file_is_reported_in_one_line_and_left_alone(tmp_path, payload, message):
    path = write_state(tmp_path, payload)
    before = path.read_bytes()
    with pytest.raises(ValueError, match=message) as info:
        SingleUserOAuthProvider(oauth_config_from_env(make_env(tmp_path)))
    assert "\n" not in str(info.value) and "left unchanged" in str(info.value)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def test_unreadable_entries_are_kept_verbatim(tmp_path):
    """An entry a stricter SDK rejects is neither used nor deleted from the file."""
    state = json.loads(json.dumps(VERSION_1_STATE))
    state["clients"]["broken"] = {"client_id": "broken", "redirect_uris": "not-a-list"}
    state["refresh_tokens"]["broken-token"] = {"client_id": "broken", "scopes": ["mcp"], "expires_at": 4_000_000_000}
    state["refresh_tokens"]["no-expiry"] = {"client_id": "dcr-client-1"}
    write_state(tmp_path, state)
    provider = SingleUserOAuthProvider(oauth_config_from_env(make_env(tmp_path)), fetch=FakeWeb())
    assert "broken" not in provider._clients and "dcr-client-1" in provider._clients
    asyncio.run(provider.register_client(dcr_client("new")))
    saved = json.loads(provider.config.state_file.read_text())
    assert saved["clients"]["broken"] == state["clients"]["broken"]
    assert saved["refresh_tokens"]["broken-token"] == state["refresh_tokens"]["broken-token"]
    assert saved["refresh_tokens"]["no-expiry"] == state["refresh_tokens"]["no-expiry"]
    assert "new" in saved["clients"]


# --------------------------------------------------------------------------- #
# OPS-14: a sign-in that disappeared while Intervals.icu answered
# --------------------------------------------------------------------------- #


def test_intervals_callback_after_the_request_vanished_shows_a_page(tmp_path):
    env = make_env(tmp_path, OAUTH_LOGIN="intervals", INTERVALS_OAUTH_CLIENT_ID="1", INTERVALS_OAUTH_CLIENT_SECRET="s")
    holder: dict[str, Any] = {}

    class VanishingIntervals(FakeIntervals):  # pylint: disable=too-few-public-methods
        """Intervals.icu answers after the athlete denied the request in another tab."""

        async def __call__(self, code: str) -> dict[str, Any]:
            holder["provider"]._pending.clear()  # denied in another tab meanwhile
            return await super().__call__(code)

    _, provider, client = make_app(env, intervals=VanishingIntervals())
    holder["provider"] = provider
    _, challenge = pkce_pair()
    request_id = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge))
    response = submit_consent(client, {"request": request_id, "action": "intervals"})
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    callback = client.get("/oauth/intervals/callback", params={"state": state, "code": "c"})
    assert callback.status_code == 400 and "expired" in callback.text
    with pytest.raises(LoginError):
        provider.complete_login("gone", "k")
    with pytest.raises(LoginError):
        provider.deny_login("gone")


# --------------------------------------------------------------------------- #
# OPS-10: issuer with a path
# --------------------------------------------------------------------------- #


def test_issuer_with_a_path_has_one_protected_resource_document(tmp_path):
    env = make_env(tmp_path, MCP_PUBLIC_URL="https://mcp.example.com/intervals")
    _, _, client = make_app(env)
    paths = [getattr(r, "path", "") for r in client.app.router.routes if getattr(r, "path", "").startswith(PRM_PATH)]  # type: ignore[attr-defined]
    assert paths == [PRM_PATH + "/intervals"]
    document = client.get(PRM_PATH + "/intervals", headers={"Host": "mcp.example.com"}).json()
    assert "intervals:read" in document["scopes_supported"]





def test_failed_write_keeps_concurrent_changes(tmp_path, monkeypatch):
    """Undoing a failed write removes only that request's changes, not a code issued meanwhile."""
    provider = provider_for(tmp_path)
    registered = dcr_client()
    asyncio.run(provider.register_client(registered))
    request_id = authorize_as(provider, registered, "198.51.100.1")
    other_request = authorize_as(provider, registered, "198.51.100.2")
    holder: dict[str, str] = {}

    def disk_full(_data: dict[str, Any]) -> None:
        # another request finishes its sign-in while this write runs
        holder["code"] = parse_qs(urlsplit(provider.complete_login(other_request, "198.51.100.2")).query)["code"][0]
        raise OSError(28, "No space left on device")

    code = parse_qs(urlsplit(provider.complete_login(request_id, "198.51.100.1")).query)["code"][0]
    loaded = asyncio.run(provider.load_authorization_code(registered, code))
    assert loaded is not None
    monkeypatch.setattr(provider, "_write_state", disk_full)
    with pytest.raises(OSError):
        asyncio.run(provider.exchange_authorization_code(registered, loaded))
    assert code in provider._tokens.codes and holder["code"] in provider._tokens.codes
    assert not provider._tokens.access and not provider._tokens.refresh


def test_consent_page_after_the_request_vanished(tmp_path, monkeypatch):
    """A wrong password for a request that expired during the check shows the expired page, not 500."""
    _, provider, client = make_app(make_env(tmp_path))
    _, challenge = pkce_pair()
    request_id = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge))
    original = provider.verify_credentials

    def slow_check(username: str, password: str) -> bool:
        provider._pending.clear()
        return original(username, password)

    monkeypatch.setattr(provider, "verify_credentials", slow_check)
    wrong = submit_consent(client, {"request": request_id, "action": "password", "username": "athlete", "password": "no"})
    assert wrong.status_code == 400 and "expired" in wrong.text
    request_id = request_id_from(authorize(client, CHATGPT_ID, STABLE_REDIRECT, challenge))
    right = submit_consent(client, {"request": request_id, "action": "password", "username": "athlete", "password": PASSWORD})
    assert right.status_code == 400 and "expired" in right.text


def test_apps_without_the_address_middleware_keep_the_global_limits(tmp_path):
    """Without a client address (e.g. FastMCP's own sse_app) only the global caps apply."""
    provider = provider_for(tmp_path)
    for i in range(auth.REGISTRATIONS_PER_KEY + 2):
        asyncio.run(provider.register_client(dcr_client(f"c{i}")))
    registered = dcr_client("c0")

    async def authorize_unknown() -> None:
        await provider.authorize(registered, auth_params())

    for _ in range(auth.MAX_PENDING_PER_KEY + 5):
        asyncio.run(authorize_unknown())
    assert len(provider._pending) == auth.MAX_PENDING_PER_KEY + 5
