"""
Regression tests from the second review of the plan-vs-execution alignment (R26-1 to R26-12):
lap presses keep their boundaries and real deviations, step boundaries inside device auto-laps
are placed at intensity changes (only with evidence), overruns of auto-lapped steps are
reported, steps shorter than a lap are found, low-confidence layouts carry a caveat, many laps
stay fast, NP across a recording pause, distance plans and the report finding.
"""

import os
import pathlib
import random
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.tools.report import _execution_findings  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.execution import Profile, analyze, format_execution, plan_steps  # pylint: disable=wrong-import-position

FTP = 250
RIDE = {"ftp": FTP, "lthr": 165, "power_zones": [55, 75, 90, 105, 120, 150, 999], "activity_type": "Ride",
        "hr_zones": [133, 147, 153, 164, 169, 174, 188]}
RUN = {"threshold_pace": 4.0, "lthr": 165, "pace_zones": [77.5, 87.7, 94.3, 100.0, 103.4, 111.5, 999.0],
       "activity_type": "Run", "hr_zones": [133, 147, 153, 164, 169, 174, 188]}
T = 4.0


def build(segments, laps="distance", lap_m=1000.0, lap_s=None, extra_cuts=(), seed=0, noise=0.03):  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    """Synthetic 1 Hz activity from (secs, watts or None, speed or None, hr) segments.

    ``laps``: "distance" (auto-lap every ``lap_m``), "time" (every ``lap_s``) or "steps" (a lap
    press at every segment boundary); ``extra_cuts`` adds lap presses at sample indices.
    """
    rnd = random.Random(seed)
    watts, speed, heart, dist = [], [], [], []
    total, bounds, pulse = 0.0, [0], None
    for secs, power, velocity, target_hr in segments:
        for _ in range(int(secs)):
            watts.append(None if power is None else max(0.0, power * (1 + rnd.gauss(0, noise))))
            sample = None if velocity is None else max(0.1, velocity * (1 + rnd.gauss(0, noise / 2)))
            speed.append(sample)
            pulse = target_hr if pulse is None else pulse + (target_hr - pulse) / 25.0
            heart.append(round(pulse + rnd.gauss(0, 1)))
            dist.append(total)
            total += sample if sample is not None else (power or 0) / 30.0
        bounds.append(len(dist))
    n = len(dist)
    cuts = set(extra_cuts)
    if laps == "distance":
        nxt = lap_m
        for index, metres in enumerate(dist):
            if metres >= nxt:
                cuts.add(index)
                nxt += lap_m
    elif laps == "time":
        cuts |= set(range(lap_s, n, lap_s))
    elif laps == "steps":
        cuts |= set(bounds[1:-1])
    edges = [0] + sorted(c for c in cuts if 0 < c < n) + [n]
    intervals = []
    for start, end in zip(edges, edges[1:], strict=False):
        metres = (dist[end] if end < n else total) - dist[start]
        power = [w for w in watts[start:end] if w is not None]
        intervals.append({"type": "WORK", "start_index": start, "end_index": end, "start_time": start, "end_time": end,
                          "elapsed_time": end - start, "moving_time": end - start, "distance": metres,
                          "average_speed": metres / (end - start), "average_watts": sum(power) / len(power) if power else None,
                          "average_heartrate": sum(heart[start:end]) / (end - start)})
    streams = [{"type": "time", "data": list(range(n))}, {"type": "heartrate", "data": heart}, {"type": "distance", "data": dist}]
    if any(w is not None for w in watts):
        streams.append({"type": "watts", "data": watts})
    if any(v is not None for v in speed):
        streams.append({"type": "velocity_smooth", "data": speed})
    return streams, intervals


def pw(low, high):
    """Power target in % FTP."""
    return {"power": {"start": low, "end": high, "units": "%ftp"}}


def pc(low, high):
    """Pace target in % threshold pace."""
    return {"pace": {"start": low, "end": high, "units": "%pace"}}


def run(plan, streams, intervals, ctx):
    """Plan steps and analyse."""
    return analyze(plan_steps(plan, ctx), intervals, streams, context=ctx)


def texts(row):
    """Texts of the clear deviations of a row."""
    return [d["text"] for d in row["deviations"] if d["severity"] == "deviation"]


# Real deviations with lap presses: rep 1 12 min, recovery 2 min, rep 3 8 min, recovery 6 min.
DEVIATING = [(900, 0.58 * FTP, None, 120), (720, 0.97 * FTP, None, 160), (120, 0.5 * FTP, None, 135), (600, 0.97 * FTP, None, 160),
             (240, 0.5 * FTP, None, 130), (480, 0.97 * FTP, None, 160), (360, 0.5 * FTP, None, 130), (600, 0.55 * FTP, None, 125)]


def test_lap_presses_keep_real_deviations_for_recoveries_without_target():
    """R26-1: step laps are exact; an untargeted recovery never receives work samples, so the real
    "2:00 shorter" stays and rep 3 is not falsely below target."""
    plan = [{"duration": 900, "warmup": True, **pw(50, 65)}, {"reps": 3, "steps": [{"duration": 600, **pw(95, 100)}, {"duration": 240, "text": "easy"}]},
            {"duration": 600, "cooldown": True, **pw(50, 60)}]
    streams, intervals = build(DEVIATING, laps="steps")
    result = run(plan, streams, intervals, RIDE)
    rows = result["rows"]
    assert not any(r.get("carried_over") for r in rows) and result["summary"]["alignment_confidence"] == "high"
    assert rows[1]["overrun"]["moving_time_s"] == 120 and texts(rows[2]) == ["2:00 shorter than planned"]
    assert texts(rows[5]) == ["2:00 shorter than planned"] and rows[5]["adherence"]["status"] == "in range"
    assert rows[6]["overrun"]["moving_time_s"] == 120


def test_lap_presses_kept_without_the_target_stream():
    """R26-1: a power plan ridden without power keeps the lap boundaries and its 4 deviations."""
    plan = [{"duration": 900, "warmup": True, **pw(50, 65)}, {"reps": 3, "steps": [{"duration": 600, **pw(95, 100)}, {"duration": 240, **pw(45, 55)}]},
            {"duration": 600, "cooldown": True, **pw(50, 60)}]
    streams, intervals = build(DEVIATING, laps="steps")
    no_power = [s for s in streams if s["type"] != "watts"]
    for interval in intervals:
        interval["average_watts"] = None
    result = run(plan, no_power, intervals, RIDE)
    assert result["summary"]["steps_with_deviations"] == 4 and result["summary"]["boundaries_off_lap"] == 0


def test_adjacent_zones_real_shift_with_lap_presses_and_auto_laps():
    """R26-1: tempo 88-93 % then steady 78-87 % (bands overlap with the tolerance). A real 5 min
    longer tempo is reported with lap presses and, at the pace change, with 1 km auto-laps."""
    plan = [{"duration": 900, "warmup": True, **pc(70, 78)}, {"duration": 1200, **pc(88, 93)}, {"duration": 600, **pc(78, 87)},
            {"duration": 600, "cooldown": True, **pc(70, 78)}]
    segments = [(900, None, 0.74 * T, 135), (1500, None, 0.905 * T, 158), (300, None, 0.825 * T, 148), (600, None, 0.74 * T, 140)]
    for laps in ("steps", "distance"):
        rows = run(plan, *build(segments, laps=laps), RUN)["rows"]
        assert abs(rows[1]["overrun"]["moving_time_s"] - 300) <= 5, laps
        assert abs(rows[2]["metrics"]["moving_time_s"] - 300) <= 5 and any("shorter than planned" in t for t in texts(rows[2])), laps
    perfect = [(900, None, 0.74 * T, 135), (1200, None, 0.905 * T, 158), (600, None, 0.825 * T, 148), (600, None, 0.74 * T, 140)]
    result = run(plan, *build(perfect), RUN)
    assert result["summary"]["steps_with_deviations"] == 0 and result["summary"]["alignment_confidence"] == "medium"


def test_random_step_lap_workouts_never_move_boundaries():
    """R26-1: lap presses are never moved, whatever the neighbouring targets (fuzz)."""
    rnd = random.Random(8)
    for _ in range(25):
        levels = [rnd.choice([(50, 60), (88, 93), (95, 105), (90, 95), (100, 110)]) for _ in range(rnd.randint(3, 7))]
        plan = [{"duration": rnd.choice([180, 300, 600]), **pw(*lv)} for lv in levels]
        segments = [(step["duration"] + rnd.choice([-45, 0, 0, 45, 120]), (sum(lv) / 200) * FTP, None, 140)
                    for step, lv in zip(plan, levels, strict=True)]
        result = run(plan, *build(segments, laps="steps", seed=rnd.randint(0, 99)), RIDE)
        assert not any(r.get("carried_over") for r in result["rows"])
        assert result["summary"]["alignment_confidence"] == "high"


def test_overrun_of_an_auto_lapped_step_is_reported():
    """R26-2: a 60 min Z2 block run 10 min longer with 1 km auto-laps is "10:00 longer than planned",
    not "riding before the first planned step"."""
    plan = [{"duration": 1200, "warmup": True, **pc(70, 78)}, {"duration": 3600, **pc(78, 87)}, {"duration": 600, "cooldown": True, **pc(70, 78)}]
    segments = [(1200, None, 0.74 * T, 135), (4200, None, 0.83 * T, 145), (600, None, 0.74 * T, 140)]
    result = run(plan, *build(segments), RUN)
    z2 = result["rows"][1]
    assert result["summary"]["pre_plan_s"] == 0 and abs(z2["overrun"]["moving_time_s"] - 600) <= 5
    assert any(t.startswith("10:0") and "longer than planned" in t for t in texts(z2))
    distance_plan = [{"distance": 3000, "warmup": True, **pc(70, 80)}, {"distance": 10000, **pc(85, 92)}, {"distance": 2000, "cooldown": True, **pc(70, 80)}]
    segments = [(round(3000 / (0.75 * T)), None, 0.75 * T, 135), (round(12000 / (0.88 * T)), None, 0.88 * T, 155),
                (round(2000 / (0.75 * T)), None, 0.75 * T, 140)]
    rows = run(distance_plan, *build(segments), RUN)["rows"]
    assert rows[1].get("overrun") and any("longer than planned" in t for t in texts(rows[1]))


def test_perfect_auto_lap_session_has_no_residual_offsets():
    """R26-5: a perfectly executed 3 x 10 min / 3 min run with 1 km laps gives exact steps."""
    plan = [{"duration": 900, "warmup": True, **pc(70, 78)}, {"reps": 3, "steps": [{"duration": 600, **pc(97, 102)}, {"duration": 180, **pc(65, 75)}]},
            {"duration": 600, "cooldown": True, **pc(70, 78)}]
    segments = [(900, None, 0.74 * T, 140)] + [(600, None, 0.995 * T, 165), (180, None, 0.70 * T, 145)] * 3 + [(600, None, 0.74 * T, 140)]
    result = run(plan, *build(segments), RUN)
    summary = result["summary"]
    assert summary["steps_with_deviations"] == 0 and summary["extra_time_inside_plan_s"] == 0
    for row, planned in zip(result["rows"], [900, 600, 180, 600, 180, 600, 180, 600], strict=True):
        assert abs(row["metrics"]["moving_time_s"] - planned) <= 3


def test_steps_shorter_than_a_lap_are_placed_at_the_intensity_changes():
    """R26-4: 5 x (3 min / 2 min) with 1 km laps: the jogs have no lap of their own but are found."""
    plan = [{"duration": 900, "warmup": True, **pc(70, 78)}, {"reps": 5, "steps": [{"duration": 180, **pc(103, 108)}, {"duration": 120, **pc(65, 75)}]},
            {"duration": 600, "cooldown": True, **pc(70, 78)}]
    segments = [(900, None, 0.74 * T, 140)] + [(180, None, 1.055 * T, 165), (120, None, 0.70 * T, 145)] * 5 + [(600, None, 0.74 * T, 140)]
    result = run(plan, *build(segments), RUN)
    assert result["summary"]["matched"] == 12 and result["summary"]["steps_with_deviations"] == 0
    assert all(abs(r["metrics"]["moving_time_s"] - r["planned"]["duration"]) <= 3 for r in result["rows"])
    assert sum(1 for r in result["rows"] if r.get("carved")) >= 3
    assert result["summary"]["alignment_confidence"] == "medium"


def test_laps_longer_than_the_steps_get_a_low_confidence_caveat():
    """R26-4: VO2 steps inside 5 km laps cannot be separated reliably: low confidence, caveat, JSON flag."""
    plan = [{"duration": 600, "warmup": True, **pw(50, 65)}, {"reps": 3, "steps": [{"duration": 480, **pw(110, 120)}, {"duration": 240, **pw(45, 55)}]},
            {"duration": 600, "cooldown": True, **pw(50, 60)}]
    segments = ([(5400, 0.68 * FTP, None, 135), (600, 0.58 * FTP, None, 120)] + [(480, 1.15 * FTP, None, 170), (240, 0.5 * FTP, None, 135)] * 3
                + [(600, 0.55 * FTP, None, 125), (5400, 0.68 * FTP, None, 135)])
    result = run(plan, *build(segments, lap_m=5000), RIDE)
    assert result["summary"]["alignment_confidence"] == "low" and result["summary"]["alignment_notes"]
    assert "Caveat (low confidence): the laps look like device auto-laps (every 5.00 km)" in format_execution(result, "")


def test_long_steps_with_very_many_laps_are_matched_quickly():
    """R26-6: a 3 h Z2 step needs about 315 laps of 200 m; 2,100 laps of 5 s stay fast."""
    plan = [{"duration": 900, "warmup": True, **pw(50, 65)}, {"duration": 10800, **pw(65, 75)}, {"duration": 600, "cooldown": True, **pw(50, 60)}]
    segments = [(900, 0.58 * FTP, None, 120), (10800, 0.70 * FTP, None, 140), (600, 0.55 * FTP, None, 125)]
    result = run(plan, *build(segments, lap_m=200), RIDE)
    assert result["summary"]["steps_with_deviations"] == 0 and result["rows"][1]["metrics"]["moving_time_s"] == 10800
    plan = [{"duration": 900, "warmup": True, **pw(50, 65)}, {"reps": 5, "steps": [{"duration": 1200, **pw(88, 93)}, {"duration": 600, **pw(50, 60)}]},
            {"duration": 600, "cooldown": True, **pw(50, 60)}]
    segments = [(900, 0.58 * FTP, None, 120)] + [(1200, 0.9 * FTP, None, 155), (600, 0.55 * FTP, None, 130)] * 5 + [(600, 0.55 * FTP, None, 125)]
    streams, intervals = build(segments, laps="time", lap_s=5)
    assert len(intervals) > 2000
    began = time.perf_counter()
    result = run(plan, streams, intervals, RIDE)
    assert time.perf_counter() - began < 8 and result["summary"]["matched"] == 12


def test_np_window_spans_a_recording_pause_with_zeros():
    """R26-7: a pause counts as 0 W in the 30 s window (as Intervals.icu), it does not restart it."""
    seconds = list(range(0, 600)) + list(range(631, 1231))  # 31 s pause after 600 s
    watts = [300.0] * 600 + [200.0] * 600
    profile = Profile([{"type": "time", "data": seconds}, {"type": "watts", "data": watts}])
    grid = [300.0] * 600 + [0.0] * 31 + [200.0] * 600
    rolling = [sum(grid[max(0, i - 29): i + 1]) / min(i + 1, 30) for i in range(len(grid))]
    expected = (sum(rolling[i] ** 4 for i in range(631, 1231)) / 600) ** 0.25
    assert round(profile.normalized_power(600, 1200), 3) == round(expected, 3)


def test_distance_plan_restarts_the_clock_and_labels_estimates():
    """R26-8: after a distance step the plan clock restarts at its actual end (no false "started
    earlier"); estimated totals are labelled; a distance step without pace target is added as km."""
    plan = [{"distance": 3000, "warmup": True, **pc(70, 80)}, {"distance": 10000, **pc(85, 92)}, {"distance": 1000, **pc(100, 105)},
            {"distance": 2000, "cooldown": True, **pc(70, 80)}]
    segments = [(round(3000 / (0.80 * T)), None, 0.80 * T, 135), (round(10000 / (0.92 * T)), None, 0.92 * T, 155),
                (round(1000 / (1.05 * T)), None, 1.05 * T, 165), (round(2000 / (0.80 * T)), None, 0.80 * T, 140)]
    result = run(plan, *build(segments), RUN)
    assert not any("plan timeline" in d["text"] for r in result["rows"] for d in r["deviations"])
    assert "Plan: 4 steps, ≈1:18:56 (distance steps estimated at the target pace) planned" in format_execution(result, "", True)
    hr_plan = [{"duration": 600, "warmup": True, **pc(70, 80)}, {"distance": 5000, "hr": {"start": 80, "end": 88, "units": "%lthr"}},
               {"duration": 300, **pc(100, 105)}, {"duration": 600, "cooldown": True, **pc(70, 78)}]
    segments = [(600, None, 0.75 * T, 135), (round(5000 / (0.85 * T)), None, 0.85 * T, 140), (300, None, 1.02 * T, 165), (600, None, 0.74 * T, 140)]
    result = run(hr_plan, *build(segments, laps="steps"), RUN)
    assert "25:00 + 5.00 km planned" in format_execution(result, "", True)
    assert not any("plan timeline" in d["text"] for r in result["rows"] for d in r["deviations"])


def test_report_lists_clearly_different_work_targets_separately():
    """R26-10: an easy block and strides are not merged into one envelope."""
    plan = [{"duration": 600, "warmup": True, **pc(72, 78)}, {"duration": 1200, **pc(80, 86)},
            {"reps": 2, "steps": [{"duration": 20, **pc(115, 125)}, {"duration": 100, **pc(65, 75)}]}, {"duration": 300, "cooldown": True, **pc(70, 78)}]
    segments = [(600, None, 0.75 * T, 135), (1200, None, 0.83 * T, 145), (20, None, 1.2 * T, 160), (100, None, 0.7 * T, 150),
                (20, None, 1.2 * T, 160), (100, None, 0.7 * T, 150), (300, None, 0.74 * T, 140)]
    finding = _execution_findings(run(plan, *build(segments, laps="steps"), RUN))[0]
    assert "work steps 5:01/km vs 5:12/km to 4:51/km planned; 3:27/km, 3:29/km vs 3:37/km to 3:20/km planned" in finding


def test_short_overrun_of_the_last_step_is_summarised():
    """R26-12: a cool-down 1:30 longer than planned (below the 2:00 extension threshold) is named in the summary."""
    plan = [{"duration": 600, "warmup": True, **pw(50, 65)}, {"duration": 600, **pw(90, 95)}, {"duration": 300, "cooldown": True, **pw(50, 60)}]
    segments = [(600, 0.58 * FTP, None, 120), (600, 0.92 * FTP, None, 155), (390, 0.55 * FTP, None, 125)]
    text = format_execution(run(plan, *build(segments, laps="steps"), RIDE), "")
    assert "The last step ran 1:30 beyond its planned duration (less than 2:00, not reported as a separate block)." in text


# ----------------------------------------------------------------- re-check (N1-N5)
def build_reset(segments, manual_cuts, lap_m=1000.0, seed=0):  # pylint: disable=too-many-locals
    """Garmin-like auto-laps whose distance counter restarts at every lap press."""
    streams, _ = build(segments, laps="steps", seed=seed)
    n, dist, heart = len(streams[0]["data"]), streams[2]["data"], streams[1]["data"]
    watts = next((s["data"] for s in streams if s["type"] == "watts"), None)
    cuts, last, manual = set(manual_cuts), 0.0, set(manual_cuts)
    for index in range(n):
        if index in manual:
            last = dist[index]
        elif dist[index] - last >= lap_m:
            cuts.add(index)
            last = dist[index]
    edges = [0] + sorted(c for c in cuts if 0 < c < n) + [n]
    intervals = []
    for start, end in zip(edges, edges[1:], strict=False):
        metres = (dist[end] if end < n else dist[-1] + 1.0) - dist[start]
        row = {"type": "WORK", "start_index": start, "end_index": end, "start_time": start, "end_time": end, "elapsed_time": end - start,
               "moving_time": end - start, "distance": metres, "average_speed": metres / (end - start),
               "average_heartrate": sum(heart[start:end]) / (end - start)}
        if watts:
            row["average_watts"] = sum(w for w in watts[start:end] if w is not None) / (end - start)
        intervals.append(row)
    return streams, intervals


def random_auto_lap_activity(seed):  # pylint: disable=too-many-locals
    """A random run/ride plan recorded with device auto-laps (the review's fuzz generator)."""
    rnd = random.Random(seed)
    sport = rnd.choice(["run", "ride"])
    levels = [(0.65, 0.75), (0.78, 0.87), (0.88, 0.93), (0.97, 1.03), (1.05, 1.12), (1.15, 1.25)]
    plan, segments = [], []

    def add(duration, low, high, warm=False, cool=False, no_target=False):
        step = {"duration": duration}
        if warm:
            step["warmup"] = True
        if cool:
            step["cooldown"] = True
        if no_target:
            step["text"] = "easy"
        else:
            step.update(pc(int(low * 100), int(high * 100)) if sport == "run" else pw(int(low * 100), int(high * 100)))
        plan.append(step)
        actual = max(10, int(duration * rnd.choice([1, 1, 1, 1, 0.8, 1.2, 1.5, 0.6])))
        mid = (low + high) / 2 * rnd.uniform(0.98, 1.02)
        segments.append((actual, None if sport == "run" else mid * FTP, mid * T if sport == "run" else None, 140))

    add(rnd.choice([600, 900, 1200]), 0.65, 0.75, warm=True)
    for _ in range(rnd.randint(3, 14)):
        low, high = rnd.choice(levels)
        add(rnd.choice([15, 20, 30, 60, 90, 120, 180, 240, 300, 480, 600, 900, 1200, 1800]), low, high, no_target=rnd.random() < 0.1)
    add(rnd.choice([300, 600]), 0.65, 0.75, cool=True)
    lap = rnd.choice([1000, 1000, 500, 1609]) if sport == "run" else rnd.choice([1000, 5000, 2000])
    if rnd.choice(["grid", "reset"]) == "grid":
        streams, intervals = build(segments, lap_m=lap, seed=seed)
    else:
        bounds = [0]
        for segment in segments:
            bounds.append(bounds[-1] + segment[0])
        streams, intervals = build_reset(segments, [b for b in bounds[1:-1] if rnd.random() < 0.3], lap_m=lap, seed=seed)
    return plan, streams, intervals, RUN if sport == "run" else RIDE


def test_strides_inside_auto_laps_do_not_crash():
    """N1: a long run with strides inside 1 km auto-laps (no lap of their own) used to raise
    IndexError after an earlier boundary move left stale lap lists; also the review's crash seeds."""
    plan = [{"duration": 1200, "warmup": True, **pc(70, 78)}, {"duration": 3600, **pc(78, 87)},
            {"reps": 6, "steps": [{"duration": 20, **pc(115, 125)}, {"duration": 100, **pc(65, 77)}]}, {"duration": 600, "cooldown": True, **pc(70, 78)}]
    segments = [(1200, None, 0.74 * T, 135), (3600, None, 0.83 * T, 145)] + [(20, None, 1.2 * T, 160), (100, None, 0.72 * T, 150)] * 6
    segments.append((600, None, 0.74 * T, 140))
    result = run(plan, *build(segments), RUN)
    assert "plan_skipped" not in result["summary"] and result["summary"]["matched"] >= 10
    for row in result["rows"]:
        if row.get("metrics"):
            assert row["interval_indices"] and row["metrics"]["moving_time_s"] > 0
    format_execution(result, "", True)
    for seed in (171, 257, 264, 306, 413) + tuple(range(30)):  # the first five crashed on the previous head
        plan, streams, intervals, ctx = random_auto_lap_activity(seed)
        result = run(plan, streams, intervals, ctx)
        assert "plan_skipped" not in result["summary"], seed
        format_execution(result, "", ctx is RUN)


def test_an_internal_error_degrades_to_the_interval_analysis(monkeypatch):
    """N1 guard: whatever goes wrong in the plan comparison, the tool still reports the intervals."""
    from intervals_mcp_server.utils import execution  # pylint: disable=import-outside-toplevel

    def broken(*_args, **_kwargs):
        raise IndexError("list index out of range")

    monkeypatch.setattr(execution, "_analyze_plan", broken)
    plan = [{"duration": 600, **pw(50, 65)}, {"duration": 600, **pw(90, 95)}]
    result = run(plan, *build([(600, 0.58 * FTP, None, 120), (600, 0.92 * FTP, None, 155)], laps="steps"), RIDE)
    text = format_execution(result, "")
    assert "Note: plan comparison failed (IndexError: list index out of range); intervals shown without the plan." in text
    assert "No planned workout: 2 intervals" in text


def test_equal_length_lap_presses_are_not_auto_laps():
    """N2: 3/3 min with an untargeted recovery and hill repeats with lap presses have laps of the
    planned step length: no auto-lap caveat, and a real 50 % overrun stays a deviation."""
    plan = [{"duration": 180, "warmup": True, **pw(50, 65)}, {"reps": 6, "steps": [{"duration": 180, **pw(106, 120)}, {"duration": 180, "text": "easy"}]},
            {"duration": 180, "cooldown": True, **pw(50, 60)}]
    segments = [(180, 0.58 * FTP, None, 120)] + [(180, 1.13 * FTP, None, 165), (180, 0.5 * FTP, None, 125)] * 2
    segments += [(270, 1.13 * FTP, None, 165), (90, 0.5 * FTP, None, 125)] + [(180, 1.13 * FTP, None, 165), (180, 0.5 * FTP, None, 125)] * 3
    segments.append((180, 0.55 * FTP, None, 125))
    result = run(plan, *build(segments, laps="steps"), RIDE)
    assert result["summary"]["alignment_confidence"] == "high" and result["summary"]["auto_laps"] is None
    rows = result["rows"]
    assert rows[5]["overrun"]["moving_time_s"] == 90 and any("longer than planned" in t for t in texts(rows[5]))
    assert texts(rows[6]) == ["1:30 shorter than planned"]
    rnd = random.Random(3)
    hills = [{"duration": 900, "warmup": True, **pw(50, 65)}, {"reps": 6, "steps": [{"duration": 360, **pw(100, 105)}, {"duration": 360, "text": "descent"}]},
             {"duration": 600, "cooldown": True, **pw(50, 60)}]
    segments = [(900, 0.58 * FTP, None, 120)]
    for rep in range(6):
        up, down = (450, 270) if rep == 2 else (360 + rnd.randint(-8, 8), 360 + rnd.randint(-8, 8))
        segments += [(up, 1.02 * FTP, None, 165), (down, 0.45 * FTP, None, 125)]
    segments.append((600, 0.55 * FTP, None, 125))
    result = run(hills, *build(segments, laps="steps"), RIDE)
    assert result["summary"]["alignment_confidence"] == "high" and result["summary"]["steps_with_deviations"] == 2
    assert "Caveat" not in format_execution(result, "")


def test_final_partial_lap_goes_back_to_the_cool_down():
    """N3: after the tempo's overrun moved a boundary, the activity's last partial lap belongs to the
    cool-down again, so a cool-down of exactly 10 min is not "shorter than planned"."""
    plan = [{"duration": 900, "warmup": True, **pc(70, 78)}, {"duration": 2400, **pc(88, 93)}, {"duration": 600, "cooldown": True, **pc(70, 78)}]
    for extra in (300, 600, 1200):
        segments = [(900, None, 0.74 * T, 135), (2400 + extra, None, 0.905 * T, 158), (600, None, 0.74 * T, 140)]
        rows = run(plan, *build(segments), RUN)["rows"]
        assert abs(rows[2]["metrics"]["moving_time_s"] - 600) <= 3 and not texts(rows[2]), extra
        assert abs(rows[1]["overrun"]["moving_time_s"] - extra) <= 3, extra


def test_alignment_stays_bounded_with_similar_targets_and_many_laps(monkeypatch):
    """N4: surplus-lap absorption with similar neighbouring targets stays fast; a time budget makes
    the plan comparison give up (intervals only, with a note) instead of running for minutes."""
    from intervals_mcp_server.utils import execution  # pylint: disable=import-outside-toplevel

    streams, intervals = build([(7200, 0.7 * FTP, None, 140)], laps="time", lap_s=12)
    plan = [{"duration": 600, **pw(65, 75)} for _ in range(20)]
    began = time.perf_counter()
    result = run(plan, streams, intervals, RIDE)
    assert time.perf_counter() - began < 10 and result["summary"]["matched"] == 12
    monkeypatch.setattr(execution, "ALIGN_TIME_BUDGET_S", 0.0)
    result = run(plan, streams, intervals, RIDE)
    assert "plan comparison skipped" in result["summary"]["plan_skipped"]
    assert "Note: plan comparison skipped: 20 planned steps x 600 intervals exceed the 0 s budget." in format_execution(result, "")


def test_np_of_sparse_recordings_holds_the_value():
    """N5: a constant 200 W recorded every 2 or 3 s has NP 200 W; only pauses count as 0 W."""
    for spacing in (1, 2, 3):
        seconds = list(range(0, 1200, spacing))
        profile = Profile([{"type": "time", "data": seconds}, {"type": "watts", "data": [200.0] * len(seconds)}])
        assert round(profile.normalized_power(0, len(seconds)), 3) == 200.0
