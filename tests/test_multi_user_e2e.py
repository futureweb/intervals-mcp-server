# pylint: disable=missing-function-docstring,redefined-outer-name,too-many-locals
"""End to end over loopback: the real server entry point in multi-user mode, two athletes and the owner.

A fake Intervals.icu (OAuth authorize + token endpoint and the API, keyed by credential) runs in a
thread on 127.0.0.1; the server runs as a subprocess (``python -m intervals_mcp_server.cli``)
with ``MCP_TENANCY=multi``. Every connection signs in like a browser would (dynamic client
registration, consent page, "Continue with Intervals.icu" or the owner's password, PKCE token
exchange) and then calls tools over streamable HTTP in parallel. Any request the fake API sees
with a credential that does not belong to the athlete of the path fails the test.
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from intervals_mcp_server.tenancy import intervals_scopes_for
from intervals_mcp_server.token_vault import generate_key
from tests.test_multi_user import ALPHA, BRAVO, OWNER, OWNER_KEY, TAGS, TOKENS, FakeApi

PASSWORD = "owner-password-e2e"
CLIENT_REDIRECT = "http://127.0.0.1:9/callback"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeIntervals:  # pylint: disable=too-few-public-methods
    """Fake Intervals.icu: /oauth/authorize, /api/oauth/token and /api/v1 (FakeApi)."""

    def __init__(self) -> None:
        self.api = FakeApi()
        self.scopes: list[str] = []
        self.codes: dict[str, str] = {}

    def app(self) -> Starlette:
        async def authorize(request: Request) -> Response:
            params = request.query_params
            athlete = request.headers["x-test-athlete"]  # who signs in at "Intervals.icu"
            code = secrets.token_urlsafe(12)
            self.codes[code] = athlete
            self.scopes.append(params["scope"])
            return RedirectResponse(f"{params['redirect_uri']}?{urlencode({'code': code, 'state': params['state']})}", 302)

        async def token(request: Request) -> Response:
            form = await request.form()
            athlete = self.codes.pop(str(form.get("code")), None)
            if athlete is None or form.get("client_secret") != "app-secret":
                return JSONResponse({"error": "invalid_grant"}, 400)
            access = next((t for t, a in TOKENS.items() if a == athlete), "tok-unused")
            return JSONResponse(
                {"token_type": "Bearer", "access_token": access, "scope": self.scopes[-1], "athlete": {"id": athlete[1:], "name": "x"}}
            )

        async def api(request: Request) -> Response:
            proxied = httpx.Request(request.method, str(request.url), headers=dict(request.headers))
            answer = self.api(proxied)
            return Response(answer.content, answer.status_code, media_type="application/json")

        return Starlette(
            routes=[
                Route("/oauth/authorize", authorize),
                Route("/api/oauth/token", token, methods=["POST"]),
                Route("/api/v1/{path:path}", api, methods=["GET", "POST", "PUT", "DELETE"]),
            ]
        )


@pytest.fixture
def fake_intervals() -> Iterator[tuple[FakeIntervals, int]]:
    fake = FakeIntervals()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(fake.app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield fake, port
    server.should_exit = True
    thread.join(5)
    assert not fake.api.violations, fake.api.violations


@pytest.fixture
def server(tmp_path, fake_intervals) -> Iterator[dict[str, Any]]:
    fake, fake_port = fake_intervals
    port = free_port()
    key_file = tmp_path / "token.key"
    key_file.write_text(generate_key() + "\n")
    key_file.chmod(0o600)
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("MCP_", "OAUTH_", "FASTMCP_", "INTERVALS_", "API_KEY", "ATHLETE_"))
    }
    env.update(
        {
            "MCP_TRANSPORT": "streamable-http",
            "FASTMCP_HOST": "127.0.0.1",
            "FASTMCP_PORT": str(port),
            "MCP_AUTH": "oauth",
            "MCP_TENANCY": "multi",
            "MCP_PUBLIC_URL": f"http://127.0.0.1:{port}",
            "MCP_PERMISSIONS": "read,write",
            "OAUTH_LOGIN": "intervals,password",
            "OAUTH_PASSWORD": PASSWORD,
            "OAUTH_STATE_FILE": str(tmp_path / "state" / "oauth_state.json"),
            "OAUTH_TOKEN_KEY_FILE": str(key_file),
            "OAUTH_ALLOWED_ATHLETES": f"{OWNER},{ALPHA},{BRAVO}",
            "INTERVALS_OAUTH_CLIENT_ID": "1304",
            "INTERVALS_OAUTH_CLIENT_SECRET": "app-secret",
            "INTERVALS_OAUTH_BASE_URL": f"http://127.0.0.1:{fake_port}",
            "INTERVALS_API_BASE_URL": f"http://127.0.0.1:{fake_port}/api/v1",
            "ATHLETE_ID": OWNER,
            "API_KEY": OWNER_KEY,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        }
    )
    log = (tmp_path / "server.log").open("w")
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-m", "intervals_mcp_server.cli"], cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/.well-known/oauth-authorization-server", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if process.poll() is not None:
            break
        time.sleep(0.2)
    else:
        process.kill()
    try:
        assert process.poll() is None, (tmp_path / "server.log").read_text()
        yield {"base": base, "fake": fake, "state": tmp_path / "state" / "oauth_state.json", "log": tmp_path / "server.log"}
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()


def connect(base: str, athlete: str | None, granted: str = "read") -> dict[str, str]:
    """Sign in like a browser: register, authorize, consent, Intervals.icu (or password), token."""
    with httpx.Client(base_url=base, follow_redirects=False, timeout=20) as browser:
        client_id = browser.post(
            "/register",
            json={"redirect_uris": [CLIENT_REDIRECT], "token_endpoint_auth_method": "none", "client_name": "E2E",
                  "scope": "mcp intervals:read intervals:write"},
        ).json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        start = browser.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CLIENT_REDIRECT, "state": "s1",
            "code_challenge": challenge, "code_challenge_method": "S256", "scope": "mcp intervals:read intervals:write",
        })
        request_id = parse_qs(urlsplit(start.headers["location"]).query)["request"][0]
        page = browser.get("/oauth/login", params={"request": request_id})
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert csrf is not None
        form = {"request": request_id, "csrf": csrf.group(1), "grant": granted}
        if athlete is None:
            form.update(action="password", username="athlete", password=PASSWORD)
            final = browser.post("/oauth/login", data=form)
        else:
            assert "stores your Intervals.icu access token" in page.text
            form["action"] = "intervals"
            upstream = browser.post("/oauth/login", data=form)
            back = httpx.get(upstream.headers["location"], headers={"x-test-athlete": athlete}, follow_redirects=False)
            final = browser.get(back.headers["location"])
        code = parse_qs(urlsplit(final.headers["location"]).query)["code"][0]
        tokens = browser.post("/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": CLIENT_REDIRECT,
            "client_id": client_id, "code_verifier": verifier,
        })
        assert tokens.status_code == 200, tokens.text
        return {"client_id": client_id, **tokens.json()}


async def call_tools(base: str, access_token: str, calls: list[tuple[str, dict[str, Any]]]) -> list[str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(headers=headers, timeout=60) as http:
        async with streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                results = []
                for name, arguments in calls:
                    result = await session.call_tool(name, arguments)
                    results.append("\n".join(getattr(item, "text", "") for item in result.content))
                return results


CALLS = [
    ("get_athlete_profile", {}),
    ("get_activities", {"start_date": "2026-09-01", "end_date": "2026-10-05"}),
    ("get_wellness_data", {"start_date": "2026-10-01", "end_date": "2026-10-01"}),
    ("get_server_status", {"output_format": "json"}),
    ("get_activities", {"athlete_id": BRAVO}),
]


def test_isolation_over_the_wire(server):
    base, fake = server["base"], server["fake"]
    tokens = {ALPHA: connect(base, ALPHA), BRAVO: connect(base, BRAVO), OWNER: connect(base, None)}
    assert fake.scopes == [intervals_scopes_for(["read"])] * 2

    async def everyone() -> list[list[str]]:
        return list(await asyncio.gather(*(call_tools(base, tokens[a]["access_token"], CALLS) for a in (ALPHA, BRAVO, OWNER))))

    outputs = dict(zip((ALPHA, BRAVO, OWNER), asyncio.run(everyone()), strict=True))
    for athlete, texts in outputs.items():
        joined = "\n".join(texts[:4])
        assert TAGS[athlete] in joined, (athlete, joined[:400])
        for other, tag in TAGS.items():
            if other != athlete:
                assert tag not in "\n".join(texts) and other not in joined, (athlete, other)
        status = json.loads(texts[3])
        assert status["tenancy"] == "multi" and status["athlete_id"] == athlete
    assert "not the athlete of this connection" in outputs[ALPHA][4] and "BRAVODATA" not in outputs[ALPHA][4]
    assert "BRAVODATA" in outputs[BRAVO][4]  # its own id is fine
    seen = {who for who, _, _ in fake.api.requests}
    assert seen == {ALPHA, BRAVO, OWNER}

    state_text = server["state"].read_text()
    for secret in (*TOKENS, OWNER_KEY):
        assert secret not in state_text
    assert json.loads(state_text)["version"] == 2
    log_text = server["log"].read_text()
    for secret in (*TOKENS, OWNER_KEY):
        assert secret not in log_text

    # Revoking ALPHA's grant deletes its stored token; BRAVO keeps working.
    revoked = httpx.post(f"{base}/revoke", data={"token": tokens[ALPHA]["refresh_token"], "client_id": tokens[ALPHA]["client_id"]})
    assert revoked.status_code == 200, revoked.text
    athletes = {g["athlete_id"] for g in json.loads(server["state"].read_text())["grants"].values()}
    assert ALPHA not in athletes and BRAVO in athletes
    gone = httpx.post(f"{base}/mcp", headers={"Authorization": f"Bearer {tokens[ALPHA]['access_token']}"}, json={})
    assert gone.status_code == 401
    assert "BRAVODATA" in asyncio.run(call_tools(base, tokens[BRAVO]["access_token"], CALLS[:1]))[0]
