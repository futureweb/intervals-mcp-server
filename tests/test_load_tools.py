"""
Tool-level tests for the training load and intensity tools: get_training_load,
get_load_projection, get_intensity_distribution, get_durability and get_coach_context, plus
the training_load_review prompt. A synthetic athlete trains on a fixed weekly pattern (ride
Tuesday and Saturday, strength without load Wednesday, run Thursday, hike Sunday); every API
call is routed to generated fixtures, "today" is fixed.
"""

import asyncio
import json
import os
import pathlib
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.mcp_instance import tool_permissions  # pylint: disable=wrong-import-position
from intervals_mcp_server.server import (  # pylint: disable=wrong-import-position
    get_coach_context,
    get_durability,
    get_intensity_distribution,
    get_load_projection,
    get_training_load,
)
from intervals_mcp_server.tools.status import training_load_review  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.load_metrics import day_range, model_step  # pylint: disable=wrong-import-position
from tests.test_coaching_tools import _install_router  # pylint: disable=wrong-import-position

TODAY = date(2026, 10, 9)  # a Friday without training
FIRST = date(2026, 8, 1)
ERROR = {"error": True, "message": "boom"}


def _power(*secs):
    return [{"id": f"Z{i + 1}", "secs": s} for i, s in enumerate(secs)] + [{"id": "SS", "secs": 1234}]


def _activity(day, sport, load, secs, **extra):
    activity = {"id": f"i{day.strftime('%m%d')}{sport[:2]}", "name": f"{sport} {day.isoformat()}", "type": sport,
                "start_date_local": f"{day.isoformat()}T09:00:00", "icu_training_load": load, "moving_time": secs,
                "elapsed_time": secs + 100}
    activity.update(extra)
    return activity


def _build_activities():
    acts = []
    for day in day_range(FIRST, TODAY):
        weekday = day.weekday()
        if weekday == 1:
            acts.append(_activity(
                day, "Ride", 90, 5400, icu_zone_times=_power(1500, 2400, 600, 700, 150, 50, 0),
                icu_hr_zone_times=[3000, 1500, 600, 300, 0, 0, 0], icu_intensity=88.0, decoupling=3.0,
                icu_variability_index=1.05, average_temp=18.0, average_heartrate=140, icu_efficiency_factor=1.45,
                gear={"id": "b1"}, EPOC=60.0,
            ))
            if day == date(2026, 10, 6):
                acts[-1].update(trainer=True, average_temp=None)
        elif weekday == 2:
            acts.append(_activity(day, "WeightTraining", None, 2700))
        elif weekday == 3:
            acts.append(_activity(
                day, "Run", 50, 3000, icu_hr_zone_times=[2000, 700, 200, 100, 0, 0, 0],
                pace_zone_times=[1500, 1200, 300, 0, 0, 0, 0], icu_intensity=78.0, decoupling=2.0,
                average_heartrate=145, EPOC=40.0,
            ))
        elif weekday == 5:
            acts.append(_activity(
                day, "Ride", 150, 10800, icu_zone_times=_power(3000, 6050, 1200, 400, 100, 50, 0),
                icu_hr_zone_times=[7000, 3000, 800, 0, 0, 0, 0], icu_intensity=75.0, decoupling=6.5,
                icu_variability_index=1.08, average_temp=22.0, average_heartrate=135, icu_efficiency_factor=1.40,
                gear={"id": "b2"}, EPOC=120.0,
            ))
            if day == date(2026, 9, 19):
                acts[-1].update(elapsed_time=16000)
        elif weekday == 6:
            acts.append(_activity(day, "Hike", 60, 7200, icu_hr_zone_times=[6000, 1000, 200, 0, 0, 0, 0], icu_intensity=55.0))
    return acts


ACTIVITIES = _build_activities()
PLANNED = {date(2026, 10, 9): 100.0, date(2026, 10, 11): 120.0, date(2026, 10, 15): 80.0}
EVENTS = [
    {"id": 1, "category": "WORKOUT", "type": "Ride", "name": "Threshold", "start_date_local": "2026-10-09T00:00:00",
     "icu_training_load": 100, "moving_time": 3600},
    {"id": 2, "category": "WORKOUT", "type": "Ride", "name": "Long ride", "start_date_local": "2026-10-11T00:00:00",
     "icu_training_load": 120, "moving_time": 10800},
    {"id": 3, "category": "WORKOUT", "type": "WeightTraining", "name": "Strength", "start_date_local": "2026-10-13T00:00:00",
     "icu_training_load": None, "moving_time": 2700},
    {"id": 4, "category": "WORKOUT", "type": "Ride", "name": "Tempo", "start_date_local": "2026-10-15T00:00:00",
     "icu_training_load": 80, "moving_time": 5400},
    {"id": 5, "category": "WORKOUT", "type": "Run", "name": "Done run", "start_date_local": "2026-10-09T00:00:00",
     "icu_training_load": 40, "paired_activity_id": "i999"},
    {"id": 6, "category": "RACE_A", "type": "Ride", "name": "Gran Fondo", "start_date_local": "2026-10-18T00:00:00"},
    {"id": 7, "category": "NOTE", "name": "Holiday", "start_date_local": "2026-10-12T00:00:00"},
]


def _build_wellness():
    """Wellness generated with the exponential model; today and later include the planned load like Intervals.icu."""
    loads = {}
    for activity in ACTIVITIES:
        day = date.fromisoformat(activity["start_date_local"][:10])
        loads[day] = loads.get(day, 0.0) + (activity["icu_training_load"] or 0.0)
    loads.update(PLANNED)
    ctl = atl = 45.0
    records = []
    for index, day in enumerate(day_range(date(2026, 7, 1), TODAY + timedelta(days=40))):
        load = loads.get(day, 0.0)
        ctl, atl = model_step(ctl, load, 42), model_step(atl, load, 7)
        record = {"id": day.isoformat(), "ctl": ctl, "atl": atl, "ctlLoad": load, "atlLoad": load}
        if day <= TODAY:
            record.update({"hrv": (35 if day > TODAY - timedelta(days=7) else 40 + index % 5), "restingHR": 50,
                           "sleepSecs": 27000})
        records.append(record)
    return records


WELLNESS = _build_wellness()


def _setup(monkeypatch, overrides=None, calls=None):
    routes = {"/activities": ACTIVITIES, "/wellness": WELLNESS, "/events": EVENTS}
    routes.update(overrides or {})
    _install_router(monkeypatch, routes, calls)
    monkeypatch.setattr("intervals_mcp_server.tools.training_load.current_day", lambda: TODAY)


# ------------------------------------------------------------------ get_training_load
def test_get_training_load_text_default_end(monkeypatch):
    """Windows end yesterday without an activity today; rest days and strength without load are handled."""
    calls = []
    _setup(monkeypatch, calls=calls)
    result = asyncio.run(get_training_load())
    assert "windows ending 2026-10-08" in result
    assert "no activity is recorded today (2026-10-09) yet" in result
    assert "Acute 7 d (2026-10-02 to 2026-10-08): load 350 in 5 sessions, 1 without load on 5 days (3 days with load 0), 50.0/day" in result
    assert "Chronic 28 d (2026-09-11 to 2026-10-08): load 1400 in 20 sessions, 4 without load on 20 days (12 days with load 0), 50.0/day = 350/week" in result
    assert "Acute:chronic ratio 1.00 (daily means, coupled): inside the commonly cited range 0.8-1.3 [1]" in result
    assert "monotony 0.88" in result and "strain 309" in result and "100 % of the chronic weekly mean" in result
    assert "source recomputed_without_planned" not in result and "recomputed_without_planned" in result
    assert "cycling: acute 240 / chronic 960 (8 sessions), ratio 1.00, 7-day monotony n/a, fewer than 3 days with load (2 of 7 days with load)" in result
    assert "weighttraining: acute 0 / chronic 0 (4 sessions), ratio n/a" in result
    assert "Primary sport (cycling) monotony alone: n/a" in result
    assert "2026-W41 (2026-10-05 to 2026-10-08, partial 4 d)" in result
    assert "2026-W40 (2026-09-28 to 2026-10-04): load 350 (cycling 240, walking 60, running 50, weighttraining n/a), 5 sessions, 8.1 h, 2 rest days" in result
    assert "100 % of the trailing weekly mean" in result
    # Run has no field list in its sport settings: its real EPOC values count (phase 5), reported separately.
    assert (
        "Device load EPOC [EPOC] (own scale, not comparable with or added to the Intervals.icu load): acute 220 ml/kg (n 3), "
        "chronic 880 ml/kg (n 12); activities without a value are not counted; incl. 4 value(s) from sports without field "
        "assignment (Run)"
    ) in result
    assert "Coverage: 4 session(s) in the chronic window have no Intervals.icu load" in result
    assert "[1] Gabbett 2016" in result and "no assessment is made" in result
    activity_call = next(c for c in calls if c[0].endswith("/activities"))
    assert "icu_training_load" in activity_call[1]["fields"] and ",EPOC" in activity_call[1]["fields"]


def test_get_training_load_json_full_and_explicit_end(monkeypatch):
    """Explicit end date: no shift; JSON carries totals, sports, weeks, device loads and (full) daily loads."""
    _setup(monkeypatch)
    payload = json.loads(asyncio.run(get_training_load(end_date="2026-10-09", output_format="json", detail_level="full")))
    assert payload["load_end"] == "2026-10-09" and payload["load_end_note"] is None
    total = payload["total"]
    assert total["acwr"] == 1.0 and total["week"]["monotony"] == 0.88 and total["week"]["deload_like"] is False
    assert payload["fitness"]["source"] == "recomputed_without_planned" and payload["fitness"]["date"] == "2026-10-09"
    assert payload["sports"]["primary_sport"] == "cycling" and payload["sports"]["multi_sport"] is True
    assert len(payload["daily"]) == 28 and payload["daily"][-1] == {"date": "2026-10-09", "load": 0.0, "sessions": 0}
    epoc = payload["device_loads"][0]
    assert epoc["code"] == "EPOC" and epoc["acute"] == {"sum": 220.0, "n": 3, "from_unassigned_sports": 1, "unassigned_sports": ["Run"]}
    assert payload["references"]["acwr"]["range"] == [0.8, 1.3]
    standard = json.loads(asyncio.run(get_training_load(end_date="2026-09-30", output_format="json")))
    assert "daily" not in standard and standard["fitness"]["source"] == "intervals.icu"
    compact = asyncio.run(get_training_load(detail_level="compact"))
    assert "ISO weeks" not in compact and "Device load" not in compact and "Acute:chronic ratio" in compact
    full = asyncio.run(get_training_load(detail_level="full", weeks=1, acute_days=14, chronic_days=42))
    assert "Daily load (chronic window): " in full and "Definitions: " in full and "Acute 14 d" in full


def test_get_training_load_validation_and_errors(monkeypatch):
    """Invalid parameters and API errors are reported."""
    _setup(monkeypatch)
    assert asyncio.run(get_training_load(end_date="2026-10-20")).startswith("Error: end_date lies in the future")
    assert asyncio.run(get_training_load(acute_days=28, chronic_days=28)).startswith("Error: acute_days")
    assert asyncio.run(get_training_load(weeks=0)).startswith("Error: weeks")
    assert asyncio.run(get_training_load(detail_level="huge")).startswith("Error: detail_level")
    assert asyncio.run(get_training_load(end_date="09.10.2026")).startswith("Error: Invalid date format")
    _setup(monkeypatch, {"/activities": ERROR})
    assert asyncio.run(get_training_load()) == "Error fetching activities: boom"
    _setup(monkeypatch, {"/wellness": ERROR})
    assert asyncio.run(get_training_load()) == "Error fetching wellness data: boom"
    _setup(monkeypatch, {"/activities": [], "/wellness": []})
    empty = asyncio.run(get_training_load(end_date="2026-10-09"))
    assert "load 0 in 0 sessions" in empty and "Acute:chronic ratio n/a" in empty and "n/a (no chronic load)" in empty
    assert "Fitness: n/a (no CTL/ATL" in empty


# ------------------------------------------------------------------ get_load_projection
def test_get_load_projection_text_and_json(monkeypatch):
    """Planned loads drive the projection; missing planned loads are reported; races and Intervals.icu's projection."""
    _setup(monkeypatch)
    result = asyncio.run(get_load_projection())
    assert result.startswith("PROJECTION: load projection for athlete i1, 2026-10-09 to 2026-11-06 (assumes every planned workout")
    assert "Start (end of 2026-10-08, Intervals.icu)" in result
    assert "Today 2026-10-09: completed load 0 + planned, not yet done 100" in result
    assert "Planned workouts: 4 (3 with a planned load, sum 300); 1 without a planned load are not included" in result
    assert "Intervals.icu's own projection" in result
    assert "Races: 2026-10-18 RACE_A 'Gran Fondo'" in result
    assert "reproduces the stored CTL within 0.00 and ATL within 0.00" in result
    assert "2026-W42 (2026-10-12 to 2026-10-18): load 80 (2 planned, 1 without load)" in result
    payload = json.loads(asyncio.run(get_load_projection(end_date="2026-10-20", output_format="json")))
    assert payload["mode"] == "projection" and len(payload["days"]) == 12
    end_row = payload["days"][-1]
    assert end_row["ctl"] == payload["intervals_icu_end"]["ctl"] and end_row["atl"] == payload["intervals_icu_end"]["atl"]
    assert payload["days"][0]["load"] == 100 and payload["days"][2]["load"] == 120
    assert payload["races"][0]["form"] is not None and payload["lowest_form"]["date"] >= "2026-10-09"
    compact = asyncio.run(get_load_projection(detail_level="compact"))
    assert "Weeks (" not in compact and "Lowest projected form" in compact
    full = asyncio.run(get_load_projection(detail_level="full", ctl_days=30))
    assert "Days: 10-09 load 100" in full and "model CTL 30 d" in full


def test_get_load_projection_validation_and_missing_data(monkeypatch):
    """End date must lie ahead; time constants are checked; without CTL there is nothing to project."""
    _setup(monkeypatch)
    assert asyncio.run(get_load_projection(end_date="2026-10-09")).startswith("Error: end_date must lie after today")
    assert asyncio.run(get_load_projection(end_date="2027-12-01")).startswith("Error: end_date must lie after today")
    assert asyncio.run(get_load_projection(ctl_days=7, atl_days=7)).startswith("Error: ctl_days")
    _setup(monkeypatch, {"/wellness": []})
    assert asyncio.run(get_load_projection()).startswith("No CTL/ATL found")
    _setup(monkeypatch, {"/events": ERROR})
    assert asyncio.run(get_load_projection()) == "Error fetching events: boom"
    _setup(monkeypatch, {"/events": []})
    assert "Planned workouts: none in the calendar" in asyncio.run(get_load_projection())


def test_projection_and_coach_context_say_when_nothing_is_planned(monkeypatch):
    """Phase 5 (F): without planned workouts the header says so at every detail level, also in the coach context."""
    _setup(monkeypatch, {"/events": []})
    for level in ("compact", "standard", "full"):
        text = asyncio.run(get_load_projection(detail_level=level))
        assert text.startswith("PROJECTION WITHOUT PLANNED TRAINING (no planned workouts in the calendar): load projection")
        assert "only the decay of CTL and ATL without any training" in text.splitlines()[0]
    payload = json.loads(asyncio.run(get_load_projection(output_format="json")))
    assert payload["projection_basis"]["kind"] == "no_planned_workouts"
    context = asyncio.run(get_coach_context())
    assert "Planned 2026-10-10 to 2026-10-16: NO PLANNED WORKOUTS in the calendar" in context
    unloaded = [dict(e, icu_training_load=None) for e in EVENTS if e["category"] == "WORKOUT" and not e.get("paired_activity_id")]
    _setup(monkeypatch, {"/events": unloaded})
    text = asyncio.run(get_load_projection(detail_level="compact"))
    assert text.startswith("PROJECTION WITHOUT PLANNED LOAD (4 planned workouts, none with a planned load)")
    _setup(monkeypatch)
    assert asyncio.run(get_load_projection(detail_level="compact")).startswith("PROJECTION: ")


# ------------------------------------------------------------------ get_intensity_distribution
def test_get_intensity_distribution_text(monkeypatch):
    """Power for rides, HR for runs and hikes; strength without zones is counted, not treated as easy."""
    calls = []
    _setup(monkeypatch, calls=calls)
    result = asyncio.run(get_intensity_distribution())
    assert "2026-09-12 to 2026-10-09 (28 days, three-zone model, zone basis auto" in result
    assert "Coverage: 16 of 20 sessions with usable zones" in result and "excluded: no zone times 4" in result
    assert "cycling: " in result and "[basis power 100 %]" in result and "walking: " in result
    assert "hard sessions 4 on 4 days" in result
    assert "Drift 2026-09-12..2026-09-25 -> 2026-09-26..2026-10-09" in result
    assert "7 zones Z1-Z2 | Z3-Z4 | Z5-Z7; hr 3 zones" in result and "Treff et al. 2019" in result
    assert "power Z4 counted as moderate" in result
    high = asyncio.run(get_intensity_distribution(threshold_as="high"))
    assert "power Z4 counted as high" in high and "7 zones Z1-Z2 | Z3 | Z4-Z7" in high
    assert asyncio.run(get_intensity_distribution(threshold_as="max")).startswith("Error: threshold_as must be one of")
    assert "ISO weeks:" in result and "2026-W37 (2026-09-12 to 2026-09-13, 2 d)" in result
    assert "icu_zone_times" in calls[0][1]["fields"]
    compact = asyncio.run(get_intensity_distribution(detail_level="compact"))
    assert "ISO weeks" not in compact and "Period: " in compact
    full = asyncio.run(get_intensity_distribution(detail_level="full", start_date="2026-10-05"))
    assert "min Z1/Z2/Z3 (power, 7 zones)" in full and "hard: IF 0.88" in full
    assert "no zones (no zone times)" in full


def test_get_intensity_distribution_json_basis_and_filters(monkeypatch):
    """zone_basis forces one basis; sport_types filters; invalid input is rejected."""
    _setup(monkeypatch)
    payload = json.loads(asyncio.run(get_intensity_distribution(output_format="json")))
    total = payload["result"]["total"]
    assert set(total["basis_pct"]) == {"hr", "power"} and "sessions" not in payload["result"]
    assert sum(total["pct"]) == 100.0 and payload["mapping"]["hr"]["7"] == "Z1-Z2 | Z3-Z4 | Z5-Z7"
    hr_only = json.loads(asyncio.run(get_intensity_distribution(zone_basis="HR", output_format="json")))
    assert hr_only["result"]["total"]["basis_pct"] == {"hr": 100.0}
    runs = json.loads(asyncio.run(get_intensity_distribution(sport_types="Run", output_format="json", detail_level="full")))
    assert list(runs["result"]["by_sport"]) == ["running"] and len(runs["result"]["sessions"]) == 4
    assert runs["result"]["total"]["class"] == "Base"
    assert asyncio.run(get_intensity_distribution(zone_basis="watts")).startswith("Error: zone_basis")
    assert asyncio.run(get_intensity_distribution(start_date="2026-10-10", end_date="2026-10-01")).startswith("Error: start_date")
    assert asyncio.run(get_intensity_distribution(start_date="2024-01-01")).startswith("Error: the period")
    _setup(monkeypatch, {"/activities": []})
    assert "Period: no zone data" in asyncio.run(get_intensity_distribution())


# ------------------------------------------------------------------ get_durability
def test_get_durability_text_and_filters(monkeypatch):
    """Quality filter with reasons, median and recent comparison, EF with several bikes, environment filter."""
    _setup(monkeypatch)
    result = asyncio.run(get_durability(detail_level="full"))
    assert "2026-08-29 to 2026-10-09" in result
    assert ("Sessions considered 18, qualifying 11, excluded 7: moving time below 85 % of elapsed time (long stops) 1, "
            "moving time below the minimum 6") in result
    assert "cycling: decoupling median 3.0 % (n 11" in result and "5 above 5 %" in result
    assert "last 7 d median 4.8 % (n 2), +1.8 pp vs window: higher" in result
    assert "efficiency factor mean 1.43 (n 12)" in result and "2 different bikes/shoes" in result
    assert "running: no qualifying sessions" in result
    assert "Excluded:" in result and "(long stops)" in result
    outdoor = json.loads(asyncio.run(get_durability(environment="outdoor", output_format="json")))
    assert outdoor["decoupling"]["excluded_by_reason"]["environment"] == 1
    assert "excluded_sessions" not in outdoor["decoupling"] and outdoor["filters"]["environment"] == "outdoor"
    warm = json.loads(asyncio.run(get_durability(max_temp_c=20, min_minutes=45, output_format="json", detail_level="compact")))
    assert warm["decoupling"]["excluded_by_reason"]["heat"] == 5 and "sessions" not in warm["decoupling"]["by_sport"]["cycling"]
    assert warm["decoupling"]["by_sport"]["running"]["n"] == 6
    hikes = json.loads(asyncio.run(get_durability(sport_types="Hike", output_format="json")))
    assert list(hikes["decoupling"]["by_sport"]) == ["walking"] and hikes["decoupling"]["excluded_by_reason"] == {"no_hr": 6}
    assert asyncio.run(get_durability(environment="garage")).startswith("Error: environment")
    assert asyncio.run(get_durability(max_vi=0.9)).startswith("Error: min_minutes")


# ------------------------------------------------------------------ get_coach_context
def test_get_coach_context_text_and_json(monkeypatch):
    """One compact overview: load, fitness, intensity, recovery numbers, durability, top sessions, plan."""
    _setup(monkeypatch)
    result = asyncio.run(get_coach_context())
    assert len(result) < 3000
    assert "Coach context for athlete i1 at 2026-10-09 (load windows end 2026-10-08" in result
    assert "Load: 7 d 350 (5 sessions, 3 days with load 0) | 28 d 1400 (350/week) | ratio 1.00 (inside 0.8-1.3)" in result
    assert "Intensity 7 d: " in result and "Intensity 28 d: " in result
    assert "HRV 7-d mean 35 ms (n 7) vs 42-d" in result and "resting HR 7-d mean 50 bpm" in result
    assert "Durability 28 d: cycling decoupling median" in result
    assert "Top sessions 7 d: 10-03 Ride 180 min load 150" in result
    assert "Planned 2026-10-10 to 2026-10-16: 3 workouts, load 200 (1 without planned load)" in result
    assert "next race 2026-10-18 RACE_A 'Gran Fondo'" in result
    assert "wellness days with HRV 28" in result
    compact = asyncio.run(get_coach_context(detail_level="compact"))
    assert len(compact) < len(result) and "Top sessions" not in compact
    full = asyncio.run(get_coach_context(detail_level="full"))
    assert "Weeks: " in full and "References: " in full
    payload = json.loads(asyncio.run(get_coach_context(end_date="2026-10-01", output_format="json")))
    assert payload["plan"] is None and payload["load_end"] == "2026-10-01"
    assert payload["recovery"]["hrv"]["baseline_n"] > 0 and payload["coverage"]["sessions_without_load"] == 4
    assert "sessions" not in payload["durability"]["by_sport"]["cycling"]
    assert asyncio.run(get_coach_context(end_date="2026-10-10")).startswith("Error: end_date lies in the future")


def test_new_tools_are_read_only_and_prompt():
    """All new tools are in the read class; the review prompt points to them and keeps the no-diagnosis rule."""
    permissions = tool_permissions()
    for name in ("get_training_load", "get_load_projection", "get_intensity_distribution", "get_durability", "get_coach_context"):
        assert permissions[name] == "read"
    text = training_load_review("2026-10-09")
    assert "get_coach_context" in text and "end_date='2026-10-09'" in text
    assert "not a risk statement" in text and "Do not state causes" in text


def test_new_tools_through_mcp_layer(monkeypatch):
    """The registered tools accept JSON arguments via FastMCP (argument validation included)."""
    from intervals_mcp_server.mcp_instance import mcp  # pylint: disable=import-outside-toplevel

    _setup(monkeypatch)
    result = asyncio.run(mcp.call_tool("get_training_load", {"acute_days": 7, "chronic_days": 28, "detail_level": "compact"}))
    text = json.dumps(result, default=str)
    assert "Acute:chronic ratio 1.00" in text
    result = asyncio.run(mcp.call_tool("get_durability", {"max_temp_c": None, "min_minutes": 45.5}))
    assert "no temperature limit" in json.dumps(result, default=str)
