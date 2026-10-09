"""
Segment detection for Intervals.icu activity streams.

Splits the altitude profile of an activity into climbs, descents and the sections
in between ("other"), reports per-segment metrics (elevation, grade, VAM, power,
heart rate, cadence, speed and statistics of custom streams) and lists the pauses
of the recording.

Conventions (see ``utils.streams``): streams are sample-aligned, ``time`` holds
seconds since the start and jumps across recording stops, and index ranges are end
exclusive. A segment covers the samples ``start_index .. end_index - 1``;
``start_time`` / ``end_time`` are the ``time`` values of its first and last sample
and ``duration_s`` their difference. A recording stop inside a segment therefore
counts towards its duration, one right after its last sample does not. Nothing is
interpolated: every statistic uses recorded numeric samples only and a value that
cannot be computed is ``None``.

Climbs are found with a hysteresis walk over the smoothed profile: a rising run
continues while the altitude has not dropped more than ``hysteresis_m`` below its
running maximum and ends at that maximum; falling runs are the mirror image. A
rising run is a climb when its gain and average grade reach the thresholds, a
falling run a descent when its loss and (negative) grade do. When a distance
stream is available the ends of a run are trimmed to where the grade over a 100 m
window reaches the minimum grade, so a noisy flat approach is not part of a climb.
"""

import math
from dataclasses import dataclass
from typing import Any

from intervals_mcp_server.utils.custom_fields import CustomFieldDefs, format_value, is_missing
from intervals_mcp_server.utils.streams import (
    describe_stream,
    find_stream,
    format_stats,
    numeric_values,
    range_stats,
    stream_label,
)

# A jump in the time stream larger than this is a recording stop.
RECORDING_STOP_MIN_GAP_S = 5
# Sections between climbs and descents are only reported when longer than this.
OTHER_SEGMENT_MIN_DURATION_S = 120
# Rolling distance window of the maximum grade.
MAX_GRADE_WINDOW_M = 100.0
# Normalized power: rolling window and minimum amount of data.
NP_WINDOW_S = 30
NP_MIN_DURATION_S = 60

ALTITUDE_SOURCES = ("fixed_altitude", "altitude")
SEGMENT_KEYS = (
    "type", "start_index", "end_index", "start_time", "end_time", "duration_s",
    "distance_m", "elevation_gain_m", "elevation_loss_m", "avg_grade_pct", "max_grade_pct",
    "vam_m_per_h", "avg_watts", "normalized_power", "max_watts", "avg_hr", "max_hr",
    "avg_cadence_nonzero", "avg_speed_m_s", "streams",
)

Samples = list[float | None]
Run = tuple[int, int, int]
Range = tuple[int, int, str]
Window = tuple[int, int, float]  # first index, last index (inclusive), grade in percent


@dataclass
class _Thresholds:
    """Detection thresholds of ``detect_segments``."""

    min_climb_gain_m: float
    min_descent_loss_m: float
    min_grade_pct: float
    pause_min_secs: int
    stationary_speed_m_s: float


@dataclass
class _Profile:  # pylint: disable=too-many-instance-attributes
    """Sample-aligned, numeric view of the streams used by the detection."""

    altitude_source: str
    samples: int
    time: Samples
    altitude: Samples  # smoothed
    distance: Samples | None
    velocity: Samples | None
    metrics: dict[str, list[Any]]  # raw watts / heartrate / cadence streams
    stat_streams: list[dict[str, Any]]  # custom and requested streams, API order


def _num(value: Any) -> float | None:
    """Numeric sample as float; None for null, NaN (float or "NaN" string), bools and non-numbers."""
    if isinstance(value, bool) or is_missing(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _numbers(data: Any) -> Samples:
    """Convert a raw stream to floats with None for every missing sample."""
    return [_num(value) for value in data] if isinstance(data, list) else []


def _smooth(values: Samples, window: int) -> Samples:
    """Centred moving average over ``window`` samples; missing samples are ignored and stay missing."""
    count = len(values)
    if window <= 1:
        return list(values)
    half = window // 2
    sums = [0.0] * (count + 1)
    counts = [0] * (count + 1)
    for index, value in enumerate(values):
        sums[index + 1] = sums[index] + (value if value is not None else 0.0)
        counts[index + 1] = counts[index] + (1 if value is not None else 0)
    out: Samples = []
    for index, value in enumerate(values):
        if value is None:
            out.append(None)
            continue
        low, high = max(0, index - half), min(count, index + half + 1)
        out.append((sums[high] - sums[low]) / (counts[high] - counts[low]))
    return out


def _first_last(data: Samples | None, start: int, end: int) -> tuple[float, float] | None:
    """First and last numeric sample of a range, None when there are fewer than two."""
    if data is None:
        return None
    nums = [value for value in data[start:end] if value is not None]
    if len(nums) < 2:
        return None
    return nums[0], nums[-1]


def _delta(data: Samples | None, start: int, end: int) -> float | None:
    """Last minus first numeric sample of a range."""
    pair = _first_last(data, start, end)
    return None if pair is None else pair[1] - pair[0]


def _gain_loss(altitude: Samples, start: int, end: int) -> tuple[float, float] | None:
    """Sum of the positive and of the negative altitude deltas between consecutive numeric samples."""
    gain = loss = 0.0
    previous: float | None = None
    steps = 0
    for value in altitude[start:end]:
        if value is None:
            continue
        if previous is not None:
            steps += 1
            change = value - previous
            if change > 0:
                gain += change
            else:
                loss -= change
        previous = value
    return (gain, loss) if steps else None


def _round(value: float | None, ndigits: int) -> float | int | None:
    """Round for output; 0 digits yields an int. None stays None."""
    if value is None:
        return None
    return round(value) if ndigits == 0 else round(value, ndigits)


def _mean(values: list[int | float], ndigits: int) -> float | int | None:
    """Rounded mean, None for no values."""
    return _round(sum(values) / len(values), ndigits) if values else None


# --- profile ---------------------------------------------------------------------


def _altitude_source(streams: list[dict[str, Any]]) -> tuple[str, Samples] | None:
    """Pick ``fixed_altitude`` when it has numeric data, else ``altitude``."""
    for source in ALTITUDE_SOURCES:
        stream = find_stream(streams, source)
        if stream is None:
            continue
        data = _numbers(stream.get("data"))
        if any(value is not None for value in data):
            return source, data
    return None


def _stat_streams(streams: list[dict[str, Any]], extra: tuple[str, ...]) -> list[dict[str, Any]]:
    """Custom streams plus the explicitly requested stream types, in API order."""
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        stream_type = stream.get("type")
        if not isinstance(stream_type, str) or stream_type in seen:
            continue
        if stream.get("custom") or stream_type in extra:
            selected.append(stream)
            seen.add(stream_type)
    return selected


def _optional(streams: list[dict[str, Any]], stream_type: str) -> Samples | None:
    """Numeric samples of a stream, None when the stream is absent."""
    stream = find_stream(streams, stream_type)
    return None if stream is None else _numbers(stream.get("data"))


def _build_profile(
    streams: list[dict[str, Any]], smooth_samples: int, extra: tuple[str, ...]
) -> _Profile | str:
    """Assemble the profile or return a message naming what is missing."""
    time_stream = find_stream(streams, "time")
    time = _numbers(time_stream.get("data")) if time_stream else []
    altitude = _altitude_source(streams)
    missing = []
    if not any(value is not None for value in time):
        missing.append("time")
    if altitude is None:
        missing.append("altitude (no 'fixed_altitude' or 'altitude' stream with numeric data)")
    if missing or altitude is None:
        return "cannot detect segments, missing stream(s): " + "; ".join(missing)
    source, raw = altitude
    samples = min(len(time), len(raw))
    metrics = {
        name: stream.get("data") or []
        for name in ("watts", "heartrate", "cadence")
        if (stream := find_stream(streams, name)) is not None
    }
    return _Profile(
        altitude_source=source,
        samples=samples,
        time=time[:samples],
        altitude=_smooth(raw[:samples], smooth_samples),
        distance=_optional(streams, "distance"),
        velocity=_optional(streams, "velocity_smooth"),
        metrics=metrics,
        stat_streams=_stat_streams(streams, extra),
    )


# --- hysteresis walk --------------------------------------------------------------


@dataclass
class _Extreme:
    """Running extreme of the profile: its value and the first/last index where it occurred."""

    first: int
    last: int
    value: float

    def update(self, index: int, value: float, higher: bool) -> None:
        """Advance with a sample: a tie extends ``last``, a new extreme resets both indexes."""
        if value == self.value:
            self.last = index
        elif (value > self.value) if higher else (value < self.value):
            self.first = self.last = index
            self.value = value


class _Walker:
    """State of the hysteresis walk: direction, anchor of the current run and running extremes."""

    def __init__(self, index: int, value: float, hysteresis: float) -> None:
        self.hysteresis = hysteresis
        self.direction = 0
        self.anchor = index
        self.low = _Extreme(index, index, value)
        self.high = _Extreme(index, index, value)
        self.runs: list[Run] = []

    def step(self, index: int, value: float) -> None:
        """Feed the next numeric sample."""
        if self.direction == 0:
            self._step_flat(index, value)
        elif self.direction > 0:
            self._step_rising(index, value)
        else:
            self._step_falling(index, value)

    def _step_flat(self, index: int, value: float) -> None:
        self.low.update(index, value, higher=False)
        self.high.update(index, value, higher=True)
        if value - self.low.value > self.hysteresis:
            self._turn(1, self.low.last, index, value)
        elif self.high.value - value > self.hysteresis:
            self._turn(-1, self.high.last, index, value)

    def _step_rising(self, index: int, value: float) -> None:
        self.high.update(index, value, higher=True)
        if self.high.value - value > self.hysteresis:
            self.runs.append((self.anchor, self.high.first, 1))
            self._turn(-1, self.high.last, index, value)

    def _step_falling(self, index: int, value: float) -> None:
        self.low.update(index, value, higher=False)
        if value - self.low.value > self.hysteresis:
            self.runs.append((self.anchor, self.low.first, -1))
            self._turn(1, self.low.last, index, value)

    def _turn(self, direction: int, anchor: int, index: int, value: float) -> None:
        self.direction = direction
        self.anchor = anchor
        if direction > 0:
            self.high = _Extreme(index, index, value)
        else:
            self.low = _Extreme(index, index, value)

    def finish(self) -> list[Run]:
        """Close the open run at its running extreme and return all runs."""
        if self.direction > 0:
            self.runs.append((self.anchor, self.high.first, 1))
        elif self.direction < 0:
            self.runs.append((self.anchor, self.low.first, -1))
        return self.runs


def _hysteresis_runs(altitude: Samples, hysteresis: float) -> list[Run]:
    """Alternating rising (+1) and falling (-1) runs as ``(start, end, direction)``, end inclusive.

    A run ends at the first index of its running extreme; the next run starts at the
    last index of that extreme, so a plateau at a summit belongs to neither. Missing
    samples are skipped.
    """
    walker: _Walker | None = None
    for index, value in enumerate(altitude):
        if value is None:
            continue
        if walker is None:
            walker = _Walker(index, value, hysteresis)
        else:
            walker.step(index, value)
    return walker.finish() if walker else []


# --- segments ---------------------------------------------------------------------


def _avg_grade(profile: _Profile, start: int, end: int) -> float | None:
    """Net altitude change over the distance covered, in percent."""
    distance = _delta(profile.distance, start, end)
    climb = _delta(profile.altitude, start, end)
    if distance is None or distance <= 0 or climb is None:
        return None
    return climb / distance * 100


def _classify(profile: _Profile, thresholds: _Thresholds, run: Run) -> str | None:
    """Climb / descent when the run passes the gain and grade thresholds, else None."""
    start, end, direction = run
    gain_loss = _gain_loss(profile.altitude, start, end)
    if gain_loss is None:
        return None
    gain, loss = gain_loss
    grade = _avg_grade(profile, start, end)
    if direction > 0:
        steep = grade is None or grade >= thresholds.min_grade_pct
        return "climb" if gain >= thresholds.min_climb_gain_m and steep else None
    steep = grade is None or grade <= -thresholds.min_grade_pct
    return "descent" if loss >= thresholds.min_descent_loss_m and steep else None


def _classify_runs(runs: list[Run], profile: _Profile, thresholds: _Thresholds) -> list[Range]:
    """Turn runs into non-overlapping, end-exclusive climb/descent ranges."""
    kept: list[Range] = []
    previous_end = 0
    for anchor, last, direction in runs:
        start, end = max(anchor, previous_end), last + 1
        previous_end = end
        start, end = _trim(profile, thresholds, start, end, direction)
        if end - start < 2:
            continue
        kind = _classify(profile, thresholds, (start, end, direction))
        if kind is not None:
            kept.append((start, end, kind))
    return kept


def _with_other_sections(kept: list[Range], profile: _Profile) -> list[Range]:
    """Insert the sections between kept segments when they last longer than the minimum."""
    ranges: list[Range] = []
    previous_end = 0
    for start, end, kind in kept:
        _append_other(ranges, profile, previous_end, start)
        ranges.append((start, end, kind))
        previous_end = end
    _append_other(ranges, profile, previous_end, profile.samples)
    return ranges


def _append_other(ranges: list[Range], profile: _Profile, start: int, end: int) -> None:
    span = _delta(profile.time, start, end) if end - start >= 2 else None
    if span is not None and span > OTHER_SEGMENT_MIN_DURATION_S:
        ranges.append((start, end, "other"))


def _window_grades(profile: _Profile, start: int, end: int) -> list[Window]:
    """Grade of every rolling window of at least ``MAX_GRADE_WINDOW_M`` metres inside a range."""
    if profile.distance is None:
        return []
    points: list[tuple[int, float, float]] = []
    for index in range(start, end):
        distance, altitude = profile.distance[index], profile.altitude[index]
        if distance is not None and altitude is not None:
            points.append((index, distance, altitude))
    windows: list[Window] = []
    ahead = 0
    for index, distance, altitude in points:
        while ahead < len(points) and points[ahead][1] - distance < MAX_GRADE_WINDOW_M:
            ahead += 1
        if ahead >= len(points):
            break
        far_index, far_distance, far_altitude = points[ahead]
        windows.append((index, far_index, (far_altitude - altitude) / (far_distance - distance) * 100))
    return windows


def _max_grade(profile: _Profile, kind: str, start: int, end: int) -> float | None:
    """Steepest rolling-window grade: highest for climbs, lowest for descents, largest magnitude otherwise."""
    grades = [grade for _, _, grade in _window_grades(profile, start, end)]
    if not grades:
        return None
    if kind == "climb":
        return max(grades)
    if kind == "descent":
        return min(grades)
    return max(grades, key=abs)


def _trim(
    profile: _Profile, thresholds: _Thresholds, start: int, end: int, direction: int
) -> tuple[int, int]:
    """Drop the ends of a run where the rolling-window grade stays below the minimum grade.

    The hysteresis walk anchors a run at the lowest (highest) point before it, which
    on a noisy flat approach can lie well before the road actually starts to climb.
    Runs without any window (no distance stream, or shorter than the window) are kept
    as they are; a run without a single steep window is emptied.
    """
    windows = _window_grades(profile, start, end)
    if not windows:
        return start, end
    steep = [window for window in windows if window[2] * direction >= thresholds.min_grade_pct]
    if not steep:
        return start, start
    return steep[0][0], steep[-1][1] + 1


def _terrain_metrics(
    profile: _Profile, kind: str, start: int, end: int, duration_s: float | None
) -> dict[str, Any]:
    """Distance, elevation, grade and VAM of a segment."""
    gain_loss = _gain_loss(profile.altitude, start, end)
    gain, loss = gain_loss if gain_loss is not None else (None, None)
    vam = gain / (duration_s / 3600) if kind == "climb" and gain is not None and duration_s else None
    return {
        "distance_m": _round(_delta(profile.distance, start, end), 1),
        "elevation_gain_m": _round(gain, 1),
        "elevation_loss_m": _round(loss, 1),
        "avg_grade_pct": _round(_avg_grade(profile, start, end), 2),
        "max_grade_pct": _round(_max_grade(profile, kind, start, end), 1),
        "vam_m_per_h": _round(vam, 0),
    }


def _normalized_power(profile: _Profile, start: int, end: int) -> float | None:
    """Normalized power: 4th root of the mean 4th power of the 30 s rolling average.

    The rolling window is defined on the time stream (``time - 30 < t <= time``) and
    only full windows count. None with less than 60 s of power data.
    """
    watts = profile.metrics.get("watts")
    if watts is None:
        return None
    samples = [
        (time, power)
        for time, power in zip(profile.time[start:end], _numbers(watts[start:end]), strict=False)
        if time is not None and power is not None
    ]
    if not samples or samples[-1][0] - samples[0][0] < NP_MIN_DURATION_S:
        return None
    head, window_sum = 0, 0.0
    fourth_powers: list[float] = []
    for position, (time, power) in enumerate(samples):
        window_sum += power
        while samples[head][0] <= time - NP_WINDOW_S:
            window_sum -= samples[head][1]
            head += 1
        if time - samples[0][0] >= NP_WINDOW_S - 1:
            fourth_powers.append((window_sum / (position - head + 1)) ** 4)
    if not fourth_powers:
        return None
    return (sum(fourth_powers) / len(fourth_powers)) ** 0.25


def _effort_metrics(profile: _Profile, start: int, end: int) -> dict[str, Any]:
    """Power, heart rate and cadence of a segment (cadence ignores 0 and missing samples)."""

    def values(name: str) -> list[int | float]:
        data = profile.metrics.get(name)
        return numeric_values(data[start:end]) if data else []

    watts, heartrate = values("watts"), values("heartrate")
    cadence = [value for value in values("cadence") if value != 0]
    return {
        "avg_watts": _mean(watts, 0),
        "normalized_power": _round(_normalized_power(profile, start, end), 0),
        "max_watts": max(watts) if watts else None,
        "avg_hr": _mean(heartrate, 0),
        "max_hr": max(heartrate) if heartrate else None,
        "avg_cadence_nonzero": _mean(cadence, 0),
    }


def _segment_metrics(profile: _Profile, segment_range: Range) -> dict[str, Any]:
    """All reported values of one segment, in ``SEGMENT_KEYS`` order."""
    start, end, kind = segment_range
    times = _first_last(profile.time, start, end)
    duration = times[1] - times[0] if times else None
    segment: dict[str, Any] = {
        "type": kind,
        "start_index": start,
        "end_index": end,
        "start_time": times[0] if times else None,
        "end_time": times[1] if times else None,
        "duration_s": duration,
    }
    segment.update(_terrain_metrics(profile, kind, start, end, duration))
    segment.update(_effort_metrics(profile, start, end))
    distance = segment["distance_m"]
    segment["avg_speed_m_s"] = _round(distance / duration, 2) if duration and distance is not None else None
    segment["streams"] = {
        str(stream["type"]): range_stats(stream.get("data") or [], [(start, end)])
        for stream in profile.stat_streams
    }
    return segment


# --- pauses -----------------------------------------------------------------------


def _pause(kind: str, start: int, end: int, start_time: float, end_time: float) -> dict[str, Any]:
    return {
        "kind": kind,
        "start_index": start,
        "end_index": end,
        "start_time": start_time,
        "end_time": end_time,
        "duration_s": end_time - start_time,
    }


def _recording_stops(time: Samples) -> list[dict[str, Any]]:
    """Jumps between consecutive numeric time samples larger than the recording-stop gap."""
    stops: list[dict[str, Any]] = []
    previous_index = 0
    previous_time: float | None = None
    for index, value in enumerate(time):
        if value is None:
            continue
        if previous_time is not None and value - previous_time > RECORDING_STOP_MIN_GAP_S:
            stops.append(_pause("recording_stop", previous_index, index, previous_time, value))
        previous_index, previous_time = index, value
    return stops


def _stationary_runs(profile: _Profile, thresholds: _Thresholds) -> list[dict[str, Any]]:
    """Runs of samples slower than the stationary speed lasting at least the minimum.

    A run is cut at recording stops and at missing samples; its duration spans its
    first to its last sample.
    """
    if profile.velocity is None:
        return []
    runs: list[dict[str, Any]] = []
    run_start: int | None = None
    for index in range(profile.samples):
        time, speed = profile.time[index], profile.velocity[index]
        slow = time is not None and speed is not None and speed < thresholds.stationary_speed_m_s
        previous = profile.time[index - 1] if index else None
        gap = time is not None and previous is not None and time - previous > RECORDING_STOP_MIN_GAP_S
        if run_start is not None and (not slow or gap):
            _close_stationary(runs, profile, run_start, index, thresholds.pause_min_secs)
            run_start = None
        if slow and run_start is None:
            run_start = index
    if run_start is not None:
        _close_stationary(runs, profile, run_start, profile.samples, thresholds.pause_min_secs)
    return runs


def _close_stationary(
    runs: list[dict[str, Any]], profile: _Profile, start: int, end: int, min_secs: int
) -> None:
    times = _first_last(profile.time, start, end)
    if times is not None and times[1] - times[0] >= min_secs:
        runs.append(_pause("stationary", start, end, times[0], times[1]))


def _detect_pauses(profile: _Profile, thresholds: _Thresholds) -> list[dict[str, Any]]:
    """Recording stops and stationary runs, sorted by position."""
    pauses = _recording_stops(profile.time) + _stationary_runs(profile, thresholds)
    pauses.sort(key=lambda pause: (pause["start_index"], pause["kind"]))
    return pauses


# --- summary ----------------------------------------------------------------------


def _climb_summary(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "start_index": segment["start_index"],
        "gain_m": segment["elevation_gain_m"],
        "distance_m": segment["distance_m"],
        "avg_grade_pct": segment["avg_grade_pct"],
        "vam_m_per_h": segment["vam_m_per_h"],
    }


def _summary(segments: list[dict[str, Any]], pauses: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts, totals, longest and steepest climb and the total pause time."""
    climbs = [segment for segment in segments if segment["type"] == "climb"]
    descents = [segment for segment in segments if segment["type"] == "descent"]
    longest = max(
        climbs, key=lambda s: (s["distance_m"] or 0, s["duration_s"] or 0), default=None
    )
    graded = [segment for segment in climbs if segment["avg_grade_pct"] is not None]
    steepest = max(graded, key=lambda s: s["avg_grade_pct"], default=None)
    return {
        "climbs": len(climbs),
        "descents": len(descents),
        "total_climb_gain_m": round(sum(s["elevation_gain_m"] or 0 for s in climbs), 1),
        "total_descent_loss_m": round(sum(s["elevation_loss_m"] or 0 for s in descents), 1),
        "longest_climb": _climb_summary(longest) if longest else None,
        "steepest_climb": _climb_summary(steepest) if steepest else None,
        "pause_time_s": round(sum(pause["duration_s"] for pause in pauses), 1),
    }


# --- public API -------------------------------------------------------------------


def detect_segments(  # pylint: disable=too-many-arguments
    streams: list[dict[str, Any]],
    *,
    min_climb_gain_m: float = 30.0,
    min_descent_loss_m: float = 30.0,
    min_grade_pct: float = 2.0,
    hysteresis_m: float = 8.0,
    smooth_samples: int = 9,
    pause_min_secs: int = 60,
    stationary_speed_m_s: float = 0.5,
    extra_stream_types: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Detect climbs, descents, in-between sections and pauses in activity streams.

    Returns ``{"altitude_source", "samples", "segments", "pauses", "summary"}`` or
    ``{"error": ...}`` when the time or altitude stream is missing (never raises).
    ``fixed_altitude`` is preferred over ``altitude``; the profile is smoothed with a
    centred moving average of ``smooth_samples`` samples before the hysteresis walk.
    Each segment has the keys in ``SEGMENT_KEYS``; ``streams`` holds ``range_stats``
    of every custom stream and of every present type in ``extra_stream_types``.
    """
    profile = _build_profile(streams, smooth_samples, extra_stream_types)
    if isinstance(profile, str):
        return {"error": profile}
    thresholds = _Thresholds(
        min_climb_gain_m=min_climb_gain_m,
        min_descent_loss_m=min_descent_loss_m,
        min_grade_pct=min_grade_pct,
        pause_min_secs=pause_min_secs,
        stationary_speed_m_s=stationary_speed_m_s,
    )
    runs = _hysteresis_runs(profile.altitude, hysteresis_m)
    ranges = _with_other_sections(_classify_runs(runs, profile, thresholds), profile)
    segments = [_segment_metrics(profile, segment_range) for segment_range in ranges]
    pauses = _detect_pauses(profile, thresholds)
    return {
        "altitude_source": profile.altitude_source,
        "samples": profile.samples,
        "segments": segments,
        "pauses": pauses,
        "summary": _summary(segments, pauses),
    }


def _clock(seconds: float | None) -> str:
    """Seconds as mm:ss or h:mm:ss; 'n/a' for None."""
    if seconds is None:
        return "n/a"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _fmt(value: Any, suffix: str = "") -> str:
    """Value with suffix, 'n/a' for None."""
    return "n/a" if value is None else f"{format_value(value)}{suffix}"


def _km(metres: float | None) -> str:
    return "n/a" if metres is None else f"{metres / 1000:.1f} km"


def _kmh(metres_per_second: float | None) -> str:
    return "n/a" if metres_per_second is None else f"{metres_per_second * 3.6:.1f} km/h"


def _segment_line(number: int, segment: dict[str, Any]) -> str:
    """The one-line description of a segment."""
    head = (
        f"[{number}] {str(segment['type']).capitalize()} "
        f"{_clock(segment['start_time'])}-{_clock(segment['end_time'])} "
        f"(idx {segment['start_index']}-{segment['end_index']}): "
        f"+{_fmt(segment['elevation_gain_m'])} m / -{_fmt(segment['elevation_loss_m'])} m "
        f"over {_km(segment['distance_m'])}, avg grade {_fmt(segment['avg_grade_pct'])} %, "
        f"max {_fmt(segment['max_grade_pct'])} %"
    )
    if segment["type"] == "climb":
        head += f", VAM {_fmt(segment['vam_m_per_h'])} m/h"
    parts = [
        head,
        f"power avg {_fmt(segment['avg_watts'])} W, NP {_fmt(segment['normalized_power'])} W, "
        f"max {_fmt(segment['max_watts'])} W",
        f"HR avg {_fmt(segment['avg_hr'])}, max {_fmt(segment['max_hr'])} bpm",
        f"cadence {_fmt(segment['avg_cadence_nonzero'])} rpm",
        f"speed {_kmh(segment['avg_speed_m_s'])}",
    ]
    return "; ".join(parts)


def _summary_lines(result: dict[str, Any]) -> list[str]:
    """Header: altitude source, sample count, counts and totals, longest and steepest climb."""
    summary = result.get("summary") or {}
    lines = [
        f"Segments (altitude from {result.get('altitude_source')}, {result.get('samples')} samples, "
        f"{len(result.get('segments') or [])} listed): "
        f"{summary.get('climbs', 0)} climbs (+{_fmt(summary.get('total_climb_gain_m'))} m), "
        f"{summary.get('descents', 0)} descents (-{_fmt(summary.get('total_descent_loss_m'))} m), "
        f"pause time {_fmt(summary.get('pause_time_s'))} s"
    ]
    for label in ("longest", "steepest"):
        climb = summary.get(f"{label}_climb")
        if climb:
            lines.append(
                f"{label.capitalize()} climb: idx {climb['start_index']}, +{_fmt(climb['gain_m'])} m "
                f"over {_km(climb['distance_m'])}, avg grade {_fmt(climb['avg_grade_pct'])} %, "
                f"VAM {_fmt(climb['vam_m_per_h'])} m/h"
            )
    return lines


def _pause_lines(pauses: list[dict[str, Any]]) -> list[str]:
    if not pauses:
        return ["Pauses: none detected"]
    total = sum(pause["duration_s"] for pause in pauses)
    lines = [f"Pauses ({len(pauses)}, {_fmt(total)} s total):"]
    for pause in pauses:
        kind = str(pause["kind"]).replace("_", " ")
        lines.append(
            f"  {kind} {_clock(pause['start_time'])}-{_clock(pause['end_time'])} "
            f"(idx {pause['start_index']}-{pause['end_index']}): {_fmt(pause['duration_s'])} s"
        )
    return lines


def format_segments(
    result: dict[str, Any], stream_defs: CustomFieldDefs | None = None, max_segments: int = 40
) -> str:
    """Compact text report of a ``detect_segments`` result.

    ``stream_defs`` (custom stream definitions keyed by code) provides the labels of
    the per-segment stream statistics. Missing values are written as ``n/a``.
    """
    if "error" in result:
        return f"Segment detection not possible: {result['error']}"
    defs = stream_defs or {}
    lines = _summary_lines(result)
    segments = result.get("segments") or []
    for number, segment in enumerate(segments[:max_segments], 1):
        lines.append(_segment_line(number, segment))
        for stream_type, stats in (segment.get("streams") or {}).items():
            info = describe_stream({"type": stream_type}, defs)
            lines.append(f"  {stream_label(info)}: {format_stats(stats)}")
    if len(segments) > max_segments:
        lines.append(f"... {len(segments) - max_segments} more segments not shown")
    lines.extend(_pause_lines(result.get("pauses") or []))
    return "\n".join(lines)


def _json_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_clean(item) for item in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def segments_to_json(result: dict[str, Any]) -> dict[str, Any]:
    """JSON-serialisable deep copy of a ``detect_segments`` result (NaN/inf become null)."""
    cleaned = _json_clean(result)
    return cleaned if isinstance(cleaned, dict) else {}
