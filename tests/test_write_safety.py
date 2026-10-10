"""Write safety (stage 3B): dry runs, duplicate checks before creating events, read-back of what
Intervals.icu stored, and deletions that name what was deleted.

Every test runs against mocked requests or a recording HTTP client; nothing reaches Intervals.icu
(tests/conftest.py blocks the network as well).
"""

import asyncio
import json
import os
import pathlib
import sys
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

# pylint: disable=wrong-import-position,missing-function-docstring,unused-argument
from intervals_mcp_server.api import client as api_client  # noqa: E402
from intervals_mcp_server.tool_guard import guarded  # noqa: E402
from intervals_mcp_server.tools import athlete as athlete_module  # noqa: E402
from intervals_mcp_server.tools import custom_items as custom_items_module  # noqa: E402
from intervals_mcp_server.tools.activities import add_activity_message, update_activity  # noqa: E402
from intervals_mcp_server.tools.athlete import update_sport_settings  # noqa: E402
from intervals_mcp_server.tools.custom_items import create_custom_item, delete_custom_item, update_custom_item  # noqa: E402
from intervals_mcp_server.tools.events import (  # noqa: E402
    add_events_bulk,
    add_or_update_event,
    add_or_update_note,
    delete_event,
)
from intervals_mcp_server.tools.wellness import update_wellness  # noqa: E402
from intervals_mcp_server.tools.workout_library import (  # noqa: E402
    add_event_from_library,
    create_library_workout,
    delete_library_workout,
)
from intervals_mcp_server.utils.types import WorkoutDoc  # noqa: E402
from intervals_mcp_server.utils.write_safety import (  # noqa: E402
    READ_BACK_CHARS,
    parse_warnings,
    read_back_text,
    sent_shape,
    stored_summary,
    workout_signature,
)

# Tool modules whose request function the tests replace.
MODULES = tuple(f"intervals_mcp_server.tools.{name}" for name in ("events", "workout_library", "custom_items", "activities", "wellness"))
NOT_FOUND = {"error": True, "status_code": 404, "message": "404 Not Found: The requested endpoint or ID doesn't exist."}

STEPS = {"steps": [
    {"duration": 600, "power": {"value": 55, "units": "%ftp"}, "warmup": True},
    {"reps": 3, "steps": [
        {"duration": 600, "power": {"value": 90, "units": "%ftp"}},
        {"duration": 300, "power": {"value": 55, "units": "%ftp"}},
    ]},
    {"duration": 600, "power": {"value": 50, "units": "%ftp"}, "cooldown": True},
]}
# What Intervals.icu stores after parsing STEPS (repeat blocks carry their total; no labels).
PARSED = {"steps": [
    {"duration": 600, "power": {"value": 55, "units": "%ftp"}, "warmup": True},
    {"reps": 3, "text": "3x ", "duration": 2700, "distance": 0, "steps": [
        {"duration": 600, "power": {"value": 90, "units": "%ftp"}},
        {"duration": 300, "power": {"value": 55, "units": "%ftp"}},
    ]},
    {"duration": 600, "power": {"value": 50, "units": "%ftp"}, "cooldown": True},
]}


def _router(monkeypatch, responder) -> list[dict[str, Any]]:
    """Patch the request function of the write tool modules; *responder(url, method, params, data)* answers."""
    calls: list[dict[str, Any]] = []

    async def fake_request(url=None, api_key=None, params=None, method="GET", data=None):  # pylint: disable=unused-argument
        calls.append({"url": url, "method": method, "params": params, "data": data})
        return responder(url, method, params, data)

    for module in MODULES:
        monkeypatch.setattr(f"{module}.make_intervals_request", fake_request)
    return calls


def _writes(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [call for call in calls if call["method"] != "GET"]


def _calendar(day_events: dict[str, list[dict[str, Any]]], stored: dict[str, Any] | None = None, created: Any = None):
    """Responder: the events of a date range (by their start day; after the write also *stored*),
    one stored event by id, *created* for a write."""
    events = [event for listed in day_events.values() for event in listed]
    written: list[bool] = []

    def respond(_url, method, params, _data):
        if method == "GET" and params and "oldest" in params:
            pool = events + ([stored] if stored and written else [])
            return [e for e in pool if params["oldest"] <= str(e.get("start_date_local"))[:10] <= params["newest"]]
        if method == "GET":
            return stored if stored is not None else {}
        written.append(True)
        return created if created is not None else {"id": 900}

    return respond


# ------------------------------------------------------------------ dry runs, end to end
class RecordingClient:
    """httpx stand-in: answers GETs from *routes*, records every request (a write fails the test)."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, Any]] = []
        self.is_closed = False

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        path = httpx.URL(url).path.split("/api/v1", 1)[-1]
        self.requests.append((method, path, kwargs.get("params")))
        if method != "GET":
            return httpx.Response(200, json={"id": "written"}, request=httpx.Request(method, url))
        payload: Any = []
        for fragment, value in self.routes.items():
            if path.endswith(fragment):
                payload = value(kwargs.get("params")) if callable(value) else value
                break
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))

    async def aclose(self) -> None:
        self.is_closed = True


def _recording_client(monkeypatch, routes: dict[str, Any]) -> RecordingClient:
    fake = RecordingClient(routes)

    async def get_client():
        return fake

    monkeypatch.setattr(api_client, "_get_httpx_client", get_client)
    custom_items_module._CUSTOM_ITEMS_CACHE.clear()  # pylint: disable=protected-access
    athlete_module._SPORT_SETTINGS_CACHE.clear()  # pylint: disable=protected-access
    athlete_module._ATHLETE_CACHE.clear()  # pylint: disable=protected-access
    return fake


FOLDERS = [{"id": 10, "name": "Cycling", "type": "FOLDER", "children": []}]
LIBRARY_WORKOUT = {"id": 101, "name": "Sweet Spot 3x10", "type": "Ride", "description": "- 10m 55%\n3x\n- 10m 90%\n- 5m 55%",
                   "moving_time": 3600, "folder_id": 10, "workout_doc": STEPS}
SETTINGS = [{"id": 7, "types": ["Ride", "VirtualRide"], "ftp": 250, "lthr": 165, "max_hr": 190}]
CUSTOM_ITEM = {"id": 5, "name": "Carbs", "type": "ACTIVITY_FIELD", "content": {"code": "Carbs", "type": "numeric", "aggregate": "AVERAGE"}}
EXISTING_RIDE = {"id": 41, "start_date_local": "2026-10-12T00:00:00", "category": "WORKOUT", "type": "Ride", "name": "Sweet Spot 3x10"}

DRY_RUNS = [
    pytest.param(lambda: add_or_update_event(name="Endurance", workout_type="Ride", start_date="2026-10-12",
                                             workout_doc=WorkoutDoc.from_dict(STEPS), dry_run=True),
                 "POST", "/athlete/i1/events", id="add_or_update_event create"),
    pytest.param(lambda: add_or_update_event(event_id="77", name="Renamed", dry_run=True), "PUT", "/athlete/i1/events/77",
                 id="add_or_update_event update"),
    pytest.param(lambda: add_or_update_note(name="Travel", description="Flight at 9", start_date="2026-10-12", dry_run=True),
                 "POST", "/athlete/i1/events", id="add_or_update_note"),
    pytest.param(lambda: add_events_bulk([{"name": "Easy", "start_date": "2026-10-13", "workout_type": "Run", "moving_time": 2700}],
                                         dry_run=True), "POST", "/athlete/i1/events/bulk", id="add_events_bulk"),
    pytest.param(lambda: add_event_from_library(workout_id="101", date="2026-10-14", dry_run=True), "POST", "/athlete/i1/events",
                 id="add_event_from_library"),
    pytest.param(lambda: create_library_workout(name="Over-unders", sport_type="Ride", workout_doc=WorkoutDoc.from_dict(STEPS),
                                                dry_run=True), "POST", "/athlete/i1/workouts", id="create_library_workout"),
    pytest.param(lambda: update_activity(activity_id="i55", rpe=6, dry_run=True), "PUT", "/activity/i55", id="update_activity"),
    pytest.param(lambda: update_wellness(date="2026-10-10", fatigue=3, dry_run=True), "PUT", "/athlete/i1/wellness/2026-10-10",
                 id="update_wellness"),
    pytest.param(lambda: update_custom_item(item_id=5, content={"aggregate": "SUM"}, dry_run=True), "PUT", "/athlete/i1/custom-item/5",
                 id="update_custom_item"),
    pytest.param(lambda: create_custom_item(name="Gels", item_type="ACTIVITY_FIELD", content={"code": "Gels", "type": "numeric"},
                                            dry_run=True), "POST", "/athlete/i1/custom-item", id="create_custom_item"),
    pytest.param(lambda: update_sport_settings(sport_type="Ride", ftp=260, dry_run=True), "PUT", "/athlete/i1/sport-settings/7",
                 id="update_sport_settings"),
    pytest.param(lambda: add_activity_message(activity_id="i55", content="Legs heavy", dry_run=True), "POST", "/activity/i55/messages",
                 id="add_activity_message"),
]


@pytest.mark.parametrize("call,method,path", DRY_RUNS)
def test_dry_runs_return_the_exact_request_and_send_no_write(monkeypatch, call, method, path):
    """Through the real API client: a dry run sends GETs only and returns the request as compact JSON."""
    fake = _recording_client(monkeypatch, {
        "/events": [], "/folders": FOLDERS, "/workouts/101": LIBRARY_WORKOUT, "/sport-settings": SETTINGS,
        "/custom-item/5": CUSTOM_ITEM, "/events/77": {"id": 77, "category": "WORKOUT"},
    })
    answer = asyncio.run(call())
    payload = json.loads(answer)
    assert ": " not in answer.split('"message"')[0] and ", " not in answer.split('"message"')[0]  # compact JSON
    assert payload["dry_run"] is True and payload["validation"]["ok"] is True
    assert payload["request"]["method"] == method and payload["request"]["path"] == path
    assert payload["request"]["body"]
    assert all(request_method == "GET" for request_method, _, _ in fake.requests), fake.requests


def test_dry_run_bodies_are_the_requests_after_defaults_and_merges(monkeypatch):
    _recording_client(monkeypatch, {
        "/events": [], "/folders": FOLDERS, "/workouts/101": LIBRARY_WORKOUT, "/sport-settings": SETTINGS,
        "/custom-item/5": CUSTOM_ITEM,
    })
    event = json.loads(asyncio.run(add_or_update_event(name="Endurance", workout_type="Ride", start_date="2026-10-12",
                                                       workout_doc=WorkoutDoc.from_dict(STEPS), dry_run=True)))
    body = event["request"]["body"]
    assert body["category"] == "WORKOUT" and body["type"] == "Ride" and body["start_date_local"] == "2026-10-12T00:00:00"
    assert body["description"] == str(WorkoutDoc.from_dict(STEPS))
    assert event["duplicate_check"].startswith("no event of this category and sport")
    note = json.loads(asyncio.run(add_or_update_note(name="Travel", dry_run=True)))
    assert note["request"]["body"]["color"] == "green" and note["request"]["body"]["category"] == "NOTE"
    item = json.loads(asyncio.run(update_custom_item(item_id=5, content={"aggregate": "SUM"}, dry_run=True)))
    assert item["request"]["body"] == {"content": {"code": "Carbs", "type": "numeric", "aggregate": "SUM"}}
    settings = json.loads(asyncio.run(update_sport_settings(sport_type="Ride", ftp=260, dry_run=True)))
    assert settings["request"]["params"] == {"recalcHrZones": "false"} and settings["request"]["body"] == {"ftp": 260}
    assert settings["sport_setting"]["current"] == {"ftp": 250}
    bulk = json.loads(asyncio.run(add_events_bulk(
        [{"name": "Easy", "start_date": "2026-10-13", "workout_type": "Run", "workout_doc": {"steps": [{"duration": 600, "power": {"value": 70, "units": "%ftp"}}]}}],
        dry_run=True)))
    assert bulk["request"]["params"] == {"upsert": False, "upsertOnUid": False, "updatePlanApplied": False}
    assert bulk["entries_sent"] == [0] and bulk["refused"] == []
    assert bulk["validation"]["warnings"] and bulk["validation"]["warnings"][0].startswith("entry 0: ")  # power target on a run


def test_dry_run_refusals_are_the_refusals_of_the_real_call(monkeypatch):
    calls = _router(monkeypatch, _calendar({"2026-10-12": [EXISTING_RIDE]}))
    duplicate = asyncio.run(add_or_update_event(name=" sweet  SPOT 3x10 ", workout_type="Ride", start_date="2026-10-12", dry_run=True))
    assert duplicate.startswith("Error: 2026-10-12 already has a WORKOUT (Ride) with the same name: event 41 'Sweet Spot 3x10'")
    invalid = asyncio.run(add_or_update_event(name="Bad", workout_type="Ride", dry_run=True,
                                              workout_doc=WorkoutDoc.from_dict({"steps": [{"duration": -5, "power": {"value": 50, "units": "%ftp"}}]})))
    assert invalid.startswith("Error: workout_doc is not valid") and invalid.endswith("Nothing was written.")
    assert not _writes(calls)


def test_a_nested_dry_run_is_read_only_too(monkeypatch):
    """R29-10: a write tool called with dry_run=true from inside another tool call cannot write either."""
    fake = _recording_client(monkeypatch, {"/events": []})

    async def inner(dry_run: bool = False) -> Any:  # pylint: disable=unused-argument
        return await api_client.make_intervals_request("/athlete/i1/events", method="POST", data={"name": "x"})

    inner_tool = guarded(inner)

    async def outer() -> Any:
        return await inner_tool(dry_run=True)

    refused = asyncio.run(guarded(outer)())
    assert refused["read_only"] and not fake.requests


def test_the_guard_refuses_writes_during_a_dry_run(monkeypatch):
    """Second line of defence: during dry_run=true the API client refuses every non-GET request."""
    fake = _recording_client(monkeypatch, {"/events": []})

    async def buggy_tool(dry_run: bool = False) -> Any:  # pylint: disable=unused-argument
        return await api_client.make_intervals_request("/athlete/i1/events", method="POST", data={"name": "x"})

    tool = guarded(buggy_tool)
    refused = asyncio.run(tool(dry_run=True))
    assert refused["error"] and refused["read_only"] and "nothing was written" in refused["message"]
    assert not fake.requests
    assert asyncio.run(tool(dry_run=False)) == {"id": "written"}
    assert fake.requests == [("POST", "/athlete/i1/events", None)]
    with api_client.read_only_requests():
        assert asyncio.run(api_client.make_intervals_request("/athlete/i1/events/1", method="DELETE"))["read_only"]
    assert len(fake.requests) == 1


def test_range_delete_preview_runs_read_only(monkeypatch):
    """delete_events_by_date_range previews by default (dry_run=true): only GETs reach the client."""
    from intervals_mcp_server.tools.events import delete_events_by_date_range  # pylint: disable=import-outside-toplevel

    fake = _recording_client(monkeypatch, {"/events": [EXISTING_RIDE]})
    payload = json.loads(asyncio.run(delete_events_by_date_range("2026-10-12", "2026-10-12")))
    assert payload["dry_run"] is True and [row["id"] for row in payload["events"]] == [41]
    assert [method for method, _, _ in fake.requests] == ["GET"]


# ------------------------------------------------------------------ duplicate check
def test_create_is_refused_when_the_day_has_the_same_name(monkeypatch):
    calls = _router(monkeypatch, _calendar({"2026-10-12": [EXISTING_RIDE]}))
    answer = asyncio.run(add_or_update_event(name="SWEET spot   3x10", workout_type="Ride", start_date="2026-10-12"))
    assert "already has a WORKOUT (Ride) with the same name: event 41 'Sweet Spot 3x10'" in answer
    assert "allow_duplicate=true" in answer and "event_id=41" in answer
    assert not _writes(calls)
    assert [c["params"] for c in calls] == [{"oldest": "2026-10-12", "newest": "2026-10-12"}]


def test_create_is_refused_when_the_day_has_the_same_workout(monkeypatch):
    text = str(WorkoutDoc.from_dict(STEPS))
    existing = {**EXISTING_RIDE, "name": "Tuesday session", "description": "Coach notes first.\n\n" + text.upper()}
    calls = _router(monkeypatch, _calendar({"2026-10-12": [existing]}))
    answer = asyncio.run(add_or_update_event(name="Sweet spot", workout_type="Ride", start_date="2026-10-12",
                                             workout_doc=WorkoutDoc.from_dict(STEPS)))
    assert "same steps: event 41 'Tuesday session'" in answer and not _writes(calls)
    assert workout_signature("intro\n- 10m 55%\n3x\n- 5m 90%") == workout_signature("other intro\n -  10M 55%\n3X\n- 5m 90%")
    assert workout_signature("Warmup\n- 10m 55%\nMain set 3x\n- 5m 90%").splitlines()[1] == "main set 3x"
    assert workout_signature("- 45m Z2 HR") == "" and workout_signature("Rest day") == ""  # single generic lines


def test_duplicates_need_the_same_sport_and_non_trivial_content(monkeypatch):
    """R29-3: a brick or double-sport day is not a duplicate; short one-liners with other names are not either."""
    easy_ride = {"id": 44, "start_date_local": "2026-10-12T06:00:00", "category": "WORKOUT", "type": "Ride",
                 "name": "Easy", "description": "- 45m Z2 HR"}
    long_run = {"id": 45, "start_date_local": "2026-10-12T18:00:00", "category": "WORKOUT", "type": "Run",
                "name": "Long run", "description": "Warmup\n- 10m Z1 HR\n- 60m Z2 HR\nCooldown\n- 5m Z1 HR"}
    calls = _router(monkeypatch, _calendar({"2026-10-12": [easy_ride, long_run]}, stored={"id": 900}))
    assert asyncio.run(add_or_update_event(name="Easy", workout_type="Run", start_date="2026-10-12")).startswith("Successfully")
    same_text = asyncio.run(add_or_update_event(name="Long ride", workout_type="Ride", start_date="2026-10-12",
                                                description=long_run["description"]))
    assert same_text.startswith("Successfully")
    one_liner = asyncio.run(add_or_update_event(name="PM spin", workout_type="Ride", start_date="2026-10-12", description="- 45m Z2 HR"))
    assert one_liner.startswith("Successfully")
    assert len(_writes(calls)) == 3
    refused = asyncio.run(add_or_update_event(name=" easy ", workout_type="Ride", start_date="2026-10-12"))
    assert "already has a WORKOUT (Ride) with the same name: event 44 'Easy'" in refused
    run_steps = asyncio.run(add_or_update_event(name="Sunday run", workout_type="Run", start_date="2026-10-12",
                                                description="Warmup\n- 10m Z1 HR\n- 60m Z2 HR\nCooldown\n- 5m Z1 HR"))
    assert "already has a WORKOUT (Run) with the same steps: event 45 'Long run'" in run_steps
    assert len(_writes(calls)) == 3


def test_other_categories_days_and_allow_duplicate_are_not_refused(monkeypatch):
    note = {"id": 42, "start_date_local": "2026-10-12T00:00:00", "category": "NOTE", "name": "Sweet Spot 3x10"}
    next_day = {**EXISTING_RIDE, "start_date_local": "2026-10-13T00:00:00"}
    calls = _router(monkeypatch, _calendar({"2026-10-12": [note], "2026-10-13": [next_day]},
                                           stored={"id": 900, "name": "Sweet Spot 3x10"}))
    assert asyncio.run(add_or_update_event(name="Sweet Spot 3x10", workout_type="Ride", start_date="2026-10-12")).startswith("Successfully")
    assert len(_writes(calls)) == 1
    calls.clear()
    allowed = asyncio.run(add_or_update_event(name="Sweet Spot 3x10", workout_type="Ride", start_date="2026-10-13", allow_duplicate=True))
    assert allowed.startswith("Successfully created event id: 900")
    assert not [c for c in calls if c["params"]]  # no duplicate check
    assert len(_writes(calls)) == 1


def test_duplicate_check_failure_writes_nothing(monkeypatch):
    calls = _router(monkeypatch, lambda url, method, params, data: {"error": True, "message": "503 Service Unavailable"} if params else {"id": 1})
    answer = asyncio.run(add_or_update_note(name="Rest", start_date="2026-10-12"))
    assert answer == ("Error: the duplicate check could not run: the events of 2026-10-12 could not be read "
                      "(503 Service Unavailable). Nothing was written; try again in a moment.")
    assert "allow_duplicate" not in answer and not _writes(calls)  # R29-5: no invitation to skip the check


def test_note_and_library_creates_are_checked(monkeypatch):
    note = {"id": 43, "start_date_local": "2026-10-12T00:00:00", "category": "NOTE", "name": "Travel",
            "description": "Flight 9:00\nHotel near the station"}
    calls = _router(monkeypatch, _calendar({"2026-10-12": [note, EXISTING_RIDE]}, stored={"id": 900}))
    same_text = asyncio.run(add_or_update_note(name="Trip", description="flight  9:00\n hotel near the STATION", start_date="2026-10-12"))
    assert "already has a NOTE with the same text: event 43" in same_text and "add_or_update_note with event_id=43" in same_text
    assert not _writes(calls)
    assert asyncio.run(add_or_update_note(name="Trip", description="Flight 9:00", start_date="2026-10-12")).startswith("Successfully")

    def library(url, method, params, data):
        if url.endswith("/workouts/101"):
            return LIBRARY_WORKOUT
        return _calendar({"2026-10-12": [note, EXISTING_RIDE]})(url, method, params, data)

    calls = _router(monkeypatch, library)
    answer = asyncio.run(add_event_from_library(workout_id="101", date="2026-10-12"))
    assert "already has a WORKOUT (Ride) with the same name: event 41" in answer and not _writes(calls)


def test_bulk_refuses_duplicate_entries_and_creates_the_others(monkeypatch):
    entries = [
        {"name": "Sweet Spot 3x10", "start_date": "2026-10-12", "workout_type": "Ride"},  # existing event 41
        {"name": "Easy run", "start_date": "2026-10-13", "workout_type": "Run", "moving_time": 2700},
        {"name": "easy RUN", "start_date": "2026-10-13", "workout_type": "Run", "moving_time": 2700},  # repeats entry 1 exactly
        {"name": "Easy run", "start_date": "2026-10-13", "workout_type": "Run", "moving_time": 1800},  # R29-4: a second session
        {"name": "Rest", "start_date": "2026-10-14", "category": "NOTE", "description": "Full rest"},
    ]
    stored = [{"id": 501, "start_date_local": "2026-10-13T00:00:00", "category": "WORKOUT", "type": "Run", "name": "Easy run", "moving_time": 2700},
              {"id": 502, "start_date_local": "2026-10-13T00:00:00", "category": "WORKOUT", "type": "Run", "name": "Easy run", "moving_time": 1800},
              {"id": 503, "start_date_local": "2026-10-14T00:00:00", "category": "NOTE", "name": "Rest", "description": "Full rest"}]

    def respond(url, method, params, data):
        if method == "GET":
            return stored if any(c["method"] == "POST" for c in calls) else [EXISTING_RIDE]
        return [{"id": 501, "name": "Easy run"}, {"id": 502, "name": "Easy run"}, {"id": 503, "name": "Rest"}]

    calls = _router(monkeypatch, respond)
    answer = asyncio.run(add_events_bulk(entries))
    payload = json.loads(answer)
    gets = [c["params"] for c in calls if c["method"] == "GET"]
    # R29-6: one range GET for the duplicate check, one for the read-back of the created events
    assert gets == [{"oldest": "2026-10-12", "newest": "2026-10-14"}, {"oldest": "2026-10-13", "newest": "2026-10-14"}]
    assert [body["moving_time"] if "moving_time" in body else None for body in _writes(calls)[0]["data"]] == [2700, 1800, None]
    assert [row["index"] for row in payload["created"]] == [1, 3, 4] and payload["created_count"] == 3
    assert payload["created"][0] == {"index": 1, "status": "created", "id": 501, "date": "2026-10-13", "name": "Easy run"}
    assert payload["created"][1]["warnings"] == ["same day, sport and name as entry 1: created as a second session"]
    refused = {row["index"]: row for row in payload["refused"]}
    assert refused[0] == {"index": 0, "status": "refused", "date": "2026-10-12", "name": "Sweet Spot 3x10", "same": "name",
                          "existing_id": 41, "existing_name": "Sweet Spot 3x10"}
    assert refused[2]["duplicate_of_index"] == 1 and refused[2]["same"] == "entry"
    assert "Created 3 of 5 entries. 2 entries were refused" in payload["message"]
    assert ": " not in answer.split('"message"')[0] and "stored" not in payload["created"][0]  # compact by default
    calls.clear()
    full = json.loads(asyncio.run(add_events_bulk(entries, detail_level="full")))
    assert full["created"][0]["stored"] == {"start_date_local": "2026-10-13T00:00:00", "category": "WORKOUT", "type": "Run",
                                            "moving_time": 2700, "duration": "45:00", "steps": 0}
    assert full["created"][2]["stored"] == {"start_date_local": "2026-10-14T00:00:00", "category": "NOTE", "text_chars": 9}


def test_bulk_answer_stays_small(monkeypatch):
    """R29-7: 100 created entries in the compact answer stay far below the output limit."""
    entries = [{"name": f"Ride {i}", "start_date": f"2027-{1 + i // 28:02d}-{1 + i % 28:02d}", "workout_type": "Ride",
                "description": "Warmup\n- 10m 55%\n- 40m 70%\nCooldown\n- 10m 50%"} for i in range(100)]
    created = [{"id": 1000 + i, "name": f"Ride {i}"} for i in range(100)]
    stored = [{"id": 1000 + i, "start_date_local": f"2027-{1 + i // 28:02d}-{1 + i % 28:02d}T00:00:00", "category": "WORKOUT",
               "type": "Ride", "name": f"Ride {i}", "moving_time": 3600, "icu_training_load": 50,
               "workout_doc": {"steps": [{"duration": 600}, {"duration": 2400}, {"duration": 600}]}} for i in range(100)]
    posted: list[bool] = []

    def respond(url, method, params, data):
        if method == "GET":
            return stored if posted else []  # empty calendar before the write, the stored events after it
        posted.append(True)
        return created

    _router(monkeypatch, respond)
    answer = asyncio.run(add_events_bulk(entries))
    payload = json.loads(answer)
    assert payload["created_count"] == 100 and len(answer) < 12_000, len(answer)


def test_bulk_with_only_duplicates_sends_nothing(monkeypatch):
    calls = _router(monkeypatch, _calendar({"2026-10-12": [EXISTING_RIDE]}))
    payload = json.loads(asyncio.run(add_events_bulk([{"name": "Sweet Spot 3x10", "start_date": "2026-10-12", "workout_type": "Ride"}])))
    assert payload["created_count"] == 0 and payload["refused"][0]["existing_id"] == 41 and not _writes(calls)
    assert payload["message"].startswith("No events were sent: every entry duplicates")


def test_bulk_duplicate_check_respects_the_request_budget(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_MAX_REQUESTS", "2")  # the check (1) plus the write and the read-back (2) do not fit
    calls = _router(monkeypatch, _calendar({}))
    entries = [{"name": f"Ride {day}", "start_date": f"2026-10-{day:02d}", "workout_type": "Ride"} for day in range(10, 15)]
    payload = json.loads(asyncio.run(add_events_bulk(entries)))
    assert payload["message"] == ("No events were sent: the duplicate check could not run: this tool call has no requests "
                                  "left for the duplicate check. Nothing was written; try again in a moment.")
    assert not calls


# ------------------------------------------------------------------ read-back
def test_read_back_reports_what_was_stored_and_parsed(monkeypatch):
    stored = {"id": 900, "start_date_local": "2026-10-12T00:00:00", "category": "WORKOUT", "type": "Ride", "name": "Sweet spot",
              "moving_time": 3900, "icu_training_load": 62, "workout_doc": PARSED}
    calls = _router(monkeypatch, _calendar({}, stored=stored))
    answer = asyncio.run(add_or_update_event(name="Sweet spot", workout_type="Ride", start_date="2026-10-12",
                                             workout_doc=WorkoutDoc.from_dict(STEPS)))
    head, readback = answer.split("\n")
    assert head == "Successfully created event id: 900"
    assert readback == ("Read-back: Intervals.icu stored 2026-10-12 WORKOUT Ride 'Sweet spot', 1:05:00, load 62, "
                        "4 steps parsed (4 sent). Parse warnings: none.")
    assert [c["url"] for c in calls if c["method"] == "GET" and not c["params"]] == ["/athlete/i1/events/900"]


def test_parse_warnings_name_dropped_steps_units_and_missing_duration():
    sent = sent_shape(STEPS)
    changed = {"id": 1, "name": "Sweet spot", "type": "VirtualRide", "moving_time": None, "workout_doc": {"steps": [
        {"duration": 600, "power": {"value": 55, "units": "w"}},
        {"reps": 2, "steps": [{"duration": 600, "power": {"value": 90, "units": "%ftp"}}]},
        {"power": {"value": 50, "units": "%ftp"}},
    ]}}
    warnings = parse_warnings({"name": "Sweet spot", "type": "Ride"}, changed, sent)
    assert "sport 'Ride' sent, 'VirtualRide' stored" in warnings
    assert "step(s) 3 stored without duration or distance" in warnings
    assert "Intervals.icu parsed 2 steps of 4 sent: steps were dropped or merged" in warnings
    assert "repeats 3x sent, 2x stored" in warnings
    assert "no planned duration stored" in warnings
    same_count = {"workout_doc": {"steps": [
        {"duration": 540, "power": {"value": 55, "units": "w"}},
        {"reps": 3, "steps": [{"duration": 600, "hr": {"value": 90, "units": "%lthr"}}, {"duration": 300, "power": {"value": 55, "units": "%ftp"}}]},
        {"duration": 600, "power": {"value": 50, "units": "%ftp"}, "cadence": {"value": 90, "units": "rpm"}},
    ]}, "moving_time": 4440}
    detail = parse_warnings({}, same_count, sent)
    assert "step 1: duration 10:00 sent, 9:00 stored" in detail
    assert "step 1: power target units %ftp sent, w stored" in detail
    assert "step 2: power target (%ftp) not stored" in detail and "step 2: hr target stored (%lthr) that was not sent" in detail
    assert not any("cadence" in w for w in detail)  # an added cadence target is not a problem
    assert parse_warnings({"moving_time": 3600}, {"moving_time": 4500}, None) == [
        "moving_time 1:00:00 sent, 1:15:00 stored (Intervals.icu uses the workout steps)"]
    text_shape = sent_shape(None, "Warmup\n- 10m 55%\n3x\n- 5m 100%\n- 5m 50%\n")
    assert text_shape is not None and text_shape["count"] == 3 and text_shape["repeats"] == [3]
    assert parse_warnings({}, {"workout_doc": {}}, text_shape) == [
        "no workout steps stored (3 sent): Intervals.icu did not read the text as a workout"]


def test_absolute_paces_stored_in_seconds_per_distance_are_not_warnings():
    """R29-1: Intervals.icu stores "5:35/km Pace" (sent as MINS_KM) as secs/km; other units likewise."""
    sent = sent_shape({"steps": [
        {"duration": 600, "pace": {"value": 335, "units": "MINS_KM"}},
        {"distance": 1000, "pace": {"start": 4.5, "end": 4.6667, "units": "MINS_KM"}},  # decimal minutes
        {"duration": 300, "pace": {"value": 540, "units": "MINS_MILE"}},
        {"distance": 400, "pace": {"value": 105, "units": "SECS_100M"}},
    ]})
    stored = {"workout_doc": {"steps": [
        {"duration": 600, "pace": {"value": 335, "units": "secs/km"}},
        {"distance": 1000, "duration": 275, "pace": {"start": 280, "end": 270, "units": "secs/km"}},  # start/end swapped
        {"duration": 300, "pace": {"value": 541, "units": "secs/mile"}},  # within 2 s
        {"distance": 400, "duration": 420, "pace": {"value": 105, "units": "secs/100m"}},
    ]}, "moving_time": 1595}
    assert not parse_warnings({}, stored, sent)
    slower = {"workout_doc": {"steps": [{**stored["workout_doc"]["steps"][0], "pace": {"value": 350, "units": "secs/km"}},
                                        *stored["workout_doc"]["steps"][1:]]}, "moving_time": 1595}
    assert parse_warnings({}, slower, sent) == ["step 1: pace 5:35/km sent, 5:50/km stored"]
    other_units = {"workout_doc": {"steps": [{**stored["workout_doc"]["steps"][0], "pace": {"value": 90, "units": "%pace"}},
                                             *stored["workout_doc"]["steps"][1:]]}, "moving_time": 1595}
    assert parse_warnings({}, other_units, sent) == ["step 1: pace target units secs/km sent, %pace stored"]


def test_labelled_repeat_headers_are_repeats():
    """R29-2: "Main set 3x" (the guide's own native-text example) is a repeat header."""
    text = "Warmup\n- 10m 55%\n\nMain set 3x\n- 10m 90%\n- 5m 55%\n\nCooldown\n- 10m 50%"
    shape = sent_shape(None, text)
    assert shape is not None and shape["count"] == 4 and shape["repeats"] == [3]
    stored = {"moving_time": 4500, "workout_doc": {"steps": [
        {"duration": 600, "power": {"value": 55, "units": "%ftp"}, "warmup": True},
        {"reps": 3, "text": "Main set 3x", "duration": 2700, "steps": [{"duration": 600}, {"duration": 300}]},
        {"duration": 600, "power": {"value": 50, "units": "%ftp"}, "cooldown": True},
    ]}}
    assert not parse_warnings({}, stored, shape)
    assert sent_shape(None, "- 10m 55%\n3x Over-unders\n- 2m 95%\n- 1m 105%")["repeats"] == [3]
    assert sent_shape(None, "- 10m 55%\n- Squats 5x5 10m\n- 5m 50%")["repeats"] == []  # a step line is never a header


def test_notes_are_read_back_without_workout_fields(monkeypatch):
    """R29-8: date (time only when not midnight), category, name and text length; no duration, load or steps."""
    stored = {"id": 900, "start_date_local": "2026-10-12T00:00:00", "category": "NOTE", "name": "Travel",
              "description": "Flight 9:00\n- 5m walk to gate", "workout_doc": {"steps": [{"duration": 300}]}}
    _router(monkeypatch, _calendar({}, stored=stored))
    answer = asyncio.run(add_or_update_note(name="Travel", description="Flight 9:00\n- 5m walk to gate", start_date="2026-10-12"))
    assert answer.split("\n")[1] == "Read-back: Intervals.icu stored 2026-10-12 NOTE 'Travel', text 29 characters. Parse warnings: none."
    workout = stored_summary({"id": 1, "category": "WORKOUT", "type": "Ride", "name": "Spin", "workout_doc": {"steps": [{"duration": 60}]}})
    assert read_back_text(workout, [], None, "event 1") == (
        "Read-back: Intervals.icu stored WORKOUT Ride 'Spin', no planned duration, load n/a, 1 step parsed. Parse warnings: none.")


def test_read_back_text_is_compact():
    summary = stored_summary({"id": 1, "start_date_local": "2026-10-12T00:00:00", "category": "WORKOUT", "type": "Ride",
                              "name": "N" * 80, "moving_time": 3600, "icu_training_load": 50}, sent_shape(STEPS))
    warnings = [f"step {i}: power target units %ftp sent, w stored" for i in range(1, 40)]
    text = read_back_text(summary, warnings, None, "event 1")
    assert len(text) <= READ_BACK_CHARS and "more)." in text


def test_failed_read_back_says_the_write_is_not_verified(monkeypatch):
    def respond(url, method, params, _data):
        if method == "GET":
            return [] if params else {"error": True, "message": "502 Bad Gateway"}
        return {"id": 900}

    _router(monkeypatch, respond)
    answer = asyncio.run(add_or_update_event(name="Easy", workout_type="Run", start_date="2026-10-12"))
    assert answer.startswith("Successfully created event id: 900\n")
    assert "Read-back: the write succeeded but event 900 could not be verified (502 Bad Gateway)" in answer


def test_updates_and_library_workouts_are_read_back(monkeypatch):
    stored = {"id": 77, "start_date_local": "2026-10-12T07:30:00", "category": "WORKOUT", "type": "Ride", "name": "Renamed"}
    calls = _router(monkeypatch, _calendar({}, stored=stored))
    answer = asyncio.run(add_or_update_event(event_id="77", name="Renamed"))
    assert "Read-back: Intervals.icu stored 2026-10-12 07:30 WORKOUT Ride 'Renamed'" in answer
    assert not [c for c in calls if c["params"]]  # an update is never checked for duplicates

    library = {"id": 555, "name": "Over-unders", "type": "Ride", "folder_id": 10, "moving_time": 3900, "workout_doc": PARSED}

    def respond(url, method, params, data):
        if url.endswith("/folders"):
            return FOLDERS
        return library if method == "GET" else {"id": 555}

    _router(monkeypatch, respond)
    created = asyncio.run(create_library_workout(name="Over-unders", sport_type="Ride", workout_doc=WorkoutDoc.from_dict(STEPS)))
    assert created.startswith("Successfully created library workout id: 555 in folder 10\n")
    assert "Read-back: Intervals.icu stored Ride 'Over-unders', 1:05:00, load n/a, 4 steps parsed (4 sent). Parse warnings: none." in created
    library["moving_time"] = 4500
    later = asyncio.run(create_library_workout(name="Over-unders", sport_type="Ride", workout_doc=WorkoutDoc.from_dict(STEPS)))
    assert "Parse warnings: planned time 1:15:00 stored, 1:05:00 in the steps sent." in later


def test_library_event_read_back_compares_with_the_library_steps(monkeypatch):
    imported = {**LIBRARY_WORKOUT, "description": "Imported from a .zwo file"}
    stored = {"id": 901, "start_date_local": "2026-10-15T00:00:00", "category": "WORKOUT", "type": "Ride", "name": "Sweet Spot 3x10"}

    def respond(url, method, params, data):
        if url.endswith("/workouts/101"):
            return imported
        return _calendar({}, stored=stored, created={"id": 901})(url, method, params, data)

    _router(monkeypatch, respond)
    answer = asyncio.run(add_event_from_library(workout_id="101", date="2026-10-15"))
    assert "created WITHOUT steps" in answer
    assert "no workout steps stored (4 sent)" in answer


# ------------------------------------------------------------------ deletions
def test_delete_event_names_what_was_deleted(monkeypatch):
    event = {"id": 5, "start_date_local": "2026-10-13T06:00:00", "category": "WORKOUT", "type": "Ride", "name": "Done ride",
             "paired_activity_id": "i9"}
    calls = _router(monkeypatch, lambda url, method, p, d: event if method == "GET" else {})
    answer = asyncio.run(delete_event(event_id="5"))
    assert answer == "Deleted event 5: 2026-10-13 WORKOUT Ride 'Done ride' (paired with activity i9)."
    assert [(c["method"], c["url"]) for c in calls] == [("GET", "/athlete/i1/events/5"), ("DELETE", "/athlete/i1/events/5")]
    note = {"id": 6, "start_date_local": "2026-10-14T00:00:00", "category": "NOTE", "name": "Travel"}
    _router(monkeypatch, lambda url, method, p, d: note if method == "GET" else {})
    assert asyncio.run(delete_event(event_id="6")) == "Deleted event 6: 2026-10-14 NOTE 'Travel' (not paired with an activity)."


@pytest.mark.parametrize("answer", [NOT_FOUND, {}])
def test_delete_of_a_missing_event_deletes_nothing(monkeypatch, answer):
    calls = _router(monkeypatch, lambda url, method, p, d: answer)
    assert asyncio.run(delete_event(event_id="5")) == "Event 5 not found; nothing was deleted."
    assert not _writes(calls)


def test_delete_answers_for_vanished_objects_and_errors(monkeypatch):
    event = {"id": 5, "start_date_local": "2026-10-13T00:00:00", "category": "WORKOUT", "name": "x"}
    _router(monkeypatch, lambda url, method, p, d: event if method == "GET" else NOT_FOUND)
    assert asyncio.run(delete_event(event_id="5")) == "Event 5 was already gone; nothing was deleted."
    _router(monkeypatch, lambda url, method, p, d: {"error": True, "status_code": 500, "message": "boom"})
    assert asyncio.run(delete_event(event_id="5")) == "Error reading event 5 before deleting it: boom. Nothing was deleted."
    calls = _router(monkeypatch, lambda url, method, p, d: NOT_FOUND)
    assert asyncio.run(delete_custom_item(item_id=9)) == "No custom item found with ID 9; nothing was deleted."
    assert asyncio.run(delete_library_workout(workout_id="9")) == "No library workout found with id 9; nothing was deleted."
    assert not _writes(calls)
    workout = {"id": 9, "name": "Over-unders", "type": "Ride", "folder_id": 10}
    _router(monkeypatch, lambda url, method, p, d: workout if method == "GET" else {})
    assert asyncio.run(delete_library_workout(workout_id="9")) == "Deleted library workout 9 'Over-unders' (Ride, folder 10)."


def test_dry_run_through_an_mcp_tool_call(monkeypatch):
    """The new parameters are part of the tool schema and work through tools/call (a scratch server with the
    write tools, independent of MCP_PERMISSIONS)."""
    from intervals_mcp_server import mcp_instance  # pylint: disable=import-outside-toplevel

    fake = _recording_client(monkeypatch, {"/events": [EXISTING_RIDE]})
    server = mcp_instance.IntervalsFastMCP("write-safety")
    for name in ("add_or_update_event", "add_events_bulk"):
        spec = mcp_instance._TOOL_SPECS[name]  # pylint: disable=protected-access
        server.add_tool(spec.func, **spec.options)
    schema = {tool.name: tool.inputSchema["properties"] for tool in asyncio.run(server.list_tools())}
    for name in ("add_or_update_event", "add_events_bulk"):
        assert schema[name]["dry_run"] == {"default": False, "description": schema[name]["dry_run"]["description"], "type": "boolean"}
        assert schema[name]["allow_duplicate"]["type"] == "boolean"
    assert schema["add_events_bulk"]["detail_level"]["enum"] == ["compact", "full"]
    result = asyncio.run(server.call_tool("add_or_update_event", {
        "name": "Tempo", "workout_type": "Run", "start_date": "2026-10-12", "dry_run": True}))
    payload = json.loads(result[0].text)
    assert payload["request"]["method"] == "POST" and payload["request"]["body"]["name"] == "Tempo"
    refused = asyncio.run(server.call_tool("add_or_update_event", {
        "name": "sweet spot 3x10", "workout_type": "Ride", "start_date": "2026-10-12", "dry_run": True}))
    assert "already has a WORKOUT (Ride) with the same name: event 41" in refused[0].text
    assert {method for method, _, _ in fake.requests} == {"GET"}
