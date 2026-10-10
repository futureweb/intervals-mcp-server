"""Regression tests for the review findings on the write tools (WRT-1 ... WRT-17).

Every test runs against mocked requests; nothing reaches Intervals.icu.
"""

import asyncio
import json
import os
import pathlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

# pylint: disable=wrong-import-position,missing-function-docstring
from intervals_mcp_server.mcp_instance import OVERWRITE_ANNOTATIONS, PERMISSION_ANNOTATIONS, tool_permissions  # noqa: E402
from intervals_mcp_server.tools import athlete as athlete_module  # noqa: E402
from intervals_mcp_server.tools.activities import add_activity_message, update_activity  # noqa: E402
from intervals_mcp_server.tools.athlete import parse_threshold_pace, update_sport_settings  # noqa: E402
from intervals_mcp_server.tools.custom_items import delete_custom_item, update_custom_item  # noqa: E402
from intervals_mcp_server.tools.events import (  # noqa: E402
    add_events_bulk,
    add_or_update_event,
    add_or_update_note,
    delete_events_by_date_range,
)
from intervals_mcp_server.tools.workout_check import validate_workout  # noqa: E402
from intervals_mcp_server.tools.workout_library import add_event_from_library, create_library_workout  # noqa: E402
from intervals_mcp_server.utils.types import Step, WorkoutDoc  # noqa: E402
from intervals_mcp_server.utils.validation import infer_activity_type, validate_date  # noqa: E402
from intervals_mcp_server.utils.workout_validation import label_problem, validate_workout_doc  # noqa: E402

MODULES = (
    "intervals_mcp_server.api.client",
    "intervals_mcp_server.tools.events",
    "intervals_mcp_server.tools.workout_library",
    "intervals_mcp_server.tools.custom_items",
    "intervals_mcp_server.tools.activities",
    "intervals_mcp_server.tools.wellness",
    "intervals_mcp_server.tools.gear",
)


def _router(monkeypatch, responder=None) -> list[dict]:
    """Patch make_intervals_request everywhere; *responder(url, method, params, data)* answers."""
    calls: list[dict] = []

    async def fake_request(url=None, api_key=None, params=None, method="GET", data=None):  # pylint: disable=unused-argument
        calls.append({"url": url, "method": method, "params": params, "data": data})
        if responder is not None:
            return responder(url, method, params, data)
        return {"id": 1} if method != "GET" else {}

    for module in MODULES:
        monkeypatch.setattr(f"{module}.make_intervals_request", fake_request)
    return calls


def _writes(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["method"] != "GET"]


# ------------------------------------------------------------------ WRT-1 / API-16
CALENDAR = [
    {"id": 1, "start_date_local": "2026-10-12T00:00:00", "category": "WORKOUT", "type": "Ride", "name": "Threshold"},
    {"id": 2, "start_date_local": "2026-10-12T00:00:00", "category": "RACE_A", "type": "Run", "name": "Marathon"},
    {"id": 3, "start_date_local": "2026-10-13T00:00:00", "category": "NOTE", "name": "Coach note"},
    {"id": 4, "start_date_local": "2026-09-01T00:00:00", "end_date_local": "2026-12-01T00:00:00", "category": "WORKOUT", "name": "Long event"},
    {"id": 5, "start_date_local": "2026-10-13T06:00:00", "category": "WORKOUT", "type": "Ride", "name": "Done ride", "paired_activity_id": "i9"},
    {"id": 6, "start_date_local": "2026-10-13T00:00:00", "category": "SICK", "name": "Flu"},
]


def _calendar(url, method, params, _data):
    if method == "GET":
        wanted = set((params or {}).get("category", "").split(","))
        return [e for e in CALENDAR if e["category"] in wanted]
    if url.endswith("/bulk-delete"):
        return {"eventsDeleted": 1}
    return {}


def test_range_delete_is_a_preview_of_planned_workouts_by_default(monkeypatch):
    calls = _router(monkeypatch, _calendar)
    payload = json.loads(asyncio.run(delete_events_by_date_range("2026-10-12", "2026-10-13")))
    assert payload["dry_run"] is True and "nothing was deleted" in payload["message"]
    assert [e["id"] for e in payload["events"]] == [1]  # only WORKOUT, starting in the range, not paired
    reasons = {k["id"]: k["reason"] for k in payload["kept"]}
    assert "starts outside" in reasons[4] and "paired" in reasons[5]
    assert calls[0]["params"] == {"oldest": "2026-10-12", "newest": "2026-10-13", "category": "WORKOUT"}
    assert not _writes(calls)


def test_range_delete_needs_the_confirmed_ids(monkeypatch):
    calls = _router(monkeypatch, _calendar)
    for confirm in (None, "", " , ", "1,abc"):
        result = asyncio.run(delete_events_by_date_range("2026-10-12", "2026-10-13", dry_run=False, confirm_ids=confirm))
        assert result.startswith("Error:") and "Nothing was deleted" in result
    assert not calls


def test_range_delete_deletes_only_confirmed_ids_that_still_match(monkeypatch):
    """R25-2/R25-3: an event added after the preview is never deleted; per-id DELETE."""
    calendar = list(CALENDAR)

    def responder(_url, method, params, _data):
        if method == "GET":
            wanted = set((params or {}).get("category", "").split(","))
            return [e for e in calendar if e["category"] in wanted]
        return {}

    calls = _router(monkeypatch, responder)
    preview = json.loads(asyncio.run(delete_events_by_date_range("2026-10-12", "2026-10-13", categories="WORKOUT,NOTE")))
    assert [e["id"] for e in preview["events"]] == [1, 3] and 'confirm_ids="1,3"' in preview["message"]
    calendar.append({"id": 17, "start_date_local": "2026-10-13T00:00:00", "category": "WORKOUT", "name": "Added after the preview"})
    payload = json.loads(asyncio.run(delete_events_by_date_range(
        "2026-10-12", "2026-10-13", categories="WORKOUT,NOTE", dry_run=False, confirm_ids="1,3,99")))
    assert [(c["method"], c["url"]) for c in _writes(calls)] == [
        ("DELETE", "/athlete/i1/events/1"), ("DELETE", "/athlete/i1/events/3")]
    assert [e["id"] for e in payload["deleted"]] == [1, 3]
    assert [e["id"] for e in payload["not_confirmed"]] == [17] and payload["no_longer_matching"] == ["99"]
    assert "1 matching event(s) were not confirmed and were kept" in payload["message"]


def test_range_delete_reports_each_id(monkeypatch):
    def responder(url, method, params, _data):
        if method == "GET":
            return [e for e in CALENDAR if e["category"] in set(params["category"].split(","))]
        if url.endswith("/1"):
            return {"error": True, "status_code": 404, "message": "404 Not Found"}
        return {"error": True, "status_code": 500, "message": "500 Internal Server Error"}

    _router(monkeypatch, responder)
    payload = json.loads(asyncio.run(delete_events_by_date_range(
        "2026-10-12", "2026-10-13", categories="WORKOUT,NOTE", dry_run=False, confirm_ids="1,3")))
    assert [e["id"] for e in payload["already_gone"]] == [1] and payload["deleted"] == []
    assert payload["failed"][0]["id"] == 3 and "500" in payload["failed"][0]["error"]


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"start_date": "2026-10-13", "end_date": "2000-01-01"}, "must not be before"),
        ({"start_date": "1900-01-01", "end_date": "2999-12-31"}, "at most 31 days"),
        ({"start_date": "2026-10-01", "end_date": "2026-11-01"}, "at most 31 days"),
        ({"start_date": "2026-10-01", "end_date": "2026-10-02", "categories": "PLAN"}, "cannot be deleted by date range"),
        ({"start_date": "2026-10-01", "end_date": "2026-10-02", "categories": "SET_EFTP"}, "cannot be deleted by date range"),
        ({"start_date": "2026-1-5", "end_date": "2026-01-06"}, "YYYY-MM-DD"),
    ],
)
def test_range_delete_refuses_unbounded_requests(monkeypatch, kwargs, message):
    calls = _router(monkeypatch, _calendar)
    assert message in asyncio.run(delete_events_by_date_range(**kwargs, dry_run=False))
    assert not calls


def test_range_delete_keeps_destructive_class():
    assert tool_permissions()["delete_events_by_date_range"] == "destructive"


# ------------------------------------------------------------------ WRT-2
@pytest.mark.parametrize("doc", [{}, {"steps": []}, {"steps": [], "description": "  "}])
def test_blank_workout_doc_never_wipes_the_planned_workout(monkeypatch, doc):
    calls = _router(monkeypatch)
    asyncio.run(add_or_update_event(event_id="5", name="Renamed", workout_doc=WorkoutDoc.from_dict(doc)))
    assert _writes(calls) == [{"url": "/athlete/i1/events/5", "method": "PUT", "params": None, "data": {"name": "Renamed"}}]


STRUCTURED = {"id": 5, "category": "WORKOUT", "workout_doc": {"steps": [{"duration": 600, "power": {"value": 60}}]}}
TEXT_ONLY = {"id": 5, "category": "WORKOUT", "type": "WeightTraining", "description": "Squats 5x5",
             "workout_doc": {"steps": [{"text": "Squats 5x5"}]}}


def test_text_never_silently_replaces_a_structured_workout(monkeypatch):
    calls = _router(monkeypatch, lambda url, method, p, d: STRUCTURED if method == "GET" else {"id": 5})
    for kwargs in ({"workout_doc": WorkoutDoc.from_dict({"description": "Moved to Thursday"})}, {"description": "Moved to Thursday"}):
        result = asyncio.run(add_or_update_event(event_id="5", **kwargs))
        assert "holds a structured workout" in result and result.endswith("Nothing was changed.")
    assert not _writes(calls)
    asyncio.run(add_or_update_event(event_id="5", description="Easy spin instead", replace_workout=True))
    assert _writes(calls)[0]["data"] == {"description": "Easy spin instead"}


def test_text_only_workouts_can_be_created_and_edited(monkeypatch):
    """R25-4: strength / yoga sessions are plain text."""
    calls = _router(monkeypatch, lambda url, method, p, d: ([] if p else TEXT_ONLY) if method == "GET" else {"id": 5})
    asyncio.run(add_or_update_event(name="Strength", workout_type="WeightTraining", description="Squats 5x5\nDeadlifts 3x5", start_date="2026-10-12"))
    asyncio.run(add_or_update_event(event_id="5", description="Squats 5x5\nPlank 3x60s"))
    exercises = {"steps": [{"text": "Squats"}, {"text": "Deadlifts"}, {"text": "Plank"}]}
    asyncio.run(add_or_update_event(event_id="5", workout_doc=WorkoutDoc.from_dict(exercises)))
    writes = _writes(calls)
    assert writes[0]["data"]["description"] == "Squats 5x5\nDeadlifts 3x5" and writes[0]["data"]["type"] == "WeightTraining"
    assert writes[1]["data"] == {"description": "Squats 5x5\nPlank 3x60s"}
    assert "Squats" in writes[2]["data"]["description"] and "Plank" in writes[2]["data"]["description"]
    assert "either workout_doc or description" in asyncio.run(add_or_update_event(
        event_id="5", description="x", workout_doc=WorkoutDoc.from_dict({"steps": [{"duration": 60, "power": {"value": 60, "units": "%ftp"}}]})))


def test_blank_description_is_not_sent(monkeypatch):
    calls = _router(monkeypatch)
    assert asyncio.run(add_or_update_event(event_id="5", description="  ")).startswith("Error: nothing to update")
    assert not _writes(calls)


def test_library_workout_keeps_its_description_with_an_empty_doc(monkeypatch):
    folders = [{"id": 10, "name": "Strength", "type": "FOLDER"}]
    calls = _router(monkeypatch, lambda url, method, p, d: folders if method == "GET" else {"id": 77})
    asyncio.run(create_library_workout(
        name="Squats", sport_type="WeightTraining", description="Squats 5x5", workout_doc=WorkoutDoc.from_dict({"steps": []})))
    assert _writes(calls)[0]["data"]["description"] == "Squats 5x5"


# ------------------------------------------------------------------ WRT-3
def test_note_tool_never_turns_a_workout_into_a_note(monkeypatch):
    calls = _router(monkeypatch, lambda url, method, p, d: {"id": 6, "category": "WORKOUT"} if method == "GET" else {"id": 6})
    result = asyncio.run(add_or_update_note(event_id="6", name="Felt great"))
    assert "is a WORKOUT event, not a NOTE" in result and not _writes(calls)


# ------------------------------------------------------------------ WRT-4 / WRT-5 / WRT-6 / WRT-7
BAD_DOCS = [
    ({"steps": [{"text": "Main set", "steps": [{"duration": 180, "power": {"value": 115, "units": "%ftp"}}]}]}, "reps must be a positive integer"),
    ({"steps": [{"reps": 6, "duration": 30}]}, "a repeat block needs nested steps"),
    ({"steps": [{"text": "Spin", "until_lap_press": True, "power": {"value": 55, "units": "%ftp"}}, {"duration": 600, "power": {"value": 60, "units": "%ftp"}}]}, "open-ended step"),
    ({"steps": [{"duration": 600, "distance": 5000, "power": {"value": 70, "units": "%ftp"}}]}, "not both"),
    ({"steps": [{"duration": -600, "power": {"value": 60, "units": "%ftp"}}]}, "duration must be a positive number"),
    ({"steps": [{"duration": 90.5, "power": {"value": 60, "units": "%ftp"}}]}, "whole number of seconds"),
    ({"steps": [{"reps": 0, "steps": [{"duration": 60, "power": {"value": 60, "units": "%ftp"}}]}]}, "reps must be a positive integer"),
    ({"steps": [{"text": "Recover 2m easy", "duration": 90, "power": {"value": 50, "units": "%ftp"}}]}, "duration such as 2m"),
    ({"steps": [{"text": "Sprint\n- 20m 150%", "duration": 15, "power": {"value": 150, "units": "%ftp"}}]}, "line break"),
    ({"steps": [{"text": "3x through the hill"}, {"duration": 60, "power": {"value": 60, "units": "%ftp"}}]}, "repeat count"),
    ({"steps": [{"duration": 600, "hr": {"value": 80, "units": "%ftp"}}]}, "hr target cannot use units '%ftp'"),
    ({"steps": [{"duration": 600, "cadence": {"value": 90, "units": "w"}}]}, "cadence target cannot use units 'w'"),
    ({"steps": [{"duration": 600, "power": {"value": 85, "units": "%hr"}}]}, "power target cannot use units '%hr'"),
    ({"steps": [{"duration": 600, "power": {"value": 85, "start": 80, "end": 90, "units": "%ftp"}}]}, "both a value and a start/end range"),
    ({"steps": [{"duration": 600, "pace": {"value": 1.75, "units": "SECS_100M"}}]}, "implausible"),
]


@pytest.mark.parametrize("doc,problem", BAD_DOCS)
def test_invalid_workouts_are_not_written(monkeypatch, doc, problem):
    calls = _router(monkeypatch)
    result = asyncio.run(add_or_update_event(name="Intervals", workout_type="Ride", workout_doc=WorkoutDoc.from_dict(doc), start_date="2026-10-12"))
    assert result.startswith("Error: workout_doc is not valid") and problem in result
    assert not _writes(calls)
    assert any(problem in e for e in validate_workout_doc(doc, "Ride")["errors"])


def test_validate_workout_reports_open_ended_steps_as_errors():
    doc = {"steps": [{"text": "Spin until ready", "until_lap_press": True, "power": {"value": 55, "units": "%ftp"}}]}
    assert "ERRORS" in asyncio.run(validate_workout(doc, "Ride", check_calendar=False))


def test_repeat_block_warmup_is_not_counted():
    doc = {"steps": [{"reps": 2, "warmup": True, "steps": [{"duration": 60, "power": {"value": 60, "units": "%ftp"}}]}]}
    result = validate_workout_doc(doc, "Ride")
    assert result["totals"]["has_warmup"] is False and any("ignored" in w for w in result["warnings"])


@pytest.mark.parametrize("label", ["Sprint", "Main set", "Recovery", "VO2max", "Easy spin", "Over-unders", "steady"])
def test_plain_labels_pass(label):
    assert label_problem(label) is None


def test_label_line_breaks_are_never_rendered():
    assert "\n" not in str(Step.from_dict({"text": "a\nb", "duration": 60})).strip()


def test_negative_amounts_are_refused(monkeypatch):
    calls = _router(monkeypatch)
    assert "must not be negative" in asyncio.run(add_or_update_event(name="Ride", workout_type="Ride", moving_time=-3600))
    assert "must not be negative" in asyncio.run(add_or_update_event(event_id="5", distance=-5))
    assert not _writes(calls)


@pytest.mark.parametrize(
    "doc,problem",
    [
        ({"steps": "- 10m 70%"}, "steps: Input should be a valid list"),
        ({"steps": [{"duration": 90.5}]}, "fractional part"),
        ({"steps": [{"duration": 600, "power": {"value": "abc", "units": "%ftp"}}]}, "valid number"),
        ({"steps": [{"duration": 600, "power": {"value": 70, "units": "%ftp"}, "text": "Z2"}]}, "zone such as Z2"),
    ],
)
def test_bulk_uses_the_same_type_checks_and_validation(monkeypatch, doc, problem):
    calls = _router(monkeypatch)
    result = json.loads(asyncio.run(add_events_bulk([{"name": "A", "start_date": "2026-10-12", "workout_type": "Ride", "workout_doc": doc}])))
    assert result["created_count"] == 0 and problem in result["errors"][0]["error"]
    assert not calls


def test_bulk_float_durations_are_whole_seconds(monkeypatch):
    calls = _router(monkeypatch, lambda url, method, p, d: [{"id": 1}])
    asyncio.run(add_events_bulk([{"name": "A", "start_date": "2026-10-12", "workout_type": "Ride",
                                  "workout_doc": {"steps": [{"duration": 4000.0, "power": {"value": "70", "units": "%ftp"}}]}}]))
    assert "- 1h6m40s 70% ftp" in _writes(calls)[0]["data"][0]["description"]


# ------------------------------------------------------------------ WRT-9 / WRT-16 / API-3
SETTINGS = [{"id": 11, "types": ["Run"], "threshold_pace": 3.7, "pace_units": "MINS_KM", "lthr": 170, "max_hr": 190}]


def _settings_router(monkeypatch) -> list[dict]:
    athlete_module._SPORT_SETTINGS_CACHE.clear()  # pylint: disable=protected-access
    athlete_module._ATHLETE_CACHE.clear()  # pylint: disable=protected-access

    def responder(url, method, _params, data):
        if method == "PUT":
            return {"id": 11, **(data or {})}
        if url.endswith("/sport-settings"):
            return SETTINGS
        return {"id": "i1", "timezone": "Europe/Vienna", "sportSettings": SETTINGS}

    return _router(monkeypatch, responder)


@pytest.mark.parametrize(
    "value,expected",
    [("4:30/km", 3.7037), ("4:30", 3.7037), ("7:15/mi", 3.6997), ("1:45/100m", 0.9524), ("4.17 m/s", 4.17)],
)
def test_threshold_pace_needs_a_unit(value, expected):
    assert parse_threshold_pace(value, "MINS_KM") == pytest.approx(expected, abs=1e-4)


@pytest.mark.parametrize("value", [4.5, "4.5", "fast"])
def test_threshold_pace_bare_numbers_are_refused(value):
    assert str(parse_threshold_pace(value, "MINS_KM")).startswith("Error: threshold_pace")


def test_sport_settings_update_sends_mps_and_no_zone_recalculation(monkeypatch):
    calls = _settings_router(monkeypatch)
    result = asyncio.run(update_sport_settings("Run", threshold_pace="4:00/km"))
    put = _writes(calls)[0]
    assert put["params"] == {"recalcHrZones": "false"} and put["data"] == {"threshold_pace": 4.1667}
    assert "3.70 m/s (4:30/km) -> 4.17 m/s (4:00/km)" in result
    assert "ambiguous" in asyncio.run(update_sport_settings("Run", threshold_pace=4.5))


def test_profile_is_refetched_after_a_sport_settings_update(monkeypatch):
    calls = _settings_router(monkeypatch)
    asyncio.run(athlete_module.get_athlete_profile())
    asyncio.run(update_sport_settings("Run", lthr=172))
    asyncio.run(athlete_module.get_athlete_profile())
    assert [c["url"] for c in calls if c["url"] == "/athlete/i1"] == ["/athlete/i1", "/athlete/i1"]


# ------------------------------------------------------------------ WRT-10
def test_bulk_created_count_counts_returned_events(monkeypatch):
    _router(monkeypatch, lambda url, method, p, d: [{"id": 9, "name": "A"}])
    result = json.loads(asyncio.run(add_events_bulk([
        {"name": "A", "start_date": "2026-10-12", "workout_type": "Run"},
        {"name": "B", "start_date": "2026-10-13", "workout_type": "Run"},
    ])))
    assert result["created_count"] == 1 and result["sent_count"] == 2 and result["errors"]


def test_bulk_limits_entries_names_and_colours(monkeypatch):
    calls = _router(monkeypatch)
    too_many = [{"name": "A", "start_date": "2026-10-12", "workout_type": "Run"}] * 101
    assert "at most 100" in asyncio.run(add_events_bulk(too_many))
    result = json.loads(asyncio.run(add_events_bulk([
        {"name": "   ", "start_date": "2026-10-12", "workout_type": "Run"},
        {"name": "N", "start_date": "2026-10-12", "category": "NOTE", "description": "x", "color": 7},
    ])))
    assert [e["index"] for e in result["errors"]] == [0, 1] and not calls


# ------------------------------------------------------------------ WRT-11
def test_overwriting_write_tools_are_marked_destructive_for_clients():
    from intervals_mcp_server import mcp_instance  # pylint: disable=import-outside-toplevel

    assert OVERWRITE_ANNOTATIONS["destructiveHint"] is True and PERMISSION_ANNOTATIONS["write"]["destructiveHint"] is False
    original = mcp_instance.get_config().permissions
    try:
        mcp_instance.get_config().permissions = frozenset({"read", "write"})

        @mcp_instance.tool("write", overwrites=True)
        async def review_dummy_overwrite() -> str:
            """Dummy."""
            return "ok"

        registered = mcp_instance.mcp._tool_manager.get_tool("review_dummy_overwrite")  # pylint: disable=protected-access
        assert registered is not None and registered.annotations.destructiveHint is True
        assert asyncio.run(review_dummy_overwrite()) == "ok"
    finally:
        mcp_instance.get_config().permissions = original
        mcp_instance.mcp._tool_manager._tools.pop("review_dummy_overwrite", None)  # pylint: disable=protected-access
    for name in ("add_or_update_event", "add_or_update_note", "update_wellness", "update_activity"):
        assert tool_permissions()[name] == "write"


# ------------------------------------------------------------------ WRT-12
def test_library_event_copies_load_and_flags_missing_steps(monkeypatch):
    workout = {"id": 5, "name": "Over-unders", "type": "Ride", "description": "Classic over-unders from Zwift.",
               "icu_training_load": 72, "joules": 600000, "workout_doc": {"steps": [{"duration": 60}]}}
    calls = _router(monkeypatch, lambda url, method, p, d: ([] if p else workout) if method == "GET" else {"id": 9})
    result = asyncio.run(add_event_from_library(workout_id="5", date="2026-10-12"))
    data = _writes(calls)[0]["data"]
    assert data["icu_training_load"] == 72 and data["joules"] == 600000
    assert "WITHOUT steps" in result


# ------------------------------------------------------------------ WRT-13
def test_sport_is_never_guessed_as_ride(monkeypatch):
    calls = _router(monkeypatch)
    assert infer_activity_type("Arrow-straight tempo") is None and infer_activity_type("Grow the base") is None
    assert infer_activity_type("Easy run") == "Run" and infer_activity_type("Brick bike run") is None
    assert "workout_type is required" in asyncio.run(add_or_update_event(name="Track session 6x800", start_date="2026-10-12"))
    assert "workout_type is required" in asyncio.run(add_or_update_event(name="Gran Fondo", category="RACE_A", start_date="2026-10-12"))
    assert "category must be one of" in asyncio.run(add_or_update_event(name="Week target", category="TARGET"))
    assert not _writes(calls)
    asyncio.run(add_or_update_event(name="Flu", category="SICK", start_date="2026-10-12"))
    assert "type" not in _writes(calls)[0]["data"]


# ------------------------------------------------------------------ WRT-14
def test_dates_must_be_zero_padded():
    with pytest.raises(ValueError):
        validate_date("2026-1-5")
    assert validate_date("2026-01-05") == "2026-01-05"


def test_moving_an_event_keeps_its_time_of_day(monkeypatch):
    calls = _router(monkeypatch, lambda url, method, p, d: {"id": 5, "start_date_local": "2026-10-10T07:30:00"} if method == "GET" else {"id": 5})
    asyncio.run(add_or_update_event(event_id="5", start_date="2026-10-12"))
    assert _writes(calls)[0]["data"] == {"start_date_local": "2026-10-12T07:30:00"}


def test_default_date_is_today_in_the_athlete_time_zone(monkeypatch):
    monkeypatch.setenv("ATHLETE_TIMEZONE", "Pacific/Kiritimati")  # UTC+14
    calls = _router(monkeypatch)
    asyncio.run(add_or_update_event(name="Easy run"))
    expected = datetime.now(ZoneInfo("Pacific/Kiritimati")).date().isoformat()
    assert _writes(calls)[0]["data"]["start_date_local"] == expected + "T00:00:00"


# ------------------------------------------------------------------ WRT-15 / WRT-17
def test_custom_item_update_merges_content_and_refuses_empty_updates(monkeypatch):
    item = {"id": 7, "name": "Field", "type": "ACTIVITY_FIELD", "content": {"code": "X", "type": "numeric", "aggregate": "AVERAGE"}}
    calls = _router(monkeypatch, lambda url, method, p, d: item if method == "GET" else {**item, **(d or {})})
    assert asyncio.run(update_custom_item(7)).startswith("Error: nothing to update")
    assert not calls
    asyncio.run(update_custom_item(7, content={"aggregate": "SUM"}))
    assert _writes(calls)[0]["data"]["content"] == {"code": "X", "type": "numeric", "aggregate": "SUM"}


def test_delete_custom_item_reads_first(monkeypatch):
    calls = _router(monkeypatch, lambda url, method, p, d: {} if method == "GET" else {})
    assert "nothing was deleted" in asyncio.run(delete_custom_item(7))
    assert not _writes(calls)


def test_blank_names_and_messages_are_refused(monkeypatch):
    calls = _router(monkeypatch)
    assert asyncio.run(add_or_update_event(event_id="5", name="")) == "Error: name must not be blank."
    assert asyncio.run(update_activity("i5", name="  ")) == "Error: name must not be blank."
    assert "must not be blank" in asyncio.run(add_activity_message("i5", "   "))
    assert asyncio.run(add_or_update_note(event_id="6", name=" ")) == "Error: name must not be blank."
    assert not calls


# ------------------------------------------------------------------ R25-5 / R25-6 / R25-7 / R25-10
@pytest.mark.parametrize("step", [
    {"text": "Einfahren Rampe", "duration": 600, "power": {"value": 60, "units": "%ftp"}},
    {"text": "Ramp warm-up", "ramp": True, "duration": 600, "power": {"start": 50, "end": 75, "units": "%ftp"}},
    {"text": "30/30s on", "duration": 30, "power": {"value": 120, "units": "%ftp"}},
    {"text": "Hike up", "duration": 3600, "pace": {"value": 1320, "units": "MINS_KM"}},
])
def test_former_false_refusals_are_accepted(step):
    assert not validate_workout_doc({"steps": [step]}, "Ride")["errors"]


def test_slow_swim_threshold_is_accepted():
    assert parse_threshold_pace("3:30/100m", "SECS_100M") == pytest.approx(0.4762, abs=1e-4)


def test_write_answers_show_validation_warnings(monkeypatch):
    _router(monkeypatch)
    doc = {"steps": [{"duration": 600, "warmup": True, "power": {"value": 60, "units": "%ftp"}},
                     {"reps": 2, "warmup": True, "steps": [{"duration": 60, "power": {"value": 90, "units": "%ftp"}}]}]}
    result = asyncio.run(add_or_update_event(name="Run", workout_type="Run", workout_doc=WorkoutDoc.from_dict(doc), start_date="2026-10-12"))
    assert result.startswith("Successfully created") and "Validation warnings:" in result
    assert "warmup/cooldown on a repeat block is ignored" in result and "power target on a Run" in result
    assert "no cool-down step" not in result  # routine hints are not repeated


def test_blank_text_on_update_never_wipes(monkeypatch):
    calls = _router(monkeypatch, lambda url, method, p, d: {"id": 7, "category": "NOTE"} if method == "GET" else {"id": 7})
    assert asyncio.run(add_or_update_note(event_id="7", description="")).startswith("Error: nothing to update")
    assert "at least one of" in asyncio.run(update_activity("i5", description=""))
    assert not _writes(calls)
    asyncio.run(add_or_update_note(event_id="7", clear_description=True))
    asyncio.run(update_activity("i5", clear_description=True))
    assert [c["data"] for c in _writes(calls)] == [{"description": ""}, {"description": ""}]


@pytest.mark.parametrize("doc,problem", [
    ({"description": "Main set\n3x", "steps": [{"duration": 60, "power": {"value": 60, "units": "%ftp"}}]}, "workout_doc.description"),
    ({"description": "- 10m 50%", "steps": [{"duration": 60, "power": {"value": 60, "units": "%ftp"}}]}, "workout_doc.description"),
    ({"steps": [{"text": "Warmup"}, {"duration": 600, "power": {"value": 60, "units": "%ftp"}}]}, "warm-up/cool-down section"),
    ({"steps": [{"text": "- extra"}, {"duration": 600, "power": {"value": 60, "units": "%ftp"}}]}, "would be read by Intervals.icu as a step"),
])
def test_description_and_comment_lines_are_checked_for_structure(doc, problem):
    assert any(problem in e for e in validate_workout_doc(doc, "Ride")["errors"])


def test_description_is_kept_apart_from_the_steps():
    text = str(WorkoutDoc.from_dict({"description": "Threshold day", "steps": [{"duration": 600, "power": {"value": 90, "units": "%ftp"}}]}))
    assert text.startswith("Threshold day\n\n- 10m 90% ftp")


# ------------------------------------------------------------------ R25-14 / R25-15 / R25-17
@pytest.mark.parametrize("text", ["Notes for today:\n- legs heavy\n- keep it easy", "- 10m 55%\n- 20m 90%"])
def test_any_description_needs_replace_workout_on_a_structured_event(monkeypatch, text):
    calls = _router(monkeypatch, lambda url, method, p, d: STRUCTURED if method == "GET" else {"id": 5})
    result = asyncio.run(add_or_update_event(event_id="5", description=text))
    assert "holds a structured workout" in result and not _writes(calls)
    asyncio.run(add_or_update_event(event_id="5", description=text, replace_workout=True))
    assert _writes(calls) == [{"url": "/athlete/i1/events/5", "method": "PUT", "params": None, "data": {"description": text}}]


def test_bullet_notes_are_not_workout_steps():
    from intervals_mcp_server.utils.workout_validation import has_step_lines  # pylint: disable=import-outside-toplevel

    assert not has_step_lines("Notes for today:\n- legs heavy\n- keep it easy")
    assert has_step_lines("Warmup\n- 10m 55%") and has_step_lines("- 400mtr Z4")


def test_clear_flag_and_new_text_together_are_refused(monkeypatch):
    calls = _router(monkeypatch, lambda url, method, p, d: {"id": 7, "category": "NOTE"} if method == "GET" else {"id": 7})
    assert "not both" in asyncio.run(add_or_update_note(event_id="7", description="new", clear_description=True))
    assert "not both" in asyncio.run(update_activity("i5", description="new", clear_description=True))
    from intervals_mcp_server.tools.wellness import update_wellness  # pylint: disable=import-outside-toplevel

    assert "not both" in asyncio.run(update_wellness(date="2026-10-09", comments="new", clear_comments=True))
    assert not _writes(calls)


def test_range_delete_counts_unique_confirmed_ids(monkeypatch):
    _router(monkeypatch, _calendar)
    payload = json.loads(asyncio.run(delete_events_by_date_range("2026-10-12", "2026-10-13", dry_run=False, confirm_ids="1,1,1")))
    assert payload["message"].startswith("Deleted 1 of 1 confirmed")


@pytest.mark.parametrize("label", ["Stay in Z2.", "Z2/Z3", "Hold Z3:", "2m.", "Push 4:30.", "Z2+", "2-3m", "(Z2)"])
def test_tokens_with_punctuation_are_still_refused(label):
    assert label_problem(label) is not None


@pytest.mark.parametrize("label", ["30/30s on", "Einfahren Rampe", "Sprint!", "Over-unders", "VO2max"])
def test_plain_labels_with_punctuation_pass(label):
    assert label_problem(label) is None
