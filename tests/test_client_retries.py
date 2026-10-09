"""Retry policy of the API client: idempotent requests are retried on transient errors,
POST only on 429 (a POST that failed with 5xx may already have created the event)."""

import asyncio

import httpx
import pytest

from intervals_mcp_server.api import client as api_client


class FakeClient:  # pylint: disable=too-few-public-methods
    """Answers with the given statuses in order and records each request."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **_kwargs) -> httpx.Response:
        self.calls.append(method)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(status, json={"id": "e1"} if status == 200 else {"error": "x"}, request=httpx.Request(method, url))


def _run(monkeypatch, method: str, statuses: list[int]) -> tuple[FakeClient, object]:
    fake = FakeClient(statuses)

    async def get_client():
        return fake

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(api_client, "_get_httpx_client", get_client)
    monkeypatch.setattr(api_client.asyncio, "sleep", no_sleep)
    data = {"name": "x"} if method in ("POST", "PUT") else None
    result = asyncio.run(api_client.make_intervals_request("/athlete/i1/events", api_key="k", method=method, data=data))
    return fake, result


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_post_is_not_retried_on_server_errors(monkeypatch, status):
    fake, result = _run(monkeypatch, "POST", [status, 200])
    assert fake.calls == ["POST"]
    assert isinstance(result, dict) and result.get("error")


def test_post_is_retried_on_429(monkeypatch):
    fake, result = _run(monkeypatch, "POST", [429, 200])
    assert fake.calls == ["POST", "POST"] and result == {"id": "e1"}


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_idempotent_requests_are_retried(monkeypatch, method):
    fake, result = _run(monkeypatch, method, [503, 502, 200])
    assert fake.calls == [method] * 3 and result == {"id": "e1"}


def test_retry_statuses_by_method():
    assert api_client.retry_statuses("post") == {429}
    assert api_client.retry_statuses("GET") == {429, 500, 502, 503, 504}
