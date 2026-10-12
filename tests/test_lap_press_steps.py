"""Lap-press steps ("until lap press"): serialisation into Intervals.icu's native "Press lap" marker,
validation, read-back and the regression of the earlier failure (a lap-press flag that was
dropped, so the device ended the step after its time).

The fixture tests/ressources/lap_press_parser_cases.json holds what Intervals.icu's own workout
parser (cut unchanged out of the public web client and run in V8) made of each workout text, and
the FIT duration types of the documented conversion endpoint (download-workout.fit). Every test
runs offline against mocked requests; nothing reaches Intervals.icu.
"""

import asyncio
import json
import os
import pathlib
import sys

import pytest
from pydantic import TypeAdapter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

# pylint: disable=wrong-import-position,missing-function-docstring
from intervals_mcp_server.tools.events import add_events_bulk, add_or_update_event  # noqa: E402
from intervals_mcp_server.tools.workout_check import preview_workout, validate_workout  # noqa: E402
from intervals_mcp_server.utils.formatting import format_event_details  # noqa: E402
from intervals_mcp_server.utils.types import Step, WorkoutDoc, has_press_lap  # noqa: E402
from intervals_mcp_server.utils.workout_validation import validate_workout_doc, workout_text_for_write  # noqa: E402

FIXTURE = json.loads((pathlib.Path(__file__).parent / "ressources" / "lap_press_parser_cases.json").read_text(encoding="utf-8"))
CASES = {case["case"]: case for case in FIXTURE["cases"]}
POC = FIXTURE["poc"]
ADAPTER = TypeAdapter(WorkoutDoc)
MODULES = ("intervals_mcp_server.api.client", "intervals_mcp_server.tools.events")

LAP_STEP = {"text": "Bis Anstieg", "duration": 1200, "until_lap_press": True, "power": {"start": 60, "end": 70, "units": "%ftp"}}
WORK_STEP = {"text": "Anstieg", "duration": 600, "power": {"start": 240, "end": 250, "units": "w"}}


def _steps_of(parsed_steps):
    """Leaf steps of a parsed workout, each step of a repeat block once."""
    for step in parsed_steps:
        if step.get("steps"):
            yield from step["steps"]
        else:
            yield step


def _step_lines(text):
    return [line for line in text.splitlines() if line.strip().startswith("-")]


def _router(monkeypatch, stored_steps=None, stored_description=None):
    """Mock Intervals.icu: empty calendar, POST returns id 900, the read-back returns *stored_steps*."""
    calls = []

    async def fake_request(url=None, api_key=None, params=None, method="GET", data=None):  # pylint: disable=unused-argument
        calls.append({"url": url, "method": method, "params": params, "data": data})
        if method == "POST" and url.endswith("/events/bulk"):
            return [{"id": 900 + i, **entry} for i, entry in enumerate(data)]
        if method == "POST":
            return {"id": 900, **data}
        if method == "GET" and url.endswith("/events/900"):
            return {
                "id": 900, "category": "WORKOUT", "type": "Ride", "name": "Route aware",
                "start_date_local": "2026-11-21T00:00:00", "moving_time": 1800,
                "description": stored_description, "workout_doc": {"steps": stored_steps or []},
            }
        if method == "GET" and url.endswith("/events"):
            return [{"id": 900, "category": "WORKOUT", "type": "Ride", "name": "Route aware",
                     "start_date_local": "2026-11-21T00:00:00", "moving_time": 1800,
                     "workout_doc": {"steps": stored_steps or []}}] if params and params.get("category") else []
        return []

    for module in MODULES:
        monkeypatch.setattr(f"{module}.make_intervals_request", fake_request)
    return calls


def _writes(calls):
    return [c for c in calls if c["method"] != "GET"]


# ------------------------------------------------------------------ the native marker
@pytest.mark.parametrize("name", sorted(CASES))
def test_press_lap_detection_matches_the_intervals_parser(name):
    """has_press_lap reads every step line as Intervals.icu's parser did (until_lap_press or not)."""
    case = CASES[name]
    lines = _step_lines(case["text"])
    parsed = list(_steps_of(case["parsed_steps"]))
    timed_lines = [line for line in lines if any(ch.isdigit() for ch in line)]
    if not parsed:  # "- Press lap Erholung 45-55%": Intervals.icu drops a step without duration/distance
        assert name == "M12_lap_without_duration"
        return
    assert len(timed_lines) == len(parsed)
    assert [has_press_lap(line) for line in timed_lines] == [bool(step.get("until_lap_press")) for step in parsed]


@pytest.mark.parametrize(
    "text,expected",
    [("Press lap", True), ("press LAP when ready", True), ("Erholung 5m 45-55% hit lap", True), ("press, lap", True),
     ("LAP druecken", False), ("Bis Anstieg - LAP wenn oben", False), ("pressed lap", False), ("Press-lap", False), (None, False)],
)
def test_has_press_lap_rule(text, expected):
    assert has_press_lap(text) is expected


def test_lap_step_is_written_with_the_native_marker():
    text = str(WorkoutDoc.from_dict({"steps": [LAP_STEP, WORK_STEP]}))
    assert text.splitlines()[0] == "- Press lap Bis Anstieg 20m 60%-70% ftp "
    assert text == CASES["R4_new_serialiser_lap"]["text"]
    parsed = CASES["R4_new_serialiser_lap"]["parsed_steps"]
    assert parsed[0]["until_lap_press"] is True and parsed[0]["duration"] == 1200 and "until_lap_press" not in parsed[1]


# ------------------------------------------------------------------ regression: the earlier failure
def test_regression_lap_flag_is_no_longer_dropped():
    """Before: {"until_lap_press": true, "duration": 1200} was written as "- Bis Anstieg 20m ..." (main)
    or "- 20m ... Bis Anstieg" (80ef553); Intervals.icu stored a timed step and the Garmin FIT
    step ended after 20 minutes. Now the line carries the marker and validation counts the step."""
    for old in ("R1_80ef553_lap_with_duration", "R3_main_lap_with_duration"):
        assert not any(step.get("until_lap_press") for step in CASES[old]["parsed_steps"])
    doc = ADAPTER.validate_python({"steps": [LAP_STEP, WORK_STEP]})  # what FastMCP hands the tool
    written = workout_text_for_write(doc, "Ride")
    assert written.problem is None and has_press_lap(_step_lines(written.text)[0])
    assert written.text != CASES["R3_main_lap_with_duration"]["text"]
    totals = validate_workout_doc(doc, "Ride")["totals"]
    assert totals["lap_press_steps"] == 1 and totals["lap_press_estimate_s"] == 1200 and totals["duration_s"] == 1800


@pytest.mark.parametrize("alias", ["press_lap", "lap_press", "end_condition", "lap", "open_ended"])
def test_unknown_lap_fields_are_refused_not_dropped(monkeypatch, alias):
    """A "press_lap" property was dropped silently by the parameter validation; it is refused now."""
    calls = _router(monkeypatch)
    raw = {"steps": [{"text": "Bis Anstieg", "duration": 1200, alias: True, "power": {"value": 60, "units": "%ftp"}}, WORK_STEP]}
    doc = ADAPTER.validate_python(raw)
    result = asyncio.run(add_or_update_event(name="Route aware", workout_type="Ride", workout_doc=doc, start_date="2026-11-21"))
    assert result.startswith("Error: workout_doc is not valid") and f"unknown step field '{alias}'" in result
    assert "until_lap_press: true" in result and not _writes(calls)
    report = asyncio.run(validate_workout(raw, "Ride", check_calendar=False))
    assert "ERRORS" in report and alias in report


def test_unknown_fields_in_bulk_entries_are_refused(monkeypatch):
    calls = _router(monkeypatch)
    entry = {"name": "Route aware", "start_date": "2026-11-21", "workout_type": "Ride",
             "workout_doc": {"steps": [{"duration": 600, "press_lap": True, "power": {"value": 60, "units": "%ftp"}}]}}
    result = asyncio.run(add_events_bulk([entry]))
    assert "press_lap" in result and not _writes(calls)


def test_lap_step_without_planning_estimate_is_refused():
    doc = {"steps": [{"text": "Bis Anstieg", "until_lap_press": True, "power": {"value": 60, "units": "%ftp"}}, WORK_STEP]}
    errors = validate_workout_doc(doc, "Ride")["errors"]
    assert any("open-ended step (until lap press) needs a duration or distance as planning estimate" in e for e in errors)
    assert not CASES["M12_lap_without_duration"]["parsed_steps"]  # Intervals.icu would drop it


def test_lap_flag_must_be_boolean_and_not_on_repeat_blocks():
    doc = {"steps": [{"duration": 60, "until_lap_press": "yes"},
                     {"reps": 2, "until_lap_press": True, "steps": [{"duration": 60, "power": {"value": 50, "units": "%ftp"}}]}]}
    errors = validate_workout_doc(doc, "Ride")["errors"]
    assert any("until_lap_press must be true or false" in e for e in errors)
    assert any("repeat block cannot have its own until_lap_press" in e for e in errors)


# ------------------------------------------------------------------ labels
@pytest.mark.parametrize("label", ["Press lap when ready", "Erholung hit lap", "press, lap"])
def test_press_lap_in_a_label_needs_the_flag(label):
    doc = {"steps": [{"text": label, "duration": 300, "power": {"value": 50, "units": "%ftp"}}]}
    errors = validate_workout_doc(doc, "Ride")["errors"]
    assert any("set until_lap_press: true" in e for e in errors)


def test_press_lap_label_with_the_flag_is_written_once():
    step = {"text": "Press lap Erholung", "duration": 300, "until_lap_press": True, "power": {"value": 50, "units": "%ftp"}}
    assert validate_workout_doc({"steps": [step]}, "Ride")["ok"]
    assert str(Step.from_dict(step)).strip() == "- Press lap Erholung 5m 50% ftp"


def test_german_lap_hint_stays_a_plain_label():
    """"LAP druecken" in a label is only text: Intervals.icu keeps the step timed (fixture M14)."""
    doc = {"steps": [{"text": "Bis Anstieg LAP druecken", "duration": 600, "power": {"start": 55, "end": 65, "units": "%ftp"}}]}
    assert validate_workout_doc(doc, "Ride")["ok"]
    assert not CASES["M14_german_LAP_text_only"]["parsed_steps"][0].get("until_lap_press")


# ------------------------------------------------------------------ repeats, distance, preview
def test_lap_steps_inside_repeat_blocks():
    doc = POC["outdoor_threshold_route_aware_repeat"]["mcp_workout_doc"]
    result = validate_workout_doc(doc, "Ride")
    assert result["ok"] and result["totals"]["lap_press_steps"] == 3 and result["totals"]["lap_press_estimate_s"] == 3 * 450
    assert "3x Anstiege \n- Press lap Locker bis Anstieg 7m30s 110W-155W \n- Schwelle 10m 240W-250W" in result["preview"]
    parsed = POC["outdoor_threshold_route_aware_repeat"]["parsed"]["steps"][1]
    assert parsed["reps"] == 3 and parsed["steps"][0]["until_lap_press"] is True
    assert POC["outdoor_threshold_route_aware_repeat"]["fit_duration_types"] == ["time", "open", "time", "repeat_until_steps_cmplt", "time"]


def test_lap_step_with_a_distance_estimate():
    step = {"text": "Anfahrt", "distance": 3000, "until_lap_press": True, "power": {"start": 55, "end": 65, "units": "%ftp"}}
    result = validate_workout_doc({"steps": [step]}, "Ride")
    assert result["ok"] and result["totals"]["lap_press_estimate_m"] == 3000
    assert str(Step.from_dict(step)).strip() == "- Press lap Anfahrt 3km 55%-65% ftp"
    assert CASES["M13_lap_distance"]["parsed_steps"][0]["until_lap_press"] is True


def test_preview_shows_lap_steps_and_their_estimate():
    report = asyncio.run(preview_workout(POC["outdoor_threshold_route_aware"]["mcp_workout_doc"], "Ride"))
    assert "Until lap press: 3 step(s) end only on the lap button" in report and "planning estimate 20:00" in report
    assert "planned duration 1:17:00" in report and "- Press lap Locker bis Anstieg 10m 130W-155W" in report


def test_stored_lap_step_is_shown_with_one_marker():
    event = {"id": 1, "name": "Route aware", "workout_doc": {"steps": POC["outdoor_threshold_route_aware"]["parsed"]["steps"]}}
    details = format_event_details(event)
    assert "- Press lap Erholung bis Anstieg 5m 110W-135W" in details and "Press lap Press lap" not in details


def test_schema_offers_until_lap_press():
    step_schema = ADAPTER.json_schema()["$defs"]["Step"]
    assert "until_lap_press" in step_schema["properties"] and "lap button" in step_schema["description"]


# ------------------------------------------------------------------ PoC: Outdoor Threshold - Route Aware
@pytest.mark.parametrize("name", sorted(POC))
def test_poc_renders_the_verified_native_text(name):
    """The MCP input renders to exactly the text whose parse (Intervals.icu parser) and FIT export were checked."""
    poc = POC[name]
    written = workout_text_for_write(ADAPTER.validate_python(poc["mcp_workout_doc"]), "Ride")
    assert written.problem is None and written.text == poc["native_text"]
    sent = [bool(s.get("until_lap_press")) for s in _steps_of(poc["mcp_workout_doc"]["steps"])]
    stored = [bool(s.get("until_lap_press")) for s in _steps_of(poc["parsed"]["steps"])]
    assert sent == stored and sum(stored) in (1, 3)
    fit_open = [t for t in poc["fit_duration_types"] if t == "open"]
    assert len(fit_open) == sum(stored)


def test_poc_planned_duration_counts_the_estimates():
    poc = POC["outdoor_threshold_route_aware"]
    totals = validate_workout_doc(poc["mcp_workout_doc"], "Ride")["totals"]
    assert totals["duration_s"] == 4620 == poc["parsed"]["duration"]  # 77 min incl. 20 min of lap-press estimates
    assert poc["fit_duration_types"] == ["time", "open", "time", "open", "time", "open", "time", "time"]


# ------------------------------------------------------------------ write + read-back
def test_write_sends_the_marker_and_read_back_confirms_it(monkeypatch):
    stored = CASES["R4_new_serialiser_lap"]["parsed_steps"]
    calls = _router(monkeypatch, stored_steps=stored)
    doc = ADAPTER.validate_python({"steps": [LAP_STEP, WORK_STEP]})
    result = asyncio.run(add_or_update_event(name="Route aware", workout_type="Ride", workout_doc=doc, start_date="2026-11-21"))
    post = _writes(calls)[0]["data"]
    assert post["description"].startswith("- Press lap Bis Anstieg 20m 60%-70% ftp")
    assert "1 until lap press" in result and "Parse warnings: none" in result


def test_read_back_warns_when_the_lap_flag_was_not_stored(monkeypatch):
    stored = CASES["R3_main_lap_with_duration"]["parsed_steps"]  # Intervals.icu stored a timed step
    _router(monkeypatch, stored_steps=stored)
    doc = ADAPTER.validate_python({"steps": [LAP_STEP, WORK_STEP]})
    result = asyncio.run(add_or_update_event(name="Route aware", workout_type="Ride", workout_doc=doc, start_date="2026-11-21"))
    assert "step 1: sent as until lap press, stored as a timed step" in result


def test_read_back_of_native_text_compares_the_lap_steps(monkeypatch):
    text = "- Press lap Bis Anstieg 20m 60%-70% ftp\n- Anstieg 10m 240W-250W"
    _router(monkeypatch, stored_steps=CASES["R3_main_lap_with_duration"]["parsed_steps"])
    result = asyncio.run(add_or_update_event(name="Route aware", workout_type="Ride", description=text, start_date="2026-11-21"))
    assert "0 until-lap-press steps stored, 1 sent" in result


def test_dry_run_shows_the_marker(monkeypatch):
    calls = _router(monkeypatch)
    doc = ADAPTER.validate_python(POC["outdoor_threshold_route_aware"]["mcp_workout_doc"])
    result = asyncio.run(add_or_update_event(name="Outdoor Threshold - Route Aware", workout_type="Ride", workout_doc=doc,
                                             start_date="2026-11-21", dry_run=True))
    assert "Press lap Locker bis Anstieg 10m 130W-155W" in result and not _writes(calls)
