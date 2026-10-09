"""
Regression tests for the planned-vs-executed analysis with extended rides (utils.execution and
analyze_workout_execution / get_activity_report): planned steps are capped in time, a longer
interval is split logically (planned part vs remainder), everything after the plan is reported
separately with its own metrics and extra efforts, durations use moving time, a lap-split
effort is matched as one step and clear deviations are flagged instead of a perfect score.
"""

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.server import analyze_workout_execution, get_activity_report  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.execution import (  # pylint: disable=wrong-import-position
    Profile,
    Tolerances,
    align_spans,
    analyze,
    format_execution,
    plan_steps,
)
from tests.test_coaching_tools import _install_router  # pylint: disable=wrong-import-position

CONTEXT = {"ftp": 234, "lthr": 165, "activity_type": "Ride"}

# The plan of a 65-minute 3 x 10 min threshold workout (watts as in the calendar event).
PLAN_65 = {"steps": [
    {"duration": 1020, "warmup": True, "ramp": True, "power": {"start": 115, "end": 175, "units": "w"}},
    {"duration": 600, "power": {"start": 238, "end": 242, "units": "w"}, "cadence": {"start": 85, "end": 95, "units": "rpm"}},
    {"duration": 240, "power": {"start": 110, "end": 130, "units": "w"}},
    {"duration": 600, "power": {"start": 240, "end": 244, "units": "w"}, "cadence": {"start": 85, "end": 95, "units": "rpm"}},
    {"duration": 240, "power": {"start": 110, "end": 130, "units": "w"}},
    {"duration": 600, "power": {"start": 242, "end": 246, "units": "w"}, "cadence": {"start": 85, "end": 95, "units": "rpm"}},
    {"duration": 600, "cooldown": True, "ramp": True, "power": {"start": 125, "end": 90, "units": "w"}},
]}


def build_ride(segments, gaps=None):  # pylint: disable=too-many-locals
    """Streams and Intervals.icu intervals from (kind, samples, watts, hr[, split]) segments.

    ``split`` lists (samples, watts, hr) parts recorded inside one interval; ``gaps`` maps a
    sample index to a recording pause in seconds after it.
    """
    gaps = gaps or {}
    time, watts, heart, cadence, intervals = [], [], [], [], []
    clock, index = 0, 0
    for kind, samples, power, hr, *parts in segments:
        start_index, start_time = index, clock
        pieces = parts[0] if parts else [(samples, power, hr)]
        for count, piece_power, piece_hr in pieces:
            for _ in range(count):
                time.append(clock)
                watts.append(piece_power)
                heart.append(piece_hr)
                cadence.append(90)
                clock += 1 + gaps.get(index, 0)
                index += 1
        mean_power = round(sum(watts[start_index:index]) / (index - start_index))
        intervals.append({"type": kind, "label": None, "start_index": start_index, "end_index": index, "start_time": start_time,
                          "end_time": clock, "elapsed_time": clock - start_time, "moving_time": index - start_index,
                          "average_watts": mean_power, "max_watts": max(watts[start_index:index]),
                          "average_heartrate": round(sum(heart[start_index:index]) / (index - start_index)),
                          "max_heartrate": max(heart[start_index:index])})
    streams = [
        {"type": "time", "custom": False, "data": time},
        {"type": "watts", "custom": False, "data": watts},
        {"type": "heartrate", "custom": False, "data": heart},
        {"type": "cadence", "custom": False, "data": cadence},
        {"type": "Stamina", "custom": True, "data": [100 - i * 0.006 for i in range(len(time))]},
        {"type": "Elapsedtime", "custom": True, "data": list(time)},
        {"type": "GarminRunEffectiveness", "custom": True, "data": [(i % 7) * 1.0 for i in range(len(time))]},
    ]
    return streams, intervals


# Executed like the real ride: cool-down continued (17:58 at first 128 W, then 178 W), a 50 s sprint
# and 13:19 of riding home; a 13 s recording pause in the second recovery.
EXTENDED_SEGMENTS = [
    ("RECOVERY", 1019, 170, 122), ("WORK", 602, 249, 153), ("RECOVERY", 238, 121, 128), ("WORK", 600, 249, 156),
    ("RECOVERY", 243, 130, 131), ("WORK", 601, 253, 160), ("RECOVERY", 1078, 0, 0, [(600, 128, 122), (478, 178, 130)]),
    ("WORK", 50, 432, 159), ("RECOVERY", 799, 180, 135),
]
EXT_STREAMS, EXT_INTERVALS = build_ride(EXTENDED_SEGMENTS, gaps={2469: 12})


def test_extended_cooldown_is_capped_and_the_rest_reported_separately():
    """Regression (3 x 10 min ride extended to 87 min): all 7 steps matched, the 17:58 cool-down is
    evaluated on its planned 10 minutes and everything after 65 minutes is additional training."""
    planned = plan_steps(PLAN_65["steps"], CONTEXT)
    result = analyze(planned, EXT_INTERVALS, EXT_STREAMS, context=CONTEXT)
    summary = result["summary"]
    assert summary["matched"] == 7 and summary["unmatched_planned"] == 0 and summary["unmatched_intervals"] == 0
    cooldown = result["rows"][6]
    assert cooldown["metrics"]["moving_time_s"] == 600 and cooldown["metrics"]["source"] == "streams"
    assert round(cooldown["metrics"]["avg_watts"]) == 128
    assert cooldown["adherence"]["status"] == "in range"  # 128 W vs 90-125 W: +2.4 %, within tolerance
    assert cooldown["overrun"]["moving_time_s"] == 478 and cooldown["overrun"]["counted_as"] == "additional training after the plan"
    assert not [d for d in cooldown["deviations"] if d["severity"] == "deviation"]  # a longer cool-down is not "poor compliance"
    plan_end = cooldown["end_time"]
    assert plan_end == 1019 + 602 + 238 + 600 + 255 + 601 + 600
    assert summary["plan_part_actual_s"] == plan_end
    extension = result["extension"]
    assert extension["start_time"] == plan_end and extension["duration_s"] == 478 + 50 + 799
    assert summary["extension_s"] == 1327 and summary["extended_beyond_plan"] is True
    assert extension["remainder_of_last_step"]["moving_time_s"] == 478
    assert [e["average_watts"] for e in extension["efforts"]] == [432]
    assert extension["metrics"]["work_kj"] == round((478 * 178 + 50 * 432 + 799 * 180) / 1000, 1)
    assert summary["extension_work_share_pct"] > 20
    assert extension["metrics"]["estimated_load"] > 0
    # work steps: averages 249/249/253 W are within the ±5 % tolerance but above the exact range
    assert summary["work_steps_in_range"] == "3/3" and summary["work_steps_inside_exact_range"] == "0/3"
    assert result["rows"][1]["adherence"]["offset_from_range_pct"] == 2.9
    # the 13 s recording pause is excluded from the step duration (moving time)
    assert result["rows"][4]["metrics"]["paused_s"] == 12 and result["rows"][4]["duration_diff_s"] == 3
    # clock counters and running metrics on a ride are not reported per step
    assert set(result["hidden_streams"]) == {"Elapsedtime", "GarminRunEffectiveness"}
    assert set(result["rows"][1]["metrics"]["custom_streams"]) == {"Stamina"}
    text = format_execution(result, "header")
    assert "[plan 7] cooldown 10:00 @ 90-125 W -> Interval 7 (RECOVERY) first 10:00 of 17:58 from 55:15" in text
    assert "remaining 7:58 (1:05:15-1:13:13, avg 178 W, HR 130 bpm) counted as additional training after the plan" in text
    assert "The activity was extended beyond the plan: plan part 1:05:15, additional training 22:07" in text
    assert "Additional training after the plan: 22:07 from 1:05:15 to 1:27:22 (2 interval(s), starting with the last 7:58 of planned step 7 (cooldown))" in text
    assert "extra effort: 0:50 from 1:13:13 at avg 432 W (max 432 W), HR 159 (max 159) bpm [Intervals.icu WORK interval 8]" in text
    assert "in range (104% of target, +2.9% above the range, within the ±5% tolerance)" in text
    assert "Custom streams not shown per step: Elapsedtime (clock/counter stream), GarminRunEffectiveness (running/walking metric on a Ride activity)" in text


def test_middle_overrun_short_step_and_no_perfect_score():
    """A rest 2 min too long is split (extra time inside the plan); a work step 3 min short and a
    work step far above target are clear deviations, so the summary is not a perfect score."""
    segments = [
        ("RECOVERY", 1020, 150, 120), ("WORK", 600, 240, 150), ("RECOVERY", 360, 120, 130),
        ("WORK", 420, 242, 155), ("RECOVERY", 240, 120, 130), ("WORK", 600, 290, 170), ("RECOVERY", 600, 110, 120),
    ]
    streams, intervals = build_ride(segments)
    result = analyze(plan_steps(PLAN_65["steps"], CONTEXT), intervals, streams, context=CONTEXT)
    rows, summary = result["rows"], result["summary"]
    assert summary["matched"] == 7
    assert rows[2]["overrun"]["counted_as"] == "extra time inside the plan" and rows[2]["overrun"]["moving_time_s"] == 120
    assert any("longer than planned" in d["text"] and d["severity"] == "deviation" for d in rows[2]["deviations"])
    assert summary["extra_time_inside_plan_s"] == 120
    assert any(d["text"] == "3:00 shorter than planned" for d in rows[3]["deviations"])
    assert rows[5]["adherence"]["status"] == "above"
    assert summary["steps_with_deviations"] == 3
    assert summary["work_steps_in_range"] == "2/3"
    assert result["extension"] is None
    text = format_execution(result, "")
    assert "steps with clear deviations 3" in text and "Extra time inside the plan (steps longer than planned): 2:00." in text
    compact = format_execution(result, "", detail_level="compact")
    assert "[plan 4] work 10:00 @ 240-244 W: 7:00, 242 W" in compact and "| 3:00 shorter than planned" in compact
    assert "Stamina" not in compact


def test_lap_split_effort_and_pre_plan_riding():
    """A work step recorded as two laps of the same intensity is one step; riding before the plan is reported."""
    segments = [
        ("RECOVERY", 600, 140, 110), ("RECOVERY", 1020, 150, 120), ("WORK", 300, 240, 150), ("WORK", 300, 241, 152),
        ("RECOVERY", 240, 120, 130), ("WORK", 600, 242, 155), ("RECOVERY", 240, 120, 130), ("WORK", 600, 244, 158),
        ("RECOVERY", 600, 110, 120),
    ]
    streams, intervals = build_ride(segments)
    planned = plan_steps(PLAN_65["steps"], CONTEXT)
    spans = align_spans(planned, intervals, Profile(streams))
    assert spans[0] == (None, [0]) and spans[2] == (1, [2, 3])
    result = analyze(planned, intervals, streams, context=CONTEXT)
    assert result["summary"]["merged_steps"] == 1 and result["summary"]["matched"] == 7
    assert result["rows"][1]["merged"] is True and result["rows"][1]["label"] == "Interval 3-4"
    assert result["pre_plan"]["duration_s"] == 600 and result["summary"]["pre_plan_s"] == 600
    assert "Riding before the first planned step: 10:00" in format_execution(result, "")
    # A surge inside a rest is never merged into the rest.
    surge = [dict(i) for i in intervals[1:3]] + [{"type": "WORK", "elapsed_time": 30, "average_watts": 500}] + intervals[3:]
    assert (None, [2]) in align_spans(planned, surge)


def test_tolerances_pause_and_start_shift():
    """Pauses inside a step are tolerated up to pause_s; steps far from the plan timeline are flagged."""
    segments = [("RECOVERY", 1020, 150, 120), ("WORK", 600, 240, 150), ("RECOVERY", 240, 120, 130),
                ("WORK", 600, 242, 155), ("RECOVERY", 240, 120, 130), ("WORK", 600, 244, 158), ("RECOVERY", 600, 110, 120)]
    streams, intervals = build_ride(segments, gaps={1100: 90, 2000: 200})
    result = analyze(plan_steps(PLAN_65["steps"], CONTEXT), intervals, streams, context=CONTEXT)
    work = result["rows"][1]
    assert work["metrics"]["paused_s"] == 90 and work["duration_diff_s"] == 0  # moving time matches the plan
    assert any("recording paused for 1:30" in d["text"] for d in work["deviations"])
    assert any("later than the plan timeline" in d["text"] for d in result["rows"][4]["deviations"])
    relaxed = analyze(plan_steps(PLAN_65["steps"], CONTEXT), intervals, streams, context=CONTEXT,
                      tolerances=Tolerances(pause_s=120, start_shift_s=600))
    assert not relaxed["rows"][1]["deviations"] and not relaxed["rows"][4]["deviations"]
    tight = analyze(plan_steps(PLAN_65["steps"], CONTEXT), intervals, streams, context=CONTEXT,
                    tolerances=Tolerances(duration_pct=0, duration_min_s=0))
    assert tight["summary"]["split_steps"] == 0  # exact durations never split


def test_analyze_workout_execution_tool_extended_ride(monkeypatch):
    """Tool level: provided plan doc, new tolerance and detail parameters, JSON keys for the split."""
    activity = {"id": "i9", "name": "3x10 Threshold extended", "type": "Ride", "start_date_local": "2026-10-09T15:04:32",
                "icu_athlete_id": "i1", "icu_ftp": 234, "lthr": 165, "paired_event_id": None, "compliance": 0.0,
                "stream_types": ["time", "watts", "heartrate", "cadence", "Stamina", "Elapsedtime", "GarminRunEffectiveness"]}
    _install_router(monkeypatch, {"/activity/": activity, "/streams": EXT_STREAMS, "/intervals": {"icu_intervals": EXT_INTERVALS},
                                  "/events": []})
    text = asyncio.run(analyze_workout_execution("i9", planned_workout_doc=PLAN_65))
    assert "matched 7, planned without match 0" in text and "additional training 22:07" in text
    payload = json.loads(asyncio.run(analyze_workout_execution("i9", planned_workout_doc=PLAN_65, output_format="json")))
    assert payload["rows"][6]["split"] == {"planned_part_s": 600.0, "remainder_s": 478.0}
    assert payload["extension"]["efforts"][0]["average_watts"] == 432
    assert payload["hidden_streams"]["Elapsedtime"] == "clock/counter stream"
    full = asyncio.run(analyze_workout_execution("i9", planned_workout_doc=PLAN_65, detail_level="full"))
    assert "Elapsedtime: start" in full and "Custom streams not shown per step" not in full
    compact = asyncio.run(analyze_workout_execution("i9", planned_workout_doc=PLAN_65, detail_level="compact"))
    assert len(compact) < len(text) / 2
    loose = json.loads(asyncio.run(analyze_workout_execution("i9", planned_workout_doc=PLAN_65, output_format="json",
                                                            duration_tolerance_pct=50)))
    assert loose["summary"]["split_steps"] == 1  # 17:58 is more than 50 % longer than 10:00
    lenient = json.loads(asyncio.run(analyze_workout_execution("i9", planned_workout_doc=PLAN_65, output_format="json",
                                                              duration_tolerance_pct=100)))
    assert lenient["summary"]["split_steps"] == 0 and lenient["rows"][6]["metrics"]["moving_time_s"] == 1078
    assert asyncio.run(analyze_workout_execution("i9", detail_level="huge")).startswith("Error: detail_level")
    assert asyncio.run(analyze_workout_execution("i9", duration_tolerance_pct=150)).startswith("Error: duration_tolerance_pct")
    report = asyncio.run(get_activity_report("i9", planned_workout_doc=PLAN_65))
    assert "first 10:00 of 17:58" in report and "Additional training after the plan: 22:07" in report
