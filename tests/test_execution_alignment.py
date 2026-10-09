"""
Regression tests for the plan-vs-execution alignment (review findings ANA-1/2/3/4/10/11/12, API-6):
device auto-laps that split one step into many intervals, lap boundaries that do not coincide with
step boundaries, open-ended targets, distance-based steps, easy aerobic run steps, the Pw:HR drift
sign, the NP of split steps and events without a workout document.
"""

import asyncio
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.server import analyze_workout_execution  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.execution import (  # pylint: disable=wrong-import-position
    Profile,
    _pw_hr_drift,
    _time_in_target,
    adherence,
    align_spans,
    analyze,
    format_execution,
    plan_steps,
    planned_step_map,
    resolve_target,
)
from intervals_mcp_server.utils.streams import normalized_power  # pylint: disable=wrong-import-position
from tests.sample_data import EXECUTION_ACTIVITY, EXECUTION_INTERVALS, EXECUTION_STREAMS  # pylint: disable=wrong-import-position
from tests.test_coaching_tools import _install_router  # pylint: disable=wrong-import-position
from tests.test_execution_split import CONTEXT as RIDE_CONTEXT, build_ride  # pylint: disable=wrong-import-position

RUN_CONTEXT = {"threshold_pace": 4.0, "lthr": 165, "pace_zones": [77.5, 87.7, 94.3, 100.0, 103.4, 111.5, 999.0],
               "activity_type": "Run"}
ZONE_CONTEXT = {"ftp": 234, "power_zones": [55, 75, 90, 105, 120, 150, 999], "lthr": 165,
                "hr_zones": [133, 147, 153, 164, 169, 174, 188], "threshold_pace": 4.0,
                "pace_zones": [77.5, 87.7, 94.3, 100.0, 103.4, 111.5, 999.0]}


def build_run(speeds, lap_m=1000.0, lap_at=None):
    """1 Hz run from per-second speeds with a lap every ``lap_m`` metres (device auto-lap) and at
    the sample indices in ``lap_at``; returns (streams, intervals) like Intervals.icu."""
    distance, total = [], 0.0
    for speed in speeds:
        distance.append(total)
        total += speed
    cuts, next_lap = [], lap_m
    for index, metres in enumerate(distance):
        if metres >= next_lap:
            cuts.append(index)
            next_lap += lap_m
    cuts = sorted(set(cuts) | set(lap_at or []))
    bounds = [0] + [c for c in cuts if 0 < c < len(speeds)] + [len(speeds)]
    intervals = []
    for start, end in zip(bounds, bounds[1:], strict=False):
        metres = (distance[end] if end < len(distance) else total) - distance[start]
        intervals.append({"type": "WORK", "start_index": start, "end_index": end, "start_time": start, "end_time": end,
                          "elapsed_time": end - start, "moving_time": end - start, "distance": metres,
                          "average_speed": metres / (end - start)})
    streams = [{"type": "time", "data": list(range(len(speeds)))}, {"type": "velocity_smooth", "data": list(speeds)},
               {"type": "distance", "data": distance}]
    return streams, intervals


TEMPO_PLAN = [
    {"duration": 600, "warmup": True, "_pace": {"start": 2.6, "end": 2.9}},
    {"duration": 1800, "_pace": {"start": 3.5, "end": 3.7}},
    {"duration": 600, "cooldown": True, "_pace": {"start": 2.6, "end": 2.9}},
]


def test_auto_laps_split_every_step_into_many_intervals():
    """ANA-1: a perfectly executed 10/30/10 min run with 1 km auto-laps matches all three steps
    (each spanning several laps) instead of pushing the plan to the end of the activity."""
    streams, intervals = build_run([2.75] * 600 + [3.6] * 1800 + [2.75] * 600)
    planned = plan_steps(TEMPO_PLAN, RUN_CONTEXT)
    alignment = align_spans(planned, intervals, Profile(streams))
    assert [p for p, _ in alignment] == [0, 1, 2]
    assert len(alignment[1][1]) >= 6  # the tempo spans six or seven 1 km laps
    result = analyze(planned, intervals, streams, context=RUN_CONTEXT)
    summary = result["summary"]
    assert summary["matched"] == 3 and summary["unmatched_planned"] == 0 and result["pre_plan"] is None
    assert summary["steps_with_deviations"] == 0 and summary["work_steps_in_range"] == "1/1"
    warmup, tempo = result["rows"][0], result["rows"][1]
    assert warmup["metrics"]["moving_time_s"] == 600 and "overrun" not in warmup  # the straddling lap is carried over
    assert abs(tempo["start_time"] - 600) <= 1
    assert tempo["adherence"]["status"] == "in range" and tempo["metrics"]["time_in_target_pct"] > 95
    text = format_execution(result, "", True, "compact")
    assert "Riding before the first planned step" not in text and "not executed" not in text


def test_long_unmatched_block_before_the_plan_is_not_cheap():
    """ANA-1: the alignment never prefers 30+ minutes "before the plan" over matching long steps."""
    streams, intervals = build_run([2.75] * 600 + [3.6] * 1800 + [2.75] * 600, lap_m=500)
    planned = plan_steps(TEMPO_PLAN, RUN_CONTEXT)
    result = analyze(planned, intervals, streams, context=RUN_CONTEXT)
    assert result["summary"]["matched"] == 3 and result["pre_plan"] is None


def test_lap_boundary_inside_a_step_carries_time_over_instead_of_paired_deviations():
    """ANA-2: laps of 6:30 with a plan of 10 + 20 min: the 3 min after the 10th minute belong to the
    second step; no '+3:00 extra time inside the plan' / '3:00 shorter than planned' pair."""
    plan = [{"duration": 600, "warmup": True, "_pace": {"start": 2.32, "end": 2.52}},
            {"duration": 1200, "_pace": {"start": 2.45, "end": 2.65}}]
    streams, intervals = build_run([2.5] * 1800, lap_m=975)
    planned = plan_steps(plan, {"threshold_pace": 3.2258})
    result = analyze(planned, intervals, streams, context={"threshold_pace": 3.2258, "activity_type": "Run"})
    warmup, easy = result["rows"]
    assert warmup["metrics"]["moving_time_s"] == 600 and "overrun" not in warmup
    assert easy["metrics"]["moving_time_s"] == 1200
    assert easy["carried_over"] == [{"from": "previous", "seconds": 180.0}]
    assert result["summary"]["steps_with_deviations"] == 0 and result["summary"]["extra_time_inside_plan_s"] == 0
    assert result["summary"]["boundaries_off_lap"] == 1
    text = format_execution(result, "", True)
    assert "includes the last 3:00 of the previous step's lap (lap and step boundaries differ)" in text
    assert "shorter than planned" not in text
    # Interval listing without streams: the time is carried over, not reported as beyond the plan.
    mapping = planned_step_map(planned, intervals)
    assert mapping[0]["carried_to_next_s"] == 180.0 and mapping[0]["beyond_plan_s"] is None


def test_real_overrun_with_step_laps_is_still_flagged():
    """ANA-2 counter-case: laps at step boundaries and a rest really 2 min too long stays a deviation."""
    segments = [("RECOVERY", 1020, 150, 120), ("WORK", 600, 240, 150), ("RECOVERY", 360, 120, 130),
                ("WORK", 420, 242, 155), ("RECOVERY", 240, 120, 130), ("WORK", 600, 244, 158), ("RECOVERY", 600, 110, 120)]
    streams, intervals = build_ride(segments)
    plan = [{"duration": 1020, "warmup": True, "power": {"start": 115, "end": 175, "units": "w"}}] + [
        {"duration": d, "power": {"start": lo, "end": hi, "units": "w"}}
        for d, lo, hi in ((600, 238, 242), (240, 110, 130), (600, 240, 244), (240, 110, 130), (600, 242, 246))
    ] + [{"duration": 600, "cooldown": True, "power": {"start": 125, "end": 90, "units": "w"}}]
    result = analyze(plan_steps(plan, RIDE_CONTEXT), intervals, streams, context=RIDE_CONTEXT)
    assert result["rows"][2]["overrun"]["moving_time_s"] == 120
    assert any(d["text"] == "3:00 shorter than planned" for d in result["rows"][3]["deviations"])
    assert result["summary"]["boundaries_off_lap"] == 0


def test_open_ended_targets_are_lower_bounds():
    """ANA-3: top zone and start-only targets have no upper limit."""
    z7 = resolve_target({"power": {"value": 7, "units": "power_zone"}}, ZONE_CONTEXT)
    assert z7["low"] == 351.0 and z7["high"] is None
    sprint = adherence(z7, {"avg_watts": 1.8 * 234})
    assert sprint["status"] == "in range" and sprint["offset_from_range_pct"] == 0.0 and sprint["open_ended"] is True
    assert _time_in_target([1.8 * 234] * 30, z7) == 100.0
    assert adherence(z7, {"avg_watts": 300})["status"] == "below"
    z67 = resolve_target({"power": {"start": 6, "end": 7, "units": "power_zone"}}, ZONE_CONTEXT)
    assert z67["high"] is None and adherence(z67, {"avg_watts": 400})["status"] == "in range"
    start_only = resolve_target({"power": {"start": 120, "units": "%ftp"}}, ZONE_CONTEXT)
    assert start_only["low"] == 280.8 and start_only["high"] is None
    assert resolve_target({"power": {"start": 6, "units": "power_zone"}}, ZONE_CONTEXT)["high"] is None
    pace_z7 = resolve_target({"pace": {"value": 7, "units": "pace_zone"}}, ZONE_CONTEXT)
    assert pace_z7["high"] is None and adherence(pace_z7, {"avg_speed_m_s": 5.2})["status"] == "in range"
    # single values stay point targets
    single = resolve_target({"power": {"value": 80, "units": "%ftp"}}, ZONE_CONTEXT)
    assert single["low"] == single["high"]
    # a sprint step at 180 % FTP is no deviation; the text shows the bound
    segments = [("RECOVERY", 600, 150, 120), ("WORK", 20, 421, 150), ("RECOVERY", 300, 120, 130)]
    streams, intervals = build_ride(segments)
    plan = [{"duration": 600, "warmup": True, "power": {"start": 50, "end": 70, "units": "%ftp"}},
            {"duration": 20, "power": {"value": 7, "units": "power_zone"}},
            {"duration": 300, "cooldown": True, "power": {"start": 45, "end": 55, "units": "%ftp"}}]
    result = analyze(plan_steps(plan, ZONE_CONTEXT), intervals, streams, context={**ZONE_CONTEXT, "activity_type": "Ride"})
    sprint_row = result["rows"][1]
    assert sprint_row["adherence"]["status"] == "in range" and not sprint_row["deviations"]
    text = format_execution(result, "")
    assert "work 0:20 @ 351 W or more" in text and "% of the lower bound" in text


def test_easy_run_step_is_work_and_recoveries_between_efforts_are_rest():
    """ANA-11: an easy aerobic (Z2) run step is not "rest"; Z1 targets and recoveries between clearly
    harder steps (also the last repetition before the cool-down) are."""
    steps = [
        {"duration": 600, "warmup": True, "pace": {"start": 72, "end": 78, "units": "%pace"}},
        {"duration": 1200, "pace": {"start": 76, "end": 82, "units": "%pace"}},
        {"reps": 2, "steps": [{"duration": 15, "pace": {"start": 105, "end": 115, "units": "%pace"}},
                              {"duration": 105, "pace": {"start": 70, "end": 76, "units": "%pace"}}]},
        {"duration": 420, "cooldown": True, "pace": {"start": 72, "end": 78, "units": "%pace"}},
    ]
    kinds = [s["kind"] for s in plan_steps(steps, ZONE_CONTEXT)]
    assert kinds == ["warmup", "work", "work", "rest", "work", "rest", "cooldown"]
    ride = [
        {"duration": 900, "warmup": True, "power": {"start": 50, "end": 65, "units": "%ftp"}},
        {"duration": 3600, "power": {"start": 60, "end": 70, "units": "%ftp"}},  # endurance block: work
        {"reps": 3, "steps": [{"duration": 480, "power": {"value": 95, "units": "%ftp"}},
                              {"duration": 240, "power": {"value": 60, "units": "%ftp"}}]},  # active recovery: rest
        {"duration": 600, "cooldown": True, "power": {"value": 50, "units": "%ftp"}},
    ]
    kinds = [s["kind"] for s in plan_steps(ride, ZONE_CONTEXT)]
    assert kinds == ["warmup", "work", "work", "rest", "work", "rest", "work", "rest", "cooldown"]
    assert [s["kind"] for s in plan_steps([{"duration": 300, "power": {"value": 50, "units": "%ftp"}}], ZONE_CONTEXT)] == ["rest"]


def test_distance_steps_keep_the_plan_clock_and_compare_distance():
    """ANA-12: a distance step adds its estimated duration to the plan clock; it is matched and
    compared on distance, so later steps are not flagged as started late."""
    plan = [{"duration": 600, "warmup": True, "_pace": {"start": 2.6, "end": 2.9}},
            {"distance": 3000, "_pace": {"start": 3.9, "end": 4.1}},
            {"duration": 300, "_pace": {"start": 2.4, "end": 2.8}},
            {"distance": 3000, "_pace": {"start": 3.9, "end": 4.1}},
            {"duration": 600, "cooldown": True, "_pace": {"start": 2.6, "end": 2.9}}]
    planned = plan_steps(plan, RUN_CONTEXT)
    assert planned[1]["est_duration"] == 750.0 and planned[2]["planned_start"] == 1350.0
    assert planned[4]["planned_start"] == 2400.0
    speeds = [2.75] * 600 + [4.0] * 750 + [2.6] * 300 + [3.75] * 800 + [2.75] * 600  # 2nd rep: 3000 m in 800 s
    streams, intervals = build_run(speeds, lap_m=10_000, lap_at=[600, 1350, 1650, 2450])
    result = analyze(planned, intervals, streams, context=RUN_CONTEXT)
    rows = result["rows"]
    assert result["summary"]["matched"] == 5 and result["summary"]["steps_with_deviations"] == 0
    assert rows[1]["distance_diff_m"] == 0 and rows[3]["planned_s_estimated"] == 800
    assert not any("plan timeline" in d["text"] for r in rows for d in r["deviations"])
    text = format_execution(result, "", True)
    assert "[plan 2] work 3.00 km @ 4:16/km to 4:04/km" in text and "+0 m vs plan" in text
    # a distance rep cut short is reported in metres
    short = [2.75] * 600 + [4.0] * 600 + [2.6] * 300 + [3.75] * 800 + [2.75] * 600
    streams, intervals = build_run(short, lap_m=10_000, lap_at=[600, 1200, 1500, 2300])
    rows = analyze(planned, intervals, streams, context=RUN_CONTEXT)["rows"]
    assert rows[1]["distance_diff_m"] == -600 and any(d["text"] == "600 m shorter than planned" for d in rows[1]["deviations"])


def test_pw_hr_drift_uses_the_intervals_decoupling_sign():
    """ANA-4: positive = HR rose relative to power, the same as Intervals.icu decoupling."""
    watts = [200] * 1200
    heart = [140] * 600 + [150] * 600
    assert round(_pw_hr_drift(watts, heart, 1200), 1) == 6.7
    segments = [("RECOVERY", 300, 150, 110), ("WORK", 1200, 240, 150, [(600, 240, 145), (600, 240, 155)]),
                ("RECOVERY", 300, 120, 120)]
    streams, intervals = build_ride(segments)
    plan = [{"duration": 300, "warmup": True, "power": {"start": 140, "end": 160, "units": "w"}},
            {"duration": 1200, "power": {"start": 235, "end": 245, "units": "w"}},
            {"duration": 300, "cooldown": True, "power": {"start": 110, "end": 130, "units": "w"}}]
    result = analyze(plan_steps(plan, RIDE_CONTEXT), intervals, streams, context=RIDE_CONTEXT)
    assert result["rows"][1]["metrics"]["pw_hr_drift_pct"] > 6
    assert result["summary"]["pw_hr_drift_convention"].startswith("positive = heart rate rose")
    text = format_execution(result, "")
    assert "Pw:HR drift +6.5%" in text and "Pw:HR drift: positive = heart rate rose relative to power" in text
    # An exact Intervals.icu interval keeps Intervals' own decoupling value.
    intervals[1]["decoupling"] = 6.1
    result = analyze(plan_steps(plan, RIDE_CONTEXT), intervals, streams, context=RIDE_CONTEXT)
    assert result["rows"][1]["metrics"]["pw_hr_drift_pct"] == 6.1


def test_np_of_a_split_step_uses_the_activity_wide_rolling_mean():
    """ANA-10: the NP of a step that is not an exact interval includes the 30 s before it (Intervals'
    method), so a recovery right after a hard effort is not under-reported."""
    segments = [("WORK", 600, 300, 150), ("RECOVERY", 300, 100, 120), ("RECOVERY", 300, 100, 120)]
    streams, intervals = build_ride(segments)
    plan = [{"duration": 600, "power": {"start": 290, "end": 310, "units": "w"}},
            {"duration": 600, "power": {"start": 90, "end": 110, "units": "w"}}]
    result = analyze(plan_steps(plan, RIDE_CONTEXT), intervals, streams, context=RIDE_CONTEXT)
    rest = result["rows"][1]
    assert rest["merged"] is True and rest["metrics"]["source"] == "streams"
    isolated = normalized_power(streams[0]["data"], streams[1]["data"], 600, 1200)
    assert isolated is not None and round(isolated) == 100  # the slice alone forgets the effort before it
    watts = streams[1]["data"]
    rolling = [sum(watts[max(0, i - 29): i + 1]) / len(watts[max(0, i - 29): i + 1]) for i in range(len(watts))]
    expected = (sum(r ** 4 for r in rolling[600:1200]) / 600) ** 0.25
    assert round(rest["metrics"]["normalized_watts"], 3) == round(expected, 3) and expected > 115


def test_alignment_stays_fast_with_many_laps():
    """Unbounded merges stay cheap and robust: 2:40 h with 100 m laps (about 300 intervals), a
    10-step plan whose boundaries fall inside laps, and 40 min of running after the plan."""
    plan = [{"duration": 1200, "warmup": True, "_pace": {"start": 2.6, "end": 2.9}}]
    plan += [{"duration": 900, "_pace": {"start": 3.5, "end": 3.7}}, {"duration": 300, "_pace": {"start": 2.3, "end": 2.6}}] * 4
    plan += [{"duration": 1200, "cooldown": True, "_pace": {"start": 2.6, "end": 2.9}}]
    speeds = [2.75] * 1200 + ([3.6] * 900 + [2.45] * 300) * 4 + [2.75] * 1200 + [3.0] * 2400
    streams, intervals = build_run(speeds, lap_m=100)
    assert len(intervals) > 250
    began = time.perf_counter()
    result = analyze(plan_steps(plan, RUN_CONTEXT), intervals, streams, context=RUN_CONTEXT)
    assert time.perf_counter() - began < 10
    summary = result["summary"]
    assert summary["matched"] == 10 and summary["steps_with_deviations"] == 0 and summary["work_steps_in_range"] == "4/4"
    assert result["pre_plan"] is None and abs(result["extension"]["duration_s"] - 2400) < 200


def test_event_without_workout_document_does_not_crash(monkeypatch):
    """API-6: a paired event with "workout_doc": null (race, note) is analysed as intervals only."""
    activity = dict(EXECUTION_ACTIVITY, paired_event_id=77)
    _install_router(monkeypatch, {"/activity/": activity, "/streams": EXECUTION_STREAMS, "/intervals": EXECUTION_INTERVALS,
                                  "/events/77": {"id": 77, "category": "RACE_A", "name": "Race", "workout_doc": None}})
    text = asyncio.run(analyze_workout_execution("i1"))
    assert "No planned workout:" in text
    text = asyncio.run(analyze_workout_execution("i1", event_id="77"))
    assert "No planned workout:" in text


def test_report_finding_compares_in_the_unit_of_the_targets():
    """ANA-7: the report's key finding compares pace targets with the pace and HR targets with HR,
    never as watts; mixed target kinds are not combined."""
    from intervals_mcp_server.tools.report import _execution_findings  # pylint: disable=import-outside-toplevel

    streams, intervals = build_run([2.75] * 600 + [3.6] * 1800 + [2.75] * 600)
    streams.append({"type": "watts", "data": [300] * 3000})
    result = analyze(plan_steps(TEMPO_PLAN, RUN_CONTEXT), intervals, streams, context=RUN_CONTEXT)
    finding = _execution_findings(result)[0]
    assert "work steps 4:38/km vs 4:46/km to 4:30/km planned" in finding and " W " not in finding
    hr_plan = [{"duration": 600, "warmup": True, "hr": {"start": 110, "end": 130, "units": "bpm"}},
               {"duration": 1200, "hr": {"start": 145, "end": 155, "units": "bpm"}},
               {"duration": 600, "cooldown": True, "hr": {"start": 110, "end": 130, "units": "bpm"}}]
    streams, intervals = build_ride([("RECOVERY", 600, 150, 120), ("WORK", 1200, 220, 151), ("RECOVERY", 600, 120, 118)])
    result = analyze(plan_steps(hr_plan, RIDE_CONTEXT), intervals, streams, context=RIDE_CONTEXT)
    assert "work steps 151 bpm vs 145-155 bpm planned" in _execution_findings(result)[0]
    mixed = [hr_plan[0], {"duration": 600, "power": {"start": 215, "end": 225, "units": "w"}}, hr_plan[1], hr_plan[2]]
    streams, intervals = build_ride([("RECOVERY", 600, 150, 120), ("WORK", 600, 220, 140), ("WORK", 1200, 220, 151),
                                     ("RECOVERY", 600, 120, 118)])
    result = analyze(plan_steps(mixed, RIDE_CONTEXT), intervals, streams, context=RIDE_CONTEXT)
    assert "work steps" not in _execution_findings(result)[0]


def test_swim_pace_is_rendered_per_100_m():
    """ANA-16: pace targets and averages of a swim are shown per 100 m, not per km."""
    plan = [{"duration": 300, "_pace": {"start": 0.95, "end": 1.05}}, {"duration": 300, "_pace": {"start": 1.1, "end": 1.2}}]
    streams, intervals = build_run([1.0] * 300 + [1.15] * 300, lap_m=10_000, lap_at=[300])
    result = analyze(plan_steps(plan, {}), intervals, streams, context={"activity_type": "Swim"})
    text = format_execution(result, "", True)
    assert "/100 m" in text and "/km" not in text
