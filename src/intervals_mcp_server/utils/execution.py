"""
Planned-vs-executed workout analysis.

Takes a planned workout document (steps with durations and power / HR / pace targets),
the intervals Intervals.icu detected on the activity and the sample-aligned streams,
aligns planned steps with actual intervals and computes per-step execution metrics
(duration, target adherence, time in target range, HR response, fade, stamina
change, Pw:HR drift). Without a plan the same metrics are computed per interval.

Alignment uses a sequence alignment (Needleman-Wunsch style): planned steps and
actual intervals are matched in order with a cost based on duration and intensity
similarity, so an extra lap or a skipped rest does not shift every later pairing.
Nothing is interpolated: every metric is computed from recorded samples only.
"""

from typing import Any

from intervals_mcp_server.utils.custom_fields import is_missing
from intervals_mcp_server.utils.sports import format_pace, hms
from intervals_mcp_server.utils.streams import find_stream, numeric_values, range_stats

GAP_COST = 0.8
TARGET_TOLERANCE_PCT = 5.0
EXTENSION_MIN_SECS = 120  # additional training shorter than this is not reported separately
REST_POWER_FRACTION = 0.65  # of FTP: planned steps at or below this are "rest"
REST_PACE_FRACTION = 0.80  # of threshold speed
REST_HR_FRACTION = 0.80  # of LTHR
EDGE_SAMPLES = 10
DRIFT_MIN_SECS = 600


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or is_missing(value):
        return None
    return float(value)


# --------------------------------------------------------------------------- plan
def flatten_steps(steps: list[Any]) -> list[dict[str, Any]]:
    """Expand repeat blocks into the flat list of planned steps in execution order."""
    flat: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        reps = step.get("reps")
        children = step.get("steps")
        if isinstance(reps, int) and reps > 0 and isinstance(children, list):
            for rep in range(1, reps + 1):
                for child in flatten_steps(children):
                    child = dict(child)
                    child["rep"] = rep
                    child["reps"] = reps
                    flat.append(child)
            continue
        if step.get("duration") is None and step.get("distance") is None and not step.get("text"):
            continue
        flat.append(dict(step))
    return flat


def _range_from_value(value: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    """(low, high, units) of a workout target value; single values give low == high."""
    units = value.get("units")
    if value.get("start") is not None or value.get("end") is not None:
        low = _num(value.get("start"))
        high = _num(value.get("end"))
        if low is not None and high is not None and low > high:
            low, high = high, low
        return low, high, units
    single = _num(value.get("value"))
    return single, single, units


def _zone_bounds(bounds: Any, zone: float, scale: float | None) -> tuple[float | None, float | None]:
    """Absolute bounds of zone number ``zone`` from Intervals.icu upper bounds (% or bpm)."""
    uppers = numeric_values(bounds) if isinstance(bounds, list) else []
    index = int(zone) - 1
    if index < 0 or index >= len(uppers):
        return None, None
    lower = uppers[index - 1] if index > 0 else 0
    upper = uppers[index]
    if scale is not None:  # percentage zones
        return lower / 100 * scale, (upper / 100 * scale if upper < 999 else None)
    return float(lower), (float(upper) if upper < 999 else None)


def resolve_target(step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the intensity target of a planned step to absolute units.

    context carries ftp, lthr, max_hr, threshold_pace (m/s), power_zones (% FTP upper
    bounds), hr_zones (bpm upper bounds), pace_zones (% threshold speed upper bounds).
    Returns {"kind": "power"|"hr"|"pace", "low", "high", "units", "source"} or None.
    """
    for kind, resolved_key in (("power", "_power"), ("hr", "_hr"), ("pace", "_pace")):
        resolved = step.get(resolved_key)
        if isinstance(resolved, dict):
            low, high, units = _range_from_value(resolved)
            if low is not None:
                return {"kind": kind, "low": low, "high": high, "units": units, "source": "resolved by Intervals.icu"}
    for kind in ("power", "hr", "pace"):
        raw = step.get(kind)
        if isinstance(raw, dict):
            target = _resolve_raw_target(kind, raw, context)
            if target:
                return target
    return None


def _resolve_raw_target(  # pylint: disable=too-many-return-statements,too-many-branches
    kind: str, raw: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any] | None:
    """Convert a raw target (%ftp, w, %lthr, hr_zone, %pace ...) to absolute values."""
    low, high, units = _range_from_value(raw)
    if low is None:
        return None
    high = high if high is not None else low
    ftp, lthr, max_hr = _num(context.get("ftp")), _num(context.get("lthr")), _num(context.get("max_hr"))
    threshold_pace = _num(context.get("threshold_pace"))
    scale: float | None = None
    label = units
    if units == "w":
        label = "W"
    elif units == "%ftp" and ftp:
        low, high, label = low / 100 * ftp, high / 100 * ftp, "W"
    elif units == "power_zone" and ftp:
        low_b, high_b = _zone_bounds(context.get("power_zones"), low, ftp)
        _, high_b2 = _zone_bounds(context.get("power_zones"), high, ftp)
        return _target(kind, low_b, high_b2 if high != low else high_b, "W")
    elif units == "%lthr" and lthr:
        low, high, label = low / 100 * lthr, high / 100 * lthr, "bpm"
    elif units == "%hr" and max_hr:
        low, high, label = low / 100 * max_hr, high / 100 * max_hr, "bpm"
    elif units == "hr_zone":
        low_b, _ = _zone_bounds(context.get("hr_zones"), low, scale)
        _, high_b = _zone_bounds(context.get("hr_zones"), high, scale)
        return _target(kind, low_b, high_b, "bpm")
    elif units == "%pace" and threshold_pace:
        low, high, label = low / 100 * threshold_pace, high / 100 * threshold_pace, "m/s"
    elif units == "pace_zone" and threshold_pace:
        low_b, _ = _zone_bounds(context.get("pace_zones"), low, threshold_pace)
        _, high_b = _zone_bounds(context.get("pace_zones"), high, threshold_pace)
        return _target(kind, low_b, high_b, "m/s")
    elif units in ("bpm",):
        label = "bpm"
    else:
        return None
    return _target(kind, low, high, label)


def _target(kind: str, low: float | None, high: float | None, units: str) -> dict[str, Any] | None:
    if low is None:
        return None
    return {"kind": kind, "low": low, "high": high, "units": units, "source": "resolved from athlete thresholds"}


def classify_step(step: dict[str, Any], target: dict[str, Any] | None, context: dict[str, Any]) -> str:  # pylint: disable=too-many-return-statements
    """'warmup', 'cooldown', 'rest' or 'work' for a planned step."""
    if step.get("warmup"):
        return "warmup"
    if step.get("cooldown"):
        return "cooldown"
    intensity = str(step.get("intensity") or "").lower()
    if intensity in ("rest", "recovery"):
        return "rest"
    if intensity in ("active", "interval"):
        return "work"
    if target:
        mid = (target["low"] + (target["high"] or target["low"])) / 2
        ref = {
            "power": (_num(context.get("ftp")), REST_POWER_FRACTION),
            "hr": (_num(context.get("lthr")), REST_HR_FRACTION),
            "pace": (_num(context.get("threshold_pace")), REST_PACE_FRACTION),
        }[target["kind"]]
        if ref[0] and mid <= ref[0] * ref[1]:
            return "rest"
        return "work"
    text = str(step.get("text") or "").lower()
    if any(word in text for word in ("rest", "recovery", "easy", "locker", "pause", "erholung")):
        return "rest"
    return "work"


def plan_steps(steps: list[Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    """Flattened planned steps with resolved target, kind and planned timeline."""
    planned: list[dict[str, Any]] = []
    clock = 0.0
    for index, step in enumerate(flatten_steps(steps), 1):
        target = resolve_target(step, context)
        duration = _num(step.get("duration"))
        entry = {
            "index": index,
            "text": step.get("text"),
            "kind": classify_step(step, target, context),
            "duration": duration,
            "distance": _num(step.get("distance")),
            "target": target,
            "ramp": bool(step.get("ramp")),
            "rep": step.get("rep"),
            "reps": step.get("reps"),
            "planned_start": clock,
        }
        clock += duration or 0.0
        entry["planned_end"] = clock
        planned.append(entry)
    return planned


# ---------------------------------------------------------------------- alignment
def _interval_intensity(interval: dict[str, Any], kind: str) -> float | None:
    key = {"power": "average_watts", "hr": "average_heartrate", "pace": "average_speed"}[kind]
    return _num(interval.get(key))


def _match_cost(step: dict[str, Any], interval: dict[str, Any]) -> float:
    """0 = perfect match; duration mismatch and target mismatch each add up to ~1."""
    planned = step.get("duration")
    actual = _num(interval.get("elapsed_time"))
    cost = 0.0
    if planned and actual:
        cost += min(1.0, abs(planned - actual) / max(planned, actual))
    else:
        cost += 0.5
    target = step.get("target")
    if target:
        value = _interval_intensity(interval, target["kind"])
        mid = (target["low"] + (target["high"] or target["low"])) / 2
        if value is not None and mid:
            cost += min(1.0, abs(value - mid) / mid)
        else:
            cost += 0.5
    return cost


def align(planned: list[dict[str, Any]], intervals: list[dict[str, Any]]) -> list[tuple[int | None, int | None]]:
    """Order-preserving alignment of planned steps and actual intervals.

    Returns pairs of (planned index, interval index); None marks an unmatched element.
    """
    n, m = len(planned), len(intervals)
    inf = float("inf")
    score = [[inf] * (m + 1) for _ in range(n + 1)]
    trace: list[list[str]] = [[""] * (m + 1) for _ in range(n + 1)]
    score[0][0] = 0.0
    for i in range(1, n + 1):
        score[i][0], trace[i][0] = i * GAP_COST, "up"
    for j in range(1, m + 1):
        score[0][j], trace[0][j] = j * GAP_COST, "left"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            options = (
                (score[i - 1][j - 1] + _match_cost(planned[i - 1], intervals[j - 1]), "diag"),
                (score[i - 1][j] + GAP_COST, "up"),
                (score[i][j - 1] + GAP_COST, "left"),
            )
            score[i][j], trace[i][j] = min(options, key=lambda o: o[0])
    pairs: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        move = trace[i][j]
        if move == "diag":
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif move == "up":
            pairs.append((i - 1, None))
            i -= 1
        else:
            pairs.append((None, j - 1))
            j -= 1
    pairs.reverse()
    return pairs


# ------------------------------------------------------------------- interval metrics
def _slice(stream: dict[str, Any] | None, start: int, end: int) -> list[Any]:
    if stream is None:
        return []
    return (stream.get("data") or [])[start:end]


def _mean(values: list[Any]) -> float | None:
    nums = numeric_values(values)
    return sum(nums) / len(nums) if nums else None


def _fade_pct(values: list[Any]) -> float | None:
    """Second half mean vs first half mean in percent (negative = fading)."""
    nums = numeric_values(values)
    if len(nums) < 20:
        return None
    half = len(nums) // 2
    first, second = sum(nums[:half]) / half, sum(nums[half:]) / (len(nums) - half)
    if not first:
        return None
    return (second - first) / first * 100


def _time_in_target(values: list[Any], target: dict[str, Any]) -> float | None:
    nums = numeric_values(values)
    if not nums:
        return None
    low = target["low"] * (1 - TARGET_TOLERANCE_PCT / 100)
    high = (target["high"] or target["low"]) * (1 + TARGET_TOLERANCE_PCT / 100)
    inside = sum(1 for v in nums if low <= v <= high)
    return inside / len(nums) * 100


def _edge_mean(values: list[Any], tail: bool) -> float | None:
    nums = numeric_values(values)
    if not nums:
        return None
    edge = nums[-EDGE_SAMPLES:] if tail else nums[:EDGE_SAMPLES]
    return sum(edge) / len(edge)


def _pw_hr_drift(watts: list[Any], heartrate: list[Any], secs: float | None) -> float | None:
    """Pw:HR second half vs first half in percent for steady efforts >= 10 min."""
    if not secs or secs < DRIFT_MIN_SECS:
        return None
    pairs = [
        (w, h)
        for w, h in zip(watts, heartrate, strict=False)
        if isinstance(w, (int, float)) and isinstance(h, (int, float)) and h > 0 and w > 0
    ]
    if len(pairs) < 120:
        return None
    half = len(pairs) // 2
    first = sum(w for w, _ in pairs[:half]) / sum(h for _, h in pairs[:half])
    second = sum(w for w, _ in pairs[half:]) / sum(h for _, h in pairs[half:])
    return (second - first) / first * 100 if first else None


def interval_metrics(  # pylint: disable=too-many-locals
    interval: dict[str, Any],
    streams: list[dict[str, Any]],
    target: dict[str, Any] | None,
    next_interval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execution metrics of one actual interval from its samples."""
    start, end = interval.get("start_index"), interval.get("end_index")
    watts = find_stream(streams, "watts")
    heart = find_stream(streams, "heartrate")
    speed = find_stream(streams, "velocity_smooth")
    cadence = find_stream(streams, "cadence")
    secs = _num(interval.get("elapsed_time"))
    metrics: dict[str, Any] = {
        "duration_s": secs,
        "moving_time_s": _num(interval.get("moving_time")),
        "avg_watts": _num(interval.get("average_watts")),
        "max_watts": _num(interval.get("max_watts")),
        "normalized_watts": _num(interval.get("weighted_average_watts")),
        "avg_hr": _num(interval.get("average_heartrate")),
        "max_hr": _num(interval.get("max_heartrate")),
        "avg_speed_m_s": _num(interval.get("average_speed")),
        "gap_m_s": _num(interval.get("gap")),
        "avg_cadence": _num(interval.get("average_cadence")),
        "intensity_pct": _num(interval.get("intensity")),
        "samples_valid": isinstance(start, int) and isinstance(end, int) and end > start,
    }
    if not (isinstance(start, int) and isinstance(end, int) and end > start):
        return metrics
    w_seg, h_seg, s_seg = _slice(watts, start, end), _slice(heart, start, end), _slice(speed, start, end)
    metrics["cadence_nonzero_mean"] = _mean([c for c in _slice(cadence, start, end) if isinstance(c, (int, float)) and c > 0])
    metrics["hr_start"] = _edge_mean(h_seg, tail=False)
    metrics["hr_end"] = _edge_mean(h_seg, tail=True)
    metrics["power_fade_pct"] = _fade_pct(w_seg)
    metrics["speed_fade_pct"] = _fade_pct(s_seg)
    metrics["pw_hr_drift_pct"] = _pw_hr_drift(w_seg, h_seg, secs)
    if target:
        series = {"power": w_seg, "hr": h_seg, "pace": s_seg}[target["kind"]]
        metrics["time_in_target_pct"] = _time_in_target(series, target)
    if next_interval and isinstance(next_interval.get("start_index"), int):
        rest_start = next_interval["start_index"]
        rest_h = _slice(heart, rest_start, rest_start + 60)
        after = _edge_mean(rest_h, tail=True)
        if after is not None and metrics["hr_end"] is not None:
            metrics["hr_recovery_60s_drop"] = metrics["hr_end"] - after
    custom: dict[str, Any] = {}
    for stream in streams:
        if stream.get("custom") and stream.get("type") not in ("time",):
            stats = range_stats(stream.get("data") or [], [(start, end)])
            if stats.get("non_null"):
                custom[str(stream.get("type"))] = {k: stats.get(k) for k in ("first", "last", "min", "max", "mean", "delta")}
    if custom:
        metrics["custom_streams"] = custom
    return metrics


def adherence(target: dict[str, Any] | None, metrics: dict[str, Any]) -> dict[str, Any] | None:
    """Actual value vs target range: status below/in/above and percent of target midpoint."""
    if not target:
        return None
    actual = {"power": metrics.get("avg_watts"), "hr": metrics.get("avg_hr"), "pace": metrics.get("avg_speed_m_s")}[target["kind"]]
    if actual is None:
        return {"status": "no data"}
    low, high = target["low"], target["high"] or target["low"]
    mid = (low + high) / 2
    status = "in range" if low * (1 - TARGET_TOLERANCE_PCT / 100) <= actual <= high * (1 + TARGET_TOLERANCE_PCT / 100) else ("below" if actual < low else "above")
    return {"actual": actual, "status": status, "pct_of_target": actual / mid * 100 if mid else None}


# ------------------------------------------------------------------------ analysis
def _extension(
    rows: list[dict[str, Any]], intervals: list[dict[str, Any]], streams: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Training done after the last planned step was completed (extra riding, extra efforts).

    Trailing intervals without a planned counterpart are summarised as one block with
    its own metrics so that they are reported separately and not as poor compliance.
    """
    matched = [r["interval_index"] for r in rows if r.get("planned") and r.get("metrics")]
    if not matched:
        return None
    last_matched = max(i for i in matched if i is not None)
    extra = [intervals[i] for i in range(last_matched + 1, len(intervals)) if isinstance(intervals[i], dict)]
    if not extra:
        return None
    duration = sum(_num(i.get("elapsed_time")) or 0 for i in extra)
    if duration < EXTENSION_MIN_SECS:
        return None
    starts: list[int] = [int(i["start_index"]) for i in extra if isinstance(i.get("start_index"), int)]
    ends: list[int] = [int(i["end_index"]) for i in extra if isinstance(i.get("end_index"), int)]
    block = {
        "start_index": min(starts) if starts else None,
        "end_index": max(ends) if ends else None,
        "start_time": extra[0].get("start_time"),
        "end_time": extra[-1].get("end_time"),
        "elapsed_time": duration,
        "average_watts": None,
        "average_heartrate": None,
    }
    metrics = interval_metrics(block, streams, None)
    hardest = max(extra, key=lambda i: _num(i.get("average_watts")) or _num(i.get("average_speed")) or 0)
    return {
        "intervals": len(extra),
        "first_interval_index": last_matched + 1,
        "duration_s": duration,
        "start_time": block["start_time"],
        "end_time": block["end_time"],
        "metrics": metrics,
        "hardest_interval": {
            "label": hardest.get("label"),
            "elapsed_time": hardest.get("elapsed_time"),
            "average_watts": hardest.get("average_watts"),
            "max_watts": hardest.get("max_watts"),
            "average_heartrate": hardest.get("average_heartrate"),
            "average_speed": hardest.get("average_speed"),
        },
    }


def analyze(  # pylint: disable=too-many-locals
    planned: list[dict[str, Any]],
    intervals: list[dict[str, Any]],
    streams: list[dict[str, Any]],
) -> dict[str, Any]:
    """Align plan and intervals (or just evaluate intervals) and compute all metrics."""
    rows: list[dict[str, Any]] = []
    pairs = align(planned, intervals) if planned else [(None, j) for j in range(len(intervals))]
    for p_idx, i_idx in pairs:
        step = planned[p_idx] if p_idx is not None else None
        interval = intervals[i_idx] if i_idx is not None else None
        row: dict[str, Any] = {"planned": step, "interval_index": i_idx}
        if interval is not None:
            nxt = intervals[i_idx + 1] if i_idx is not None and i_idx + 1 < len(intervals) else None
            target = step["target"] if step else None
            metrics = interval_metrics(interval, streams, target, nxt)
            row["label"] = interval.get("label") or f"Interval {i_idx + 1 if i_idx is not None else '?'}"
            row["type"] = interval.get("type")
            row["start_time"] = interval.get("start_time")
            row["end_time"] = interval.get("end_time")
            row["metrics"] = metrics
            row["adherence"] = adherence(target, metrics)
            if step and step.get("duration") and metrics.get("duration_s") is not None:
                row["duration_diff_s"] = metrics["duration_s"] - step["duration"]
        rows.append(row)
    planned_total = sum(s.get("duration") or 0 for s in planned)
    actual_total = sum(_num(i.get("elapsed_time")) or 0 for i in intervals)
    work_rows = [r for r in rows if r.get("planned") and r["planned"]["kind"] == "work" and r.get("metrics")]
    in_range = [r for r in work_rows if (r.get("adherence") or {}).get("status") == "in range"]
    extension = _extension(rows, intervals, streams) if planned else None
    if extension:
        # Trailing extra intervals are reported as additional training, not as unmatched rows.
        rows = [r for r in rows if not (r.get("planned") is None and (r.get("interval_index") or -1) >= extension["first_interval_index"])]
    matched_actual = sum(_num(intervals[r["interval_index"]].get("elapsed_time")) or 0 for r in rows if r.get("planned") and r.get("metrics") and r.get("interval_index") is not None)
    summary = {
        "planned_steps": len(planned),
        "actual_intervals": len(intervals),
        "matched": sum(1 for r in rows if r.get("planned") and r.get("metrics")),
        "unmatched_planned": sum(1 for r in rows if r.get("planned") and not r.get("metrics")),
        "unmatched_intervals": sum(1 for r in rows if not r.get("planned") and r.get("metrics")),
        "planned_total_s": planned_total or None,
        "actual_total_s": actual_total or None,
        "plan_part_actual_s": matched_actual or None,
        "extension_s": extension["duration_s"] if extension else 0,
        "extended_beyond_plan": bool(extension),
        "work_steps_in_range": f"{len(in_range)}/{len(work_rows)}" if work_rows else None,
    }
    return {"rows": rows, "summary": summary, "extension": extension}


# ------------------------------------------------------------------------ rendering
def _fmt(value: Any, digits: int = 0, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}{suffix}"


def _target_text(target: dict[str, Any] | None) -> str:
    if not target:
        return "no target"
    low, high, units = target["low"], target["high"], target["units"]
    if target["kind"] == "pace":
        return f"{format_pace(low)}" + (f" to {format_pace(high)}" if high and high != low else "")
    span = f"{low:.0f}" + (f"-{high:.0f}" if high and high != low else "")
    return f"{span} {units}"


def _metric_lines(metrics: dict[str, Any], target: dict[str, Any] | None, pace_based: bool) -> list[str]:
    lines: list[str] = []
    if pace_based or metrics.get("avg_speed_m_s") and not metrics.get("avg_watts"):
        lines.append(
            f"    pace {format_pace(metrics.get('avg_speed_m_s'))} (GAP {format_pace(metrics.get('gap_m_s'))}), "
            f"speed fade {_fmt(metrics.get('speed_fade_pct'), 1, '%')}"
        )
    if metrics.get("avg_watts") is not None:
        lines.append(
            f"    power avg {_fmt(metrics.get('avg_watts'))} W, NP {_fmt(metrics.get('normalized_watts'))} W, "
            f"max {_fmt(metrics.get('max_watts'))} W, fade {_fmt(metrics.get('power_fade_pct'), 1, '%')}"
        )
    if metrics.get("avg_hr") is not None:
        hr = f"    HR avg {_fmt(metrics.get('avg_hr'))}, max {_fmt(metrics.get('max_hr'))}, start {_fmt(metrics.get('hr_start'))} -> end {_fmt(metrics.get('hr_end'))} bpm"
        if metrics.get("hr_recovery_60s_drop") is not None:
            hr += f", drop in first 60 s of next interval {_fmt(metrics['hr_recovery_60s_drop'])} bpm"
        if metrics.get("pw_hr_drift_pct") is not None:
            hr += f", Pw:HR drift {_fmt(metrics['pw_hr_drift_pct'], 1, '%')}"
        lines.append(hr)
    if metrics.get("cadence_nonzero_mean") is not None:
        lines.append(f"    cadence {_fmt(metrics['cadence_nonzero_mean'])} rpm (non-zero samples)")
    if target and metrics.get("time_in_target_pct") is not None:
        lines.append(f"    time in target range (±{TARGET_TOLERANCE_PCT:.0f}%): {_fmt(metrics['time_in_target_pct'], 0, '%')}")
    unchanged: list[str] = []
    for code, stats in (metrics.get("custom_streams") or {}).items():
        if stats.get("min") == stats.get("max"):
            unchanged.append(code)
            continue
        lines.append(
            f"    {code}: start {_fmt(stats.get('first'), 1)}, end {_fmt(stats.get('last'), 1)}, "
            f"min {_fmt(stats.get('min'), 1)}, max {_fmt(stats.get('max'), 1)}, delta {_fmt(stats.get('delta'), 1)}"
        )
    if unchanged:
        lines.append(f"    unchanged custom streams: {', '.join(unchanged)}")
    return lines


def format_execution(result: dict[str, Any], header: str, pace_based: bool = False) -> str:  # pylint: disable=too-many-branches
    """Readable report of an execution analysis."""
    summary = result["summary"]
    lines = [header, ""]
    if summary["planned_steps"]:
        lines.append(
            f"Plan: {summary['planned_steps']} steps, {hms(summary['planned_total_s'])} planned | "
            f"Actual: {summary['actual_intervals']} intervals, {hms(summary['actual_total_s'])} in total | "
            f"matched {summary['matched']}, planned without match {summary['unmatched_planned']}, "
            f"extra intervals inside the plan {summary['unmatched_intervals']} | work steps in target: {summary['work_steps_in_range'] or 'n/a'}"
        )
        if summary.get("extended_beyond_plan"):
            lines.append(
                f"The activity was extended beyond the plan: plan part {hms(summary.get('plan_part_actual_s'))}, "
                f"additional training {hms(summary['extension_s'])} (reported separately below, not counted against the plan)."
            )
    else:
        lines.append(f"No planned workout: {summary['actual_intervals']} intervals, {hms(summary['actual_total_s'])} in total")
    lines.append("")
    for row in result["rows"]:
        step = row.get("planned")
        metrics = row.get("metrics")
        if step:
            rep = f" (rep {step['rep']}/{step['reps']})" if step.get("rep") else ""
            head = f"[plan {step['index']}] {step['kind']}{rep} {hms(step['duration'])} @ {_target_text(step['target'])}"
            if step.get("text"):
                head += f" '{step['text']}'"
        else:
            head = "[no planned step]"
        if metrics:
            actual = f"-> {row['label']} ({row.get('type')}) {hms(metrics.get('duration_s'))} from {hms(row.get('start_time'))}"
            if row.get("duration_diff_s") is not None:
                actual += f", {row['duration_diff_s']:+.0f} s vs plan"
            adh = row.get("adherence")
            if adh and adh.get("status"):
                actual += f", {adh['status']}" + (f" ({adh['pct_of_target']:.0f}% of target)" if adh.get("pct_of_target") else "")
            lines.append(f"{head} {actual}")
            lines.extend(_metric_lines(metrics, step["target"] if step else None, pace_based))
        else:
            lines.append(f"{head} -> not executed / no matching interval")
    extension = result.get("extension")
    if extension:
        lines.append("")
        lines.append(
            f"Additional training after the plan: {hms(extension['duration_s'])} from {hms(extension.get('start_time'))} "
            f"to {hms(extension.get('end_time'))} ({extension['intervals']} interval(s))"
        )
        lines.extend(_metric_lines(extension["metrics"], None, pace_based))
        hardest = extension.get("hardest_interval") or {}
        if hardest.get("elapsed_time"):
            lines.append(
                f"    hardest part: {hms(hardest['elapsed_time'])} at avg {_fmt(hardest.get('average_watts'))} W "
                f"(max {_fmt(hardest.get('max_watts'))} W), HR {_fmt(hardest.get('average_heartrate'))} bpm"
                if hardest.get("average_watts") is not None
                else f"    hardest part: {hms(hardest['elapsed_time'])} at {format_pace(hardest.get('average_speed'))}"
            )
    return "\n".join(lines)
