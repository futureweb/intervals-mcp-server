"""
Tests for the what-if plan simulation of get_load_projection: the pure functions in
utils.plan_simulation (scenario parsing, weekly templates, load estimates, plan weeks and
checks, the target search) and the tool on the synthetic athlete of test_load_tools
(today 2026-10-09, a RACE_A on 2026-10-18, planned workouts with and without load).
"""

import asyncio
import json
import math
import os
import pathlib
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.server import get_load_projection  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils import plan_simulation as ps  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.load_metrics import day_range, model_step, project_fitness  # pylint: disable=wrong-import-position
from tests.test_load_tools import EVENTS, TODAY, _setup  # pylint: disable=wrong-import-position

LAST = TODAY + timedelta(days=180)


def _session(day, load, minutes=None, sport="Ride", source="proposed"):
    return {"date": day, "sport": sport, "load": load, "minutes": minutes, "intensity_factor": None, "estimated": False,
            "source": source, "name": None}


def _rows(loads, start=TODAY, days=14, ctl=50.0, atl=50.0):
    """Projection rows from the day after ``start`` with the given loads."""
    return project_fitness(ctl, atl, start, loads, start + timedelta(days=days))


# ------------------------------------------------------------------ parsing
def test_estimate_load_and_intensity_in_percent():
    """hours x IF^2 x 100: 4 h at IF 0.7 = 196; an IF of 70 is read as percent."""
    assert ps.estimate_load(240, 0.7) == pytest.approx(196.0)
    parsed = ps.parse_scenario([{"date": "2026-10-10", "duration_min": 240, "intensity_factor": 70}], TODAY, LAST)
    session = parsed["sessions"][0]
    assert session["load"] == pytest.approx(196.0) and session["estimated"] and session["intensity_factor"] == 0.7
    explicit = ps.parse_scenario('{"sessions": [{"date": "2026-10-10", "load": 120, "duration_min": 90}]}', TODAY, LAST)
    assert explicit["sessions"][0]["load"] == 120 and not explicit["sessions"][0]["estimated"]
    assert explicit["calendar"] == "add" and explicit["span"] == (date(2026, 10, 10), date(2026, 10, 10))


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("{not json", "not valid JSON"),
        ({}, "needs 'sessions' and/or 'weekly'"),
        ({"sessions": [{"date": "2026-10-10", "tss": 100}]}, "unknown key(s) tss"),
        ({"session": []}, "unknown key(s) session"),
        ({"sessions": [{"date": "2026-10-10", "duration_min": 60}]}, "give 'load', or 'duration_min' and 'intensity_factor'"),
        ({"sessions": [{"date": "2026-10-08", "load": 50}]}, "must lie from 2026-10-09"),
        ({"sessions": [{"date": "10/12/2026", "load": 50}]}, "must be a date YYYY-MM-DD"),
        ({"sessions": [{"date": "2026-10-10", "load": -5}]}, "load must be a number from 0"),
        ({"sessions": [{"date": "2026-10-10", "duration_min": 60, "intensity_factor": 2.0}]}, "intensity factor from 0.3"),
        ({"sessions": ["2026-10-10"]}, "sessions[0] must be an object"),
        ({"sessions": {"date": "2026-10-10"}}, "must be lists"),
        ({"calendar": "merge", "sessions": [{"date": "2026-10-10", "load": 1}]}, "calendar must be one of add, replace, none"),
        ({"weekly": [{"weeks": 2}]}, "give the weekly 'load', or 'hours' and 'intensity_factor'"),
        ({"weekly": [{"load": [500, 600], "hours": [9, 10, 11]}]}, "same length"),
        ({"weekly": [{"load": [500, 600], "weeks": 3}]}, "differs from the length"),
        ({"weekly": [{"load": 500, "sessions": 3, "days": ["Tue", "Sat"]}]}, "differs from the number of 'days'"),
        ({"weekly": [{"load": 500, "days": ["Tue", "tuesday"]}]}, "lists a weekday twice"),
        ({"weekly": [{"load": 500, "days": ["Tue", "Funday"]}]}, "unknown weekday"),
        ({"weekly": [{"load": 500, "days": ["Tue", "Sat"], "long_day": "Sun"}]}, "long_day must be one of the session days"),
        ({"weekly": [{"load": 500, "sessions": 4, "long_session_share": 0.1}]}, "long_session_share must be a number from 0.25"),
        ({"weekly": [{"load": 500, "start": "2027-04-01", "weeks": 2}]}, "after the last possible day"),
        ({"weekly": [{"load": 500, "sessions": 8}]}, "sessions must be a number from 1 to 7"),
    ],
)
def test_parse_scenario_errors(scenario, message):
    """Every invalid input names the field and the rule; nothing is guessed."""
    with pytest.raises(ps.ScenarioError) as info:
        ps.parse_scenario(scenario, TODAY, LAST)
    assert message in str(info.value)


def test_parse_scenario_limits_the_number_of_sessions():
    """More than 500 sessions are refused (inputs and expanded templates)."""
    many = [{"date": "2026-10-10", "load": 1}] * (ps.MAX_SCENARIO_SESSIONS + 1)
    with pytest.raises(ps.ScenarioError):
        ps.parse_scenario(many, TODAY, LAST)


def test_weekly_template_defaults_long_session_and_hours():
    """Default start next Monday, 5 sessions Tue/Wed/Thu/Sat/Sun, per-week loads, long day share and hours split."""
    parsed = ps.parse_scenario({"weekly": {"load": [500, 600], "hours": 10, "long_session_share": 0.4}}, TODAY, LAST)
    template = parsed["templates"][0]
    assert template["start"] == "2026-10-12" and template["end"] == "2026-10-25" and template["weeks"] == 2
    assert template["days"] == ["Tue", "Wed", "Thu", "Sat", "Sun"] and template["long_day"] == "Sat"
    sessions = parsed["sessions"]
    assert len(sessions) == 10 and {s["date"].weekday() for s in sessions} == {1, 2, 3, 5, 6}
    first_week = [s for s in sessions if s["date"] <= date(2026, 10, 18)]
    assert sum(s["load"] for s in first_week) == pytest.approx(500)
    assert sum(s["minutes"] for s in first_week) == pytest.approx(600)
    saturday = next(s for s in first_week if s["date"].weekday() == 5)
    assert saturday["load"] == pytest.approx(200) and saturday["minutes"] == pytest.approx(240)
    assert next(s for s in first_week if s["date"].weekday() == 1)["load"] == pytest.approx(75)
    assert parsed["span"] == (date(2026, 10, 12), date(2026, 10, 25)) and not sessions[0]["estimated"]


def test_weekly_template_estimate_days_and_patterns():
    """Hours x IF estimate, explicit days and long day, every default pattern has n distinct days."""
    parsed = ps.parse_scenario(
        {"weekly": [{"start": "2026-10-14", "weeks": 1, "hours": 8, "intensity_factor": 0.75, "days": ["sun", "Wednesday"],
                     "long_day": "Sun", "long_session_share": 0.75, "sport": "Gravel"}]},
        TODAY, LAST,
    )
    sessions = parsed["sessions"]
    assert [s["date"] for s in sessions] == [date(2026, 10, 14), date(2026, 10, 18)]
    assert sum(s["load"] for s in sessions) == pytest.approx(8 * 0.75**2 * 100) and all(s["estimated"] for s in sessions)
    assert sessions[1]["load"] == pytest.approx(0.75 * 450) and sessions[0]["sport"] == "Gravel"
    assert parsed["templates"][0]["load_estimated"] and parsed["templates"][0]["weekly_load"] == [450]
    for count, days in ps.DEFAULT_DAYS.items():
        assert len(set(days)) == count
    single = ps.parse_scenario({"weekly": [{"load": 300, "sessions": 1, "long_session_share": 0.5}]}, TODAY, LAST)
    assert single["sessions"][0]["load"] == 300 and single["templates"][0]["long_session_share"] is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5,15", (5.0, 15.0, "points")), ("-10..5", (-10.0, 5.0, "points")), ("5%, 20%", (5.0, 20.0, "pct")),
     ([5, 15], (5.0, 15.0, "points")), ("0 to 10", (0.0, 10.0, "points"))],
)
def test_parse_form_range(value, expected):
    """Points or percent of CTL, several separators, lists."""
    assert ps.parse_form_range(value) == expected


@pytest.mark.parametrize("value", ["5", "5%,15", "15,5", "a,b", "5,300", "-150%,10%", 7, "nan,5"])
def test_parse_form_range_errors(value):
    """Exactly two ends, same unit, low <= high, plausible bounds."""
    with pytest.raises(ps.ScenarioError):
        ps.parse_form_range(value)


def test_apply_calendar_modes():
    """add keeps every planned workout, replace drops those inside the span, none drops all."""
    calendar = [_session(date(2026, 10, 11), 120, source="calendar"), _session(date(2026, 10, 20), 80, source="calendar")]
    scenario = {"calendar": "add", "span": (date(2026, 10, 10), date(2026, 10, 15))}
    assert ps.apply_calendar(calendar, scenario) == (calendar, [])
    kept, dropped = ps.apply_calendar(calendar, dict(scenario, calendar="replace"))
    assert kept == calendar[1:] and dropped == calendar[:1]
    assert ps.apply_calendar(calendar, dict(scenario, calendar="none")) == ([], calendar)


# ------------------------------------------------------------------ weeks and checks
def test_plan_weeks_statistics_and_checks():
    """Rest days, monotony thresholds, longest session vs the event, sport split and the three range checks."""
    monday = date(2026, 10, 12)
    sessions = [_session(monday + timedelta(days=i), 100 + 10 * i, 60 + i) for i in range(7)]  # no rest day
    sessions.append(_session(monday + timedelta(days=5), 20, 300, sport="Run"))
    sessions.append(_session(monday + timedelta(days=9), None, 45, sport="WeightTraining"))  # no load, not a rest day
    loads = {}
    for s in sessions:
        loads[s["date"]] = loads.get(s["date"], 0.0) + (s["load"] or 0.0)
    rows = _rows(loads, start=monday - timedelta(days=1), days=14, ctl=30.0, atl=30.0)
    weeks = ps.plan_weeks(rows, loads, sessions, race_minutes=480)
    first, second = weeks[0], weeks[1]
    assert first["complete"] and first["rest_days"] == 0 and first["sessions"] == 8 and first["load"] == 930
    assert first["by_sport"] == {"cycling": 910, "running": 20}
    assert first["longest_session"]["minutes"] == 300 and first["longest_session"]["pct_of_target_event"] == 62
    assert first["hours"] == pytest.approx(round((sum(60 + i for i in range(7)) + 300) / 60, 1))
    metrics = {e["metric"] for e in first["outside_commonly_cited_range"]}
    assert metrics == {"ramp", "rest_days", "monotony"} and first["ramp"] > 8 and first["monotony"] > 2
    assert second["rest_days"] == 6 and second["sessions_without_load"] == 1 and second["by_sport"] == {"weighttraining": None}
    assert second["monotony"] is None and second["longest_session"]["minutes"] == 45
    assert {e["metric"] for e in second["outside_commonly_cited_range"]} == set()  # falling ramp is not flagged
    flagged = ps.flagged(weeks)
    assert {(e["week"], e["metric"]) for e in flagged} == {("2026-W42", m) for m in ("ramp", "rest_days", "monotony")}


def test_plan_weeks_monotony_above_two_and_partial_week():
    """A steady daily load gives a monotony above 2.0 (and no rest day); a partial week gets no rest-day check."""
    sunday = date(2026, 10, 11)
    loads = {sunday - timedelta(days=i): 60.0 + (i % 2) for i in range(7)}
    sessions = [_session(d, v, 60) for d, v in loads.items()]
    rows = _rows(loads, start=sunday - timedelta(days=7), days=7, ctl=60.0, atl=60.0)
    week = ps.plan_weeks(rows, loads, sessions)[0]
    assert week["monotony"] > 2 and {e["metric"] for e in week["outside_commonly_cited_range"]} == {"monotony", "rest_days"}
    partial_rows = [r for r in rows if r["date"] >= "2026-10-09"]
    partial = ps.plan_weeks(partial_rows, {}, [])[0]
    assert not partial["complete"] and partial["rest_days"] == 3 and partial["outside_commonly_cited_range"] == []
    assert partial["longest_session"] is None and partial["hours"] == 0


def test_recent_weeks_summary():
    """Mean load, hours and rest days of completed weeks and the longest activity."""
    rows = [{"week": "2026-W39", "start": "2026-09-21", "end": "2026-09-27", "load": 400, "hours": 8.0, "sessions": 4,
             "rest_days": 3, "monotony": 1.1},
            {"week": "2026-W40", "start": "2026-09-28", "end": "2026-10-04", "load": 600, "hours": 10.0, "sessions": 5,
             "rest_days": 2, "monotony": None}]
    acts = [{"start_date_local": "2026-09-27T08:00:00", "moving_time": 18000, "type": "Ride", "icu_training_load": 250},
            {"start_date_local": "2026-10-05T08:00:00", "moving_time": 36000, "type": "Ride"}]  # after the weeks
    summary = ps.recent_weeks(acts, rows)
    assert summary["mean_load"] == 500 and summary["max_load"] == 600 and summary["mean_rest_days"] == 2.5
    assert summary["longest_session"] == {"date": "2026-09-27", "minutes": 300, "sport": "Ride", "load": 250}
    assert ps.recent_weeks([], []) is None and ps.recent_weeks([], rows)["longest_session"] is None


# ------------------------------------------------------------------ target search
def _form_at(start, base_day, loads, target):
    ctl, atl = start
    for day in day_range(base_day + timedelta(days=1), target - timedelta(days=1)):
        ctl, atl = model_step(ctl, loads.get(day, 0.0), 42), model_step(atl, loads.get(day, 0.0), 7)
    return ctl, atl


def test_solve_target_brackets_the_range():  # pylint: disable=too-many-locals
    """The reported bounds lie in the range and the neighbouring grid points outside; 100 % equals the plan."""
    base_day, target = TODAY - timedelta(days=1), date(2026, 10, 25)
    loads = {d: 90.0 for d in day_range(TODAY, target) if d.weekday() != 0}
    start = (55.0, 60.0)
    result = ps.solve_target(start, base_day, loads, target, TODAY + timedelta(days=1), 7, (5.0, 15.0, "points"), 42, 7)
    assert result["available"] and result["window"] == {"start": "2026-10-18", "end": "2026-10-24", "days": 7}
    assert result["planned_window_load"] == 540
    pct = result["pct_of_planned"]
    assert pct["reached"] and pct["from"]["x"] < pct["to"]["x"]
    for bound, neighbour in ((pct["from"], pct["from"]["x"] - 1), (pct["to"], pct["to"]["x"] + 1)):
        assert 5 <= bound["form"] <= 15
        scaled = {d: v * (neighbour / 100 if date(2026, 10, 18) <= d <= date(2026, 10, 24) else 1) for d, v in loads.items()}
        ctl, atl = _form_at(start, base_day, scaled, target)
        if neighbour >= 0:
            assert not 5 <= ctl - atl <= 15
    ctl, atl = _form_at(start, base_day, loads, target)
    planned_form = ctl - atl
    hundred = ps._search(  # pylint: disable=protected-access
        [(100, *_form_at(start, base_day, loads, target))], -100, 100, "points")["from"]["form"]
    assert hundred == pytest.approx(round(planned_form, 1))
    weekly = result["constant_weekly"]
    assert weekly["reached"] and weekly["from"]["per_day"] == pytest.approx(weekly["from"]["x"] / 7, abs=0.5)
    # Closed form: with a constant daily load c over the N window days, form = A + c * (exp(-N/7) - exp(-N/42)).
    before = _form_at(start, base_day, loads, date(2026, 10, 18))
    decay_ctl, decay_atl = math.exp(-7 / 42), math.exp(-7 / 7)
    a_form = before[0] * decay_ctl - before[1] * decay_atl
    slope = decay_atl - decay_ctl
    exact_low = (15 - a_form) / slope * 7  # weekly load that gives form 15
    assert weekly["from"]["x"] == pytest.approx(exact_low, abs=ps.WEEKLY_STEP)


def test_solve_target_unreachable_percent_and_edge_cases():
    """A range above the form at zero load is not reached; percent ranges; no window; no planned load."""
    base_day, target = TODAY - timedelta(days=1), date(2026, 10, 20)
    loads = dict.fromkeys(day_range(TODAY, target), 100.0)
    high = ps.solve_target((50.0, 50.0), base_day, loads, target, TODAY + timedelta(days=1), 3, (40.0, 60.0, "points"), 42, 7)
    assert not high["pct_of_planned"]["reached"] and high["pct_of_planned"]["highest_form"]["x"] == 0
    assert not high["constant_weekly"]["reached"] and high["constant_weekly"]["highest_form"]["x"] == 0
    pct = ps.solve_target((50.0, 50.0), base_day, loads, target, TODAY + timedelta(days=1), 10, (5.0, 20.0, "pct"), 42, 7)
    assert pct["pct_of_planned"]["reached"] and 5 <= pct["pct_of_planned"]["from"]["form_pct_of_ctl"] <= 20
    assert pct["window"]["days"] == 10
    clipped = ps.solve_target((50.0, 50.0), base_day, loads, date(2026, 10, 12), TODAY + timedelta(days=1), 42,
                              (0.0, 10.0, "points"), 42, 7)
    assert clipped["window"] == {"start": "2026-10-10", "end": "2026-10-11", "days": 2}
    none = ps.solve_target((50.0, 50.0), base_day, loads, TODAY + timedelta(days=1), TODAY + timedelta(days=1), 7,
                           (0.0, 10.0, "points"), 42, 7)
    assert not none["available"] and "no day between tomorrow" in none["note"]
    empty = ps.solve_target((50.0, 50.0), base_day, {}, target, TODAY + timedelta(days=1), 7, (0.0, 30.0, "points"), 42, 7)
    assert empty["pct_of_planned"] is None and empty["constant_weekly"]["reached"]
    zero_ctl = ps.solve_target((0.0, 0.0), base_day, {}, target, TODAY + timedelta(days=1), 3, (5.0, 20.0, "pct"), 42, 7)
    assert zero_ctl["constant_weekly"]["searched"] == [0, ps.MIN_WEEKLY_MAX]


def test_state_at_start_uses_the_day_before():
    """The start of a day is the end of the day before; a missing row gives None."""
    rows = _rows({TODAY + timedelta(days=1): 100.0}, days=3)
    by_date = {r["date"]: r for r in rows}
    state = ps.state_at_start(by_date, TODAY + timedelta(days=2))
    assert state["ctl"] == by_date["2026-10-10"]["ctl"] and state["form_pct_of_ctl"] is not None
    assert ps.state_at_start(by_date, TODAY + timedelta(days=30)) is None


# ------------------------------------------------------------------ tool
SCENARIO = {
    "calendar": "replace",
    "sessions": [{"date": "2026-10-10", "sport": "Ride", "duration_min": 240, "intensity_factor": 0.7, "name": "long"}],
    "weekly": [{"start": "2026-10-12", "load": [500, 550, 600, 300], "hours": [9, 10, 11, 6], "sessions": 5,
                "long_session_share": 0.4}],
}


def test_simulation_text_compares_scenario_and_calendar(monkeypatch):
    """Header, scenario summary, assumptions, end/target comparison, search, weeks and checks; only GET requests."""
    calls = []
    _setup(monkeypatch, calls=calls)
    text = asyncio.run(get_load_projection(scenario=SCENARIO, target_form="5,15"))
    assert text.startswith("SIMULATION (what-if, nothing is written to the calendar): scenario vs calendar plan for athlete i1, "
                           "2026-10-09 to 2026-11-08")
    assert "1 proposed session(s), load 196" in text
    assert "template 1: 4 week(s) from 2026-10-12, 5 Ride sessions/week on Tue, Wed, Thu, Sat, Sun, long day Sat 40 %" in text
    assert "planned workouts 2026-10-10 to 2026-11-08 replaced (3 left out, load 200)" in text
    assert "1 load(s) estimated as load = hours x IF^2 x 100" in text
    assert "Calendar plan: 4 planned workouts (3 with a planned load, sum 300)" in text
    assert "Assumptions: model: CTL and ATL are exponentially weighted averages" in text and "no illness" in text
    assert "Target 2026-10-18 RACE_A 'Gran Fondo' (next RACE_A in the calendar), start of day: scenario CTL" in text
    assert "Target form range +5 to +15 (CTL - ATL): scenario outside, calendar plan inside" in text
    assert "Search on the scenario (only the 7 days 2026-10-11 to 2026-10-17 vary" in text
    assert "(a) 0-26 % of the 425 planned there" in text and "(b) a constant 0-140 per week" in text
    assert "Recent 4 completed weeks (2026-09-07 to 2026-10-04) for comparison: mean load 350/week" in text
    assert "  2026-W42 (2026-10-12 to 2026-10-18): load 500 vs 80 |" in text
    assert "    scenario: 5 sessions (cycling 500), 9.0 h, longest 3.6 h (10-17), rest days 2, monotony 1.07" in text
    assert "  scenario: none" in text and "[3] Meeusen et al. 2013" in text
    assert text.endswith("A simulation is arithmetic on the inputs, not a forecast; no assessment is made.")
    assert {method for _, _, method in calls} == {"GET"}
    activity_call = next(params for url, params, _ in calls if url.endswith("/activities"))
    assert activity_call["oldest"] == "2026-09-07"  # four completed weeks for the comparison
    events_call = next(params for url, params, _ in calls if url.endswith("/events"))
    assert events_call["newest"] == (TODAY + timedelta(days=180)).isoformat()


def test_simulation_json_add_mode_and_detail_levels(monkeypatch):
    """add: scenario loads on top of the plan; JSON keeps days and sessions for full only."""
    _setup(monkeypatch)
    scenario = [{"date": "2026-10-11", "load": 50, "sport": "Run"}, {"date": "2026-10-13", "load": 70}]
    full = json.loads(asyncio.run(get_load_projection(scenario=scenario, output_format="json", detail_level="full")))
    assert full["mode"] == "simulation" and full["simulation"]["calendar"] == "add"
    base_days = {d["date"]: d["load"] for d in full["days"]}
    sim_days = {d["date"]: d["load"] for d in full["simulation"]["days"]}
    assert sim_days["2026-10-11"] == base_days["2026-10-11"] + 50 and sim_days["2026-10-13"] == 70
    assert full["simulation"]["planned"] == {"sessions": 6, "with_load": 5, "without_load": 1, "load": 420}
    assert full["comparison"]["end"]["difference"]["ctl"] > 0 and len(full["simulation"]["sessions"]) == 2
    week = full["simulation"]["weeks"][0]
    assert week["by_sport"] == {"cycling": 220, "running": 50}
    assert full["target"]["source"] == "next_race_a" and "range" not in full["target"]  # the RACE_A, no search
    assert full["to"] == "2026-11-06" and "omitted" not in full
    standard = json.loads(asyncio.run(get_load_projection(scenario=scenario, output_format="json")))
    assert set(standard["omitted"]) == {"days", "simulation.sessions"} and "weeks" in standard["simulation"]
    compact = json.loads(asyncio.run(get_load_projection(scenario=scenario, output_format="json", detail_level="compact")))
    assert "weeks" not in compact and "weeks" not in compact["simulation"] and compact["comparison"]["weeks"]
    none_mode = json.loads(asyncio.run(get_load_projection(
        scenario={"calendar": "none", "sessions": scenario}, output_format="json", end_date="2026-10-20")))
    assert none_mode["simulation"]["calendar_dropped"] == {"sessions": 4, "load": 300}
    assert none_mode["simulation"]["planned"]["sessions"] == 2


def test_target_values_search_consistency_and_percent(monkeypatch):
    """Target from target_date: start-of-day values match the day before; the search at the plan's load reproduces them."""
    _setup(monkeypatch)
    payload = json.loads(asyncio.run(get_load_projection(
        target_date="2026-10-16", target_form="5%,40%", taper_days=5, output_format="json")))
    target = payload["target"]
    day_before = next(d for d in payload["days"] if d["date"] == "2026-10-15")
    assert target["source"] == "target_date" and target["event"] is None
    assert target["baseline"]["ctl"] == day_before["ctl"] and target["baseline"]["form"] == day_before["form"]
    assert target["range"] == {"low": 5.0, "high": 40.0, "unit": "pct"} and "scenario" not in target
    search = target["search"]
    assert search["basis"] == "calendar plan" and search["window"]["start"] == "2026-10-11"
    assert search["planned_window_load"] == 200  # the long ride and the tempo ride, the strength session has no load
    assert payload["mode"] == "projection" and payload["assumptions"]
    text = asyncio.run(get_load_projection(target_date="2026-10-16", target_form="5%,40%", taper_days=5))
    assert "Target 2026-10-16 (target_date), start of day: CTL" in text and "% of CTL:" in text
    race = json.loads(asyncio.run(get_load_projection(target_date="2026-10-18", output_format="json")))
    assert race["target"]["event"] == "Gran Fondo" and "range" not in race["target"]


def test_race_duration_longest_session_share_and_ignored_entries(monkeypatch):
    """A race event with a planned duration gives the longest session as a share of it; SICK/HOLIDAY entries are named."""
    events = [dict(e, moving_time=28800) if e["category"] == "RACE_A" else e for e in EVENTS]
    events += [{"id": 8, "category": "HOLIDAY", "name": "Trip", "start_date_local": "2026-10-13T00:00:00"},
               {"id": 9, "category": "SICK", "name": "Cold", "start_date_local": "2026-12-30T00:00:00"}]  # after the end
    _setup(monkeypatch, {"/events": events})
    scenario = [{"date": "2026-10-11", "duration_min": 360, "intensity_factor": 0.65}]
    text = asyncio.run(get_load_projection(scenario=scenario))
    assert "RACE_A 'Gran Fondo', planned 8.0 h (next RACE_A in the calendar)" in text
    assert "longest 6.0 h (10-11, 75 % of the target event)" in text
    assert "calendar entries in the period that the model ignores (no load change, constant time constants): HOLIDAY 2026-10-13." in text
    assert "SICK" not in text
    payload = json.loads(asyncio.run(get_load_projection(scenario=scenario, output_format="json")))
    assert payload["target"]["event_minutes"] == 480


def test_projection_end_extends_to_target_and_scenario(monkeypatch):
    """Without end_date the end covers the target and the scenario; with end_date both must lie inside."""
    _setup(monkeypatch)
    payload = json.loads(asyncio.run(get_load_projection(target_date="2026-12-24", output_format="json")))
    assert payload["to"] == "2026-12-24" and payload["target"]["baseline"] is not None
    sim = json.loads(asyncio.run(get_load_projection(scenario={"weekly": {"start": "2026-12-28", "load": 400}},
                                                      output_format="json", detail_level="compact")))
    assert sim["to"] == "2027-01-03"
    assert asyncio.run(get_load_projection(end_date="2026-10-20", target_date="2026-10-25")).startswith(
        "Error: target_date must lie after today and not after 2026-10-20")
    assert "must lie from 2026-10-09 to 2026-10-20" in asyncio.run(get_load_projection(
        end_date="2026-10-20", scenario=[{"date": "2026-10-21", "load": 10}]))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"taper_days": 0}, "Error: taper_days must be between 1 and 42."),
        ({"target_form": "high"}, "Error: target_form must be a range"),
        ({"target_date": "2026-10-09"}, "Error: target_date must lie after today"),
        ({"target_date": "18.10.2026"}, "Error: "),
        ({"scenario": "[{\"date\": \"2026-10-10\"}]"}, "Error: sessions[0]: give 'load'"),
        ({"scenario": {"weekly": []}}, "Error: scenario needs"),
    ],
)
def test_simulation_input_errors(monkeypatch, kwargs, message):
    """Invalid inputs return an error before any API call."""
    calls = []
    _setup(monkeypatch, calls=calls)
    assert asyncio.run(get_load_projection(**kwargs)).startswith(message)
    assert not calls


def test_target_form_without_target_day_and_blank_inputs(monkeypatch):
    """No RACE_A and no target_date: a note; blank strings count as not given."""
    _setup(monkeypatch, {"/events": [e for e in EVENTS if e["category"] != "RACE_A"]})
    payload = json.loads(asyncio.run(get_load_projection(target_form="5,15", output_format="json")))
    assert payload["target"]["available"] is False and "pass target_date" in payload["target"]["note"]
    assert "Target: no target day" in asyncio.run(get_load_projection(target_form="5,15", detail_level="compact"))
    plain = json.loads(asyncio.run(get_load_projection(scenario=" ", target_form="", output_format="json")))
    assert plain["mode"] == "projection" and "target" not in plain and "assumptions" not in plain


def test_plain_projection_checks(monkeypatch):
    """The calendar plan gets the plan statistics and checks too; compact mentions flagged weeks only."""
    heavy = [{"id": 10 + i, "category": "WORKOUT", "type": "Ride", "name": f"Ride {i}",
              "start_date_local": f"{(TODAY + timedelta(days=i)).isoformat()}T00:00:00", "icu_training_load": 150,
              "moving_time": 7200} for i in range(1, 15)]
    _setup(monkeypatch, {"/events": heavy})
    payload = json.loads(asyncio.run(get_load_projection(end_date="2026-10-25", output_format="json")))
    flagged = {(e["week"], e["metric"]) for e in payload["checks"]["baseline"]}
    assert ("2026-W42", "ramp") in flagged and ("2026-W42", "rest_days") in flagged
    assert payload["checks"]["references"]["rest_days"]["min"] == 1
    week = payload["weeks"][1]
    assert week["planned_sessions"] == 7 and week["rest_days"] == 0 and week["hours"] == 14.0
    text = asyncio.run(get_load_projection(end_date="2026-10-25"))
    assert "Plan checks, weeks outside the commonly cited range" in text and "2026-W42 ramp +" in text
    assert "0 rest days (below 1 [3])" in text
    compact = asyncio.run(get_load_projection(end_date="2026-10-25", detail_level="compact"))
    assert "week metric(s) outside the commonly cited range (details at standard)" in compact
    _setup(monkeypatch, {"/events": []})
    assert "Plan checks" not in asyncio.run(get_load_projection(detail_level="compact"))


def test_simulation_compact_and_full_text(monkeypatch):
    """Compact: no weeks, a check count; full: scenario days and the proposed sessions."""
    _setup(monkeypatch)
    compact = asyncio.run(get_load_projection(scenario=SCENARIO, detail_level="compact"))
    assert "Weeks, scenario vs calendar plan" not in compact
    assert "Plan checks: 0 week metric(s) of the scenario and 0 of the calendar plan" in compact
    full = asyncio.run(get_load_projection(scenario=SCENARIO, detail_level="full"))
    assert "Scenario days (end of day): 10-09 load 100" in full and "Proposed sessions: 2026-10-10 Ride load 196 (estimated), 240 min" in full


def test_simulation_through_mcp_layer(monkeypatch):
    """FastMCP accepts the scenario as an object and the form range as a list."""
    from intervals_mcp_server.mcp_instance import mcp  # pylint: disable=import-outside-toplevel

    _setup(monkeypatch)
    result = asyncio.run(mcp.call_tool("get_load_projection", {
        "scenario": {"sessions": [{"date": "2026-10-12", "load": 100}]}, "target_form": [0, 20], "detail_level": "compact",
    }))
    text = json.dumps(result, default=str)
    assert "SIMULATION (what-if" in text and "Target form range +0 to +20" in text


def test_race_day_values_use_one_convention(monkeypatch):
    """Regression (review P6-20-1): race lines and the target block give the same start-of-day form for one race;
    the end-of-day row of the race day (with a race session in the scenario) is labelled as such."""
    _setup(monkeypatch)
    payload = json.loads(asyncio.run(get_load_projection(target_form="5,15", output_format="json")))
    race, target = payload["races"][0], payload["target"]
    assert race["basis"] == "start_of_day" and target["date"] == race["date"] == "2026-10-18"
    assert (race["ctl"], race["atl"], race["form"]) == (target["baseline"]["ctl"], target["baseline"]["atl"], target["baseline"]["form"])
    race_day_row = next(d for d in payload["days"] if d["date"] == "2026-10-18")
    assert race_day_row["form"] != race["form"]  # end of day: one more day of decay
    assert payload["value_basis"]["races_and_target"].startswith("start of day")
    text = asyncio.run(get_load_projection(target_form="5,15"))
    assert "Races (start of day, before the race's own load): 2026-10-18 RACE_A 'Gran Fondo': CTL 42.0, ATL 32.1, form +9.9" in text
    assert "start of day: CTL 42.0 | ATL 32.1 | form +9.9" in text
    assert "End of 2026-11-06:" in text and "Lowest projected form (end of day)" in text
    with_race = [{"date": "2026-10-18", "load": 300, "sport": "Ride", "name": "Gran Fondo"}]
    sim = json.loads(asyncio.run(get_load_projection(scenario=with_race, output_format="json", detail_level="full")))
    sim_race, sim_target = sim["simulation"]["races"][0], sim["target"]["scenario"]
    assert sim_race["form"] == sim_target["form"] == sim["races"][0]["form"]  # the race's own load does not count
    end_of_race_day = next(d for d in sim["simulation"]["days"] if d["date"] == "2026-10-18")
    assert end_of_race_day["form"] < sim_race["form"] - 10
    sim_text = asyncio.run(get_load_projection(scenario=with_race))
    assert "Races (start of day, before the race's own load): 2026-10-18 RACE_A 'Gran Fondo': scenario CTL" in sim_text
    assert "Lowest form (end of day, that day's load included): scenario" in sim_text


def test_race_today_starts_from_the_intervals_values(monkeypatch):
    """A race today is reported at the start of today, i.e. the Intervals.icu values at the end of yesterday."""
    events = [dict(e, start_date_local="2026-10-09T00:00:00") if e["category"] == "RACE_A" else e for e in EVENTS]
    _setup(monkeypatch, {"/events": events})
    payload = json.loads(asyncio.run(get_load_projection(output_format="json")))
    race, start = payload["races"][0], payload["start"]
    assert start["date"] == "2026-10-08" and (race["ctl"], race["atl"], race["form"]) == (start["ctl"], start["atl"], start["form"])


def test_plan_weeks_missing_durations_sports_and_identical_loads():
    """Review P6-20-2/3: hours n/a without durations, 'sport not given', identical daily loads flagged as maximal."""
    monday = date(2026, 10, 12)
    sessions = [_session(monday + timedelta(days=i), 70.0) for i in range(7)]
    sessions[0]["sport"] = None
    loads = {s["date"]: 70.0 for s in sessions}
    rows = _rows(loads, start=monday - timedelta(days=1), days=7, ctl=70.0, atl=70.0)
    week = ps.plan_weeks(rows, loads, sessions)[0]
    assert week["hours"] is None and week["sessions_without_duration"] == 7
    assert week["by_sport"] == {"cycling": 420, "sport not given": 70}
    assert week["monotony"] is None and week["monotony_note"] == ps.IDENTICAL_LOADS
    monotony_flag = next(e for e in week["outside_commonly_cited_range"] if e["metric"] == "monotony")
    assert monotony_flag["value"] is None and monotony_flag["position"] == "above"
    partial_rows = rows[2:]  # 5 identical days: flagged although the rest-day check does not apply
    partial = ps.plan_weeks(partial_rows, loads, sessions)[0]
    assert {e["metric"] for e in partial["outside_commonly_cited_range"]} == {"monotony"}
    mixed = [dict(s, minutes=60.0) if i < 3 else s for i, s in enumerate(sessions)]
    assert ps.plan_weeks(rows, loads, mixed)[0]["hours"] == 3.0
    few = ps.plan_weeks(rows[:4], loads, sessions)[0]
    assert few["monotony_note"].startswith("needs 5 days")


def test_template_text_without_durations_and_identical_loads(monkeypatch):
    """The week line says hours n/a and monotony undefined; the check names the week."""
    _setup(monkeypatch)
    text = asyncio.run(get_load_projection(scenario={"calendar": "none", "weekly": {"load": 490, "sessions": 7}},
                                           end_date="2026-10-18"))
    assert ("scenario: 7 sessions (cycling 490), hours n/a (7 session(s) without duration), longest n/a, rest days 0, "
            "monotony undefined (identical daily loads, maximal)") in text
    assert "2026-W42 monotony undefined, identical daily loads (above 2.0 [2])" in text


def test_estimated_loads_are_capped():
    """Review P6-20-4: an estimate may not exceed the caps that apply to a given load."""
    with pytest.raises(ps.ScenarioError, match="estimated load 5400 exceeds 1500"):
        ps.parse_scenario([{"date": "2026-10-10", "duration_min": 1440, "intensity_factor": 1.5}], TODAY, LAST)
    with pytest.raises(ps.ScenarioError, match="estimated weekly load 9000 exceeds 5000"):
        ps.parse_scenario({"weekly": {"hours": 90, "intensity_factor": 1.0}}, TODAY, LAST)
