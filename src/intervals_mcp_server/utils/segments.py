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
stream is available the ends of a run are trimmed to where the grade over a distance
window (100 m by default) reaches the minimum grade, so a noisy flat approach is not part
of a climb.

Sport profiles (``sport``) set the defaults that differ between a road ride and a mountain
hike: altitude smoothing, the distance window of the grade, the minimum horizontal
distance below which no grade is computed (GPS distance on slow, steep terrain is too
short and would turn into extreme percentages), the plausible grade above which a segment
is flagged, and the speed below which a stretch is a pause candidate. Pause candidates are
classified: a real pause (no vertical or horizontal progress, little stepping) counts as
pause time, slow movement (scrambling, steep climbing with GPS speed near zero) is kept as
moving time. A device moving-time counter stream, when present, is reported for comparison
but not treated as truth. VAM and speed use the moving time of a segment (recording stops
and real pauses excluded). Segment gains are sums inside detected segments of the smoothed
profile and are not the same number as the activity's total elevation gain.
"""

# pylint: disable=too-many-lines

import math
from dataclasses import dataclass, replace
from typing import Any

from intervals_mcp_server.utils.custom_fields import CustomFieldDefs, format_value, is_missing
from intervals_mcp_server.utils.streams import (
    FOOT_SPORTS,
    describe_stream,
    find_stream,
    foreign_stream_reason,
    format_stats,
    is_counter_stream,
    normalized_power,
    numeric_values,
    range_stats,
    stream_label,
)

# A jump in the time stream larger than this is a recording stop.
RECORDING_STOP_MIN_GAP_S = 5
# Sections between climbs and descents are only reported when longer than this.
OTHER_SEGMENT_MIN_DURATION_S = 120
# Rolling distance window of the maximum grade (default profile).
MAX_GRADE_WINDOW_M = 100.0
# Normalized power: rolling window and minimum amount of data.
NP_WINDOW_S = 30
NP_MIN_DURATION_S = 60
# A pause candidate with this much horizontal progress (m/s) or stepping share is slow movement.
SLOW_MOVEMENT_MIN_SPEED_M_S = 0.15
SLOW_MOVEMENT_MIN_CADENCE_SHARE = 0.6
SLOW_MOVEMENT_MIN_VERTICAL_M = 3.0  # altitude range per chunk below this is treated as barometer/GPS noise
SLOW_CHUNK_S = 120  # pause candidates are classified in chunks of this length
MAX_LISTED_PAUSES = 20


@dataclass(frozen=True)
class SportDefaults:  # pylint: disable=too-many-instance-attributes
    """Detection defaults of a sport family."""

    name: str
    stationary_speed_m_s: float
    smooth_samples: int
    grade_window_m: float
    min_grade_distance_m: float
    plausible_grade_pct: float
    slow_vertical_m_per_h: float
    raw_window_m: float


DEFAULT_PROFILE = SportDefaults("default", 0.5, 9, MAX_GRADE_WINDOW_M, 50.0, 40.0, 100.0, 20.0)
BIKE_PROFILE = SportDefaults("bike", 0.5, 9, MAX_GRADE_WINDOW_M, 50.0, 25.0, 100.0, 20.0)
FOOT_PROFILE = SportDefaults("foot (hike / walk / run)", 0.3, 15, 50.0, 30.0, 60.0, 100.0, 10.0)


def sport_defaults(sport: Any) -> SportDefaults:
    """Detection defaults for an activity type (foot sports, bikes, everything else)."""
    name = str(sport or "").strip().lower()
    if not name:
        return DEFAULT_PROFILE
    if name in FOOT_SPORTS or name in ("backcountryski", "snowshoe"):
        return FOOT_PROFILE
    if "ride" in name or name in ("velomobile", "handcycle"):
        return BIKE_PROFILE
    return DEFAULT_PROFILE

ALTITUDE_SOURCES = ("fixed_altitude", "altitude")
SEGMENT_KEYS = (
    "type", "start_index", "end_index", "start_time", "end_time", "duration_s",
    "distance_m", "elevation_gain_m", "elevation_loss_m", "avg_grade_pct", "max_grade_pct",
    "vam_m_per_h", "avg_watts", "normalized_power", "max_watts", "avg_hr", "max_hr",
    "avg_cadence_nonzero", "avg_speed_m_s", "streams", "moving_s", "raw_avg_grade_pct",
    "raw_max_grade_pct", "quality_flags",
)

Samples = list[float | None]
Run = tuple[int, int, int]
Range = tuple[int, int, str]
Window = tuple[int, int, float]  # first index, last index (inclusive), grade in percent


@dataclass
class _Thresholds:  # pylint: disable=too-many-instance-attributes
    """Detection thresholds of ``detect_segments``."""

    min_climb_gain_m: float
    min_descent_loss_m: float
    min_grade_pct: float
    pause_min_secs: int
    stationary_speed_m_s: float
    defaults: SportDefaults = DEFAULT_PROFILE
    show_raw_grade: bool = False


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
    raw_altitude: Samples | None = None
    moving_counter: Samples | None = None  # a device moving-time counter stream, if any
    hidden_streams: dict[str, str] | None = None
    paused_prefix: list[float] | None = None  # seconds of real pauses / recording stops before each index


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


def _stat_streams(
    streams: list[dict[str, Any]], extra: tuple[str, ...], sport: Any = None
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Custom streams plus the explicitly requested stream types, in API order.

    Clock-like counter streams and (when the sport is known) streams of another sport are
    left out of the custom streams and returned with the reason; requested types are kept.
    """
    selected: list[dict[str, Any]] = []
    hidden: dict[str, str] = {}
    seen: set[str] = set()
    shown: list[tuple[str, list[Any]]] = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        stream_type = stream.get("type")
        if not isinstance(stream_type, str) or stream_type in seen:
            continue
        if stream_type in extra:
            selected.append(stream)
            seen.add(stream_type)
            continue
        if not stream.get("custom"):
            continue
        if is_counter_stream(stream.get("data") or []):
            hidden[stream_type] = "clock/counter stream"
            continue
        reason = foreign_stream_reason(stream_type, stream.get("name"), sport)
        twin = next((other for other, values in shown if values == stream.get("data")), None)
        if reason or twin is not None:
            hidden[stream_type] = reason or f"identical to {twin}"
            continue
        selected.append(stream)
        shown.append((stream_type, stream.get("data") or []))
        seen.add(stream_type)
    return selected, hidden


def _moving_counter(streams: list[dict[str, Any]]) -> Samples | None:
    """A custom counter stream whose name says 'moving' (a device's moving-time clock)."""
    for stream in streams:
        if not isinstance(stream, dict) or not stream.get("custom"):
            continue
        label = f"{stream.get('type')} {stream.get('name') or ''}".lower()
        if "moving" in label and is_counter_stream(stream.get("data") or []):
            return _numbers(stream.get("data"))
    return None


def _optional(streams: list[dict[str, Any]], stream_type: str) -> Samples | None:
    """Numeric samples of a stream, None when the stream is absent."""
    stream = find_stream(streams, stream_type)
    return None if stream is None else _numbers(stream.get("data"))


def _build_profile(
    streams: list[dict[str, Any]], smooth_samples: int, extra: tuple[str, ...], sport: Any = None
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
    stat_streams, hidden = _stat_streams(streams, extra, sport)
    return _Profile(
        altitude_source=source,
        samples=samples,
        time=time[:samples],
        altitude=_smooth(raw[:samples], smooth_samples),
        distance=_optional(streams, "distance"),
        velocity=_optional(streams, "velocity_smooth"),
        metrics=metrics,
        stat_streams=stat_streams,
        raw_altitude=raw[:samples],
        moving_counter=_moving_counter(streams),
        hidden_streams=hidden,
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


def _avg_grade(profile: _Profile, start: int, end: int, min_distance: float = 0.0, altitude: Samples | None = None) -> float | None:
    """Net altitude change over the distance covered, in percent; None below ``min_distance`` metres."""
    distance = _delta(profile.distance, start, end)
    climb = _delta(altitude if altitude is not None else profile.altitude, start, end)
    if distance is None or distance <= 0 or distance < min_distance or climb is None:
        return None
    return climb / distance * 100


def _classify(profile: _Profile, thresholds: _Thresholds, run: Run) -> str | None:
    """Climb / descent when the run passes the gain and grade thresholds, else None."""
    start, end, direction = run
    gain_loss = _gain_loss(profile.altitude, start, end)
    if gain_loss is None:
        return None
    gain, loss = gain_loss
    grade = _avg_grade(profile, start, end, thresholds.defaults.min_grade_distance_m)
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


def _window_grades(
    profile: _Profile, start: int, end: int, window_m: float = MAX_GRADE_WINDOW_M, altitude: Samples | None = None
) -> list[Window]:
    """Grade of every rolling window of at least ``window_m`` metres of distance inside a range."""
    if profile.distance is None:
        return []
    source = altitude if altitude is not None else profile.altitude
    points: list[tuple[int, float, float]] = []
    for index in range(start, end):
        distance = profile.distance[index] if index < len(profile.distance) else None
        height = source[index] if index < len(source) else None
        if distance is not None and height is not None:
            points.append((index, distance, height))
    windows: list[Window] = []
    ahead = 0
    for index, distance, height in points:
        while ahead < len(points) and points[ahead][1] - distance < window_m:
            ahead += 1
        if ahead >= len(points):
            break
        far_index, far_distance, far_height = points[ahead]
        windows.append((index, far_index, (far_height - height) / (far_distance - distance) * 100))
    return windows


def _pick_max(grades: list[float], kind: str) -> float | None:
    if not grades:
        return None
    if kind == "climb":
        return max(grades)
    if kind == "descent":
        return min(grades)
    return max(grades, key=abs)


def _max_grade(profile: _Profile, kind: str, start: int, end: int, window_m: float = MAX_GRADE_WINDOW_M) -> float | None:
    """Steepest rolling-window grade: highest for climbs, lowest for descents, largest magnitude otherwise."""
    return _pick_max([grade for _, _, grade in _window_grades(profile, start, end, window_m)], kind)


def _trim(
    profile: _Profile, thresholds: _Thresholds, start: int, end: int, direction: int
) -> tuple[int, int]:
    """Drop the ends of a run where the rolling-window grade stays below the minimum grade.

    The hysteresis walk anchors a run at the lowest (highest) point before it, which
    on a noisy flat approach can lie well before the road actually starts to climb.
    Runs without any window (no distance stream, or shorter than the window) are kept
    as they are; a run without a single steep window is emptied.
    """
    windows = _window_grades(profile, start, end, thresholds.defaults.grade_window_m)
    if not windows:
        return start, end
    steep = [window for window in windows if window[2] * direction >= thresholds.min_grade_pct]
    if not steep:
        return start, start
    return steep[0][0], steep[-1][1] + 1


def _moving_seconds(profile: _Profile, start: int, end: int) -> float | None:
    """Seconds of [start, end) without recording stops and real pauses (first to last sample)."""
    times = _first_last(profile.time, start, end)
    if times is None:
        return None
    span = times[1] - times[0]
    if profile.paused_prefix is None:
        return span
    last = max(start, min(end, profile.samples) - 1)
    paused = profile.paused_prefix[last] - profile.paused_prefix[start]
    return max(0.0, span - paused)


def _terrain_metrics(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    profile: _Profile, thresholds: _Thresholds, kind: str, start: int, end: int, moving_s: float | None
) -> dict[str, Any]:
    """Distance, elevation, grade (smoothed and optionally raw) and VAM of a segment."""
    defaults = thresholds.defaults
    gain_loss = _gain_loss(profile.altitude, start, end)
    gain, loss = gain_loss if gain_loss is not None else (None, None)
    vam = gain / (moving_s / 3600) if kind == "climb" and gain is not None and moving_s else None
    raw_avg = raw_max = None
    if thresholds.show_raw_grade and profile.raw_altitude is not None:
        raw_avg = _avg_grade(profile, start, end, 0.0, profile.raw_altitude)
        raw_max = _pick_max([g for _, _, g in _window_grades(profile, start, end, defaults.raw_window_m, profile.raw_altitude)], kind)
    return {
        "distance_m": _round(_delta(profile.distance, start, end), 1),
        "elevation_gain_m": _round(gain, 1),
        "elevation_loss_m": _round(loss, 1),
        "avg_grade_pct": _round(_avg_grade(profile, start, end, defaults.min_grade_distance_m), 2),
        "max_grade_pct": _round(_max_grade(profile, kind, start, end, defaults.grade_window_m), 1),
        "vam_m_per_h": _round(vam, 0),
        "raw_avg_grade_pct": _round(raw_avg, 2),
        "raw_max_grade_pct": _round(raw_max, 1),
    }


def _normalized_power(profile: _Profile, start: int, end: int) -> float | None:
    """Normalized power of the segment (see ``streams.normalized_power``)."""
    watts = profile.metrics.get("watts")
    if watts is None:
        return None
    return normalized_power(profile.time, _numbers(watts), start, end)


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


def _quality_flags(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    thresholds: _Thresholds, segment: dict[str, Any], pauses: list[dict[str, Any]],
    slow: list[dict[str, Any]], start: int, end: int,
) -> list[str]:
    """Data-quality notes of a segment: grade not computed / implausible, stops and slow movement inside."""
    defaults = thresholds.defaults
    flags = []
    distance = segment["distance_m"]
    if segment["type"] != "other" and distance is not None and distance < defaults.min_grade_distance_m:
        flags.append(f"horizontal distance only {distance:.0f} m: grade not computed")
    grade = segment["avg_grade_pct"]
    if grade is not None and abs(grade) > defaults.plausible_grade_pct:
        flags.append(
            f"average grade {grade:.0f}% over {distance or 0:.0f} m horizontal exceeds {defaults.plausible_grade_pct:.0f}% "
            f"({defaults.name}): very steep terrain or a too short GPS distance; treat the grade as approximate"
        )
    max_grade = segment["max_grade_pct"]
    if max_grade is not None and abs(max_grade) > defaults.plausible_grade_pct and not (grade is not None and abs(grade) > defaults.plausible_grade_pct):
        flags.append(f"steepest {defaults.grade_window_m:.0f} m window {max_grade:.0f}% is approximate (a few metres of GPS error change it a lot)")
    stops = sum(p["duration_s"] for p in pauses if p["kind"] == "recording_stop" and start <= p["start_index"] and p["end_index"] < end)
    if stops:
        flags.append(f"contains recording stops of {stops:.0f} s")
    rests = sum(p["duration_s"] for p in pauses if p["kind"] == "stationary" and start <= p["start_index"] < end)
    if rests:
        flags.append(f"contains pauses of {rests:.0f} s (excluded from VAM and speed)")
    crawl = sum(p["duration_s"] for p in slow if start <= p["start_index"] < end)
    if crawl and segment["duration_s"] and crawl > 0.25 * segment["duration_s"]:
        flags.append(f"{crawl:.0f} s of very slow movement (GPS speed near zero while moving)")
    return flags


def _segment_metrics(
    profile: _Profile, thresholds: _Thresholds, segment_range: Range, pauses: list[dict[str, Any]], slow: list[dict[str, Any]]
) -> dict[str, Any]:
    """All reported values of one segment, in ``SEGMENT_KEYS`` order."""
    start, end, kind = segment_range
    times = _first_last(profile.time, start, end)
    duration = times[1] - times[0] if times else None
    moving = _moving_seconds(profile, start, end)
    segment: dict[str, Any] = {
        "type": kind,
        "start_index": start,
        "end_index": end,
        "start_time": times[0] if times else None,
        "end_time": times[1] if times else None,
        "duration_s": duration,
    }
    terrain = _terrain_metrics(profile, thresholds, kind, start, end, moving)
    segment.update({k: v for k, v in terrain.items() if not k.startswith("raw_")})
    segment.update(_effort_metrics(profile, start, end))
    distance = segment["distance_m"]
    segment["avg_speed_m_s"] = _round(distance / moving, 2) if moving and distance is not None else None
    segment["streams"] = {
        str(stream["type"]): range_stats(stream.get("data") or [], [(start, end)])
        for stream in profile.stat_streams
    }
    segment["moving_s"] = _round(moving, 0)
    segment["raw_avg_grade_pct"] = terrain["raw_avg_grade_pct"]
    segment["raw_max_grade_pct"] = terrain["raw_max_grade_pct"]
    segment["quality_flags"] = _quality_flags(thresholds, segment, pauses, slow, start, end)
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
    first to its last sample. Each run is classified as a real pause or slow movement.
    """
    if profile.velocity is None:
        return []
    runs: list[dict[str, Any]] = []
    run_start: int | None = None
    for index in range(profile.samples):
        time = profile.time[index]
        speed = profile.velocity[index] if index < len(profile.velocity) else None
        slow = time is not None and speed is not None and speed < thresholds.stationary_speed_m_s
        previous = profile.time[index - 1] if index else None
        gap = time is not None and previous is not None and time - previous > RECORDING_STOP_MIN_GAP_S
        if run_start is not None and (not slow or gap):
            _close_stationary(runs, profile, thresholds, run_start, index)
            run_start = None
        if slow and run_start is None:
            run_start = index
    if run_start is not None:
        _close_stationary(runs, profile, thresholds, run_start, profile.samples)
    return runs


def _cadence_share(profile: _Profile, start: int, end: int) -> float | None:
    cadence = profile.metrics.get("cadence")
    nums = numeric_values(cadence[start:end]) if cadence else []
    return sum(1 for value in nums if value > 0) / len(nums) if nums else None


def _run_stats(profile: _Profile, start: int, end: int) -> dict[str, Any]:
    """Vertical and horizontal progress and stepping share of a sample range."""
    times = _first_last(profile.time, start, end)
    duration = max(times[1] - times[0], 1.0) if times else 1.0
    heights = [value for value in profile.altitude[start:end] if value is not None]
    climbed = (max(heights) - min(heights)) if heights else 0.0
    progress = _delta(profile.distance, start, end)
    return {
        "climbed": climbed,
        "vertical": climbed / duration * 3600,
        "horizontal": progress / duration if progress is not None else 0.0,
        "share": _cadence_share(profile, start, end),
    }


def _is_moving(stats: dict[str, Any], thresholds: _Thresholds) -> bool:
    share = stats["share"]
    return (
        (stats["vertical"] >= thresholds.defaults.slow_vertical_m_per_h and stats["climbed"] >= SLOW_MOVEMENT_MIN_VERTICAL_M)
        or stats["horizontal"] >= SLOW_MOVEMENT_MIN_SPEED_M_S
        or (share is not None and share >= SLOW_MOVEMENT_MIN_CADENCE_SHARE)
    )


def _chunks(profile: _Profile, start: int, end: int) -> list[tuple[int, int]]:
    """Consecutive index ranges of about ``SLOW_CHUNK_S`` seconds (a short tail joins the last chunk)."""
    chunks: list[tuple[int, int]] = []
    chunk_start = start
    for index in range(start, end):
        first, current = profile.time[chunk_start], profile.time[index]
        if first is not None and current is not None and current - first >= SLOW_CHUNK_S:
            chunks.append((chunk_start, index))
            chunk_start = index
    tail = (chunk_start, end)
    if chunks and _delta(profile.time, *tail) is not None and (_delta(profile.time, *tail) or 0) < SLOW_CHUNK_S / 2:
        chunks[-1] = (chunks[-1][0], end)
    else:
        chunks.append(tail)
    return chunks


def _close_stationary(runs: list[dict[str, Any]], profile: _Profile, thresholds: _Thresholds, start: int, end: int) -> None:
    """Split a slow run into chunks, classify them (pause / slow movement) and emit merged sub-runs."""
    times = _first_last(profile.time, start, end)
    if times is None or times[1] - times[0] < thresholds.pause_min_secs:
        return
    labels = [(a, b, "slow_movement" if _is_moving(_run_stats(profile, a, b), thresholds) else "stationary")
              for a, b in _chunks(profile, start, end)]
    merged: list[list[Any]] = []
    for a, b, kind in labels:
        if merged and merged[-1][2] == kind:
            merged[-1][1] = b
        else:
            merged.append([a, b, kind])
    for a, b, kind in merged:
        span = _first_last(profile.time, a, b)
        if span is None:
            continue
        if kind == "stationary" and span[1] - span[0] < thresholds.pause_min_secs:
            kind = "slow_movement"
        stats = _run_stats(profile, a, b)
        pause = _pause(kind, a, b, span[0], span[1])
        pause.update(
            vertical_m_per_h=round(stats["vertical"]), horizontal_m_s=round(stats["horizontal"], 3),
            cadence_share=round(stats["share"], 2) if stats["share"] is not None else None,
            device_moving_s=_delta(profile.moving_counter, a, b) if profile.moving_counter else None,
        )
        runs.append(pause)


def _detect_pauses(profile: _Profile, thresholds: _Thresholds) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(pauses, slow movement): recording stops and real pauses sorted by position, and slow-movement runs."""
    runs = _stationary_runs(profile, thresholds)
    pauses = _recording_stops(profile.time) + [run for run in runs if run["kind"] == "stationary"]
    pauses.sort(key=lambda pause: (pause["start_index"], pause["kind"]))
    slow = [run for run in runs if run["kind"] == "slow_movement"]
    return pauses, slow


def _paused_prefix(profile: _Profile, pauses: list[dict[str, Any]]) -> list[float]:
    """Prefix sums of paused seconds per sample gap (recording stops and real pauses)."""
    paused = [0.0] * (profile.samples + 1)
    gaps = [0.0] * profile.samples
    for pause in pauses:
        start, end = pause["start_index"], pause["end_index"]
        if pause["kind"] == "recording_stop":
            if 0 <= start < profile.samples:
                gaps[start] += pause["duration_s"]
            continue
        for index in range(start, min(end, profile.samples) - 1):
            current, following = profile.time[index], profile.time[index + 1]
            if current is not None and following is not None:
                gaps[index] += following - current
    for index in range(profile.samples):
        paused[index + 1] = paused[index] + gaps[index]
    return paused


# --- summary ----------------------------------------------------------------------


def _climb_summary(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "start_index": segment["start_index"],
        "gain_m": segment["elevation_gain_m"],
        "distance_m": segment["distance_m"],
        "avg_grade_pct": segment["avg_grade_pct"],
        "vam_m_per_h": segment["vam_m_per_h"],
    }


def _summary(segments: list[dict[str, Any]], pauses: list[dict[str, Any]], slow: list[dict[str, Any]], profile: _Profile) -> dict[str, Any]:
    """Counts, totals, longest and steepest climb, pause time and slow movement."""
    climbs = [segment for segment in segments if segment["type"] == "climb"]
    descents = [segment for segment in segments if segment["type"] == "descent"]
    longest = max(
        climbs, key=lambda s: (s["distance_m"] or 0, s["duration_s"] or 0), default=None
    )
    graded = [
        segment for segment in climbs
        if segment["avg_grade_pct"] is not None and not any("grade" in flag for flag in segment.get("quality_flags") or [])
    ]
    steepest = max(graded, key=lambda s: s["avg_grade_pct"], default=None)
    whole = _gain_loss(profile.altitude, 0, profile.samples)
    times = _first_last(profile.time, 0, profile.samples)
    elapsed = times[1] - times[0] if times else None
    pause_time = round(sum(pause["duration_s"] for pause in pauses), 1)
    return {
        "climbs": len(climbs),
        "descents": len(descents),
        "total_climb_gain_m": round(sum(s["elevation_gain_m"] or 0 for s in climbs), 1),
        "total_descent_loss_m": round(sum(s["elevation_loss_m"] or 0 for s in descents), 1),
        "longest_climb": _climb_summary(longest) if longest else None,
        "steepest_climb": _climb_summary(steepest) if steepest else None,
        "pause_time_s": pause_time,
        "recording_stop_s": round(sum(p["duration_s"] for p in pauses if p["kind"] == "recording_stop"), 1),
        "slow_movement_s": round(sum(run["duration_s"] for run in slow), 1),
        "slow_movement_runs": len(slow),
        "elapsed_s": elapsed,
        "moving_estimate_s": round(elapsed - pause_time, 1) if elapsed is not None else None,
        "device_moving_s": _delta(profile.moving_counter, 0, profile.samples) if profile.moving_counter else None,
        "profile_gain_m": round(whole[0], 1) if whole else None,
        "profile_loss_m": round(whole[1], 1) if whole else None,
        "flagged_segments": sum(1 for s in segments if s.get("quality_flags")),
    }


# --- public API -------------------------------------------------------------------


def detect_segments(  # pylint: disable=too-many-arguments,too-many-locals
    streams: list[dict[str, Any]],
    *,
    min_climb_gain_m: float = 30.0,
    min_descent_loss_m: float = 30.0,
    min_grade_pct: float = 2.0,
    hysteresis_m: float = 8.0,
    smooth_samples: int | None = None,
    pause_min_secs: int = 60,
    stationary_speed_m_s: float | None = None,
    extra_stream_types: tuple[str, ...] = (),
    sport: Any = None,
    grade_window_m: float | None = None,
    min_grade_distance_m: float | None = None,
    show_raw_grade: bool = False,
) -> dict[str, Any]:
    """Detect climbs, descents, in-between sections, pauses and slow movement in activity streams.

    Returns ``{"altitude_source", "samples", "segments", "pauses", "slow_movement",
    "summary", "settings", "hidden_streams"}`` or ``{"error": ...}`` when the time or
    altitude stream is missing (never raises). ``fixed_altitude`` is preferred over
    ``altitude``; the profile is smoothed with a centred moving average before the
    hysteresis walk. ``sport`` (an activity type) selects the sport defaults for
    smoothing, grade window, minimum grade distance, plausible grade and stationary
    speed; explicit arguments override them. Each segment has the keys in
    ``SEGMENT_KEYS``; ``streams`` holds ``range_stats`` of every custom stream (counters
    and other-sport streams left out) and of every present type in ``extra_stream_types``.
    """
    defaults = sport_defaults(sport)
    defaults = replace(
        defaults,
        smooth_samples=defaults.smooth_samples if smooth_samples is None else smooth_samples,
        stationary_speed_m_s=defaults.stationary_speed_m_s if stationary_speed_m_s is None else stationary_speed_m_s,
        grade_window_m=defaults.grade_window_m if grade_window_m is None else grade_window_m,
        min_grade_distance_m=defaults.min_grade_distance_m if min_grade_distance_m is None else min_grade_distance_m,
    )
    profile = _build_profile(streams, defaults.smooth_samples, extra_stream_types, sport)
    if isinstance(profile, str):
        return {"error": profile}
    thresholds = _Thresholds(
        min_climb_gain_m=min_climb_gain_m,
        min_descent_loss_m=min_descent_loss_m,
        min_grade_pct=min_grade_pct,
        pause_min_secs=pause_min_secs,
        stationary_speed_m_s=defaults.stationary_speed_m_s,
        defaults=defaults,
        show_raw_grade=show_raw_grade,
    )
    pauses, slow = _detect_pauses(profile, thresholds)
    profile.paused_prefix = _paused_prefix(profile, pauses)
    runs = _hysteresis_runs(profile.altitude, hysteresis_m)
    ranges = _with_other_sections(_classify_runs(runs, profile, thresholds), profile)
    segments = [_segment_metrics(profile, thresholds, segment_range, pauses, slow) for segment_range in ranges]
    return {
        "altitude_source": profile.altitude_source,
        "samples": profile.samples,
        "segments": segments,
        "pauses": pauses,
        "slow_movement": slow,
        "summary": _summary(segments, pauses, slow, profile),
        "settings": {
            "sport_profile": defaults.name, "smooth_samples": defaults.smooth_samples, "grade_window_m": defaults.grade_window_m,
            "min_grade_distance_m": defaults.min_grade_distance_m, "plausible_grade_pct": defaults.plausible_grade_pct,
            "stationary_speed_m_s": defaults.stationary_speed_m_s, "slow_vertical_m_per_h": defaults.slow_vertical_m_per_h,
            "hysteresis_m": hysteresis_m, "show_raw_grade": show_raw_grade,
        },
        "hidden_streams": profile.hidden_streams or {},
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
    grade = f"avg grade {_fmt(segment['avg_grade_pct'])} %"
    if segment.get("raw_avg_grade_pct") is not None:
        grade += f" (raw {_fmt(segment['raw_avg_grade_pct'])} %)"
    grade += f", max {_fmt(segment['max_grade_pct'])} %"
    if segment.get("raw_max_grade_pct") is not None:
        grade += f" (raw {_fmt(segment['raw_max_grade_pct'])} %)"
    head = (
        f"[{number}] {str(segment['type']).capitalize()} "
        f"{_clock(segment['start_time'])}-{_clock(segment['end_time'])} "
        f"(idx {segment['start_index']}-{segment['end_index']}): "
        f"+{_fmt(segment['elevation_gain_m'])} m / -{_fmt(segment['elevation_loss_m'])} m "
        f"over {_km(segment['distance_m'])}, {grade}"
    )
    if segment["type"] == "climb":
        head += f", VAM {_fmt(segment['vam_m_per_h'])} m/h"
    moving = segment.get("moving_s")
    if moving is not None and segment.get("duration_s") is not None and segment["duration_s"] - moving > 1:
        head += f", moving {_clock(moving)}"
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
    """Header: altitude source, counts and totals, elevation, moving time, settings, longest/steepest climb."""
    summary = result.get("summary") or {}
    settings = result.get("settings") or {}
    lines = [
        f"Segments (altitude from {result.get('altitude_source')}, {result.get('samples')} samples, "
        f"{len(result.get('segments') or [])} listed): "
        f"{summary.get('climbs', 0)} climbs (+{_fmt(summary.get('total_climb_gain_m'))} m), "
        f"{summary.get('descents', 0)} descents (-{_fmt(summary.get('total_descent_loss_m'))} m), "
        f"pause time {_fmt(summary.get('pause_time_s'))} s"
    ]
    if summary.get("profile_gain_m") is not None:
        lines.append(
            f"Elevation: +{_fmt(summary.get('total_climb_gain_m'))} m inside the detected climbs; whole activity from the "
            f"smoothed altitude +{_fmt(summary.get('profile_gain_m'))} m / -{_fmt(summary.get('profile_loss_m'))} m "
            "(segment sums are not the activity's total elevation gain, which Intervals.icu computes with its own correction)"
        )
    if summary.get("elapsed_s") is not None:
        moving = (
            f"Time: elapsed {_clock(summary['elapsed_s'])}, moving about {_clock(summary.get('moving_estimate_s'))} "
            f"(real pauses and recording stops {_clock(summary.get('pause_time_s'))}, of which recording stops "
            f"{_clock(summary.get('recording_stop_s'))}; very slow movement {_clock(summary.get('slow_movement_s'))} counted as moving)"
        )
        if summary.get("device_moving_s") is not None:
            moving += f"; device moving-time counter {_clock(summary['device_moving_s'])} (shown for comparison, not used)"
        lines.append(moving)
    if settings:
        lines.append(
            f"Settings ({settings.get('sport_profile')}): grade over >= {_fmt(settings.get('grade_window_m'))} m of distance, "
            f"no grade below {_fmt(settings.get('min_grade_distance_m'))} m horizontal distance, segments above "
            f"{_fmt(settings.get('plausible_grade_pct'))} % flagged, altitude smoothed over {settings.get('smooth_samples')} samples, "
            f"pause candidates below {_fmt(settings.get('stationary_speed_m_s'))} m/s"
        )
    for label in ("longest", "steepest"):
        climb = summary.get(f"{label}_climb")
        if climb:
            lines.append(
                f"{label.capitalize()} climb: idx {climb['start_index']}, +{_fmt(climb['gain_m'])} m "
                f"over {_km(climb['distance_m'])}, avg grade {_fmt(climb['avg_grade_pct'])} %, "
                f"VAM {_fmt(climb['vam_m_per_h'])} m/h"
            )
    if summary.get("flagged_segments"):
        lines.append(f"Segments with data-quality flags: {summary['flagged_segments']} (see 'data quality' lines; not used for the steepest climb)")
    return lines


def _pause_lines(pauses: list[dict[str, Any]], title: str = "Pauses", empty: str = "Pauses: none detected") -> list[str]:
    if not pauses:
        return [empty] if empty else []
    total = sum(pause["duration_s"] for pause in pauses)
    lines = [f"{title} ({len(pauses)}, {_fmt(total)} s total):"]
    for pause in pauses[:MAX_LISTED_PAUSES]:
        kind = str(pause["kind"]).replace("_", " ")
        line = (
            f"  {kind} {_clock(pause['start_time'])}-{_clock(pause['end_time'])} "
            f"(idx {pause['start_index']}-{pause['end_index']}): {_fmt(pause['duration_s'])} s"
        )
        if pause.get("vertical_m_per_h") is not None and pause["kind"] == "slow_movement":
            line += f" ({pause['vertical_m_per_h']} m/h vertical, {pause['horizontal_m_s']} m/s horizontal)"
        lines.append(line)
    if len(pauses) > MAX_LISTED_PAUSES:
        lines.append(f"  ... {len(pauses) - MAX_LISTED_PAUSES} more")
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
    hidden = result.get("hidden_streams") or {}
    if hidden:
        lines.append("Custom streams left out per segment: " + ", ".join(f"{code} ({why})" for code, why in sorted(hidden.items())))
    segments = result.get("segments") or []
    for number, segment in enumerate(segments[:max_segments], 1):
        lines.append(_segment_line(number, segment))
        if segment.get("quality_flags"):
            lines.append("  data quality: " + "; ".join(segment["quality_flags"]))
        for stream_type, stats in (segment.get("streams") or {}).items():
            # min / max are enough to recognise gear positions defined with tooth units
            info = describe_stream({"type": stream_type, "data": [stats.get("min"), stats.get("max")]}, defs)
            lines.append(f"  {stream_label(info)}: {format_stats(stats)}")
    if len(segments) > max_segments:
        lines.append(f"... {len(segments) - max_segments} more segments not shown")
    lines.extend(_pause_lines(result.get("pauses") or []))
    lines.extend(_pause_lines(result.get("slow_movement") or [], "Very slow movement kept as moving time", ""))
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
