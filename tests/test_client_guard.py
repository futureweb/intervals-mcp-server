"""API client safety: identifiers cannot leave their path segment, secrets never reach tools,
and the shared HTTP client survives the end of other sessions."""

import asyncio

import httpx
import pytest

from intervals_mcp_server.api import client as api_client


class RecordingClient:  # pylint: disable=too-few-public-methods
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.urls: list[str] = []
        self.is_closed = False

    async def request(self, method: str, url: str, **_kwargs) -> httpx.Response:
        self.urls.append(url)
        return httpx.Response(200, json=self.payload, request=httpx.Request(method, url))

    async def aclose(self) -> None:
        self.is_closed = True


def _patch(monkeypatch, payload: object) -> RecordingClient:
    fake = RecordingClient(payload)

    async def get_client():
        return fake

    monkeypatch.setattr(api_client, "_get_httpx_client", get_client)
    return fake


@pytest.mark.parametrize(
    "url",
    [
        "/activity/../athlete/i1",
        "/athlete/i1/events/1/..?oldest=2000-01-01&category=WORKOUT",
        "/activity/i1?x=1",
        "/activity/i1#frag",
        "/activity/%2e%2e",
        "/activity/a b",
        "/activity/i1\\..",
        "/athlete//events",
        "activity/i1",
    ],
)
def test_unsafe_paths_are_rejected_without_a_request(monkeypatch, url):
    fake = _patch(monkeypatch, {"id": "x"})
    result = asyncio.run(api_client.make_intervals_request(url, api_key="k"))
    assert isinstance(result, dict) and result.get("error") and "Invalid identifier" in result["message"]
    assert not fake.urls


@pytest.mark.parametrize(
    "url",
    ["/activity/i194378945/streams", "/athlete/i1/events/12345", "/athlete/0/activities",
     "/athlete/i1/wellness/2026-10-09", "/athlete/i1/athlete-summary.json", "/athlete/i1/gear/b12345"],
)
def test_normal_paths_pass(monkeypatch, url):
    fake = _patch(monkeypatch, {"id": "x"})
    assert asyncio.run(api_client.make_intervals_request(url, api_key="k")) == {"id": "x"}
    assert len(fake.urls) == 1


def test_secret_fields_are_scrubbed(monkeypatch):
    _patch(monkeypatch, {"id": "i1", "icu_api_key": "SECRET", "nested": [{"api_key": "S2", "ok": 1}], "email": "a@b"})
    result = asyncio.run(api_client.make_intervals_request("/athlete/i1", api_key="k"))
    assert result == {"id": "i1", "nested": [{"ok": 1}], "email": "a@b"}


def test_tool_with_crafted_id_does_not_reach_another_endpoint(monkeypatch):
    from intervals_mcp_server.tools.activities import get_activity_details  # pylint: disable=import-outside-toplevel

    fake = _patch(monkeypatch, {"id": "i1", "icu_api_key": "SECRET"})
    result = asyncio.run(get_activity_details(activity_id="../athlete/i1", output_format="json"))
    assert "SECRET" not in result and not fake.urls


def test_shared_client_closes_only_after_the_last_session(monkeypatch):
    fake = RecordingClient({})
    monkeypatch.setattr(api_client, "httpx_client", fake)
    monkeypatch.setattr(api_client, "_ACTIVE_SESSIONS", 0)

    async def scenario() -> list[bool]:
        states = []
        first = api_client.setup_api_client(None)  # type: ignore[arg-type]
        second = api_client.setup_api_client(None)  # type: ignore[arg-type]
        await first.__aenter__()
        await second.__aenter__()
        await first.__aexit__(None, None, None)
        states.append(fake.is_closed)
        await second.__aexit__(None, None, None)
        states.append(fake.is_closed)
        return states

    assert asyncio.run(scenario()) == [False, True]
