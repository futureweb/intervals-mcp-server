"""
Planned-vs-executed workout analysis.

Takes a planned workout document (steps with durations and power / HR / pace targets),
the intervals Intervals.icu detected on the activity and the sample-aligned streams,
aligns planned steps with actual intervals and computes per-step execution metrics
(duration, target adherence, time in target range, HR response, fade, stamina change,
Pw:HR drift). Without a plan the same metrics are computed per interval.

Alignment is an order-preserving sequence alignment (Needleman-Wunsch style): planned
steps are matched with one actual interval or with any number of consecutive intervals (an
effort split by laps, e.g. 1 km device auto-laps on a run, or a stop), with a cost based
on the planned duration (or distance) and the target intensity measured on the planned
part of the span. A step absorbs a further interval only while the intervals after it do
not yet cover its planned duration. Interval names are never used. Extra intervals before
the first or after the last planned step are cheap when short (the cost grows with their
duration), so riding before or after the workout does not distort the pairing.

Lap boundaries need not coincide with step boundaries: when one step runs longer than
planned and the adjacent step is correspondingly short, the boundary is moved on the
continuous timeline (time carried over) when the moved samples fit the receiving step at
least as well, instead of reporting a pair of opposite false deviations.

Planned steps are capped in time: when the matched span is longer than the planned
duration plus a tolerance, it is split logically (analysis only, nothing is changed on
Intervals.icu). The planned part is evaluated against the plan from the samples; the
remainder of the last planned step plus everything after it is reported as additional
training after the plan, the remainder of an earlier step as extra time inside the plan.
Durations are compared on moving time (recording pauses excluded). Distance-based steps
are compared on distance. Open-ended targets (top zone, a range given by its start only)
are lower bounds, not point targets. Nothing is interpolated: every metric is computed
from recorded samples only, time-weighted by the sample spacing.
"""

# pylint: disable=too-many-lines

from dataclasses import dataclass
from typing import Any

from intervals_mcp_server.utils.custom_fields import is_missing
from intervals_mcp_server.utils.sports import cadence_text, default_pace_units, format_pace, hms
from intervals_mcp_server.utils.streams import (
    NP_WINDOW_S,
    foreign_stream_reason,
    is_counter_stream,
    numeric_values,
    range_stats,
    rolling_fourth_powers,
)

GAP_COST = 0.8
EDGE_EXTRA_COST = 0.1  # an extra interval before the first / after the last planned step ...
EDGE_COST_PER_HOUR = 0.6  # ... plus this per hour of its moving time, so a long block is never cheap
MERGE_PENALTY = 0.05  # once for a planned step matched with several consecutive intervals
MAX_MERGE_WITHOUT_AMOUNT = 3  # a step without duration or distance absorbs at most this many intervals
MAX_MERGE_PARTS = 250  # upper limit of intervals in one step (bounds the alignment's run time)
MERGE_PART_MAX_COST = 0.3  # a merged interval may be at most this much further off target than the span
TARGET_TOLERANCE_PCT = 5.0
EXTENSION_MIN_SECS = 120  # additional training shorter than this is not reported separately
# Planned steps whose target lies in the recovery zone (Z1) are "rest"; without zone settings
# these fractions of FTP / threshold speed / LTHR are used (default Intervals.icu Z1 tops).
RECOVERY_POWER_FRACTION = 0.55
RECOVERY_PACE_FRACTION = 0.775
RECOVERY_HR_FRACTION = 0.80
# A step up to these fractions that sits between two clearly harder steps is a recovery
# between efforts ("rest") even above Z1, e.g. 4 min at 60 % FTP between threshold efforts.
REST_POWER_FRACTION = 0.65
REST_PACE_FRACTION = 0.80
REST_HR_FRACTION = 0.85
HARDER_FACTOR = 1.1  # a neighbour is "clearly harder" when its target midpoint is 10 % higher
EDGE_SAMPLES = 10
DRIFT_MIN_SECS = 600
RECORDING_GAP_S = 5  # a jump in the time stream larger than this is a recording pause
EXTRA_EFFORT_MIN_FTP = 0.9  # trailing intervals at or above this fraction of FTP are listed as efforts
MAX_LISTED_EFFORTS = 5
TIME_IN_TARGET_LOW_PCT = 50.0
STREAM_KEYS = {"power": "watts", "hr": "heartrate", "pace": "velocity_smooth"}
INTERVAL_KEYS = {"power": "average_watts", "hr": "average_heartrate", "pace": "average_speed"}
DETAIL_LEVELS = ("compact", "standard", "full")
DRIFT_CONVENTION = "positive = heart rate rose relative to power (Intervals.icu decoupling convention)"


@dataclass(frozen=True)
class Tolerances:
    """Tolerances of the execution analysis.

    duration_pct / duration_min_s: a step may be this much longer or shorter than planned
    (the larger of both) before it is split (longer) or flagged as short.
    start_shift_s: a step starting more than this away from the plan timeline is flagged.
    pause_s: recording pauses inside a step up to this total are not reported.
    target_pct: average within the target range widened by this percentage counts as in range.
    """

    duration_pct: float = 10.0
    duration_min_s: float = 30.0
    start_shift_s: float = 120.0
    pause_s: float = 60.0
    target_pct: float = TARGET_TOLERANCE_PCT

    def duration_allowance(self, planned: float) -> float:
        """Seconds a step may deviate from its planned duration."""
        return max(self.duration_min_s, planned * self.duration_pct / 100)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or is_missing(value):
        return None
    return float(value)


# --------------------------------------------------------------------------- plan
def _flatten(steps: list[Any], block: tuple[int, ...] = ()) -> list[tuple[dict[str, Any], tuple[int, ...]]]:
    """Flattened steps with a key naming their position inside the repeat blocks."""
    flat: list[tuple[dict[str, Any], tuple[int, ...]]] = []
    for position, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        reps = step.get("reps")
        children = step.get("steps")
        if isinstance(reps, int) and reps > 0 and isinstance(children, list):
            for rep in range(1, reps + 1):
                for child, key in _flatten(children, block + (position,)):
                    child = dict(child)
                    child["rep"] = rep
                    child["reps"] = reps
                    flat.append((child, key))
            continue
        if step.get("duration") is None and step.get("distance") is None and not step.get("text"):
            continue
        flat.append((dict(step), block + (position,)))
    return flat


def flatten_steps(steps: list[Any]) -> list[dict[str, Any]]:
    """Expand repeat blocks into the flat list of planned steps in execution order."""
    return [step for step, _ in _flatten(steps)]


def _range_from_value(value: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    """(low, high, units) of a workout target value.

    A single value gives low == high; a range given by its start only is open-ended
    (high None = no upper limit).
    """
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
    """Absolute bounds of zone number ``zone`` from Intervals.icu upper bounds (% or bpm).

    The upper bound of the top zone (999 in Intervals.icu) is None: no upper limit.
    """
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
    Returns {"kind": "power"|"hr"|"pace", "low", "high", "units", "source"} or None;
    ``high`` None means open-ended (a lower bound only, e.g. the top zone).
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


def _zone_target(kind: str, bounds: Any, low: float, high: float | None, scale: float | None, units: str) -> dict[str, Any] | None:
    """Target of a zone or zone range; a zone range given by its start only is open-ended."""
    low_b, high_b = _zone_bounds(bounds, low, scale)
    if high is None:
        high_b = None
    elif high != low:
        _, high_b = _zone_bounds(bounds, high, scale)
    return _target(kind, low_b, high_b, units)


def _resolve_raw_target(  # pylint: disable=too-many-return-statements
    kind: str, raw: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any] | None:
    """Convert a raw target (%ftp, w, %lthr, hr_zone, %pace ...) to absolute values."""
    low, high, units = _range_from_value(raw)
    if low is None:
        return None
    ftp, lthr, max_hr = _num(context.get("ftp")), _num(context.get("lthr")), _num(context.get("max_hr"))
    threshold_pace = _num(context.get("threshold_pace"))
    label = {"w": "W", "%ftp": "W", "%lthr": "bpm", "%hr": "bpm", "bpm": "bpm", "%pace": "m/s"}.get(str(units), "")

    def scaled(reference: float) -> dict[str, Any] | None:
        return _target(kind, low / 100 * reference, None if high is None else high / 100 * reference, label)

    if units in ("w", "bpm"):
        return _target(kind, low, high, label)
    if units == "%ftp" and ftp:
        return scaled(ftp)
    if units == "%lthr" and lthr:
        return scaled(lthr)
    if units == "%hr" and max_hr:
        return scaled(max_hr)
    if units == "%pace" and threshold_pace:
        return scaled(threshold_pace)
    if units == "power_zone" and ftp:
        return _zone_target(kind, context.get("power_zones"), low, high, ftp, "W")
    if units == "hr_zone":
        return _zone_target(kind, context.get("hr_zones"), low, high, None, "bpm")
    if units == "pace_zone" and threshold_pace:
        return _zone_target(kind, context.get("pace_zones"), low, high, threshold_pace, "m/s")
    return None


def _target(kind: str, low: float | None, high: float | None, units: str) -> dict[str, Any] | None:
    if low is None:
        return None
    return {"kind": kind, "low": low, "high": high, "units": units, "source": "resolved from athlete thresholds"}


def _target_mid(target: dict[str, Any]) -> float:
    """Midpoint of a target range; the lower bound of an open-ended target."""
    high = target["high"]
    return target["low"] if high is None else (target["low"] + high) / 2


def _reference(kind: str, context: dict[str, Any]) -> float | None:
    key = {"power": "ftp", "hr": "lthr", "pace": "threshold_pace"}[kind]
    return _num(context.get(key))


def _recovery_ceiling(kind: str, context: dict[str, Any]) -> float | None:
    """Top of the recovery zone (Z1) in absolute units: from the zone settings, else a default fraction."""
    zones = numeric_values(context.get({"power": "power_zones", "hr": "hr_zones", "pace": "pace_zones"}[kind]) or [])
    if kind == "hr" and zones:
        return zones[0]
    reference = _reference(kind, context)
    if not reference:
        return None
    if zones:
        return zones[0] / 100 * reference
    return reference * {"power": RECOVERY_POWER_FRACTION, "hr": RECOVERY_HR_FRACTION, "pace": RECOVERY_PACE_FRACTION}[kind]


def _explicit_kind(step: dict[str, Any]) -> str | None:
    """Kind set by the plan itself (warm-up/cool-down flags, step intensity)."""
    if step.get("warmup"):
        return "warmup"
    if step.get("cooldown"):
        return "cooldown"
    intensity = str(step.get("intensity") or "").lower()
    if intensity in ("rest", "recovery"):
        return "rest"
    if intensity in ("active", "interval"):
        return "work"
    return None


def classify_step(step: dict[str, Any], target: dict[str, Any] | None, context: dict[str, Any]) -> str:
    """'warmup', 'cooldown', 'rest' or 'work' for a planned step on its own.

    Without warm-up/cool-down flag or explicit intensity, a target in the recovery zone (Z1
    of the athlete's zones) makes a step "rest"; easy aerobic (Z2) steps are "work" (steady
    efforts). ``plan_steps`` additionally treats a step between two clearly harder steps as
    a recovery between efforts.
    """
    explicit = _explicit_kind(step)
    if explicit:
        return explicit
    if target:
        ceiling = _recovery_ceiling(target["kind"], context)
        return "rest" if ceiling and _target_mid(target) <= ceiling else "work"
    text = str(step.get("text") or "").lower()
    if any(word in text for word in ("rest", "recovery", "easy", "locker", "pause", "erholung")):
        return "rest"
    return "work"


def _recovery_between_efforts(planned: list[dict[str, Any]], context: dict[str, Any]) -> set[tuple[int, ...]]:
    """Repeat positions of target-classified work steps that sit between two clearly harder steps."""
    keys: set[tuple[int, ...]] = set()
    for pos, entry in enumerate(planned):
        target = entry["target"]
        if entry["kind"] != "work" or entry["_explicit"] or not target or pos == 0 or pos + 1 >= len(planned):
            continue
        reference = _reference(target["kind"], context)
        fraction = {"power": REST_POWER_FRACTION, "hr": REST_HR_FRACTION, "pace": REST_PACE_FRACTION}[target["kind"]]
        mid = _target_mid(target)
        if not reference or mid > reference * fraction:
            continue
        neighbours = (planned[pos - 1]["target"], planned[pos + 1]["target"])
        if all(t and t["kind"] == target["kind"] and _target_mid(t) >= mid * HARDER_FACTOR for t in neighbours):
            keys.add(entry["_key"])
    return keys


def _estimated_duration(duration: float | None, distance: float | None, target: dict[str, Any] | None) -> float | None:
    """Planned seconds: the duration, or a distance divided by the target speed of a pace step."""
    if duration:
        return duration
    if distance and target and target["kind"] == "pace" and target["low"]:
        return distance / _target_mid(target)
    return None


def plan_steps(steps: list[Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    """Flattened planned steps with resolved target, kind and planned timeline.

    ``planned_start``/``planned_end`` are seconds on the plan clock; a distance step adds
    its estimated duration (distance / target speed for pace targets, ``est_duration``) and
    the clock is unknown (None) after a distance step without a pace target.
    """
    planned: list[dict[str, Any]] = []
    clock: float | None = 0.0
    for index, (step, key) in enumerate(_flatten(steps), 1):
        target = resolve_target(step, context)
        duration = _num(step.get("duration"))
        distance = _num(step.get("distance"))
        estimate = _estimated_duration(duration, distance, target)
        entry = {
            "index": index,
            "text": step.get("text"),
            "kind": classify_step(step, target, context),
            "duration": duration,
            "distance": distance,
            "est_duration": round(estimate, 1) if estimate is not None else None,
            "target": target,
            "ramp": bool(step.get("ramp")),
            "rep": step.get("rep"),
            "reps": step.get("reps"),
            "planned_start": clock,
            "_explicit": _explicit_kind(step) is not None,
            "_key": key,
        }
        if clock is not None:
            clock = clock + estimate if estimate is not None else (clock if not distance else None)
        entry["planned_end"] = clock
        planned.append(entry)
    recoveries = _recovery_between_efforts(planned, context)
    for entry in planned:
        if entry.pop("_key") in recoveries:
            entry["kind"] = "rest"
        entry.pop("_explicit")
    return planned


# ----------------------------------------------------------------------- profile
class _Prefix:
    """Time-weighted prefix sums of a numeric stream for O(1) means over index ranges."""

    def __init__(self, data: list[Any], weights: list[float]) -> None:
        self.sums = [0.0]
        self.weights = [0.0]
        for value, weight in zip(data, weights, strict=False):
            number = _num(value)
            self.sums.append(self.sums[-1] + (number * weight if number is not None else 0.0))
            self.weights.append(self.weights[-1] + (weight if number is not None else 0.0))
        while len(self.sums) < len(weights) + 1:  # stream shorter than the time stream
            self.sums.append(self.sums[-1])
            self.weights.append(self.weights[-1])

    def mean(self, start: int, end: int) -> float | None:
        """Time-weighted mean over [start, end); None without numeric samples."""
        start, end = max(0, start), min(end, len(self.sums) - 1)
        if end <= start:
            return None
        weight = self.weights[end] - self.weights[start]
        return (self.sums[end] - self.sums[start]) / weight if weight > 0 else None

    def total(self, start: int, end: int) -> float:
        """Time-weighted sum over [start, end) (e.g. joules for a power stream)."""
        start, end = max(0, start), min(end, len(self.sums) - 1)
        return self.sums[end] - self.sums[start] if end > start else 0.0


class Profile:  # pylint: disable=too-many-instance-attributes
    """Sample-aligned numeric view of an activity's streams.

    ``time`` is the time stream (seconds since start, jumping across recording pauses);
    without it the sample index is used (1 Hz assumption, flagged by ``time_from_index``).
    Each sample is weighted by its spacing to the next sample, a spacing longer than
    ``RECORDING_GAP_S`` (a recording pause) counting as one second.
    """

    def __init__(self, streams: list[dict[str, Any]]) -> None:
        self.streams = [s for s in streams if isinstance(s, dict)]
        self.raw: dict[str, list[Any]] = {
            str(s.get("type")): list(s.get("data") or []) for s in self.streams if isinstance(s.get("data"), list)
        }
        time_raw = self.raw.get("time") or []
        self.time_from_index = not any(_num(t) is not None for t in time_raw)
        self.n = len(time_raw) if not self.time_from_index else max((len(d) for d in self.raw.values()), default=0)
        self.time: list[float] = []
        previous = -1.0
        for index in range(self.n):
            value = _num(time_raw[index]) if index < len(time_raw) else None
            value = float(index) if self.time_from_index else (value if value is not None else previous + 1)
            self.time.append(value)
            previous = value
        self.weights: list[float] = []
        self.paused = [0.0]
        for index in range(self.n):
            step = self.time[index + 1] - self.time[index] if index + 1 < self.n else 1.0
            pause = step > RECORDING_GAP_S
            self.weights.append(1.0 if pause or step <= 0 else step)
            self.paused.append(self.paused[-1] + (step - 1.0 if pause else 0.0))
        self._prefix: dict[str, _Prefix] = {}
        self._np: tuple[list[float], list[int]] | None = None

    def has(self, name: str) -> bool:
        """True when the stream exists with at least one numeric sample."""
        return any(_num(v) is not None for v in self.raw.get(name) or [])

    def prefix(self, name: str) -> _Prefix | None:
        """Cached prefix sums of a stream; None when the stream is missing."""
        if name not in self._prefix:
            if not self.has(name):
                return None
            self._prefix[name] = _Prefix(self.raw[name][: self.n], self.weights)
        return self._prefix[name]

    def at(self, index: int) -> float:
        """Time at a sample index; the index after the last sample is one second after it."""
        if self.n == 0:
            return float(index)
        if index >= self.n:
            return self.time[-1] + 1.0 + (index - self.n)
        return self.time[max(index, 0)]

    def elapsed(self, start: int, end: int) -> float:
        """Elapsed seconds of [start, end) including recording pauses."""
        return max(0.0, self.at(end) - self.at(start))

    def active(self, start: int, end: int) -> float:
        """Seconds of [start, end) without recording pauses (the moving clock)."""
        if self.n == 0:
            return max(0.0, float(end - start))
        low, high = max(0, min(start, self.n)), max(0, min(end, self.n))
        paused = self.paused[high] - self.paused[low]
        return max(0.0, self.elapsed(start, end) - paused)

    def cap_by_time(self, start: int, end: int, secs: float) -> int:
        """Smallest index k in (start, end] whose moving time since start reaches ``secs``."""
        if self.active(start, end) <= secs:
            return end
        low, high = start + 1, end
        while low < high:
            mid = (low + high) // 2
            if self.active(start, mid) >= secs:
                high = mid
            else:
                low = mid + 1
        return low

    def cap_by_distance(self, start: int, end: int, metres: float) -> int | None:
        """Smallest index whose distance from ``start`` reaches ``metres``; None without distance."""
        distance = self.raw.get("distance")
        base = _num(distance[start]) if distance and start < len(distance) else None
        if base is None or distance is None:
            return None
        for index in range(start + 1, min(end, len(distance))):
            value = _num(distance[index])
            if value is not None and value - base >= metres:
                return index
        return end

    def mean(self, name: str, start: int, end: int) -> float | None:
        """Time-weighted mean of a stream over [start, end)."""
        prefix = self.prefix(name)
        return prefix.mean(start, end) if prefix else None

    def distance(self, start: int, end: int) -> float | None:
        """Metres covered over [start, end) from the cumulative distance stream; None without it.

        As for an Intervals.icu interval, the range runs to the first sample after it (the
        start of the next interval), so consecutive ranges add up without gaps.
        """
        data = self.raw.get("distance") or []
        if end <= start or start >= len(data):
            return None
        first = _num(data[start])
        last = next((_num(v) for v in reversed(data[start : min(end, len(data) - 1) + 1]) if _num(v) is not None), None)
        return max(0.0, last - first) if first is not None and last is not None else None

    def normalized_power(self, start: int, end: int) -> float | None:
        """NP of [start, end) with the 30 s rolling mean taken over the whole activity.

        Same method as the NP of an Intervals.icu interval, so a split or merged step is
        comparable with the interval rows; None with less than 30 power samples.
        """
        if self._np is None:
            if not self.has("watts"):
                return None
            fourth = rolling_fourth_powers(self.time, self.raw["watts"][: self.n])
            sums, counts = [0.0], [0]
            for value in fourth:
                sums.append(sums[-1] + (value or 0.0))
                counts.append(counts[-1] + (value is not None))
            self._np = (sums, counts)
        sums, counts = self._np
        start, end = max(0, start), min(end, len(sums) - 1)
        if end <= start or counts[end] - counts[start] < NP_WINDOW_S:
            return None
        return float(((sums[end] - sums[start]) / (counts[end] - counts[start])) ** 0.25)

    def values(self, name: str, start: int, end: int) -> list[Any]:
        """Raw samples of a stream over [start, end)."""
        return (self.raw.get(name) or [])[start:end]


# ---------------------------------------------------------------------- alignment
@dataclass
class _Span:  # pylint: disable=too-many-instance-attributes
    """Indices and timing of one actual interval together with the raw interval dict."""

    index: int
    start: int | None
    end: int | None
    elapsed: float
    active: float
    interval: dict[str, Any]
    distance: float | None = None


def _spans(intervals: list[dict[str, Any]], profile: Profile) -> list[_Span]:
    spans: list[_Span] = []
    for index, interval in enumerate(intervals):
        raw_start, raw_end = interval.get("start_index"), interval.get("end_index")
        start = raw_start if isinstance(raw_start, int) and not isinstance(raw_start, bool) else None
        end = raw_end if isinstance(raw_end, int) and not isinstance(raw_end, bool) else None
        if start is None or end is None or end <= start or (profile.n and end > profile.n):
            start = end = None
        elapsed = _num(interval.get("elapsed_time")) or 0.0
        distance = _num(interval.get("distance"))
        if start is not None and end is not None and profile.n:
            elapsed = profile.elapsed(start, end) or elapsed
            active = profile.active(start, end)
            distance = distance if distance is not None else profile.distance(start, end)
        else:
            active = _num(interval.get("moving_time")) or elapsed
        spans.append(_Span(index, start, end, elapsed, active, interval, distance))
    return spans


def _span_intensity(parts: list[_Span], kind: str, profile: Profile, cap_secs: float | None) -> float | None:
    """Time-weighted intensity of consecutive spans, on the first ``cap_secs`` moving seconds."""
    stream = STREAM_KEYS[kind]
    first, last = parts[0], parts[-1]
    if profile.has(stream) and first.start is not None and last.end is not None:
        end = profile.cap_by_time(first.start, last.end, cap_secs) if cap_secs else last.end
        return profile.mean(stream, first.start, end)
    weighted = [(_num(p.interval.get(INTERVAL_KEYS[kind])), p.active or p.elapsed) for p in parts]
    usable = [(value, weight) for value, weight in weighted if value is not None and weight]
    if not usable:
        return None
    return sum(v * w for v, w in usable) / sum(w for _, w in usable)


def _intensity_cost(value: float | None, target: dict[str, Any]) -> float:
    """0 inside the target range (widened by the tolerance), rising with the relative distance.

    An open-ended target (``high`` None) is a lower bound: any value above it costs 0.
    """
    if value is None:
        return 0.5
    low, high = target["low"], target["high"]
    if not low:
        return 0.5
    upper = float("inf") if high is None else high * (1 + TARGET_TOLERANCE_PCT / 100)
    if low * (1 - TARGET_TOLERANCE_PCT / 100) <= value <= upper:
        return 0.0
    distance = (low - value) / low if value < low else (value - high) / high
    return min(1.0, abs(distance) * 2)


def _amount_cost(planned: float | None, actual: float | None) -> float:
    """Cost of a planned duration (or distance) against the actual one."""
    if not planned or not actual:
        return 0.25
    if actual >= planned:  # a longer span is capped; the remainder is reported separately
        return min(0.3, (actual - planned) / planned * 0.15)
    return min(1.0, (planned - actual) / planned * 1.5)


def _planned_amount(step: dict[str, Any], has_distance: bool) -> tuple[str, float] | None:
    """("time", seconds) for a timed step, ("distance", metres) for a distance step when the
    activity has distances (else its estimated duration), None without either."""
    if step.get("duration"):
        return "time", float(step["duration"])
    if step.get("distance") and has_distance:
        return "distance", float(step["distance"])
    if step.get("est_duration"):
        return "time", float(step["est_duration"])
    return None


def _match_options(  # pylint: disable=too-many-locals,too-many-branches
    step: dict[str, Any], spans: list[_Span], j: int, profile: Profile, part_cost: Any
) -> list[tuple[int, float]]:
    """(k, cost) for matching ``step`` with the k consecutive spans ending before index ``j``.

    A span of several intervals is only valid when neither its first nor its last interval
    is superfluous: the intervals without the first and the intervals without the last
    must each stay short of the planned duration (or distance). Steps without either absorb
    at most ``MAX_MERGE_WITHOUT_AMOUNT`` intervals. Every merged interval must be adjacent
    to the next and about as close to the target as the whole span.
    """
    amount = _planned_amount(step, j > 0 and spans[j - 1].distance is not None)
    target = step.get("target")
    weight = 1.0 if step.get("kind") == "work" else 0.5
    cap_secs = step.get("duration") or step.get("est_duration")
    options: list[tuple[int, float]] = []
    last = spans[j - 1] if j else None
    covered_time, covered_distance, worst_part = 0.0, 0.0, 0.0
    weighted, weights = 0.0, 0.0  # interval averages, for intervals without sample indices
    stream = STREAM_KEYS[target["kind"]] if target else ""
    use_stream = bool(target) and last is not None and last.end is not None and profile.has(stream)
    for k in range(1, min(j, MAX_MERGE_PARTS) + 1):
        part = spans[j - k]
        if k > 1:
            following = spans[j - k + 1]
            if part.end is not None and following.start is not None and following.start != part.end:
                break
            if amount is None and k > MAX_MERGE_WITHOUT_AMOUNT:
                break
            if amount is not None and last is not None:
                if amount[0] == "time":
                    without_first, without_last = covered_time, covered_time - last.active + part.active
                else:
                    without_first = covered_distance
                    without_last = covered_distance - (last.distance or 0.0) + (part.distance or 0.0)
                if without_first >= amount[1] or without_last >= amount[1]:
                    break
        covered_time += part.active
        covered_distance += part.distance or 0.0
        if amount is None:
            cost = 0.25
        else:
            cost = _amount_cost(amount[1], covered_time if amount[0] == "time" else covered_distance or None)
        cost += MERGE_PENALTY if k > 1 else 0.0
        if not target:
            options.append((k, cost + 0.25))
            continue
        worst_part = max(worst_part, part_cost(j - k))
        average = _num(part.interval.get(INTERVAL_KEYS[target["kind"]]))
        if average is not None and (part.active or part.elapsed):
            weighted += average * (part.active or part.elapsed)
            weights += part.active or part.elapsed
        if use_stream and part.start is not None and last is not None and last.end is not None:
            end = profile.cap_by_time(part.start, last.end, cap_secs) if cap_secs else last.end
            value = profile.mean(stream, part.start, end)
        else:  # as _span_intensity without samples: time-weighted interval averages
            value = weighted / weights if weights else None
        intensity = _intensity_cost(value, target)
        if k > 1 and worst_part > intensity + MERGE_PART_MAX_COST:
            continue
        options.append((k, cost + weight * intensity))
    return options


def align_spans(  # pylint: disable=too-many-locals,too-many-branches
    planned: list[dict[str, Any]], intervals: list[dict[str, Any]], profile: Profile | None = None
) -> list[tuple[int | None, list[int]]]:
    """Order-preserving alignment of planned steps with one or more consecutive intervals.

    Returns (planned index or None, [interval indices]) in order; an empty list marks a
    planned step without a matching interval, None an interval without a planned step.
    """
    profile = profile or Profile([])
    spans = _spans(intervals, profile)
    n, m = len(planned), len(spans)
    inf = float("inf")
    part_costs: dict[tuple[int, int], float] = {}

    def part_cost_of(step_index: int) -> Any:
        target = planned[step_index].get("target")

        def cost(span_index: int) -> float:
            key = (step_index, span_index)
            if key not in part_costs:
                value = _span_intensity([spans[span_index]], target["kind"], profile, None) if target else None
                part_costs[key] = _intensity_cost(value, target) if target else 0.0
            return part_costs[key]

        return cost

    score = [[inf] * (m + 1) for _ in range(n + 1)]
    trace: list[list[tuple[str, int]]] = [[("", 0)] * (m + 1) for _ in range(n + 1)]
    score[0][0] = 0.0
    for i in range(n + 1):
        part_cost = part_cost_of(i - 1) if i > 0 else None
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            best, move = inf, ("", 0)
            if i > 0 and score[i - 1][j] + GAP_COST < best:
                best, move = score[i - 1][j] + GAP_COST, ("up", 0)
            if j > 0:
                if i in (0, n):
                    extra = EDGE_EXTRA_COST + EDGE_COST_PER_HOUR * (spans[j - 1].active or spans[j - 1].elapsed) / 3600
                else:
                    extra = GAP_COST
                if score[i][j - 1] + extra < best:
                    best, move = score[i][j - 1] + extra, ("left", 0)
            if i > 0:
                for k, match in _match_options(planned[i - 1], spans, j, profile, part_cost):
                    if score[i - 1][j - k] == inf:
                        continue
                    cost = score[i - 1][j - k] + match
                    if cost < best:
                        best, move = cost, ("diag", k)
            score[i][j], trace[i][j] = best, move
    result: list[tuple[int | None, list[int]]] = []
    i, j = n, m
    while i > 0 or j > 0:
        step, k = trace[i][j]
        if step == "diag":
            result.append((i - 1, list(range(j - k, j))))
            i, j = i - 1, j - k
        elif step == "up":
            result.append((i - 1, []))
            i -= 1
        else:
            result.append((None, [j - 1]))
            j -= 1
    result.reverse()
    return result


def align(planned: list[dict[str, Any]], intervals: list[dict[str, Any]]) -> list[tuple[int | None, int | None]]:
    """Pairs of (planned index, interval index) without stream data (None = unmatched).

    A planned step matched with several consecutive intervals yields one pair per interval.
    """
    pairs: list[tuple[int | None, int | None]] = []
    for p_idx, indices in align_spans(planned, intervals):
        if not indices:
            pairs.append((p_idx, None))
        for i_idx in indices:
            pairs.append((p_idx, i_idx))
    return pairs


def planned_step_map(  # pylint: disable=too-many-locals
    planned: list[dict[str, Any]], intervals: list[dict[str, Any]], tolerances: Tolerances | None = None
) -> dict[int, dict[str, Any]]:
    """Planned step matched to each interval index, with the time beyond the planned duration.

    Without streams: the alignment of ``align_spans`` and the intervals' moving time (elapsed
    time when missing). A matched span (one or several consecutive intervals) longer than the
    planned duration plus the tolerance gets ``beyond_plan_s`` on each of its intervals, counted
    as in ``analyze``: after the last matched step as additional training after the plan,
    earlier as extra time inside the plan. When the step or the next one spans several laps
    (e.g. device auto-laps) and the next step is short, the lap boundary differs from the
    step boundary: that time is carried over to the next step (``carried_to_next_s``)
    instead. Nothing is changed on Intervals.icu.
    """
    tol = tolerances or Tolerances()
    spans = _spans(intervals, Profile([]))
    alignment = align_spans(planned, intervals)
    matched = [(p_idx, indices) for p_idx, indices in alignment if p_idx is not None and indices]
    last_matched = matched[-1][1][-1] if matched else None
    mapping: dict[int, dict[str, Any]] = {}
    carried_in = 0.0
    for position, (p_idx, indices) in enumerate(alignment):
        if p_idx is None or not indices:
            carried_in = 0.0
            continue
        step = planned[p_idx]
        duration = step.get("duration")
        actual = sum(spans[i].active for i in indices) + carried_in
        carried_in = 0.0
        entry: dict[str, Any] = {
            "index": step["index"], "kind": step["kind"], "duration": duration,
            "span_intervals": [i + 1 for i in indices], "span_actual_s": round(actual, 1),
            "beyond_plan_s": None, "beyond_plan_counted_as": None, "carried_to_next_s": None,
        }
        nxt = alignment[position + 1] if position + 1 < len(alignment) else None
        if duration and actual > duration + tol.duration_allowance(duration) and nxt and nxt[0] is not None and nxt[1]:
            next_duration = planned[nxt[0]].get("duration")
            next_actual = sum(spans[i].active for i in nxt[1])
            if next_duration and next_actual < next_duration and (len(indices) > 1 or len(nxt[1]) > 1):
                carried_in = min(actual - duration, next_duration - next_actual)
                entry["carried_to_next_s"] = round(carried_in, 1)
                actual -= carried_in
        if duration and actual > duration + tol.duration_allowance(duration):
            entry["beyond_plan_s"] = round(actual - duration, 1)
            entry["beyond_plan_counted_as"] = (
                "additional training after the plan" if indices[-1] == last_matched else "extra time inside the plan"
            )
        for i_idx in indices:
            mapping[i_idx] = entry
    return mapping


UNPLANNED_TEXT = {
    "before_plan": "no planned step (before the plan)",
    "after_plan": "no planned step (after the plan: additional training)",
    "extra_inside_plan": "no planned step (extra inside the plan)",
}


def plan_position(mapping: dict[int, dict[str, Any]], index: int) -> str:
    """Where an interval lies relative to the matched plan: planned_step, before_plan, after_plan or extra_inside_plan."""
    if index in mapping:
        return "planned_step"
    if not mapping:
        return "no_plan"
    if index < min(mapping):
        return "before_plan"
    if index > max(mapping):
        return "after_plan"
    return "extra_inside_plan"


def planned_step_text(step: dict[str, Any] | None, position: str = "extra_inside_plan") -> str:
    """'plan step 7 cooldown 10:00, actual 17:58: 7:58 beyond the plan (...)' for interval listings."""
    if not step:
        return UNPLANNED_TEXT.get(position, "no planned step (extra)")
    text = f"plan step {step['index']} {step['kind']} {hms(step.get('duration'))}"
    if step.get("carried_to_next_s"):
        text += f", last {hms(step['carried_to_next_s'])} counted to the next step (lap and step boundaries differ)"
    if step.get("beyond_plan_s"):
        span = step.get("span_intervals") or []
        merged = f" (intervals {span[0]}-{span[-1]})" if len(span) > 1 else ""
        text += (
            f", actual {hms(step.get('span_actual_s'))}{merged}: first {hms(step.get('duration'))} inside the plan, "
            f"{hms(step['beyond_plan_s'])} beyond the plan ({step.get('beyond_plan_counted_as')})"
        )
    return text


# ------------------------------------------------------------------- slice metrics
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


def _time_in_target(values: list[Any], target: dict[str, Any], tolerance_pct: float = TARGET_TOLERANCE_PCT) -> float | None:
    """Share of samples inside the target range widened by the tolerance (open-ended: above the bound)."""
    nums = numeric_values(values)
    if not nums:
        return None
    low = target["low"] * (1 - tolerance_pct / 100)
    high = float("inf") if target["high"] is None else target["high"] * (1 + tolerance_pct / 100)
    inside = sum(1 for v in nums if low <= v <= high)
    return inside / len(nums) * 100


def _edge_mean(values: list[Any], tail: bool) -> float | None:
    nums = numeric_values(values)
    if not nums:
        return None
    edge = nums[-EDGE_SAMPLES:] if tail else nums[:EDGE_SAMPLES]
    return sum(edge) / len(edge)


def _pw_hr_drift(watts: list[Any], heartrate: list[Any], secs: float | None) -> float | None:
    """Pw:HR decoupling in percent for steady efforts >= 10 min (Intervals.icu convention).

    (power/HR of the first half - power/HR of the second half) / first half: positive means
    the heart rate rose relative to the power, as the Intervals.icu ``decoupling``.
    """
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
    return (first - second) / first * 100 if first else None


def _max(values: list[Any]) -> float | None:
    nums = numeric_values(values)
    return float(max(nums)) if nums else None


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _average_speed(profile: Profile, start: int, end: int, active: float) -> float | None:
    """Distance over moving time (as the Intervals.icu interval average), else the mean smoothed speed."""
    distance = profile.distance(start, end)
    if distance is not None and active > 0:
        return distance / active
    return profile.mean("velocity_smooth", start, end)


def estimated_load(active_s: float, np_watts: float | None, avg_watts: float | None, ftp: float | None) -> float | None:
    """Power-based load with the TSS formula (estimate; Intervals.icu computes its own load)."""
    watts = np_watts or avg_watts
    if not ftp or not watts or not active_s:
        return None
    intensity = watts / ftp
    return active_s * watts * intensity / (ftp * 3600) * 100


def slice_metrics(  # pylint: disable=too-many-arguments,too-many-locals
    profile: Profile,
    start: int,
    end: int,
    *,
    target: dict[str, Any] | None = None,
    interval: dict[str, Any] | None = None,
    steady: bool = False,
    ftp: float | None = None,
    hidden_streams: set[str] | None = None,
) -> dict[str, Any]:
    """Execution metrics of the samples [start, end).

    When ``interval`` covers exactly this range its Intervals.icu averages are used (so the
    numbers match the Intervals.icu interval table); otherwise every value is computed from
    the samples (``source`` = "streams"), NP with the 30 s rolling mean over the whole
    activity as Intervals.icu does. Pw:HR drift (Intervals.icu decoupling convention:
    positive = HR rose relative to power) is only computed for ``steady`` efforts.
    """
    use_interval = interval is not None and interval.get("start_index") == start and interval.get("end_index") == end
    iv = interval if use_interval and interval is not None else {}
    elapsed, active = profile.elapsed(start, end), profile.active(start, end)
    watts, heart = profile.values("watts", start, end), profile.values("heartrate", start, end)
    speed, cadence = profile.values("velocity_smooth", start, end), profile.values("cadence", start, end)
    np_streams = profile.normalized_power(start, end)

    def pick(key: str, computed: float | None) -> float | None:
        value = _num(iv.get(key)) if iv else None
        return value if value is not None else computed

    metrics: dict[str, Any] = {
        "duration_s": round(elapsed, 1),
        "moving_time_s": round(active, 1),
        "paused_s": round(max(0.0, elapsed - active), 1),
        "avg_watts": pick("average_watts", profile.mean("watts", start, end)),
        "max_watts": pick("max_watts", _max(watts)),
        "normalized_watts": pick("weighted_average_watts", np_streams),
        "avg_hr": pick("average_heartrate", profile.mean("heartrate", start, end)),
        "max_hr": pick("max_heartrate", _max(heart)),
        "avg_speed_m_s": pick("average_speed", _average_speed(profile, start, end, active)),
        "gap_m_s": _num(iv.get("gap")) if iv else None,
        "avg_cadence": pick("average_cadence", None),
        "intensity_pct": _num(iv.get("intensity")) if iv else None,
        "source": "intervals.icu interval" if use_interval else "streams",
        "start_index": start,
        "end_index": end,
        "start_time": profile.at(start),
        "end_time": profile.at(end),
        "samples_valid": profile.n > 0 and end > start,
    }
    prefix = profile.prefix("watts")
    metrics["work_kj"] = round(prefix.total(start, end) / 1000, 1) if prefix else None
    if metrics["avg_watts"] is not None and ftp and metrics["intensity_pct"] is None:
        metrics["intensity_pct"] = round((metrics["normalized_watts"] or metrics["avg_watts"]) / ftp * 100, 1)
    metrics["estimated_load"] = _round(estimated_load(active, metrics["normalized_watts"], metrics["avg_watts"], ftp), 1)
    if not metrics["samples_valid"]:
        return metrics
    nonzero = [c for c in cadence if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 0]
    metrics["cadence_nonzero_mean"] = sum(nonzero) / len(nonzero) if nonzero else None
    metrics["hr_start"] = _edge_mean(heart, tail=False)
    metrics["hr_end"] = _edge_mean(heart, tail=True)
    metrics["power_fade_pct"] = _fade_pct(watts)
    metrics["speed_fade_pct"] = _fade_pct(speed)
    drift = _pw_hr_drift(watts, heart, active) if steady else None
    metrics["pw_hr_drift_pct"] = pick("decoupling", drift) if drift is not None else None
    if target:
        metrics["time_in_target_pct"] = _time_in_target({"power": watts, "hr": heart, "pace": speed}[target["kind"]], target)
    custom: dict[str, Any] = {}
    for stream in profile.streams:
        stream_type = str(stream.get("type"))
        if not stream.get("custom") or stream_type == "time" or stream_type in (hidden_streams or set()):
            continue
        stats = range_stats(stream.get("data") or [], [(start, end)])
        if stats.get("non_null"):
            custom[stream_type] = {k: stats.get(k) for k in ("first", "last", "min", "max", "mean", "delta")}
    if custom:
        metrics["custom_streams"] = custom
    return metrics


def _hr_recovery(profile: Profile, metrics: dict[str, Any], next_start: int | None) -> None:
    """HR drop from the end of a step into the first 60 s after it."""
    if next_start is None or metrics.get("hr_end") is None:
        return
    after = _edge_mean(profile.values("heartrate", next_start, next_start + 60), tail=True)
    if after is not None:
        metrics["hr_recovery_60s_drop"] = metrics["hr_end"] - after


def _interval_only_metrics(interval: dict[str, Any]) -> dict[str, Any]:
    """Metrics as reported by Intervals.icu when no sample range is available."""
    return {
        "duration_s": _num(interval.get("elapsed_time")),
        "moving_time_s": _num(interval.get("moving_time")) or _num(interval.get("elapsed_time")),
        "avg_watts": _num(interval.get("average_watts")),
        "max_watts": _num(interval.get("max_watts")),
        "normalized_watts": _num(interval.get("weighted_average_watts")),
        "avg_hr": _num(interval.get("average_heartrate")),
        "max_hr": _num(interval.get("max_heartrate")),
        "avg_speed_m_s": _num(interval.get("average_speed")),
        "gap_m_s": _num(interval.get("gap")),
        "avg_cadence": _num(interval.get("average_cadence")),
        "intensity_pct": _num(interval.get("intensity")),
        "source": "intervals.icu interval",
        "samples_valid": False,
    }


def interval_metrics(
    interval: dict[str, Any],
    streams: list[dict[str, Any]],
    target: dict[str, Any] | None,
    next_interval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execution metrics of one actual interval from its samples."""
    profile = Profile(streams)
    start, end = interval.get("start_index"), interval.get("end_index")
    if not (isinstance(start, int) and isinstance(end, int) and end > start) or profile.n == 0:
        return _interval_only_metrics(interval)
    metrics = slice_metrics(profile, start, end, target=target, interval=interval, steady=str(interval.get("type")) == "WORK")
    nxt = next_interval.get("start_index") if next_interval else None
    _hr_recovery(profile, metrics, nxt if isinstance(nxt, int) else None)
    return metrics


def adherence(target: dict[str, Any] | None, metrics: dict[str, Any], tolerance_pct: float = TARGET_TOLERANCE_PCT) -> dict[str, Any] | None:
    """Actual value vs target range: status, percent of the target midpoint and offset from the range.

    An open-ended target (``high`` None, e.g. the top zone) is a lower bound: any value at
    or above it is in range, and ``pct_of_target`` relates to the bound (``open_ended``).
    """
    if not target:
        return None
    actual = {"power": metrics.get("avg_watts"), "hr": metrics.get("avg_hr"), "pace": metrics.get("avg_speed_m_s")}[target["kind"]]
    if actual is None:
        return {"status": "no data"}
    low, high = target["low"], target["high"]
    open_ended = high is None
    upper = float("inf") if high is None else high
    reference = _target_mid(target)
    offset = 0.0
    if actual > upper and upper:
        offset = (actual - upper) / upper * 100
    elif actual < low and low:
        offset = (actual - low) / low * 100
    within = low * (1 - tolerance_pct / 100) <= actual <= upper * (1 + tolerance_pct / 100)
    status = "in range" if within else ("below" if actual < low else "above")
    result = {
        "actual": actual, "status": status, "pct_of_target": actual / reference * 100 if reference else None,
        "offset_from_range_pct": round(offset, 1), "inside_exact_range": offset == 0.0,
    }
    if open_ended:
        result["open_ended"] = True
    return result


# ------------------------------------------------------------------------ analysis
def hidden_custom_streams(streams: list[dict[str, Any]], activity_type: Any, defs: dict[str, Any] | None = None) -> dict[str, str]:
    """Custom streams left out of step statistics, with the reason (counters, duplicates, other sports)."""
    hidden: dict[str, str] = {}
    shown: list[tuple[str, list[Any]]] = []
    for stream in streams:
        if not isinstance(stream, dict) or not stream.get("custom"):
            continue
        stream_type = str(stream.get("type"))
        data = stream.get("data") or []
        label = ((defs or {}).get(stream_type) or {}).get("name") or stream.get("name")
        if is_counter_stream(data):
            hidden[stream_type] = "clock/counter stream"
            continue
        twin = next((other for other, values in shown if values == data), None)
        if twin is not None:
            hidden[stream_type] = f"identical to {twin}"
            continue
        shown.append((stream_type, data))
        reason = foreign_stream_reason(stream_type, label, activity_type)
        if reason:
            hidden[stream_type] = reason
    return hidden


def _deviation(severity: str, text: str) -> dict[str, str]:
    return {"severity": severity, "text": text}


def _distance_text(metres: float | None) -> str:
    if metres is None:
        return "n/a"
    return f"{metres / 1000:.2f} km" if metres >= 1000 else f"{metres:.0f} m"


def _step_deviations(row: dict[str, Any], step: dict[str, Any], tol: Tolerances, span_active: float, planned_s: float | None) -> None:
    """Fill row["deviations"]: duration, intensity, time in target, pauses and start shift."""
    kind = step.get("kind")
    metrics = row["metrics"]
    overrun = row.get("overrun")
    if overrun and overrun["counted_as"].startswith("extra"):
        severity = "info" if kind == "warmup" else "deviation"
        row["deviations"].append(_deviation(severity, f"{hms(overrun['moving_time_s'])} longer than planned; the remainder is reported separately"))
    elif not overrun and planned_s and span_active < planned_s - tol.duration_allowance(planned_s):
        if step.get("duration"):
            text = f"{hms(planned_s - span_active)} shorter than planned"
        else:
            text = f"{_distance_text(-(row.get('distance_diff_m') or 0))} shorter than planned"
        row["deviations"].append(_deviation("deviation", text))
    adh = row.get("adherence") or {}
    if adh.get("status") in ("above", "below"):
        severity = "deviation" if kind in ("work", "rest") else "info"
        row["deviations"].append(_deviation(severity, f"average {adh['offset_from_range_pct']:+.1f}% {adh['status']} the target range"))
    tit = metrics.get("time_in_target_pct")
    if kind == "work" and tit is not None and tit < TIME_IN_TARGET_LOW_PCT:
        row["deviations"].append(_deviation("info", f"only {tit:.0f}% of the time within ±{tol.target_pct:.0f}% of the target range"))
    if (metrics.get("paused_s") or 0) > tol.pause_s:
        row["deviations"].append(_deviation("info", f"recording paused for {hms(metrics['paused_s'])} inside the step"))
    shift = row.get("start_offset_s")
    if shift is not None and abs(shift) > tol.start_shift_s:
        row["deviations"].append(_deviation("info", f"started {hms(abs(shift))} {'later' if shift > 0 else 'earlier'} than the plan timeline"))
    for carried in row.get("carried_over") or []:
        if carried["seconds"] > 0:
            part = "last" if carried["from"] == "previous" else "first"
            row["deviations"].append(_deviation(
                "info", f"includes the {part} {hms(carried['seconds'])} of the {carried['from']} step's lap (lap and step boundaries differ)"
            ))


def _planned_secs(step: dict[str, Any], profile: Profile, start: int, end: int) -> float | None:
    """Planned seconds of a step on [start, end): its duration, or for a distance step the
    moving time needed to cover the planned distance at the actual speed (estimated from the
    target speed without a distance stream)."""
    if step.get("duration"):
        return float(step["duration"])
    distance = step.get("distance")
    actual = profile.distance(start, end) if distance else None
    if distance and actual:
        if actual >= distance:
            cut = profile.cap_by_distance(start, end, distance)
            return profile.active(start, cut if cut is not None else end)
        return profile.active(start, end) * distance / actual
    return step.get("est_duration")


def _segment_cost(step: dict[str, Any], profile: Profile, start: int, end: int) -> float | None:
    """Intensity cost of the samples [start, end) against a step's target; None without target or data."""
    target = step.get("target")
    if not target:
        return None
    value = profile.mean(STREAM_KEYS[target["kind"]], start, end)
    return None if value is None else _intensity_cost(value, target)


def _receives(receiver: dict[str, Any], giver: dict[str, Any], profile: Profile, start: int, end: int) -> bool:
    """Whether the samples [start, end) of the giver's lap fit the receiving step at least as well."""
    to_receiver, to_giver = _segment_cost(receiver, profile, start, end), _segment_cost(giver, profile, start, end)
    if to_receiver is None or to_giver is None:
        return True  # without targets the plan timeline decides
    return to_receiver <= to_giver + 1e-9


def _worth_moving(short: float, long: float, moved: float, allow_short: float, allow_long: float) -> bool:
    """Whether moving ``moved`` seconds puts right a step flagged as too short or too long."""
    return short > allow_short or long - moved <= allow_long < long


def _carry_over(  # pylint: disable=too-many-locals
    steps: tuple[dict[str, Any], dict[str, Any]], segs: tuple[dict[str, Any], dict[str, Any]], profile: Profile, tol: Tolerances
) -> None:
    """Move the boundary between two adjacent matched steps off the lap boundary.

    When the first step is longer than planned and the second shorter (or vice versa), the
    part of the lap that belongs to the other step is carried over, so a lap boundary that
    does not coincide with the step boundary (e.g. device auto-laps) does not produce two
    opposite deviations. Only done when keeping the lap boundary would flag a step that the
    moved time puts right, and when the moved samples fit the receiving step's target at
    least as well as the giving step's.
    """
    (step_a, step_b), (seg_a, seg_b) = steps, segs
    plan_a = _planned_secs(step_a, profile, seg_a["start"], seg_a["end"])
    plan_b = _planned_secs(step_b, profile, seg_b["start"], seg_b["end"])
    if not plan_a or not plan_b:
        return
    act_a, act_b = profile.active(seg_a["start"], seg_a["end"]), profile.active(seg_b["start"], seg_b["end"])
    allow_a, allow_b = tol.duration_allowance(plan_a), tol.duration_allowance(plan_b)
    if act_a > plan_a and act_b < plan_b:  # the tail of step a's lap belongs to step b
        moved = min(act_a - plan_a, plan_b - act_b)
        if not _worth_moving(plan_b - act_b, act_a - plan_a, moved, allow_b, allow_a):
            return
        cut = profile.cap_by_time(seg_a["start"], seg_a["end"], act_a - moved)
        if seg_a["start"] < cut < seg_a["end"] and _receives(step_b, step_a, profile, cut, seg_a["end"]):
            seconds = round(profile.active(cut, seg_a["end"]), 1)
            seg_a["carried"].append({"from": "next", "seconds": -seconds})
            seg_b["carried"].append({"from": "previous", "seconds": seconds})
            seg_a["end"] = seg_b["start"] = cut
    elif act_a < plan_a and act_b > plan_b:  # the head of step b's lap belongs to step a
        moved = min(plan_a - act_a, act_b - plan_b)
        if not _worth_moving(plan_a - act_a, act_b - plan_b, moved, allow_a, allow_b):
            return
        cut = profile.cap_by_time(seg_b["start"], seg_b["end"], moved)
        if seg_b["start"] < cut < seg_b["end"] and _receives(step_a, step_b, profile, seg_b["start"], cut):
            seconds = round(profile.active(seg_b["start"], cut), 1)
            seg_a["carried"].append({"from": "next", "seconds": seconds})
            seg_b["carried"].append({"from": "previous", "seconds": -seconds})
            seg_a["end"] = seg_b["start"] = cut


def _step_segments(
    planned: list[dict[str, Any]], alignment: list[tuple[int | None, list[int]]], spans: list[_Span], profile: Profile, tol: Tolerances
) -> dict[int, dict[str, Any]]:
    """Sample range of every matched step (by alignment position), boundaries carried over between adjacent steps."""
    segs: dict[int, dict[str, Any]] = {}
    if not profile.n:
        return segs
    for position, (p_idx, indices) in enumerate(alignment):
        if p_idx is None or not indices:
            continue
        first, last = spans[indices[0]], spans[indices[-1]]
        if first.start is not None and last.end is not None:
            segs[position] = {"start": first.start, "end": last.end, "carried": []}
    for position, seg in sorted(segs.items()):
        following = segs.get(position + 1)
        if following is None or seg["end"] != following["start"]:
            continue  # a missed step or an extra interval lies between: keep the lap boundary
        steps = (planned[alignment[position][0]], planned[alignment[position + 1][0]])  # type: ignore[index]
        _carry_over(steps, (seg, following), profile, tol)
    return segs


def _step_row(  # pylint: disable=too-many-arguments,too-many-locals
    step: dict[str, Any],
    parts: list[_Span],
    profile: Profile,
    tol: Tolerances,
    *,
    seg: dict[str, Any] | None,
    is_last: bool,
    anchor: float | None,
    ftp: float | None,
    hidden: set[str],
) -> dict[str, Any]:
    """Row of a matched planned step: evaluated (capped) part, remainder and deviations."""
    first, last = parts[0], parts[-1]
    interval = first.interval
    row: dict[str, Any] = {
        "planned": step, "interval_index": first.index, "interval_indices": [p.index for p in parts],
        "label": interval.get("label") or (f"Interval {first.index + 1}" + (f"-{last.index + 1}" if len(parts) > 1 else "")),
        "type": interval.get("type"), "interval_types": [p.interval.get("type") for p in parts],
        "merged": len(parts) > 1, "deviations": [],
    }
    kind = step.get("kind")
    types = {str(t) for t in row["interval_types"] if t}
    if kind in ("work", "rest") and types and types != {"WORK" if kind == "work" else "RECOVERY"}:
        row["type_note"] = f"Intervals.icu type {'/'.join(sorted(types))}, planned {kind}"
    target = step.get("target")
    if seg is None:
        # No usable sample indices or streams: evaluate the interval as reported.
        metrics = _interval_only_metrics(interval)
        row.update(start_time=interval.get("start_time"), end_time=last.interval.get("end_time"), metrics=metrics,
                   adherence=adherence(target, metrics, tol.target_pct))
        span_active = sum(p.active for p in parts)
        planned_s = step.get("duration") or step.get("est_duration")
        if step.get("duration") and metrics.get("duration_s") is not None:
            row["duration_diff_s"] = metrics["duration_s"] - step["duration"]
        _step_deviations(row, step, tol, span_active, planned_s)
        return row
    start, end = seg["start"], seg["end"]
    span_active = profile.active(start, end)
    planned_s = _planned_secs(step, profile, start, end)
    cap = end
    if planned_s and span_active > planned_s + tol.duration_allowance(planned_s):
        cap = profile.cap_by_time(start, end, planned_s)
    metrics = slice_metrics(profile, start, cap, target=target, interval=interval if len(parts) == 1 else None,
                            steady=kind == "work", ftp=ftp, hidden_streams=hidden)
    row.update(start_time=metrics["start_time"], end_time=metrics["end_time"], start_index=start, end_index=cap,
               metrics=metrics, adherence=adherence(target, metrics, tol.target_pct))
    if seg["carried"]:
        row["carried_over"] = seg["carried"]
    if step.get("duration"):
        row["duration_diff_s"] = round(span_active - step["duration"], 1)
    elif step.get("distance"):
        actual_distance = profile.distance(start, end)
        if actual_distance is not None:
            row["distance_diff_m"] = round(actual_distance - step["distance"], 1)
        row["planned_s_estimated"] = _round(planned_s, 1)
    if anchor is not None and step.get("planned_start") is not None:
        row["start_offset_s"] = round(profile.at(start) - (anchor + step["planned_start"]), 1)
    if cap < end:
        remainder = slice_metrics(profile, cap, end, ftp=ftp, hidden_streams=hidden)
        row["overrun"] = {**remainder, "counted_as": "additional training after the plan" if is_last else "extra time inside the plan"}
        row["split"] = {"planned_part_s": metrics["moving_time_s"], "remainder_s": remainder["moving_time_s"]}
    _step_deviations(row, step, tol, span_active, planned_s)
    return row


def _efforts(spans: list[_Span], ftp: float | None) -> list[dict[str, Any]]:
    """Intervals worth listing separately: at >= 90 % FTP when power and FTP are known, else WORK intervals."""
    listed = []
    for span in spans:
        iv = span.interval
        watts = _num(iv.get("average_watts"))
        if ftp and watts is not None:
            hard = watts >= EXTRA_EFFORT_MIN_FTP * ftp
        else:
            hard = str(iv.get("type")) == "WORK"
        if not hard:
            continue
        listed.append({
            "interval_index": span.index, "type": iv.get("type"), "start_time": iv.get("start_time"),
            "end_time": iv.get("end_time"), "start_index": span.start, "end_index": span.end,
            "elapsed_time": iv.get("elapsed_time"), "average_watts": iv.get("average_watts"),
            "max_watts": iv.get("max_watts"), "average_heartrate": iv.get("average_heartrate"),
            "max_heartrate": iv.get("max_heartrate"), "average_speed": iv.get("average_speed"),
        })
    return listed[:MAX_LISTED_EFFORTS]


def _block(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    profile: Profile, start: int, end: int, spans: list[_Span], ftp: float | None, hidden: set[str]
) -> dict[str, Any]:
    """Metrics of riding outside the plan (before or after it) with its intervals."""
    metrics = slice_metrics(profile, start, end, ftp=ftp, hidden_streams=hidden)
    hardest = max(spans, key=lambda s: _num(s.interval.get("average_watts")) or _num(s.interval.get("average_speed")) or 0) if spans else None
    block: dict[str, Any] = {
        "intervals": len(spans),
        "first_interval_index": spans[0].index if spans else None,
        "interval_indices": [s.index for s in spans],
        "duration_s": metrics["duration_s"],
        "moving_time_s": metrics["moving_time_s"],
        "start_time": metrics["start_time"],
        "end_time": metrics["end_time"],
        "start_index": start,
        "end_index": end,
        "metrics": metrics,
        "efforts": _efforts(spans, ftp),
    }
    if hardest is not None:
        iv = hardest.interval
        block["hardest_interval"] = {
            "label": iv.get("label"), "elapsed_time": iv.get("elapsed_time"), "average_watts": iv.get("average_watts"),
            "max_watts": iv.get("max_watts"), "average_heartrate": iv.get("average_heartrate"), "average_speed": iv.get("average_speed"),
        }
    return block


def _unplanned_row(span: _Span, profile: Profile, ftp: float | None, hidden: set[str], next_start: int | None) -> dict[str, Any]:
    """Row of an interval without a planned step (no plan, or an extra interval inside the plan)."""
    interval = span.interval
    row: dict[str, Any] = {"planned": None, "interval_index": span.index, "label": interval.get("label") or f"Interval {span.index + 1}",
                           "type": interval.get("type"), "start_time": interval.get("start_time"), "end_time": interval.get("end_time"),
                           "adherence": None}
    if span.start is not None and span.end is not None and profile.n:
        work = str(interval.get("type")) == "WORK"
        row["metrics"] = slice_metrics(profile, span.start, span.end, interval=interval, steady=work, ftp=ftp, hidden_streams=hidden)
        if work:
            _hr_recovery(profile, row["metrics"], next_start)
    else:
        row["metrics"] = _interval_only_metrics(interval)
    return row


def _analyze_without_plan(intervals: list[dict[str, Any]], profile: Profile, ftp: float | None, hidden: set[str]) -> dict[str, Any]:
    spans = _spans(intervals, profile)
    rows = [
        _unplanned_row(span, profile, ftp, hidden, spans[span.index + 1].start if span.index + 1 < len(spans) else None)
        for span in spans
    ]
    summary = {
        "planned_steps": 0, "actual_intervals": len(intervals), "matched": 0, "unmatched_planned": 0,
        "unmatched_intervals": len(rows), "planned_total_s": None,
        "actual_total_s": sum(_num(i.get("elapsed_time")) or 0 for i in intervals) or None,
        "plan_part_actual_s": None, "extension_s": 0, "extended_beyond_plan": False, "work_steps_in_range": None,
    }
    return {"rows": rows, "summary": summary, "extension": None, "pre_plan": None}


def _extension_block(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    profile: Profile, spans: list[_Span], rows: list[dict[str, Any]], plan_end_index: int | None,
    post_spans: list[_Span], ftp: float | None, hidden: set[str],
) -> dict[str, Any] | None:
    """Everything after the end of the last planned step (its remainder plus trailing intervals)."""
    if plan_end_index is not None and profile.n:
        ends = [s.end for s in spans if s.end is not None]
        end = max([profile.n] + ends)
        if profile.elapsed(plan_end_index, end) < EXTENSION_MIN_SECS:
            return None
        block = _block(profile, plan_end_index, end, post_spans, ftp, hidden)
        last_row = next((r for r in reversed(rows) if r.get("overrun") and r["overrun"]["counted_as"].startswith("additional")), None)
        if last_row is not None:
            block["remainder_of_last_step"] = {
                "planned_index": last_row["planned"]["index"], "kind": last_row["planned"]["kind"],
                **{k: last_row["overrun"].get(k) for k in ("duration_s", "moving_time_s", "start_time", "end_time", "avg_watts", "avg_hr", "max_watts")},
            }
        return block
    if not post_spans:
        return None
    duration = sum(s.elapsed for s in post_spans)  # no streams: trailing intervals as reported
    if duration < EXTENSION_MIN_SECS:
        return None
    return {"intervals": len(post_spans), "first_interval_index": post_spans[0].index, "duration_s": duration,
            "start_time": post_spans[0].interval.get("start_time"), "end_time": post_spans[-1].interval.get("end_time"),
            "metrics": {}, "efforts": _efforts(post_spans, ftp)}


def _summary(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    planned: list[dict[str, Any]], intervals: list[dict[str, Any]], rows: list[dict[str, Any]], profile: Profile,
    extension: dict[str, Any] | None, pre_plan: dict[str, Any] | None, tol: Tolerances,
) -> dict[str, Any]:
    work_rows = [r for r in rows if r.get("planned") and r["planned"]["kind"] == "work" and r.get("metrics")]
    in_range = [r for r in work_rows if (r.get("adherence") or {}).get("status") == "in range"]
    exact = [r for r in work_rows if (r.get("adherence") or {}).get("inside_exact_range")]
    tits = [r["metrics"]["time_in_target_pct"] for r in work_rows if r["metrics"].get("time_in_target_pct") is not None]
    matched_rows = [r for r in rows if r.get("planned") and r.get("metrics")]
    start = matched_rows[0].get("start_time") if matched_rows else None
    end = matched_rows[-1].get("end_time") if matched_rows else None
    prefix = profile.prefix("watts")
    total_kj = prefix.total(0, profile.n) / 1000 if prefix else None
    extension_kj = ((extension or {}).get("metrics") or {}).get("work_kj")
    inside = [r["overrun"] for r in matched_rows if (r.get("overrun") or {}).get("counted_as", "").startswith("extra")]
    return {
        "planned_steps": len(planned),
        "actual_intervals": len(intervals),
        "matched": len(matched_rows),
        "unmatched_planned": sum(1 for r in rows if r.get("planned") and not r.get("metrics")),
        "unmatched_intervals": sum(1 for r in rows if not r.get("planned") and r.get("metrics")),
        "merged_steps": sum(1 for r in matched_rows if r.get("merged")),
        "split_steps": sum(1 for r in matched_rows if r.get("overrun")),
        "boundaries_off_lap": sum(1 for r in matched_rows for c in r.get("carried_over") or [] if c["seconds"] > 0),
        "planned_total_s": sum(s.get("duration") or s.get("est_duration") or 0 for s in planned) or None,
        "actual_total_s": sum(_num(i.get("elapsed_time")) or 0 for i in intervals) or None,
        "plan_start_s": start,
        "plan_end_s": end,
        "plan_part_actual_s": round(end - start, 1) if end is not None and start is not None else None,
        "plan_part_moving_s": round(sum(r["metrics"].get("moving_time_s") or 0 for r in matched_rows), 1) or None,
        "extra_time_inside_plan_s": round(sum(o.get("moving_time_s") or 0 for o in inside), 1),
        "extension_s": (extension or {}).get("duration_s", 0) or 0,
        "extended_beyond_plan": bool(extension),
        "pre_plan_s": (pre_plan or {}).get("duration_s", 0) or 0,
        "total_work_kj": round(total_kj, 1) if total_kj else None,
        "extension_work_kj": extension_kj,
        "extension_work_share_pct": round(extension_kj / total_kj * 100, 1) if extension_kj and total_kj else None,
        "work_steps_in_range": f"{len(in_range)}/{len(work_rows)}" if work_rows else None,
        "work_steps_inside_exact_range": f"{len(exact)}/{len(work_rows)}" if work_rows else None,
        "work_time_in_target_pct_mean": round(sum(tits) / len(tits), 1) if tits else None,
        "steps_with_deviations": sum(
            1 for r in rows if r.get("planned") and any(d["severity"] == "deviation" for d in r.get("deviations", []))
        ),
        "tolerances": {"duration_pct": tol.duration_pct, "duration_min_s": tol.duration_min_s,
                       "start_shift_s": tol.start_shift_s, "pause_s": tol.pause_s, "target_pct": tol.target_pct},
        "pw_hr_drift_convention": DRIFT_CONVENTION,
    }


def analyze(  # pylint: disable=too-many-locals,too-many-statements
    planned: list[dict[str, Any]],
    intervals: list[dict[str, Any]],
    streams: list[dict[str, Any]],
    *,
    tolerances: Tolerances | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Align plan and intervals (or just evaluate intervals) and compute all metrics.

    context may carry ``ftp`` (for kJ shares and the estimated load), ``activity_type`` and
    ``stream_defs`` (to hide counter and sport-foreign custom streams from step statistics;
    ``include_all_streams`` keeps them) and ``pace_units`` (Intervals.icu pace units for the
    text; default by sport: /100 m for swims, /500 m for rowing, else /km).
    """
    tol = tolerances or Tolerances()
    ctx = context or {}
    ftp = _num(ctx.get("ftp"))
    profile = Profile(streams)
    hidden_map = hidden_custom_streams(streams, ctx.get("activity_type"), ctx.get("stream_defs"))
    hidden = set() if ctx.get("include_all_streams") else set(hidden_map)
    pace_units = ctx.get("pace_units") or default_pace_units(ctx.get("activity_type"))
    if not planned:
        result = _analyze_without_plan(intervals, profile, ftp, hidden)
        result["hidden_streams"] = hidden_map
        result["activity_type"] = ctx.get("activity_type")
        result["pace_units"] = pace_units
        return result
    spans = _spans(intervals, profile)
    alignment = align_spans(planned, intervals, profile)
    segments = _step_segments(planned, alignment, spans, profile, tol)
    matched = [(p, idx) for p, idx in alignment if p is not None and idx]
    first_matched = matched[0][1][0] if matched else None
    last_matched = matched[-1][1][-1] if matched else None
    anchor = None
    first_planned_start = planned[matched[0][0]].get("planned_start") if matched else None  # type: ignore[index]
    if matched and spans[matched[0][1][0]].start is not None and profile.n and first_planned_start is not None:
        anchor = profile.at(spans[matched[0][1][0]].start or 0) - first_planned_start
    rows: list[dict[str, Any]] = []
    pre_spans: list[_Span] = []
    post_spans: list[_Span] = []
    plan_end_index: int | None = None
    for position, (p_idx, indices) in enumerate(alignment):
        following = next((spans[idx[0]] for _, idx in alignment[position + 1:] if idx), None)
        if p_idx is None:
            span = spans[indices[0]]
            if first_matched is None or span.index < first_matched:
                pre_spans.append(span)
            elif last_matched is not None and span.index > last_matched:
                post_spans.append(span)
            else:
                rows.append(_unplanned_row(span, profile, ftp, hidden, following.start if following else None))
            continue
        step = planned[p_idx]
        if not indices:
            rows.append({"planned": step, "interval_index": None, "deviations": [_deviation("deviation", "not executed / no matching interval")]})
            continue
        is_last = indices[-1] == last_matched
        row = _step_row(step, [spans[i] for i in indices], profile, tol, seg=segments.get(position), is_last=is_last,
                        anchor=anchor, ftp=ftp, hidden=hidden)
        if step.get("kind") == "work" and row.get("end_index") is not None:
            _hr_recovery(profile, row["metrics"], row["end_index"])
        if is_last:
            plan_end_index = row.get("end_index")
        rows.append(row)
    extension = _extension_block(profile, spans, rows, plan_end_index, post_spans, ftp, hidden)
    pre_plan = None
    if first_matched is not None and profile.n and (spans[first_matched].start or 0) > 0:
        start_index = spans[first_matched].start or 0
        if profile.elapsed(0, start_index) >= EXTENSION_MIN_SECS:
            pre_plan = _block(profile, 0, start_index, pre_spans, ftp, hidden)
    summary = _summary(planned, intervals, rows, profile, extension, pre_plan, tol)
    return {
        "rows": rows, "summary": summary, "extension": extension, "pre_plan": pre_plan, "hidden_streams": hidden_map,
        "activity_type": ctx.get("activity_type"), "pace_units": pace_units,
    }


# ------------------------------------------------------------------------ rendering
def _fmt(value: Any, digits: int = 0, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}{suffix}"


def target_text(target: dict[str, Any] | None, pace_units: str | None = None) -> str:
    """Target range as text: '238-242 W', '351 W or more', '6:48/km to 6:18/km', '4:47/km or faster'."""
    if not target:
        return "no target"
    low, high, units = target["low"], target["high"], target["units"]
    if target["kind"] == "pace":
        if high is None:
            return f"{format_pace(low, pace_units)} or faster"
        return f"{format_pace(low, pace_units)}" + (f" to {format_pace(high, pace_units)}" if high != low else "")
    if high is None:
        return f"{low:.0f} {units} or more"
    span = f"{low:.0f}" + (f"-{high:.0f}" if high != low else "")
    return f"{span} {units}"


def _amount_text(step: dict[str, Any]) -> str:
    """Planned duration, or the planned distance of a distance step."""
    if not step.get("duration") and step.get("distance"):
        return _distance_text(step["distance"])
    return hms(step.get("duration"))


def _metric_lines(  # pylint: disable=too-many-branches,too-many-arguments,too-many-positional-arguments
    metrics: dict[str, Any], target: dict[str, Any] | None, pace_based: bool, detail_level: str = "standard", sport: Any = None,
    units: str | None = None,
) -> list[str]:
    lines: list[str] = []
    if pace_based or metrics.get("avg_speed_m_s") and not metrics.get("avg_watts"):
        lines.append(
            f"    pace {format_pace(metrics.get('avg_speed_m_s'), units)} (GAP {format_pace(metrics.get('gap_m_s'), units)}), "
            f"speed fade {_fmt(metrics.get('speed_fade_pct'), 1, '%')}"
        )
    if metrics.get("avg_watts") is not None:
        line = (
            f"    power avg {_fmt(metrics.get('avg_watts'))} W, NP {_fmt(metrics.get('normalized_watts'))} W, "
            f"max {_fmt(metrics.get('max_watts'))} W, fade {_fmt(metrics.get('power_fade_pct'), 1, '%')}"
        )
        if metrics.get("work_kj") is not None and detail_level != "compact":
            line += f", {metrics['work_kj']:.0f} kJ"
        lines.append(line)
    if metrics.get("avg_hr") is not None:
        hr = f"    HR avg {_fmt(metrics.get('avg_hr'))}, max {_fmt(metrics.get('max_hr'))}, start {_fmt(metrics.get('hr_start'))} -> end {_fmt(metrics.get('hr_end'))} bpm"
        if metrics.get("hr_recovery_60s_drop") is not None:
            hr += f", drop in first 60 s of next interval {_fmt(metrics['hr_recovery_60s_drop'])} bpm"
        if metrics.get("pw_hr_drift_pct") is not None:
            hr += f", Pw:HR drift {metrics['pw_hr_drift_pct']:+.1f}%"
        lines.append(hr)
    if metrics.get("cadence_nonzero_mean") is not None:
        lines.append(f"    cadence {cadence_text(metrics['cadence_nonzero_mean'], sport)} (non-zero samples)")
    if target and metrics.get("time_in_target_pct") is not None:
        lines.append(f"    time in target range (±{TARGET_TOLERANCE_PCT:.0f}%): {_fmt(metrics['time_in_target_pct'], 0, '%')}")
    if detail_level == "compact":
        return lines
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


def _adherence_text(adh: dict[str, Any] | None) -> str:
    if not adh or not adh.get("status"):
        return ""
    text = f", {adh['status']}"
    if adh.get("pct_of_target"):
        text += f" ({adh['pct_of_target']:.0f}% of {'the lower bound' if adh.get('open_ended') else 'target'}"
        offset = adh.get("offset_from_range_pct")
        if offset and adh["status"] == "in range":
            text += f", {offset:+.1f}% {'above' if offset > 0 else 'below'} the range, within the ±{TARGET_TOLERANCE_PCT:.0f}% tolerance"
        text += ")"
    return text


def _compact_row(row: dict[str, Any], units: str | None = None) -> str:
    step = row.get("planned")
    metrics = row.get("metrics") or {}
    head = f"[plan {step['index']}] {step['kind']} {_amount_text(step)} @ {target_text(step['target'], units)}" if step else f"[extra] {row.get('label')}"
    if not metrics:
        return f"{head}: not executed"
    parts = [f"{hms(metrics.get('moving_time_s'))}"]
    if metrics.get("avg_watts") is not None:
        parts.append(f"{metrics['avg_watts']:.0f} W")
    if metrics.get("avg_hr") is not None:
        parts.append(f"HR {metrics['avg_hr']:.0f}/{_fmt(metrics.get('max_hr'))}")
    if metrics.get("time_in_target_pct") is not None:
        parts.append(f"{metrics['time_in_target_pct']:.0f}% in target")
    text = f"{head}: " + ", ".join(parts) + _adherence_text(row.get("adherence"))
    if row.get("overrun"):
        text += f"; +{hms(row['overrun'].get('moving_time_s'))} {row['overrun']['counted_as']}"
    notes = [d["text"] for d in row.get("deviations", []) if d["severity"] == "deviation" and "longer than planned" not in d["text"]]
    if notes:
        text += " | " + "; ".join(notes)
    return text


def _row_lines(row: dict[str, Any], pace_based: bool, detail_level: str, sport: Any = None, units: str | None = None) -> list[str]:
    if detail_level == "compact":
        return [_compact_row(row, units)]
    step = row.get("planned")
    metrics = row.get("metrics")
    if step:
        rep = f" (rep {step['rep']}/{step['reps']})" if step.get("rep") else ""
        head = f"[plan {step['index']}] {step['kind']}{rep} {_amount_text(step)} @ {target_text(step['target'], units)}"
        if step.get("text"):
            head += f" '{step['text']}'"
    else:
        head = "[no planned step]"
    if not metrics:
        return [f"{head} -> not executed / no matching interval"]
    overrun = row.get("overrun")
    type_text = row.get("type_note") or row.get("type")
    if overrun:
        total = (metrics.get("moving_time_s") or 0) + (overrun.get("moving_time_s") or 0)
        actual = f"-> {row['label']} ({type_text}) first {hms(metrics.get('moving_time_s'))} of {hms(total)} from {hms(row.get('start_time'))}"
    else:
        actual = f"-> {row['label']} ({type_text}) {hms(metrics.get('duration_s'))} from {hms(row.get('start_time'))}"
        if metrics.get("paused_s"):
            actual += f" ({hms(metrics.get('moving_time_s'))} moving)"
        if row.get("duration_diff_s") is not None:
            actual += f", {row['duration_diff_s']:+.0f} s vs plan"
        elif row.get("distance_diff_m") is not None:
            actual += f", {row['distance_diff_m']:+.0f} m vs plan"
    actual += _adherence_text(row.get("adherence"))
    lines = [f"{head} {actual}"]
    if overrun:
        lines.append(
            f"    remaining {hms(overrun.get('moving_time_s'))} ({hms(overrun.get('start_time'))}-{hms(overrun.get('end_time'))}, "
            f"avg {_fmt(overrun.get('avg_watts'))} W, HR {_fmt(overrun.get('avg_hr'))} bpm) counted as {overrun['counted_as']}"
        )
    lines.extend(_metric_lines(metrics, step["target"] if step else None, pace_based, detail_level, sport, units))
    notes = [d["text"] for d in row.get("deviations", []) if not (overrun and "longer than planned" in d["text"])]
    if notes:
        lines.append("    notes: " + "; ".join(notes))
    return lines


def _effort_line(effort: dict[str, Any], units: str | None = None) -> str:
    where = f"[Intervals.icu {effort.get('type')} interval {effort['interval_index'] + 1}]"
    if effort.get("average_watts") is not None:
        return (
            f"    extra effort: {hms(effort.get('elapsed_time'))} from {hms(effort.get('start_time'))} at avg {_fmt(effort.get('average_watts'))} W "
            f"(max {_fmt(effort.get('max_watts'))} W), HR {_fmt(effort.get('average_heartrate'))}"
            + (f" (max {_fmt(effort.get('max_heartrate'))})" if effort.get("max_heartrate") is not None else "") + f" bpm {where}"
        )
    return f"    extra effort: {hms(effort.get('elapsed_time'))} from {hms(effort.get('start_time'))} at {format_pace(effort.get('average_speed'), units)} {where}"


def _block_lines(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    title: str, block: dict[str, Any], pace_based: bool, detail_level: str, sport: Any = None, units: str | None = None
) -> list[str]:
    metrics = block.get("metrics") or {}
    head = f"{title}: {hms(block['duration_s'])} from {hms(block.get('start_time'))} to {hms(block.get('end_time'))} ({block['intervals']} interval(s)"
    remainder = block.get("remainder_of_last_step")
    if remainder:
        head += f", starting with the last {hms(remainder.get('moving_time_s'))} of planned step {remainder['planned_index']} ({remainder['kind']})"
    head += ")"
    if block.get("start_index") is not None:
        head += f", samples {block['start_index']}-{block['end_index']}"
    lines = [head]
    if metrics.get("estimated_load") is not None:
        lines.append(f"    estimated load ≈ {metrics['estimated_load']:.0f} (TSS formula from NP and FTP; the Intervals.icu load covers the whole activity)")
    lines.extend(_metric_lines(metrics, None, pace_based, detail_level, sport, units))
    lines.extend(_effort_line(effort, units) for effort in block.get("efforts") or [])
    hardest = block.get("hardest_interval") or {}
    if hardest.get("elapsed_time"):
        lines.append(
            f"    hardest part: {hms(hardest['elapsed_time'])} at avg {_fmt(hardest.get('average_watts'))} W "
            f"(max {_fmt(hardest.get('max_watts'))} W), HR {_fmt(hardest.get('average_heartrate'))} bpm"
            if hardest.get("average_watts") is not None
            else f"    hardest part: {hms(hardest['elapsed_time'])} at {format_pace(hardest.get('average_speed'), units)}"
        )
    return lines


def _summary_lines(summary: dict[str, Any]) -> list[str]:
    lines = [
        f"Plan: {summary['planned_steps']} steps, {hms(summary['planned_total_s'])} planned | "
        f"Actual: {summary['actual_intervals']} intervals, {hms(summary['actual_total_s'])} in total | "
        f"matched {summary['matched']}, planned without match {summary['unmatched_planned']}, "
        f"extra intervals inside the plan {summary['unmatched_intervals']} | work steps in target: {summary['work_steps_in_range'] or 'n/a'}"
    ]
    quality = []
    if summary.get("work_steps_inside_exact_range"):
        quality.append(f"work steps with the average inside the exact target range {summary['work_steps_inside_exact_range']}")
    if summary.get("work_time_in_target_pct_mean") is not None:
        quality.append(f"mean time in target {summary['work_time_in_target_pct_mean']:.0f}%")
    quality.append(f"steps with clear deviations {summary.get('steps_with_deviations', 0)}")
    if summary.get("split_steps"):
        quality.append(f"steps capped at their planned duration {summary['split_steps']}")
    if summary.get("merged_steps"):
        quality.append(f"steps matched to several intervals {summary['merged_steps']}")
    if summary.get("boundaries_off_lap"):
        quality.append(f"step boundaries set on the plan timeline inside a lap {summary['boundaries_off_lap']}")
    lines.append("Execution: " + ", ".join(quality))
    if summary.get("extended_beyond_plan"):
        share = f", {summary['extension_work_share_pct']:.0f}% of the activity's work in kJ" if summary.get("extension_work_share_pct") is not None else ""
        lines.append(
            f"The activity was extended beyond the plan: plan part {hms(summary.get('plan_part_actual_s'))}, "
            f"additional training {hms(summary['extension_s'])}{share} (reported separately below, not counted against the plan)."
        )
    if summary.get("pre_plan_s"):
        lines.append(f"Riding before the first planned step: {hms(summary['pre_plan_s'])} (reported separately below).")
    if summary.get("extra_time_inside_plan_s"):
        lines.append(f"Extra time inside the plan (steps longer than planned): {hms(summary['extra_time_inside_plan_s'])}.")
    return lines


def format_execution(result: dict[str, Any], header: str, pace_based: bool = False, detail_level: str = "standard") -> str:
    """Readable report of an execution analysis (detail_level compact / standard / full)."""
    summary = result["summary"]
    lines = [header, ""] if header else []
    if summary["planned_steps"]:
        lines.extend(_summary_lines(summary))
    else:
        lines.append(f"No planned workout: {summary['actual_intervals']} intervals, {hms(summary['actual_total_s'])} in total")
    hidden = result.get("hidden_streams") or {}
    if hidden and detail_level == "standard":
        lines.append("Custom streams not shown per step: " + ", ".join(f"{code} ({why})" for code, why in sorted(hidden.items())))
    if detail_level != "compact" and any((r.get("metrics") or {}).get("pw_hr_drift_pct") is not None for r in result["rows"]):
        lines.append(f"Pw:HR drift: {DRIFT_CONVENTION}.")
    lines.append("")
    sport = result.get("activity_type")
    units = result.get("pace_units") or default_pace_units(sport)
    for row in result["rows"]:
        lines.extend(_row_lines(row, pace_based, detail_level, sport, units))
    if result.get("pre_plan"):
        lines.append("")
        lines.extend(_block_lines("Riding before the plan", result["pre_plan"], pace_based, detail_level, sport, units))
    if result.get("extension"):
        lines.append("")
        lines.extend(_block_lines("Additional training after the plan", result["extension"], pace_based, detail_level, sport, units))
    return "\n".join(lines)
