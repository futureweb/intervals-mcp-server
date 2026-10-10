"""
Tests for the submaximal fatigue test trend: normalising Intervals.icu's submax_fatigue_test,
the context checks (containing work interval, effort continued, hard riding before, HR course
and drop after the test), the validity filter, the trend per metric and week, and the
get_submax_test_trends tool. Synthetic data only.
"""

import asyncio
import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.tools.fatigue import get_submax_test_trends  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.submax import (  # pylint: disable=wrong-import-position
    context_checks,
    normalise_test,
    trends,
    validity,
)
from tests.sample_data import SFT_SPORT_SETTINGS, submax_activity, submax_streams  # pylint: disable=wrong-import-position
from tests.test_fatigue_profile import _install_router  # pylint: disable=wrong-import-position

TEST_INTERVAL = {"type": "WORK", "start_index": 900, "end_index": 1080, "moving_time": 180, "average_watts": 248}
LONG_INTERVAL = {"type": "WORK", "start_index": 600, "end_index": 1500, "moving_time": 900, "average_watts": 250, "label": "Rep 1"}


def _streams_dict(streams):
    """Stream list as {type: data}."""
    return {s["type"]: s["data"] for s in streams}


def _checked(activity, streams=None, intervals=None):
    """Normalised test with its context checks attached."""
    test = normalise_test(activity)
    assert test is not None
    test["context"] = context_checks(test, _streams_dict(streams or submax_streams()), intervals or [TEST_INTERVAL])
    return test


# ------------------------------------------------------------------ pure functions
def test_normalise_test_fields_and_missing_recovery():
    """Fields of the Intervals.icu test; HRRc 0 without a recovery window is missing, with one it is 0."""
    test = normalise_test(submax_activity("a1", "2026-08-03"))
    assert test is not None
    assert test["test_type"] == "POWER" and test["average"] == 248.0 and test["target"] == 246.0
    assert test["deviation_pct"] == 0.8 and test["tolerance_pct"] == 5.0
    assert test["hrrc_bpm"] == 28.0 and test["recovery_measured"] is True
    assert test["efficiency_factor"] == pytest.approx(1.653, abs=0.001)
    assert test["gear_id"] == "b1" and test["indoor"] is False and test["ftp"] == 234.0
    missing = normalise_test(submax_activity("a2", "2026-08-03", hrrc=0, end_index_hrrc=0))
    assert missing is not None and missing["hrrc_bpm"] is None and missing["recovery_measured"] is False
    zero = normalise_test(submax_activity("a3", "2026-08-03", hrrc=0, end_index_hrrc=1140))
    assert zero is not None and zero["hrrc_bpm"] == 0.0  # measured recovery of 0 bpm stays 0
    assert normalise_test({"id": "x"}) is None
    assert normalise_test(submax_activity("a4", "2026-08-03", type="NONE")) is None
    pace = normalise_test(submax_activity("a5", "2026-08-03", type="PACE", average_mps=4.1, target=4.0, final_bpm=0))
    assert pace is not None and pace["average"] == 4.1 and pace["final_bpm"] is None


def test_context_of_a_standalone_test():
    """A stand-alone test: power before and after, HR course and the HR drop in the easy minute after."""
    context = _checked(submax_activity("a1", "2026-08-03"))["context"]
    assert context["checked"] and not context["inside_workout"] and context["hard_before"] is None
    assert context["power_before_w"] == 150 and context["power_after_w"] == 100
    assert context["hr_start"] == 120.0 and context["hr_end"] == 150.0 and context["hr_rise"] == 30.0
    assert context["hr_drop_60s"] == pytest.approx(30.2, abs=0.1)


def test_context_flags_a_test_inside_a_workout():
    """A test inside a longer work interval, continued afterwards, or after hard riding is flagged."""
    context = _checked(submax_activity("e1", "2026-09-20"), submax_streams(after_watts=250), [LONG_INTERVAL])["context"]
    assert context["inside_workout"][0] == "part of a 15:00 work interval at 250 W"
    assert context["inside_workout"][1] == "effort continued after the test (250 W in the next 60 s)"
    assert context["hr_drop_60s"] is None  # no easy minute after the test
    assert context["containing_interval"]["label"] == "Rep 1"
    hard = _checked(submax_activity("e5", "2026-09-24"), submax_streams(before_watts=240))["context"]
    assert hard["hard_before"] == "240 W in the 5 min before the test"


def test_context_not_checked_for_pace_or_bad_indices():
    """Pace tests and tests outside the streams are not context-checked (and say so)."""
    pace = normalise_test(submax_activity("p", "2026-08-03", type="PACE", average_mps=4.0, target=4.0))
    assert pace is not None
    assert context_checks(pace, _streams_dict(submax_streams()), [])["checked"] is False
    bad = normalise_test(submax_activity("b", "2026-08-03", start_index=5000, end_index=5180))
    assert bad is not None
    out = context_checks(bad, _streams_dict(submax_streams()), [])
    assert out["checked"] is False and "context not checked" in out["note"]
    assert context_checks(bad, {}, [])["checked"] is False


def test_validity_reasons():
    """Every exclusion reason, the tolerance override and the optional recovery requirement."""
    assert not validity(_checked(submax_activity("a1", "2026-08-03")))
    off = _checked(submax_activity("o", "2026-08-03", average_watts=270))
    assert [r["code"] for r in validity(off)] == ["off_target"]
    assert "+9.8 % (tolerance ±5 %)" in validity(off)[0]["text"]
    assert not validity(off, tolerance_pct=10)
    assert [r["code"] for r in validity(_checked(submax_activity("i", "2026-08-03", ignore=True)))] == ["ignored"]
    assert [r["code"] for r in validity(_checked(submax_activity("c", "2026-08-03", cv=12)))] == ["not_steady"]
    embedded = _checked(submax_activity("e1", "2026-09-20"), submax_streams(after_watts=250), [LONG_INTERVAL])
    reason = validity(embedded)[0]
    assert reason["code"] == "inside_workout" and reason["text"].startswith("detected inside a regular workout")
    hard = _checked(submax_activity("e5", "2026-09-24"), submax_streams(before_watts=240))
    assert [r["code"] for r in validity(hard)] == ["hard_before"]
    no_rec = _checked(submax_activity("n", "2026-08-03", hrrc=0, end_index_hrrc=0))
    assert not validity(no_rec) and [r["code"] for r in validity(no_rec, require_recovery=True)] == ["no_recovery"]
    incomplete = normalise_test(submax_activity("x", "2026-08-03", target=None))
    assert incomplete is not None and [r["code"] for r in validity(incomplete)] == ["incomplete"]


def test_trends_slope_weeks_and_notes():
    """Trend per metric (n, change, slope per week, small sample), ISO weeks and comparability notes."""
    rows = []
    for number, (day, bpm, hrrc) in enumerate((("2026-08-03", 150, 28), ("2026-08-17", 148, 30), ("2026-08-31", 146, 0),
                                               ("2026-09-14", 144, 32))):
        activity = submax_activity(f"a{number}", day, final_bpm=bpm, hrrc=hrrc, end_index_hrrc=1140 if hrrc else 0,
                                   efficiency_factor=round(248 / bpm, 4))
        rows.append(_checked(activity))
    result = trends(rows)["groups"][0]
    assert (result["sport_family"], result["test_type"], result["sports"], result["tests"]) == ("cycling", "POWER", ["Ride"], 4)
    assert result["units"] == {"average": "W", "efficiency_factor": "W/bpm"}
    final = result["metrics"]["final_bpm"]
    assert final["n"] == 4 and not final["small_sample"]
    assert final["first"] == 150 and final["last"] == 144 and final["change"] == -6
    assert final["slope_per_week"] == pytest.approx(-1.0) and final["weeks"] == 6.0
    hrrc = result["metrics"]["hrrc_bpm"]
    assert hrrc["n"] == 3 and hrrc["small_sample"] and hrrc["first"] == 28 and hrrc["last"] == 32
    assert result["metrics"]["efficiency_factor"]["change"] > 0
    assert result["metrics"]["hr_drop_60s"]["n"] == 4
    assert [w["week"] for w in result["weeks"]] == ["2026-W32", "2026-W34", "2026-W36", "2026-W38"]
    assert result["weeks"][2]["hrrc_bpm"] is None
    assert not result["notes"]
    rows[0]["target"] = 230.0
    rows[1]["gear_id"] = "b2"
    rows[2]["indoor"] = True
    notes = trends(rows)["groups"][0]["notes"]
    assert notes[0].startswith("targets differ (230-246 W)")
    assert "2 different bikes/shoes (power meters)" in notes and "indoor and outdoor mixed (1 indoor)" in notes
    assert not trends([])["groups"]


def _pace_test(aid, day, mps, bpm):
    """A running pace test (no context checks for pace tests)."""
    activity = submax_activity(aid, day, type="PACE", average_watts=0, average_mps=mps, target=3.6, final_bpm=bpm,
                               efficiency_factor=round(mps / bpm, 5))
    activity["type"] = "Run"
    test = normalise_test(activity)
    assert test is not None
    test["context"] = {"checked": False}
    return test


def test_trends_never_pool_power_and_pace_tests():
    """Power (W) and pace (m/s) tests and sport families get separate trends with their own units and n."""
    power = [_checked(submax_activity(f"p{i}", day, final_bpm=bpm)) for i, (day, bpm) in
             enumerate((("2026-08-03", 150), ("2026-08-17", 148), ("2026-08-31", 146)))]
    pace = [_pace_test("r1", "2026-08-04", 3.6, 160), _pace_test("r2", "2026-08-18", 3.62, 158)]
    groups = trends(power + pace)["groups"]
    assert [(g["sport_family"], g["test_type"], g["tests"]) for g in groups] == [("cycling", "POWER", 3), ("running", "PACE", 2)]
    cycling, running = groups
    assert running["units"] == {"average": "m/s", "efficiency_factor": "m/s per bpm"}
    assert running["metrics"]["efficiency_factor"]["mean"] == pytest.approx(0.0227, abs=0.0002)
    assert cycling["metrics"]["efficiency_factor"]["mean"] == pytest.approx(1.653, abs=0.001)
    assert running["weeks"][0]["average"] == 3.6 and cycling["weeks"][0]["average"] == 248.0
    assert not running["notes"] and not cycling["notes"]


# ------------------------------------------------------------------ tool
def _routes():
    """Router overrides: 4 valid tests, 5 excluded ones, an activity without test and a NONE test."""
    activities = [
        submax_activity("e5", "2026-09-24"),
        submax_activity("e4", "2026-09-23", cv=12),
        submax_activity("e3", "2026-09-22", ignore=True),
        submax_activity("e2", "2026-09-21", average_watts=270),
        submax_activity("e1", "2026-09-20"),
        submax_activity("a4", "2026-09-14", final_bpm=144, hrrc=32),
        submax_activity("a3", "2026-08-31", final_bpm=146, hrrc=0, end_index_hrrc=0),
        submax_activity("a2", "2026-08-17", final_bpm=148, hrrc=30),
        submax_activity("a1", "2026-08-03", final_bpm=150, hrrc=28),
        {"id": "n1", "name": "No test", "type": "Ride", "start_date_local": "2026-08-05T07:00:00"},
        {"id": "n2", "name": "Run", "type": "Run", "start_date_local": "2026-08-06T07:00:00",
         "submax_fatigue_test": {"type": "NONE"}},
    ]
    routes = {
        "/activity/e1/streams": submax_streams(after_watts=250),
        "/activity/e1/intervals": {"icu_intervals": [LONG_INTERVAL]},
        "/activity/e5/streams": submax_streams(before_watts=240),
        "/streams": submax_streams(),
        "/intervals": {"icu_intervals": [TEST_INTERVAL]},
        "/sport-settings": SFT_SPORT_SETTINGS,
        "/activities": activities,
    }
    return routes


def test_tool_text_lists_valid_and_excluded_tests(monkeypatch):
    """Text output lists every test with its status and trends the valid ones."""
    calls = []
    _install_router(monkeypatch, _routes(), calls)
    result = asyncio.run(get_submax_test_trends(start_date="2026-08-01", end_date="2026-09-30"))
    assert "Submaximal fatigue tests for athlete i1, 2026-08-01 to 2026-09-30: 9 detected (4 valid, 5 excluded)." in result
    assert "Test settings: Ride: POWER 180 s at 105 % of FTP (±5 %, CV <= 10 %, start within 30 min)." in result
    assert "(a3): POWER 180 s, 248 W vs target 246 W (+0.8 %), CV 5.0 %, HR end 146 bpm, EF 1.65, HRRc not measured" in result
    assert "HRRc 28 bpm, HR 120->150 bpm, power 5 min before 150 W, 60 s after 100 W, HR drop 60 s after 30 bpm (stream) -> VALID" in result
    assert "(e1)" in result and "EXCLUDED: detected inside a regular workout (not a stand-alone test): part of a 15:00 work interval" in result
    assert "EXCLUDED: marked as ignored in Intervals.icu" in result
    assert "EXCLUDED: average outside the target tolerance: +9.8 %" in result
    assert "EXCLUDED: coefficient of variation above the limit: CV 12.0 % > 10 %" in result
    assert "EXCLUDED: hard riding right before the test: 240 W in the 5 min before the test" in result
    assert "Trend for cycling POWER tests (Ride; average in W, efficiency factor in W/bpm) over 4 valid test(s):" in result
    assert "  HR at the end of the test (Intervals.icu): n 4, 150 -> 144 bpm (change -6 bpm), mean 147 bpm" in result
    assert "  Efficiency factor (average / final HR): n 4, 1.653 -> 1.653 W/bpm" in result
    assert "slope -1.0 bpm/week over 6 weeks" in result
    assert "  HR recovery HRRc (Intervals.icu): n 3, 28 -> 32 bpm" in result and "small sample, not reliable" in result
    assert "  Per ISO week (means of valid tests):" in result
    assert "    2026-W36: 1 test(s), HR end 146 bpm, EF 1.653 W/bpm, HRRc n/a bpm, average 248 W vs target 246 W" in result
    assert "API calls: 20" in result  # settings, list, 9 x (intervals + streams)
    stream_params = next(c[1] for c in calls if c[0].endswith("/streams"))
    assert stream_params == {"types": "time,watts,heartrate"}


def test_tool_json_compact_and_options(monkeypatch):
    """JSON output, require_recovery, check_context=False, tolerance override and limit."""
    _install_router(monkeypatch, _routes())
    payload = json.loads(asyncio.run(get_submax_test_trends(
        start_date="2026-08-01", end_date="2026-09-30", output_format="json", detail_level="compact", require_recovery=True)))
    assert payload["valid"] == 3 and payload["excluded"] == 6
    assert payload["excluded_by_reason"]["no_recovery"] == 1
    a3 = next(t for t in payload["tests"] if t["activity_id"] == "a3")
    assert a3 == {"activity_id": "a3", "date": "2026-08-31", "average": 248.0, "target": 246.0, "final_bpm": 146.0,
                  "efficiency_factor": pytest.approx(1.653, abs=0.001), "hrrc_bpm": None, "valid": False, "reasons": ["no_recovery"]}
    assert payload["settings"]["Ride"]["sft_target_percent"] == 105
    assert payload["trend"]["groups"][0]["metrics"]["final_bpm"]["n"] == 3
    assert payload["trend"]["rule"].startswith("one trend per sport family and test type")
    calls = []
    _install_router(monkeypatch, _routes(), calls)
    unchecked = json.loads(asyncio.run(get_submax_test_trends(
        start_date="2026-08-01", end_date="2026-09-30", output_format="json", check_context=False, tolerance_pct=10, limit=5)))
    assert unchecked["api_calls"] == 2 and unchecked["filters"]["tests_beyond_limit"] == 4
    assert {t["activity_id"] for t in unchecked["tests"] if not t["reasons"]} == {"e1", "e2", "e5"}
    assert unchecked["tests"][0]["context"]["note"] == "context not checked (check_context=False)"
    compact = asyncio.run(get_submax_test_trends(start_date="2026-08-01", end_date="2026-09-30", detail_level="compact"))
    assert "Excluded by reason:" in compact and "HR rise" not in compact


def test_tool_without_tests_and_errors(monkeypatch):
    """No valid or no detected tests, sport settings errors and parameter validation."""
    _install_router(monkeypatch, {"/activities": [submax_activity("e1", "2026-09-20", average_watts=300)],
                                  "/sport-settings": SFT_SPORT_SETTINGS,
                                  "/streams": submax_streams(), "/intervals": {"icu_intervals": []}})
    result = asyncio.run(get_submax_test_trends(sport_types="Ride"))
    assert "1 detected (0 valid, 1 excluded)" in result and "No valid test yet" in result
    assert "Trend: no valid tests." in result
    _install_router(monkeypatch, {"/activities": [], "/sport-settings": {"error": True, "message": "x"}})
    empty = asyncio.run(get_submax_test_trends(sport_types="Run"))
    assert "No submax tests detected in this period" in empty and "Test settings: not available." in empty
    _install_router(monkeypatch, {"/activities": {"error": True, "message": "boom"}})
    assert asyncio.run(get_submax_test_trends()) == "Error fetching activities: boom"
    assert asyncio.run(get_submax_test_trends(detail_level="x")).startswith("Error: detail_level")
    assert asyncio.run(get_submax_test_trends(tolerance_pct=0)).startswith("Error: tolerance_pct")
    assert asyncio.run(get_submax_test_trends(start_date="2026-10-02", end_date="2026-10-01")).startswith("Error: start_date")


def test_tool_separates_ride_and_run_tests(monkeypatch):
    """A run pace test next to ride power tests gets its own trend block with m/s units."""
    run = submax_activity("r1", "2026-09-01", type="PACE", average_watts=0, average_mps=3.6, target=3.6, final_bpm=160,
                          efficiency_factor=0.0225)
    run["type"] = "Run"
    routes = {"/activities": [run, submax_activity("a1", "2026-08-03")], "/sport-settings": SFT_SPORT_SETTINGS,
              "/streams": submax_streams(), "/intervals": {"icu_intervals": [TEST_INTERVAL]}}
    _install_router(monkeypatch, routes)
    result = asyncio.run(get_submax_test_trends(start_date="2026-08-01", end_date="2026-09-30"))
    assert "Trend for cycling POWER tests (Ride; average in W, efficiency factor in W/bpm) over 1 valid test(s):" in result
    assert "Trend for running PACE tests (Run; average in m/s, efficiency factor in m/s per bpm) over 1 valid test(s):" in result
    assert "  Efficiency factor (average / final HR): n 1, 0.0225 -> 0.0225 m/s per bpm" in result
    assert "average 3.60 m/s vs target 3.60 m/s" in result
    assert "3.60 m/s vs target 3.60 m/s (+0.0 %)" in result
