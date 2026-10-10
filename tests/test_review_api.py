"""Regression tests for the review findings on the API client and read paths
(API-3 ... API-17, OPS-6/7/8/12, SEC-3 residual, ANA-13).

Every test runs against mocked requests (httpx MockTransport or patched functions).
"""

import asyncio
import json
import os
import pathlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

# pylint: disable=wrong-import-position,missing-function-docstring,protected-access
from intervals_mcp_server import tool_guard  # noqa: E402
from intervals_mcp_server.api import client as api_client  # noqa: E402
from intervals_mcp_server.mcp_instance import mcp  # noqa: E402
from intervals_mcp_server.tools import athlete as athlete_module  # noqa: E402
from intervals_mcp_server.tools import custom_items as custom_items_module  # noqa: E402
from intervals_mcp_server.tools import gear as gear_module  # noqa: E402
from intervals_mcp_server.tools.activities import get_activity_intervals, get_activity_streams, update_activity  # noqa: E402
from intervals_mcp_server.tools.events import delete_event  # noqa: E402
from intervals_mcp_server.tools.performance import compare_best_efforts, compare_workouts, find_similar_intervals  # noqa: E402
from intervals_mcp_server.tools.training_review import get_weekly_summary  # noqa: E402
from intervals_mcp_server.tools.wellness import get_wellness_data  # noqa: E402
from intervals_mcp_server.utils import cache as cache_module  # noqa: E402
from intervals_mcp_server.utils.dates import athlete_today  # noqa: E402

KEY = "SECRET-KEY-1234567890"


# ------------------------------------------------------------------ transport helpers
def _transport(monkeypatch, handler) -> list[httpx.Request]:
    """Route the shared httpx client through a MockTransport; returns the sent requests."""
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request, len(sent))

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))

    async def get_client():
        return client

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(api_client, "_get_httpx_client", get_client)
    monkeypatch.setattr(api_client.asyncio, "sleep", no_sleep)
    return sent


def _request(*args, **kwargs):
    return asyncio.run(api_client.make_intervals_request(*args, **kwargs))


# ------------------------------------------------------------------ API-11 / OPS-8
def test_api_reason_and_status_are_kept(monkeypatch):
    _transport(monkeypatch, lambda req, n: httpx.Response(422, json={"error": "Invalid oldest: Text 'bad' could not be parsed"}))
    result = _request("/athlete/i1/activities", api_key=KEY)
    assert result["status_code"] == 422
    assert result["message"].startswith("422 Unprocessable")
    assert "Intervals.icu says: Invalid oldest: Text 'bad' could not be parsed" in result["message"]


def test_html_error_page_keeps_its_status(monkeypatch):
    html = "<html><body><h1>502 Bad Gateway</h1></body></html>"
    _transport(monkeypatch, lambda req, n: httpx.Response(502, text=html, headers={"content-type": "text/html"}))
    result = _request("/athlete/i1/activities", api_key=KEY)
    assert result["status_code"] == 502 and "Invalid JSON" not in result["message"] and "<html" not in result["message"]


def test_api_key_never_appears_in_error_messages(monkeypatch):
    _transport(monkeypatch, lambda req, n: httpx.Response(400, text=f"bad key {KEY} given"))
    result = _request("/athlete/i1/activities", api_key=KEY, method="PUT", data={"a": 1})
    assert KEY not in result["message"] and "[redacted]" in result["message"]


# ------------------------------------------------------------------ API-17 / WRT-10
def test_get_is_retried_after_a_transport_error(monkeypatch):
    def handler(request, n):
        if n == 1:
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(200, json={"id": "i1"})

    sent = _transport(monkeypatch, handler)
    assert _request("/athlete/i1", api_key=KEY) == {"id": "i1"} and len(sent) == 2


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_writes_are_not_retried_after_a_transport_error(monkeypatch, method):
    def handler(request, _n):
        raise httpx.ReadTimeout("", request=request)

    sent = _transport(monkeypatch, handler)
    result = _request("/athlete/i1/events", api_key=KEY, method=method, data={"name": "x"} if method != "DELETE" else None)
    assert len(sent) == 1 and result["message"].startswith("Request error (ReadTimeout)")
    assert result["maybe_applied"] is True and "before retrying" in result["message"]


def test_a_refused_connection_is_known_not_to_be_applied(monkeypatch):
    def handler(request, _n):
        raise httpx.ConnectError("refused", request=request)

    _transport(monkeypatch, handler)
    result = _request("/athlete/i1/events", api_key=KEY, method="POST", data={"name": "x"})
    assert "maybe_applied" not in result and "ConnectError" in result["message"]


def test_post_is_still_not_retried_on_server_errors(monkeypatch):
    sent = _transport(monkeypatch, lambda req, n: httpx.Response(503, json={"error": "down"}))
    result = _request("/athlete/i1/events", api_key=KEY, method="POST", data={"name": "x"})
    assert len(sent) == 1 and result["status_code"] == 503


# ------------------------------------------------------------------ OPS-6
def test_request_budget_of_a_tool_call(monkeypatch):
    sent = _transport(monkeypatch, lambda req, n: httpx.Response(200, json={}))

    async def run():
        with api_client.call_limits(max_requests=2, timeout_s=60) as limits:
            results = [await api_client.make_intervals_request("/athlete/i1", api_key=KEY) for _ in range(3)]
        return results, limits

    results, limits = asyncio.run(run())
    assert len(sent) == 2 and results[2]["limit_reached"] is True
    assert "limit of 2 Intervals.icu API requests" in results[2]["message"] and limits.hit


def test_deadline_of_a_tool_call(monkeypatch):
    sent = _transport(monkeypatch, lambda req, n: httpx.Response(200, json={}))

    async def run():
        with api_client.call_limits(max_requests=10, timeout_s=0.5):
            return await api_client.make_intervals_request("/athlete/i1", api_key=KEY)

    result = asyncio.run(run())
    assert not sent and "time limit" in result["message"]


def test_guard_says_when_a_limit_cut_the_result(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_MAX_REQUESTS", "1")
    _transport(monkeypatch, lambda req, n: httpx.Response(200, json={"ok": True}))

    async def two_requests() -> str:
        first = await api_client.make_intervals_request("/athlete/i1", api_key=KEY)
        second = await api_client.make_intervals_request("/athlete/i1", api_key=KEY)
        return f"{first} {second.get('message', '') if isinstance(second, dict) else second}"

    result = asyncio.run(tool_guard.guarded(two_requests)())
    assert "Note: this tool call reached its limit of 1 Intervals.icu API requests" in result


def test_compare_best_efforts_caps_durations():
    result = asyncio.run(compare_best_efforts(activity_ids="i1", durations=",".join(str(d) for d in range(1, 12))))
    assert result.startswith("Error: 11 durations; at most 10")


# ------------------------------------------------------------------ SEC-3 residual
@pytest.mark.parametrize("call", [
    lambda: delete_event(event_id="a/b"),
    lambda: update_activity(activity_id="i123/intervals", name="x"),
    lambda: compare_best_efforts(activity_ids="i1,i2/streams"),
    lambda: get_activity_streams("i1?x=1"),
])
def test_ids_with_a_slash_never_reach_the_api(monkeypatch, call):
    sent = _transport(monkeypatch, lambda req, n: httpx.Response(200, json={}))
    assert "is not a valid identifier" in asyncio.run(call())
    assert not sent


def test_seg_confines_an_internal_id_to_one_segment(monkeypatch):
    sent = _transport(monkeypatch, lambda req, n: httpx.Response(200, json={}))
    url = f"/athlete/i1/events/{api_client.seg('a/b')}"
    assert url == "/athlete/i1/events/a%2Fb"
    assert "Invalid identifier" in _request(url, api_key=KEY, method="DELETE")["message"] and not sent
    assert api_client.seg("i194378945") == "i194378945" and api_client.seg(12) == "12"


def test_guard_keeps_the_tool_schema_and_checks_ids_through_mcp(monkeypatch):
    sent = _transport(monkeypatch, lambda req, n: httpx.Response(200, json={}))
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    schema = tools["get_activity_streams"].inputSchema
    assert schema["properties"]["max_points"]["default"] == 2000 and "activity_id" in schema["required"]
    result = asyncio.run(mcp.call_tool("get_event_by_id", {"event_id": "1/../../activity/i9"}))
    assert "not a valid identifier" in str(result) and not sent


# ------------------------------------------------------------------ API-12 / ANA-13
def test_today_follows_the_athlete_profile_time_zone(monkeypatch):
    monkeypatch.delenv("ATHLETE_TIMEZONE", raising=False)
    athlete_module._ATHLETE_CACHE.clear()
    athlete_module._TIMEZONE_FAILURES.clear()
    athlete_module._TIMEZONE_CACHE.clear()
    zones = {"i1": "Pacific/Kiritimati", "i2": "Pacific/Pago_Pago"}  # UTC+14 and UTC-11: always different days
    urls = []

    async def fake_request(url=None, **_kwargs):
        urls.append(url)
        athlete = url.split("/")[2]
        return {"athlete": {"id": athlete, "timezone": zones[athlete]}, "customItems": []}

    monkeypatch.setattr(api_client, "make_intervals_request", fake_request)

    async def today_tool(athlete_id: str) -> str:  # pylint: disable=unused-argument
        return athlete_today().isoformat()

    guarded = tool_guard.guarded(today_tool)
    east, west = asyncio.run(guarded(athlete_id="i1")), asyncio.run(guarded(athlete_id="i2"))
    assert east == datetime.now(ZoneInfo("Pacific/Kiritimati")).date().isoformat()
    assert west == datetime.now(ZoneInfo("Pacific/Pago_Pago")).date().isoformat()
    assert east != west
    # The light /profile endpoint is used, once per athlete; a cleared athlete cache keeps the zone.
    athlete_module._ATHLETE_CACHE.clear()
    asyncio.run(guarded(athlete_id="i1"))
    assert urls == ["/athlete/i1/profile", "/athlete/i2/profile"]


def test_unknown_time_zone_falls_back_to_the_server_clock(monkeypatch):
    monkeypatch.setenv("ATHLETE_TIMEZONE", "Mars/Olympus")
    assert athlete_today() == datetime.now().date()


# ------------------------------------------------------------------ API-3 / API-4 / OPS-7 / API-13 caches
def test_caches_expire_and_are_keyed_by_api_key(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: clock["t"])
    gear_module._GEAR_RAW_CACHE.clear()
    calls = []

    async def fake_request(url=None, api_key=None, **_kwargs):
        calls.append((url, api_key))
        return [{"id": "b1", "name": "Bike"}]

    monkeypatch.setattr(gear_module, "make_intervals_request", fake_request)
    asyncio.run(gear_module.get_gear_raw("i1"))
    asyncio.run(gear_module.get_gear_raw("i1"))
    asyncio.run(gear_module.get_gear_raw("i1", api_key="other-account-key"))
    assert len(calls) == 2
    clock["t"] += gear_module.GEAR_CACHE_TTL_S + 1
    asyncio.run(gear_module.get_gear_raw("i1"))
    assert len(calls) == 3


def test_cache_partitions_per_api_key_are_never_reused(monkeypatch):
    monkeypatch.setattr(cache_module, "_MAX_KEY_SLOTS", 2)
    cache_module._KEY_SLOTS.clear()
    first = cache_module.cache_key("i1", "key-a")
    assert cache_module.cache_key("i1", "key-a") == first and cache_module.cache_key("i1", None) == ("i1", "default")
    seen = {first[1], cache_module.cache_key("i1", "key-b")[1]}
    after_reset = cache_module.cache_key("i1", "key-c")[1]  # the slot table was full and is reset
    assert after_reset not in seen and cache_module.cache_key("i1", "key-a")[1] not in seen | {after_reset}


def test_failed_gear_fetch_is_not_cached_and_is_reported(monkeypatch):
    gear_module._GEAR_RAW_CACHE.clear()
    answers = [{"error": True, "status_code": 429, "message": "429 Too Many Requests"}, [{"id": "b1", "name": "Bike"}]]

    async def fake_request(**_kwargs):
        return answers.pop(0)

    monkeypatch.setattr(gear_module, "make_intervals_request", fake_request)
    assert "Error fetching gear for athlete i1: 429" in asyncio.run(gear_module.get_gear_list())
    assert "Bike" in asyncio.run(gear_module.get_gear_list())


def test_custom_item_changes_drop_every_alias():
    custom_items_module._CUSTOM_ITEMS_CACHE.clear()
    custom_items_module._CUSTOM_ITEMS_CACHE.set(("0", "default"), [{"id": 1}])
    custom_items_module._CUSTOM_ITEMS_CACHE.set(("i1", "default"), [{"id": 1}])
    custom_items_module.invalidate_custom_items_cache("0")
    assert len(custom_items_module._CUSTOM_ITEMS_CACHE) == 0


def test_weekly_summary_with_the_alias_zero(monkeypatch):
    athlete_module._ATHLETE_CACHE.clear()

    async def fake_request(url=None, **_kwargs):
        if url == "/athlete/0":
            return {"id": "i219504"}
        return [{"athlete_id": "i219504", "date": "2026-10-05", "count": 3, "time": 3600},
                {"athlete_id": "i777", "date": "2026-10-05", "count": 9}]

    monkeypatch.setattr("intervals_mcp_server.tools.training_review.make_intervals_request", fake_request)
    monkeypatch.setattr(api_client, "make_intervals_request", fake_request)
    result = asyncio.run(get_weekly_summary(athlete_id="0", start_date="2026-10-05", end_date="2026-10-11"))
    assert "No weekly summary data" not in result


# ------------------------------------------------------------------ API-7
def test_profile_error_reason_is_shown(monkeypatch):
    athlete_module._ATHLETE_CACHE.clear()

    async def fake_request(**_kwargs):
        return {"error": True, "status_code": 503, "message": "503 Service Unavailable"}

    monkeypatch.setattr(api_client, "make_intervals_request", fake_request)
    assert asyncio.run(athlete_module.get_athlete_profile()).endswith("503 Service Unavailable")
    assert "Error fetching sport settings" in asyncio.run(athlete_module.get_training_zones(refresh=True))


def test_report_and_execution_name_api_errors(monkeypatch):
    from intervals_mcp_server.tools.analysis import analyze_workout_execution  # pylint: disable=import-outside-toplevel
    from intervals_mcp_server.tools.report import get_activity_report  # pylint: disable=import-outside-toplevel

    activity = {"id": "i5", "name": "Ride", "type": "Ride", "start_date_local": "2026-10-05T08:00:00", "stream_types": ["watts", "time"]}
    error = {"error": True, "status_code": 429, "message": "429 Too Many Requests"}

    async def fake_request(url=None, **_kwargs):
        if url.endswith("/intervals") or url.endswith("/streams"):
            return error
        if url.startswith("/activity/"):
            return activity
        return []

    for module in ("report", "analysis", "activities", "custom_items", "gear"):
        monkeypatch.setattr(f"intervals_mcp_server.tools.{module}.make_intervals_request", fake_request)
    monkeypatch.setattr(api_client, "make_intervals_request", fake_request)
    report = asyncio.run(get_activity_report("i5"))
    assert "intervals could not be loaded from Intervals.icu (429 Too Many Requests)" in report
    assert "no intervals detected" not in report and "file not retained" not in report
    execution = asyncio.run(analyze_workout_execution("i5"))
    assert "INCOMPLETE DATA" in execution and "429 Too Many Requests" in execution


# ------------------------------------------------------------------ API-8
def test_compare_best_efforts_names_partial_errors(monkeypatch):
    activities = {"a1": {"id": "a1", "name": "Big day", "start_date_local": "2026-10-01T08:00:00", "type": "Ride"},
                  "a2": {"id": "a2", "name": "Easy", "start_date_local": "2026-10-02T08:00:00", "type": "Ride"}}

    async def fake_request(url=None, **_kwargs):
        if url.endswith("/best-efforts"):
            if "/a1/" in url:
                return {"error": True, "status_code": 429, "message": "429 Too Many Requests"}
            return {"efforts": [{"average": 200}]}
        if url.startswith("/activity/"):
            return activities[url.rsplit("/", 1)[1]]
        return []

    monkeypatch.setattr("intervals_mcp_server.tools.performance.make_intervals_request", fake_request)
    monkeypatch.setattr("intervals_mcp_server.tools.gear.make_intervals_request", fake_request)
    result = asyncio.run(compare_best_efforts(activity_ids="a1,a2", durations="60"))
    assert "| error |" in result and "INCOMPLETE" in result and "Errors (1 of 2 requests failed" in result


# ------------------------------------------------------------------ API-9
def test_truncated_interval_search_is_reported(monkeypatch):
    found = [{"id": f"a{i}", "name": "Threshold", "type": "Ride", "start_date_local": f"2026-{(i % 9) + 1:02d}-01T08:00:00"} for i in range(100)]

    async def fake_request(url=None, **_kwargs):
        return found if url.endswith("/interval-search") else []

    monkeypatch.setattr("intervals_mcp_server.tools.performance.make_intervals_request", fake_request)
    monkeypatch.setattr("intervals_mcp_server.tools.gear.make_intervals_request", fake_request)
    result = asyncio.run(find_similar_intervals(600, 900, 90, 100, end_date="2025-06-30"))
    assert "returned its maximum of 100 results" in result and "older matches are not included" in result


def test_name_search_with_dates_lists_the_range(monkeypatch):
    calls = []
    listing = [{"id": "s1", "name": "Sweet Spot 3x15", "type": "Ride", "start_date_local": "2025-04-02T08:00:00"},
               {"id": "s2", "name": "Endurance", "type": "Ride", "start_date_local": "2025-04-05T08:00:00"}]

    async def fake_request(url=None, params=None, **_kwargs):
        calls.append((url, params))
        if url.endswith("/activities"):
            return listing
        if url.endswith("/intervals"):
            return {"icu_intervals": []}
        return []

    monkeypatch.setattr("intervals_mcp_server.tools.performance.make_intervals_request", fake_request)
    monkeypatch.setattr("intervals_mcp_server.tools.gear.make_intervals_request", fake_request)
    payload = json.loads(asyncio.run(compare_workouts(query="sweet spot", start_date="2025-03-01", end_date="2025-05-31", output_format="json")))
    assert not any(url.endswith("/search-full") for url, _ in calls)
    assert [row["id"] for row in payload["activities"]] == ["s1"]


# ------------------------------------------------------------------ API-10 / OPS-12 / API-14
STREAMS = [
    {"type": "time", "data": list(range(500))},
    {"type": "watts", "data": [200 + i % 50 for i in range(500)]},
    {"type": "heartrate", "data": [140 + i % 20 for i in range(500)]},
]


def _streams(monkeypatch):
    custom_items_module._CUSTOM_ITEMS_CACHE.clear()

    async def fake_request(url=None, **_kwargs):
        return STREAMS if url.endswith("/streams") else []

    monkeypatch.setattr("intervals_mcp_server.tools.activities.make_intervals_request", fake_request)
    monkeypatch.setattr(api_client, "make_intervals_request", fake_request)


def test_stream_output_is_paged_by_size(monkeypatch):
    _streams(monkeypatch)
    monkeypatch.setattr("intervals_mcp_server.tools.activities.output_budget", lambda: 2000)
    text = asyncio.run(get_activity_streams("i1", output_format="full"))
    assert "by the size limit of one tool result. Continue with start_index=" in text and len(text) < 3000
    payload = json.loads(asyncio.run(get_activity_streams("i1", output_format="json")))
    assert payload["next_start_index"] == payload["index_end"] and payload["returned_samples"] < 500
    assert payload["streams"]["watts"]["data"][0] == 200 and payload["note"].startswith("Note: output cut")


def test_stream_json_is_one_json_object(monkeypatch):
    _streams(monkeypatch)
    payload = json.loads(asyncio.run(get_activity_streams("i1", output_format="json", max_points=100)))
    assert payload["activity_id"] == "i1" and payload["returned_samples"] == 100 and payload["next_start_index"] == 100
    assert payload["selected_samples"] == 500 and "time" in payload["streams"]


def test_stream_max_points_are_capped(monkeypatch):
    _streams(monkeypatch)
    payload = json.loads(asyncio.run(get_activity_streams("i1", output_format="json", max_points=10**9)))
    assert payload["returned_samples"] == 500 and payload["next_start_index"] is None


def test_wellness_is_paged_by_day(monkeypatch):
    days = [{"id": f"2026-09-{d:02d}", "weight": 70.0, "restingHR": 50, "comments": "x" * 400} for d in range(1, 31)]

    async def fake_request(**_kwargs):
        return days

    monkeypatch.setattr("intervals_mcp_server.tools.wellness.make_intervals_request", fake_request)
    monkeypatch.setattr("intervals_mcp_server.tools.wellness.output_budget", lambda: 3000)
    result = asyncio.run(get_wellness_data(start_date="2026-09-01", end_date="2026-09-30"))
    assert "Note: output stopped after" in result and "Continue with start_date=2026-09-" in result and len(result) < 4000


def test_global_output_cap_never_cuts_silently():
    text = "line\n" * 50_000
    capped = tool_guard.cap_output(text, limit=10_000)
    assert len(capped) <= 10_000 and "[Output truncated: showing the first" in capped
    payload = json.dumps({"athlete": "i1", "rows": [{"id": i, "text": "x" * 100} for i in range(500)]})
    cut = tool_guard.cap_output(payload, limit=10_000)
    shrunk = json.loads(cut)  # still valid JSON, the list is cut and says so
    assert len(cut) <= 10_000 and shrunk["athlete"] == "i1" and shrunk["rows"][0]["id"] == 0
    assert shrunk["truncated"] == [{"path": "rows", "kept": len(shrunk["rows"]), "total": 500, "kept_items": "first"}]
    assert "detail_level" in shrunk["truncated_note"] and "offset" in shrunk["truncated_note"]
    blob = json.dumps({"blob": "z" * 50_000})
    replaced = json.loads(tool_guard.cap_output(blob, limit=10_000))
    assert replaced["error"] == "output_too_large" and replaced["chars"] == len(blob)
    assert tool_guard.cap_output("small", limit=10_000) == "small"


# ------------------------------------------------------------------ API-15
def test_interval_definitions_come_from_the_activity_owner(monkeypatch):
    custom_items_module._CUSTOM_ITEMS_CACHE.clear()
    calls = []

    async def fake_request(url=None, **_kwargs):
        calls.append(url)
        if url.endswith("/intervals"):
            return {"id": "i5", "icu_intervals": [{"type": "WORK", "start_index": 0, "end_index": 10, "moving_time": 600}]}
        if url == "/activity/i5":
            return {"id": "i5", "type": "Ride", "icu_athlete_id": "i42"}
        return []

    monkeypatch.setattr("intervals_mcp_server.tools.activities.make_intervals_request", fake_request)
    monkeypatch.setattr(api_client, "make_intervals_request", fake_request)
    asyncio.run(get_activity_intervals("i5"))
    assert "/athlete/i42/custom-item" in calls and "/athlete/i1/custom-item" not in calls


# ------------------------------------------------------------------ R25-16
def test_json_cut_sets_next_offset_and_keeps_newest_items():
    activities = {"total": 130, "offset": 0, "limit": 200, "next_offset": None,
                  "activities": [{"id": f"i{i}", "start_date_local": f"2026-10-{30 - i % 30:02d}T08:00:00", "x": "y" * 700}
                                 for i in range(130)]}
    cut = json.loads(tool_guard.cap_output(json.dumps(activities), limit=50_000))
    kept = len(cut["activities"])
    assert kept < 130 and cut["next_offset"] == kept and cut["activities"][0]["id"] == "i0"
    weeks = {"groups": [{"group": f"2025-{w // 4 + 1:02d}-{w % 4 * 7 + 1:02d} (ISO week)", "x": "c" * 2000} for w in range(48)]}
    cut = json.loads(tool_guard.cap_output(json.dumps(weeks), limit=30_000))
    assert cut["groups"][-1]["group"].startswith("2025-12-22") and cut["truncated"][0]["kept_items"] == "last (newest)"


def test_json_cut_is_proportional():
    report = {"activity": {"id": "i1"}, "intervals": [{"n": i, "x": "a" * 900} for i in range(96)],
              "execution": {"rows": [{"n": i, "x": "b" * 900} for i in range(96)]}}
    cut = json.loads(tool_guard.cap_output(json.dumps(report), limit=60_000))
    kept = {entry["path"]: entry["kept"] for entry in cut["truncated"]}
    assert kept["intervals"] == kept["execution.rows"] > 20
