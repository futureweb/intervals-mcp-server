"""
Submaximal fatigue tests (SFT) detected by Intervals.icu (pure functions, no API access).

Intervals.icu detects a steady effort at a configured target early in an activity (sport
settings ``sft_*``: type POWER or PACE, duration, target in % of FTP, tolerance, maximum
coefficient of variation, latest start) and stores it on the activity as
``submax_fatigue_test`` (OpenAPI schema ``SubmaxFatigueTest``): ``start_index`` /
``end_index`` of the test, ``average_watts`` (or ``average_mps``), ``target``, ``cv``,
``final_bpm`` (HR at the end), ``hrrc`` and ``end_index_hrrc`` (HR recovery after the test,
when the athlete eased off), ``efficiency_factor`` (average power / final HR), ``rpe``,
``tte_mins`` and ``ignore``. Feature announcement: "Automatic Submaximal Fatigue Testing"
(https://forum.intervals.icu/t/automatic-submaximal-fatigue-testing/132525); HRRc:
https://forum.intervals.icu/t/heart-rate-recovery-hrrc/387.

A detection is not automatically a benchmark: the same 3 minutes can be the first part of a
threshold interval. Every test therefore passes a validity filter and only valid tests feed
the trend; excluded tests are listed with their reasons. A recovery value of 0 without a
recovery window (``end_index_hrrc`` 0) means "not measured", never 0 bpm.
"""

import statistics
from datetime import date
from typing import Any

from intervals_mcp_server.utils.custom_fields import is_missing

DEFAULT_TOLERANCE_PCT = 5.0
CONTAINING_INTERVAL_EXTRA_S = 60  # a WORK interval this much longer than the test contains it
CONTAINING_OVERLAP = 0.8  # share of the test window inside that interval
AFTER_WINDOW_S = 60
AFTER_CONTINUED_PCT = 80.0  # power after the test at or above this share of the target: effort continued
AFTER_EASY_PCT = 50.0  # power after the test below this share: easy minute, HR drop measurable
BEFORE_WINDOW_S = 300
BEFORE_HARD_PCT = 90.0  # power before the test at or above this share of the target: hard riding before
MIN_TREND_TESTS = 4

REASONS: dict[str, str] = {
    "ignored": "marked as ignored in Intervals.icu",
    "incomplete": "test without type, target or average",
    "off_target": "average outside the target tolerance",
    "not_steady": "coefficient of variation above the limit",
    "inside_workout": "detected inside a regular workout (not a stand-alone test)",
    "hard_before": "hard riding right before the test",
    "no_recovery": "no HR recovery part (required)",
}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or is_missing(value) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rnd(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def normalise_test(activity: dict[str, Any]) -> dict[str, Any] | None:
    """The activity's test as a flat row (None when the activity carries no test)."""
    test = activity.get("submax_fatigue_test")
    if not isinstance(test, dict) or not test:
        return None
    kind = str(test.get("type") or "").upper()
    if kind in ("", "NONE"):
        return None
    value = _num(test.get("average_watts")) if kind == "POWER" else _num(test.get("average_mps"))
    target = _num(test.get("target"))
    hrrc, hrrc_end = _num(test.get("hrrc")), _num(test.get("end_index_hrrc"))
    recovery = hrrc is not None and bool(hrrc_end)
    deviation = (value - target) / target * 100 if value is not None and target else None
    final_bpm = _num(test.get("final_bpm"))
    return {
        "activity_id": activity.get("id"), "name": activity.get("name"), "type": activity.get("type"),
        "date": str(activity.get("start_date_local") or "")[:10], "test_type": kind,
        "start_index": test.get("start_index"), "end_index": test.get("end_index"),
        "duration_s": _num(test.get("duration")), "average": value, "target": target,
        "deviation_pct": _rnd(deviation, 1), "tolerance_pct": _num(test.get("tolerance_percent")),
        "cv_pct": _rnd(_num(test.get("cv")), 1), "max_cv_pct": _num(test.get("max_cv_percent")),
        "final_bpm": final_bpm if final_bpm else None,
        "hrrc_bpm": hrrc if recovery else None, "recovery_measured": recovery,
        "efficiency_factor": _rnd(_num(test.get("efficiency_factor")), 3) or None,
        "rpe": test.get("rpe"), "tte_mins": test.get("tte_mins"), "ignore": bool(test.get("ignore")),
        "gear_id": (activity.get("gear") or {}).get("id") if isinstance(activity.get("gear"), dict) else activity.get("gear_id"),
        "indoor": bool(activity.get("trainer")) or str(activity.get("type") or "").startswith("Virtual"),
        "ftp": _num(activity.get("icu_ftp")),
    }


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _window(times: list[float], values: list[Any], start_s: float, end_s: float) -> list[float]:
    """Numeric samples with start_s <= time < end_s."""
    return [v for t, raw in zip(times, values, strict=False) if start_s <= t < end_s and (v := _num(raw)) is not None]


def _interval_secs(interval: dict[str, Any]) -> float | None:
    return _num(interval.get("moving_time")) or _num(interval.get("elapsed_time"))


def _containing_interval(test: dict[str, Any], intervals: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A WORK interval that holds most of the test window and is clearly longer than the test."""
    start, end, duration = test.get("start_index"), test.get("end_index"), test.get("duration_s") or 0
    if not isinstance(start, int) or not isinstance(end, int) or end <= start:
        return None
    for interval in intervals:
        if str(interval.get("type") or "").upper() != "WORK":
            continue
        low, high = interval.get("start_index"), interval.get("end_index")
        secs = _interval_secs(interval)
        if not isinstance(low, int) or not isinstance(high, int) or secs is None:
            continue
        overlap = max(0, min(end, high) - max(start, low))
        if overlap >= CONTAINING_OVERLAP * (end - start) and secs >= duration + CONTAINING_INTERVAL_EXTRA_S:
            return interval
    return None


def context_checks(  # pylint: disable=too-many-locals
    test: dict[str, Any], streams: dict[str, list[Any]], intervals: list[dict[str, Any]]
) -> dict[str, Any]:
    """Stream and interval context of a power test: what came before and after, HR course, containing interval.

    ``streams`` maps stream types (time, watts, heartrate) to their data. Returns the flags
    ``inside_workout`` (list of reasons) and ``hard_before`` plus the measured values.
    """
    times = [t for t in (_num(v) for v in streams.get("time") or []) if t is not None]
    watts, heart = streams.get("watts") or [], streams.get("heartrate") or []
    start, end = test.get("start_index"), test.get("end_index")
    out: dict[str, Any] = {"checked": False, "inside_workout": [], "hard_before": None}
    usable_streams = test["test_type"] == "POWER" and bool(times) and len(times) == len(streams.get("time") or [])
    if not usable_streams or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(times):
        out["note"] = "context not checked (power test with time and power streams needed)"
        return out
    target = test["target"] or 0.0
    t_start, t_end = times[start], times[end - 1] + 1
    before = _mean(_window(times, watts, t_start - BEFORE_WINDOW_S, t_start))
    after = _mean(_window(times, watts, t_end, t_end + AFTER_WINDOW_S))
    hr_test = _window(times, heart, t_start, t_end)
    hr_end = next((v for v in (_num(h) for h in reversed(heart[start:end])) if v), None)
    hr_after = _window(times, heart, t_end + AFTER_WINDOW_S - 5, t_end + AFTER_WINDOW_S + 5)
    out.update(checked=True, power_before_w=_rnd(before, 0), power_after_w=_rnd(after, 0),
               hr_start=next((v for v in (_num(h) for h in heart[start:end]) if v), None), hr_end=hr_end,
               hr_mean=_rnd(_mean(hr_test), 1), hr_drop_60s=None)
    if out["hr_start"] is not None and hr_end is not None:
        out["hr_rise"] = round(hr_end - out["hr_start"], 1)
    containing = _containing_interval(test, intervals)
    if containing is not None:
        secs = _interval_secs(containing) or 0
        watts_text = f" at {containing['average_watts']:.0f} W" if _num(containing.get("average_watts")) is not None else ""
        out["inside_workout"].append(f"part of a {int(secs // 60)}:{int(secs % 60):02d} work interval{watts_text}")
        out["containing_interval"] = {k: containing.get(k) for k in ("start_index", "end_index", "moving_time", "average_watts", "label")}
    if after is not None and target and after >= AFTER_CONTINUED_PCT / 100 * target:
        out["inside_workout"].append(f"effort continued after the test ({after:.0f} W in the next {AFTER_WINDOW_S} s)")
    if after is not None and target and after < AFTER_EASY_PCT / 100 * target and hr_end is not None and hr_after:
        out["hr_drop_60s"] = round(hr_end - _mean(hr_after), 1)  # type: ignore[operator]
    if before is not None and target and before >= BEFORE_HARD_PCT / 100 * target:
        out["hard_before"] = f"{before:.0f} W in the {BEFORE_WINDOW_S // 60} min before the test"
    return out


def validity(test: dict[str, Any], tolerance_pct: float | None = None, require_recovery: bool = False) -> list[dict[str, str]]:
    """Reasons that exclude a test from the trend (empty list: valid). Context must be attached first."""
    reasons: list[dict[str, str]] = []

    def add(code: str, detail: str) -> None:
        reasons.append({"code": code, "text": f"{REASONS[code]}: {detail}" if detail else REASONS[code]})

    if test["ignore"]:
        add("ignored", "")
    if test["average"] is None or not test["target"]:
        add("incomplete", "")
        return reasons
    tolerance = tolerance_pct if tolerance_pct is not None else (test["tolerance_pct"] or DEFAULT_TOLERANCE_PCT)
    if test["deviation_pct"] is not None and abs(test["deviation_pct"]) > tolerance:
        add("off_target", f"{test['deviation_pct']:+.1f} % (tolerance ±{tolerance:g} %)")
    if test["cv_pct"] is not None and test["max_cv_pct"] and test["cv_pct"] > test["max_cv_pct"]:
        add("not_steady", f"CV {test['cv_pct']:.1f} % > {test['max_cv_pct']:g} %")
    context = test.get("context") or {}
    if context.get("inside_workout"):
        add("inside_workout", "; ".join(context["inside_workout"]))
    if context.get("hard_before"):
        add("hard_before", context["hard_before"])
    if require_recovery and not test["recovery_measured"]:
        add("no_recovery", "")
    return reasons


def _iso_week(day: str) -> str | None:
    try:
        year, week, _ = date.fromisoformat(day).isocalendar()
    except ValueError:
        return None
    return f"{year}-W{week:02d}"


def _metric_trend(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """n, first/last, change, mean, SD and least-squares slope per week of one metric (chronological rows)."""
    points = [(date.fromisoformat(r["date"]), float(r[key])) for r in rows if r.get(key) is not None and r.get("date")]
    out: dict[str, Any] = {"metric": key, "n": len(points), "small_sample": len(points) < MIN_TREND_TESTS}
    if not points:
        return out
    values = [v for _, v in points]
    out.update(first=values[0], last=values[-1], change=round(values[-1] - values[0], 3),
               mean=round(statistics.fmean(values), 3), sd=round(statistics.stdev(values), 3) if len(values) > 1 else None,
               slope_per_week=None, weeks=round((points[-1][0] - points[0][0]).days / 7, 1))
    if len(points) >= 3:
        xs = [(d - points[0][0]).days / 7 for d, _ in points]
        if len(set(xs)) > 1:
            slope = statistics.linear_regression(xs, values).slope
            out["slope_per_week"] = round(slope, 3)
    return out


TREND_METRICS = ("final_bpm", "efficiency_factor", "hrrc_bpm", "hr_rise", "hr_drop_60s")


def trends(valid: list[dict[str, Any]]) -> dict[str, Any]:
    """Trend of every metric over the valid tests, a weekly table and comparability notes."""
    rows = sorted(valid, key=lambda r: r["date"])
    flat = [{**r, "hr_rise": (r.get("context") or {}).get("hr_rise"), "hr_drop_60s": (r.get("context") or {}).get("hr_drop_60s")}
            for r in rows]
    weeks: dict[str, list[dict[str, Any]]] = {}
    for row in flat:
        week = _iso_week(row["date"])
        if week:
            weeks.setdefault(week, []).append(row)
    table = []
    for week, items in sorted(weeks.items()):
        entry: dict[str, Any] = {"week": week, "tests": len(items)}
        for key in ("final_bpm", "efficiency_factor", "hrrc_bpm", "average", "target"):
            values = [float(i[key]) for i in items if i.get(key) is not None]
            entry[key] = round(statistics.fmean(values), 3) if values else None
        table.append(entry)
    notes = []
    targets = sorted({r["target"] for r in rows if r.get("target")})
    if targets and targets[-1] > targets[0] * 1.01:
        notes.append(f"targets differ ({targets[0]:g}-{targets[-1]:g}): HR at the end of the test is not like for like; "
                     "the efficiency factor (W per bpm) partly accounts for it")
    gears = {r.get("gear_id") for r in rows if r.get("gear_id")}
    if len(gears) > 1:
        notes.append(f"{len(gears)} different bikes/power meters")
    indoor = sum(1 for r in rows if r.get("indoor"))
    if 0 < indoor < len(rows):
        notes.append(f"indoor and outdoor mixed ({indoor} indoor)")
    return {"metrics": {key: _metric_trend(flat, key) for key in TREND_METRICS}, "weeks": table, "notes": notes}
