"""
Lap-press steps in the plan-vs-execution analysis (review R36-11): the duration or distance of an
``until_lap_press`` step is only a planning estimate. The step is open: no shorter/longer deviation,
"ended by lap after <actual> (planning estimate <estimate>)", the following steps are judged from
the actual lap boundary, the target adherence stays, and the neighbouring timed steps are neither
shifted nor downgraded (exact laps, device auto-laps, carried-over boundaries, repeats).
"""

import os
import random
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.tools.report import _execution_findings  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.execution import (  # pylint: disable=wrong-import-position
    Profile,
    _spans,
    format_execution,
    lap_layout,
    plan_steps,
    planned_step_map,
    planned_step_text,
)
from tests.test_execution_autolaps import FTP, RIDE, RUN, T, build, build_reset, pc, pw, run  # pylint: disable=wrong-import-position

LAP = {"until_lap_press": True}
RIDE_W = {"ftp": 234, "activity_type": "Ride"}


def watts(low, high):
    """Absolute power target."""
    return {"power": {"start": low, "end": high, "units": "w"}}


# The athlete's outdoor VO2max workout (event 144186380): a lap-press warm-up with a 17 min
# estimate, 4 x (3 min + 3 min), a last 3 min interval and a 10 min cool-down.
VO2_PLAN = [
    {"text": "Einrollen bis Startpunkt", "duration": 1020, "warmup": True, "freeride": True, **LAP, **watts(110, 175)},
    {"reps": 4, "steps": [{"text": "VO2 Intervall", "duration": 180, **watts(275, 285)},
                          {"text": "Erholung", "duration": 180, **watts(115, 135)}]},
    {"text": "Letztes Intervall", "duration": 180, **watts(275, 285)},
    {"text": "Ausrollen", "duration": 600, "cooldown": True, "freeride": True, **watts(95, 130)},
]


def vo2_segments(warmup_s):
    """The executed VO2 session with a warm-up of ``warmup_s`` seconds (one lap per step)."""
    return [(warmup_s, 150, None, 125)] + [(180, 285, None, 165), (180, 125, None, 140)] * 4 + [(180, 280, None, 165), (600, 115, None, 125)]


def deviation_texts(result):
    """All deviation and info texts of the planned rows."""
    return [d["text"] for r in result["rows"] if r.get("planned") for d in r.get("deviations", [])]


def test_lap_press_warmup_is_open_with_exact_laps():
    """R36-11: a warm-up ended by lap after 12:00, 16:01 or 25:00 (estimate 17:00) is never shorter
    or longer than planned, is not split, and the timed steps after it start on the plan timeline."""
    for warmup_s in (720, 961, 1500):
        result = run(VO2_PLAN, *build(vo2_segments(warmup_s), laps="steps"), RIDE_W)
        rows, summary = result["rows"], result["summary"]
        warmup = rows[0]
        assert warmup["lap_press"]["elapsed_s"] == warmup_s and warmup["lap_press"]["estimate_s"] == 1020
        assert warmup["metrics"]["moving_time_s"] == warmup_s and "overrun" not in warmup and "duration_diff_s" not in warmup
        assert warmup["adherence"]["status"] == "in range" and warmup["metrics"]["time_in_target_pct"] > 90
        assert all(abs(r["start_offset_s"]) < 1 for r in rows[1:]), warmup_s
        assert all(r["metrics"]["moving_time_s"] == r["planned"]["duration"] for r in rows[1:])
        assert deviation_texts(result) == [], warmup_s
        assert summary["steps_with_deviations"] == 0 and summary["split_steps"] == 0 and summary["extra_time_inside_plan_s"] == 0
        assert summary["lap_press_steps"] == 1 and summary["lap_press_estimate_s"] == 1020
        assert summary["alignment_confidence"] == "high"


def test_lap_press_text_standard_compact_and_report():
    """The warm-up reads "ended by lap after 16:01 (planning estimate 17:00)" in every output."""
    result = run(VO2_PLAN, *build(vo2_segments(961), laps="steps"), RIDE_W)
    standard = format_execution(result, "", False, "standard")
    assert "[plan 1] warmup until lap press @ 110-175 W 'Einrollen bis Startpunkt' -> Interval 1 (WORK) from 0:00, " \
           "ended by lap after 16:01 (planning estimate 17:00), in range" in standard
    assert "vs plan" not in standard.split("[plan 2]", maxsplit=1)[0]
    assert "Plan: 11 steps, ≈54:00 (incl. 17:00 planning estimate of 1 lap-press step) planned" in standard
    assert "shorter than planned" not in standard and "longer than planned" not in standard
    compact = format_execution(result, "", False, "compact")
    assert "[plan 1] warmup until lap press @ 110-175 W: ended by lap after 16:01 (planning estimate 17:00), 150 W" in compact
    assert "[plan 2] work 3:00 @ 275-285 W: 3:00, 285 W" in compact
    finding = _execution_findings(result)[0]
    assert "steps with clear deviations: 0; lap-press steps (open length, not judged): plan 1 warmup ended by lap after 16:01 " \
           "(planning estimate 17:00)" in finding


def test_timed_warmup_still_judged():
    """Counter-case: the same session with a timed 17 min warm-up keeps the duration deviation and
    the start shift of the following steps."""
    plan = [dict(VO2_PLAN[0], until_lap_press=False)] + VO2_PLAN[1:]
    result = run(plan, *build(vo2_segments(720), laps="steps"), RIDE_W)
    assert "5:00 shorter than planned" in deviation_texts(result)
    assert "started 5:00 earlier than the plan timeline" in deviation_texts(result)
    assert "lap_press" not in result["rows"][0] and result["summary"]["lap_press_steps"] == 0


def test_lap_press_warmup_with_device_auto_laps_ends_at_the_press():
    """With 1 km auto-laps (counter reset at the press) the warm-up runs to the lap press, not to the
    last full auto-lap, whatever its length; the timed reps after it are exact."""
    plan = [{"duration": 600, "warmup": True, **LAP, **pc(70, 78)},
            {"reps": 3, "steps": [{"duration": 180, **pc(103, 108)}, {"duration": 120, **pc(65, 75)}]},
            {"duration": 600, "cooldown": True, **pc(70, 78)}]
    for warmup_s in (420, 600, 1200):
        segments = [(warmup_s, None, 0.74 * T, 140)] + [(180, None, 1.055 * T, 165), (120, None, 0.70 * T, 145)] * 3
        segments.append((600, None, 0.74 * T, 140))
        result = run(plan, *build_reset(segments, [warmup_s]), RUN)
        rows, summary = result["rows"], result["summary"]
        assert summary["auto_laps"] is not None and summary["matched"] == 8, warmup_s
        assert rows[0]["metrics"]["moving_time_s"] == warmup_s and rows[0]["lap_press"]["ended_by"] == "lap"
        assert all(abs(r["metrics"]["moving_time_s"] - r["planned"]["duration"]) <= 3 for r in rows[1:]), warmup_s
        assert all(abs(r.get("start_offset_s") or 0) <= 3 for r in rows), warmup_s
        assert summary["steps_with_deviations"] == 0, warmup_s


def test_lap_press_after_a_timed_step_inside_auto_laps():
    """Carried-over boundary: the timed warm-up's end inside an auto-lap is still placed at the
    intensity change, the lap-press step keeps its lap-press end, and the hill reps start there."""
    plan = [{"duration": 600, "warmup": True, **pc(70, 78)}, {"duration": 300, **LAP, **pc(65, 75)},
            {"reps": 4, "steps": [{"duration": 120, **pc(105, 112)}, {"duration": 120, **pc(55, 65)}]},
            {"duration": 600, "cooldown": True, **pc(70, 78)}]
    for easy_s in (200, 480, 900):
        segments = [(600, None, 0.74 * T, 140), (easy_s, None, 0.70 * T, 138)] + [(120, None, 1.08 * T, 168), (120, None, 0.60 * T, 145)] * 4
        segments.append((600, None, 0.74 * T, 140))
        result = run(plan, *build_reset(segments, [600 + easy_s]), RUN)
        assert result["summary"]["auto_laps"] is not None
        warmup, easy, first_rep = result["rows"][:3]
        assert warmup["metrics"]["moving_time_s"] == 600 and warmup["carried_over"][0]["given_to"] == "next", easy_s
        assert easy["end_index"] == 600 + easy_s and easy["lap_press"]["elapsed_s"] == easy_s, easy_s
        assert first_rep["start_time"] == 600 + easy_s and abs(first_rep["start_offset_s"]) < 1, easy_s
        assert not [t for r in result["rows"][:4] for t in (d["text"] for d in r["deviations"]) if "planned" in t or "timeline" in t]


def test_lap_press_recoveries_inside_repeats():
    """Lap-press recoveries before each threshold rep (3:00, 8:00, 5:30 vs a 5:00 estimate), with
    step laps and with 5 min time auto-laps: every rep is exact and on the plan timeline."""
    plan = [{"duration": 900, "warmup": True, **pw(50, 65)},
            {"reps": 3, "steps": [{"duration": 300, **LAP, **pw(45, 60)}, {"duration": 600, **pw(95, 100)}]},
            {"duration": 600, "cooldown": True, **pw(50, 60)}]
    segments = [(900, 0.58 * FTP, None, 120)]
    presses, clock = [], 900
    for recovery in (180, 480, 330):
        segments += [(recovery, 0.52 * FTP, None, 120), (600, 0.97 * FTP, None, 160)]
        presses += [clock, clock + recovery]
        clock += recovery + 600
    segments.append((600, 0.55 * FTP, None, 125))
    for streams, intervals in (build(segments, laps="steps"), build_reset(segments, presses + [clock], lap_m=1500.0)):
        result = run(plan, streams, intervals, RIDE)
        rows = result["rows"]
        assert [r["lap_press"]["elapsed_s"] for r in rows if r.get("lap_press")] == [180, 480, 330]
        assert all(r["metrics"]["moving_time_s"] == 600 and abs(r["start_offset_s"]) < 1 for r in rows if r["planned"]["kind"] == "work")
        assert result["summary"]["steps_with_deviations"] == 0 and result["summary"]["work_steps_in_range"] == "3/3"


def test_planned_step_map_treats_lap_press_as_open():
    """Interval listing without streams: a lap-press step longer than its estimate is not "beyond
    the plan" and is named with its actual length and estimate."""
    _, intervals = build(vo2_segments(1500), laps="steps")
    mapping = planned_step_map(plan_steps(VO2_PLAN, RIDE_W), intervals)
    assert mapping[0]["beyond_plan_s"] is None and mapping[0]["lap_press"] is True
    assert planned_step_text(mapping[0]) == "plan step 1 warmup until lap press, ended by lap after 25:00 (planning estimate 17:00)"
    assert planned_step_text(mapping[1]) == "plan step 2 work 3:00"


def test_lap_layout_ignores_the_estimate():
    """An estimate equal to the auto-lap length does not turn device auto-laps into lap presses."""
    plan = [{"duration": 1200, **pw(55, 65)}, {"duration": 300, **LAP, **pw(55, 65)}, {"duration": 1200, **pw(55, 65)}]
    _, intervals = build([(2700, 0.6 * FTP, None, 130)], laps="time", lap_s=300)
    for interval in intervals:
        interval["distance"] = None  # laps by time only
    spans = _spans(intervals, Profile([]))
    assert lap_layout(spans, plan_steps(plan, RIDE)) is not None
    timed = [dict(step, until_lap_press=False) for step in plan]
    assert lap_layout(spans, plan_steps(timed, RIDE)) is None  # unchanged for timed steps


def test_last_lap_press_step_ends_with_the_recording():
    """A cool-down until lap press that runs to the end of the activity is open as well."""
    plan = [{"duration": 600, "warmup": True, **pw(50, 65)}, {"duration": 600, **pw(90, 95)},
            {"duration": 300, "cooldown": True, "until_lap_press": "true", **pw(50, 60)}]
    segments = [(600, 0.58 * FTP, None, 120), (600, 0.92 * FTP, None, 155), (1100, 0.55 * FTP, None, 125)]
    result = run(plan, *build(segments, laps="steps"), RIDE)
    cooldown = result["rows"][-1]
    assert cooldown["lap_press"]["ended_by"] == "end of recording" and cooldown["metrics"]["moving_time_s"] == 1100
    assert "overrun" not in cooldown and result["extension"] is None and result["summary"]["steps_with_deviations"] == 0
    assert "ended with the recording after 18:20 (planning estimate 5:00)" in format_execution(result, "", False, "compact")


def _random_lap_press_run(rnd):
    """A random run plan with timed and lap-press steps (0.3 to 3 times their estimate), its segments
    and the sample indices of the step ends."""
    levels = [rnd.choice([(50, 60), (70, 78), (88, 93), (100, 108)]) for _ in range(rnd.randint(3, 7))]
    plan, segments, presses, clock = [], [], [], 0
    for position, level in enumerate(levels):
        if position > 0 and levels[position - 1] == level:
            level = (60, 68) if level != (60, 68) else (80, 86)  # neighbours differ: the laps decide
            levels[position] = level
        lap_press = rnd.random() < 0.4
        estimate = rnd.choice([180, 300, 600])
        actual = round(estimate * rnd.choice([0.3, 0.6, 1.0, 1.7, 3.0])) if lap_press else estimate
        plan.append({"duration": estimate, **(LAP if lap_press else {}), **pc(*level)})
        segments.append((actual, None, (sum(level) / 200) * T, 140))
        clock += actual
        presses.append(clock)
    return plan, segments, presses


def test_random_lap_press_workouts_keep_the_timed_steps_exact():
    """Fuzz: lap-press steps anywhere in the plan, lasting 0.3 to 3 times their estimate, never shift
    or downgrade a timed step (step laps, and 500 m auto-laps reset at every press)."""
    rnd = random.Random(11)
    for case in range(30):
        plan, segments, presses = _random_lap_press_run(rnd)
        for streams, intervals in (build(segments, laps="steps", seed=case), build_reset(segments, presses[:-1], lap_m=500.0, seed=case)):
            result = run(plan, streams, intervals, RUN)
            rows = result["rows"]
            assert result["summary"]["matched"] == len(plan), case
            for row, (actual, *_rest) in zip(rows, segments, strict=True):
                assert abs(row["metrics"]["moving_time_s"] - actual) <= 3, case
                assert abs(row.get("start_offset_s") or 0) <= 3, case
                assert not any("planned" in d["text"] or "timeline" in d["text"] for d in row["deviations"]), case
                assert bool(row.get("lap_press")) == bool(row["planned"]["lap_press"]), case
