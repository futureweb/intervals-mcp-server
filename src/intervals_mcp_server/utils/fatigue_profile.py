"""
Long-ride fatigue profile (pure functions, no API access).

Compares how the athlete rides the same power before and after a given amount of work:
heart rate at matched power (drift), watts per heartbeat, cadence and Garmin stamina /
potential stamina, in steady segments of a power band, split into phases by work
thresholds (absolute kJ, also shown in kJ per kg body mass). Each threshold crossing and
each long climb carries the prior work that led to it: kJ, kJ above FTP, time above FTP
and the number of efforts above FTP, so 1,500 kJ of easy riding can be told apart from
1,500 kJ with many hard efforts.

Method (also listed in ``METHOD``):

- Work: cumulative sum of power x time on the time stream (1 Hz recordings: the sum of the
  samples, as Intervals.icu's ``icu_joules``); a recording pause (time jump > 5 s) adds
  the sample once, never the pause. kJ above FTP is the sum of the power above FTP
  (``max(0, W - FTP)``), matching Intervals.icu's ``icu_joules_above_ftp``. An effort above
  FTP is a run of at least 60 s in which the 30 s rolling power stays at or above FTP.
- Steady segments: maximal runs in which the trailing 30 s rolling power (full windows, no
  time gap > 2 s) stays inside the band widened by ``BAND_SLACK_PCT``; a segment counts
  when it lasts at least ``min_segment_secs``, its mean power lies in the band (half-open,
  low <= W < high), at most 10 % of its samples are coasting (< 10 W or missing) and heart
  rate covers 90 % of the measured part. The first 10 minutes of the ride are left out
  (warm-up, HR not settled) and segments are cut at the work thresholds, so a segment
  never spans two phases.
- Metrics of a segment are taken over the segment minus its first 60 s (HR lags behind
  power changes): time-weighted mean power, HR, W/bpm (mean power / mean HR), non-zero
  cadence and temperature; stamina and potential stamina at the segment start and end.
- Phases: before the first threshold (reference), between the thresholds and after the
  last one. A phase with fewer than 3 segments or 10 measured minutes is a small sample.
  Changes are phase minus reference; the power difference between the phases is shown
  as well, since HR also follows the power within the band.
- Climbs come from ``utils.segments.detect_segments`` (smoothed altitude, hysteresis);
  stamina at the start and end of the climb is read from the custom streams.

Background: the Intervals.icu fatigued power curves (after kJ0 / kJ1, forum thread
"Fatigue resistance", https://forum.intervals.icu/t/fatigue-resistance/4396), the request
for kJ per kg thresholds ("Power curve after kj/kg",
https://forum.intervals.icu/t/power-curve-after-kj-kg/93688) and the discussion of work
above FTP and the "good-day" effect in "Three ways field data fooled me about durability"
(https://forum.intervals.icu/t/three-ways-field-data-fooled-me-about-durability-1-350-climbs-33-amateurs/132461).
Prior work above FTP is shown as context, never as a predictor.
"""

# pylint: disable=too-many-lines

import bisect
import math
import statistics
from dataclasses import dataclass, field
from typing import Any

from intervals_mcp_server.utils.custom_fields import is_missing
from intervals_mcp_server.utils.segments import detect_segments
from intervals_mcp_server.utils.streams import find_stream

DEFAULT_THRESHOLDS_KJ: tuple[float, ...] = (750.0, 1500.0)
BAND_SLACK_PCT = 20.0  # the 30 s rolling power may leave the band by this share of its limits
ROLLING_S = 30
DEFAULT_MIN_SEGMENT_S = 120
HR_SETTLE_S = 60
WARMUP_EXCLUDE_S = 600
COAST_W = 10.0
MAX_COAST_SHARE = 0.10
MIN_HR_COVERAGE = 0.9
MAX_SAMPLE_GAP_S = 2  # larger gaps end a steady segment
RECORDING_GAP_S = 5  # larger time jumps are recording pauses (not counted as work time)
EFFORT_MIN_S = 60
SMALL_SAMPLE_SEGMENTS = 3
SMALL_SAMPLE_MINUTES = 10.0
MIN_ACTIVITIES = 3
CADENCE_SHIFT_RPM = 15.0  # a larger cadence change between phases is flagged (climbing vs flat, gearing)
DEFAULT_BAND_PCT = (75, 85)  # default band in % of FTP, rounded outward to 5 W

METHOD = (
    "Steady segments: the 30 s rolling power stays within the band widened by 20 % and the segment mean lies in "
    "the band (low <= W < high); at least the minimum duration, <= 10 % coasting (< 10 W), HR on >= 90 % of the "
    "samples; the first 10 min of the ride are left out and segments are cut at the work thresholds. HR, W/bpm, "
    "cadence and temperature exclude each segment's first 60 s (HR lag). Work = power x time on the time stream "
    "(pauses not counted); kJ above FTP = sum of power above FTP (as Intervals.icu icu_joules_above_ftp); an effort "
    "above FTP = 30 s rolling power >= FTP for >= 60 s. Changes are phase minus the phase before the first "
    "threshold; small samples (< 3 segments or < 10 min) and cadence changes > 15 rpm are flagged."
)


Samples = list[float | None]


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or is_missing(value) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rnd(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _stream(streams: list[dict[str, Any]], stream_type: str | None, length: int) -> Samples | None:
    if not stream_type:
        return None
    stream = find_stream(streams, stream_type)
    data = (stream or {}).get("data")
    if not isinstance(data, list) or not data:
        return None
    values = [_num(value) for value in data[:length]]
    values.extend([None] * (length - len(values)))
    return values


def stamina_codes(streams: list[dict[str, Any]], names: dict[str, str] | None = None) -> dict[str, str]:
    """Stream types of Garmin stamina and potential stamina: {"stamina": code, "potential": code}.

    A stream whose type or (custom item) name contains "stamina" counts; "potential" in it
    marks potential stamina. ``names`` maps stream types to custom item names.
    """
    found: dict[str, str] = {}
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        code = str(stream.get("type") or "")
        text = f"{code} {(names or {}).get(code) or ''} {stream.get('name') or ''}".lower()
        if "stamina" not in text:
            continue
        key = "potential" if "potential" in text else "stamina"
        found.setdefault(key, code)
    return found


@dataclass
class Series:  # pylint: disable=too-many-instance-attributes
    """Sample-aligned numeric streams of one ride."""

    time: list[float]
    watts: Samples
    heartrate: Samples
    cadence: Samples
    temp: Samples | None = None
    stamina: Samples | None = None
    potential: Samples | None = None
    kj: list[float] = field(default_factory=list)  # prefix sums, length n + 1
    kj_above: list[float] = field(default_factory=list)
    secs_above: list[float] = field(default_factory=list)
    efforts: list[tuple[int, int]] = field(default_factory=list)
    rolling: Samples = field(default_factory=list)

    @property
    def length(self) -> int:
        """Number of samples."""
        return len(self.time)


def build_series(streams: list[dict[str, Any]], stamina: dict[str, str] | None = None) -> Series | str:
    """Numeric series from the API streams, or an error text when time or power is missing."""
    time_stream = find_stream(streams, "time")
    raw_time = (time_stream or {}).get("data")
    if not isinstance(raw_time, list) or not raw_time:
        return "no time stream"
    times: list[float] = []
    for value in raw_time:
        number = _num(value)
        if number is None:
            break
        times.append(number)
    if len(times) != len(raw_time) or any(b < a for a, b in zip(times, times[1:], strict=False)):
        return "time stream incomplete or not monotonic"
    length = len(times)
    watts = _stream(streams, "watts", length)
    if watts is None or not any(value is not None for value in watts):
        return "no power stream"
    codes = stamina or {}
    return Series(
        time=times,
        watts=watts,
        heartrate=_stream(streams, "heartrate", length) or [None] * length,
        cadence=_stream(streams, "cadence", length) or [None] * length,
        temp=_stream(streams, "temp", length),
        stamina=_stream(streams, codes.get("stamina"), length),
        potential=_stream(streams, codes.get("potential"), length),
    )


def _rolling_mean(times: list[float], values: Samples, width: int = ROLLING_S) -> Samples:
    """Trailing rolling mean over ``width`` seconds of time; None until a full window without gaps."""
    out: Samples = [None] * len(times)
    head = 0
    start = 0  # first sample after the latest gap
    total = 0.0
    for index, moment in enumerate(times):
        if index and moment - times[index - 1] > MAX_SAMPLE_GAP_S:
            head = start = index
            total = 0.0
        total += values[index] or 0.0
        while times[head] <= moment - width:
            total -= values[head] or 0.0
            head += 1
        if moment - times[start] >= width - 1:
            out[index] = total / (index - head + 1)
    return out


def accumulate_work(series: Series, ftp: float | None) -> None:
    """Fill the work prefix sums, the efforts above FTP and the rolling power of a series."""
    length = series.length
    kj, above, secs = [0.0] * (length + 1), [0.0] * (length + 1), [0.0] * (length + 1)
    for index in range(length):
        gap = series.time[index] - series.time[index - 1] if index else 1.0
        step = gap if 0 < gap <= RECORDING_GAP_S else 1.0
        power = series.watts[index] or 0.0
        kj[index + 1] = kj[index] + power * step / 1000
        over = power - ftp if ftp else 0.0
        above[index + 1] = above[index] + (over * step / 1000 if over > 0 else 0.0)
        secs[index + 1] = secs[index] + (step if over > 0 else 0.0)
    series.kj, series.kj_above, series.secs_above = kj, above, secs
    series.rolling = _rolling_mean(series.time, series.watts)
    efforts: list[tuple[int, int]] = []
    if ftp:
        start: int | None = None
        for index in range(length + 1):
            value = series.rolling[index] if index < length else None
            if value is not None and value >= ftp:
                start = index if start is None else start
                continue
            if start is not None and series.time[index - 1] - series.time[start] + 1 >= EFFORT_MIN_S:
                efforts.append((start, index))
            start = None
    series.efforts = efforts


def _value_at(values: Samples | None, index: int, forward: bool = True) -> float | None:
    """First numeric value at or after (``forward``) / at or before ``index``."""
    if not values:
        return None
    rng = range(index, len(values)) if forward else range(min(index, len(values) - 1), -1, -1)
    for position in rng:
        if values[position] is not None:
            return values[position]
    return None


def prior_work(series: Series, index: int, weight: float | None) -> dict[str, Any]:
    """Work done before sample ``index``: kJ (per kg), kJ and time above FTP, efforts above FTP."""
    kj = series.kj[index]
    above = series.kj_above[index]
    return {
        "elapsed_s": round(series.time[min(index, series.length - 1)] - series.time[0]),
        "kj": round(kj),
        "kj_per_kg": _rnd(kj / weight, 1) if weight else None,
        "kj_above_ftp": round(above, 1),
        "above_ftp_share_pct": _rnd(above / kj * 100, 1) if kj else None,
        "secs_above_ftp": round(series.secs_above[index]),
        "efforts_above_ftp": sum(1 for _, end in series.efforts if end <= index),
    }


def threshold_crossings(series: Series, thresholds_kj: list[float], weight: float | None) -> list[dict[str, Any]]:
    """Where each threshold is reached, with the prior work and stamina at that point."""
    rows = []
    for threshold in thresholds_kj:
        position = bisect.bisect_left(series.kj, threshold)
        index = position - 1 if 0 < position <= series.length else None
        row: dict[str, Any] = {"kj": threshold, "kj_per_kg": _rnd(threshold / weight, 1) if weight else None,
                               "reached": index is not None, "index": index}
        if index is not None:
            row.update(prior_work=prior_work(series, index, weight),
                       stamina=_value_at(series.stamina, index), potential_stamina=_value_at(series.potential, index))
        rows.append(row)
    return rows


def _phase_of(index: int, boundaries: list[int]) -> int:
    return sum(1 for boundary in boundaries if boundary <= index)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _segment_stats(series: Series, start: int, end: int) -> dict[str, Any] | None:
    """Metrics of one candidate segment (None when it fails the coasting or HR rules)."""
    watts = [series.watts[i] for i in range(start, end)]
    coasting = sum(1 for value in watts if value is None or value < COAST_W)
    if coasting > MAX_COAST_SHARE * len(watts):
        return None
    measured = next((i for i in range(start, end) if series.time[i] >= series.time[start] + HR_SETTLE_S), end)
    span = range(measured, end)
    if len(span) < 2:
        return None
    heart = [series.heartrate[i] for i in span]
    pairs = [(series.watts[i] or 0.0, hr) for i, hr in zip(span, heart, strict=True) if hr is not None and hr > 0]
    if len(pairs) < MIN_HR_COVERAGE * len(span):
        return None
    mean_w = sum(w for w, _ in pairs) / len(pairs)
    mean_hr = sum(h for _, h in pairs) / len(pairs)
    cadence = [c for i in span if (c := series.cadence[i]) is not None and c > 0]
    temps = [t for i in span if series.temp and (t := series.temp[i]) is not None]
    return {
        "start_index": start, "end_index": end,
        "start_s": round(series.time[start] - series.time[0]), "duration_s": round(series.time[end - 1] - series.time[start] + 1),
        "measured_s": round(series.time[end - 1] - series.time[measured] + 1),
        "segment_watts": _rnd(_mean([w or 0.0 for w in watts]), 1),
        "watts": _rnd(mean_w, 1), "hr": _rnd(mean_hr, 1), "w_per_bpm": _rnd(mean_w / mean_hr, 3) if mean_hr else None,
        "cadence": _rnd(_mean(cadence), 1), "temp_c": _rnd(_mean(temps), 1),
        "stamina_start": _value_at(series.stamina, start), "stamina_end": _value_at(series.stamina, end - 1, forward=False),
        "potential_start": _value_at(series.potential, start),
        "potential_end": _value_at(series.potential, end - 1, forward=False),
    }


def _runs(series: Series, low: float, high: float, cuts: set[int], first: int) -> list[tuple[int, int]]:
    """Index ranges in which the rolling power stays in [low, high] (cut at gaps and ``cuts``)."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(first, series.length + 1):
        inside = index < series.length and (value := series.rolling[index]) is not None and low <= value <= high
        gap = first < index < series.length and series.time[index] - series.time[index - 1] > MAX_SAMPLE_GAP_S
        breaks = index in cuts or gap
        if start is not None and (not inside or breaks):
            runs.append((start, index))
            start = None
        if inside and start is None:
            start = index
    return runs


def steady_segments(  # pylint: disable=too-many-arguments
    series: Series,
    band: tuple[float, float],
    boundaries: list[int],
    weight: float | None,
    *,
    min_secs: int = DEFAULT_MIN_SEGMENT_S,
    slack_pct: float = BAND_SLACK_PCT,
) -> list[dict[str, Any]]:
    """Steady segments of one power band (see the module docstring), each with its phase and prior work."""
    low, high = band[0] * (1 - slack_pct / 100), band[1] * (1 + slack_pct / 100)
    first = next((i for i in range(series.length) if series.time[i] - series.time[0] >= WARMUP_EXCLUDE_S), series.length)
    segments = []
    for start, end in _runs(series, low, high, set(boundaries), first):
        if series.time[end - 1] - series.time[start] + 1 < min_secs:
            continue
        stats = _segment_stats(series, start, end)
        if stats is None or not band[0] <= (stats["segment_watts"] or 0) < band[1]:
            continue
        stats["phase"] = _phase_of(start, boundaries)
        stats["prior_work"] = prior_work(series, start, weight)
        segments.append(stats)
    return segments


def phase_labels(thresholds_kj: list[float], unit_suffix: str = "kJ") -> list[str]:
    """'0-750 kJ', '750-1500 kJ', '>= 1500 kJ'."""
    edges = [0.0, *thresholds_kj]
    labels = [f"{edges[i]:g}-{edges[i + 1]:g} {unit_suffix}" for i in range(len(thresholds_kj))]
    labels.append(f">= {thresholds_kj[-1]:g} {unit_suffix}" if thresholds_kj else f"all {unit_suffix}")
    return labels


def _phase_summary(segments: list[dict[str, Any]]) -> dict[str, Any]:
    secs = sum(s["measured_s"] for s in segments)
    out: dict[str, Any] = {"segments": len(segments), "minutes": round(secs / 60, 1),
                           "small_sample": len(segments) < SMALL_SAMPLE_SEGMENTS or secs / 60 < SMALL_SAMPLE_MINUTES}
    if not segments or not secs:
        return {**out, "watts": None, "hr": None, "w_per_bpm": None, "cadence": None, "temp_c": None,
                "stamina_first": None, "stamina_last": None, "potential_first": None, "potential_last": None}

    def weighted(key: str) -> float | None:
        items = [(s[key], s["measured_s"]) for s in segments if s[key] is not None]
        total = sum(weight for _, weight in items)
        return sum(value * weight for value, weight in items) / total if total else None

    watts, heart = weighted("watts"), weighted("hr")
    return {
        **out, "watts": _rnd(watts, 1), "hr": _rnd(heart, 1), "w_per_bpm": _rnd(watts / heart, 3) if watts and heart else None,
        "cadence": _rnd(weighted("cadence"), 1), "temp_c": _rnd(weighted("temp_c"), 1),
        "stamina_first": segments[0]["stamina_start"], "stamina_last": segments[-1]["stamina_end"],
        "potential_first": segments[0]["potential_start"], "potential_last": segments[-1]["potential_end"],
    }


def _delta(later: float | None, earlier: float | None, digits: int = 1) -> float | None:
    return None if later is None or earlier is None else round(later - earlier, digits)


def phase_changes(phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Change of every later phase against the first phase (None when either side has no data)."""
    reference = phases[0]
    changes = []
    for index, phase in enumerate(phases[1:], start=1):
        cadence = _delta(phase["cadence"], reference["cadence"])
        wpb = None
        if phase["w_per_bpm"] is not None and reference["w_per_bpm"]:
            wpb = round((phase["w_per_bpm"] / reference["w_per_bpm"] - 1) * 100, 1)
        changes.append({
            "phase": index, "label": phase["label"],
            "comparable": phase["segments"] > 0 and reference["segments"] > 0,
            "small_sample": phase["small_sample"] or reference["small_sample"],
            "hr_bpm": _delta(phase["hr"], reference["hr"]), "watts_w": _delta(phase["watts"], reference["watts"]),
            "w_per_bpm_pct": wpb, "cadence_rpm": cadence,
            "cadence_shift": cadence is not None and abs(cadence) > CADENCE_SHIFT_RPM,
            "temp_c": _delta(phase["temp_c"], reference["temp_c"]),
        })
    return changes


def band_profile(
    segments: list[dict[str, Any]], labels: list[str]
) -> dict[str, Any]:
    """Phase summaries and changes of one band from its segments."""
    phases = []
    for index, label in enumerate(labels):
        summary = _phase_summary([s for s in segments if s["phase"] == index])
        phases.append({"phase": index, "label": label, **summary})
    return {"phases": phases, "changes": phase_changes(phases)}


def _stamina_of(segment: dict[str, Any], code: str | None, key: str) -> Any:
    stats = (segment.get("streams") or {}).get(code or "") or {}
    return stats.get(key)


def ride_climbs(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    streams: list[dict[str, Any]],
    series: Series,
    stamina: dict[str, str],
    weight: float | None,
    sport: Any,
    min_gain_m: float,
    after_s: float,
) -> tuple[list[dict[str, Any]], str | None]:
    """Climbs of at least ``min_gain_m`` with power, HR, W/bpm, cadence, stamina and the prior work."""
    result = detect_segments(streams, min_climb_gain_m=min_gain_m, sport=sport)
    if "error" in result:
        return [], str(result["error"])
    climbs = []
    for segment in result["segments"]:
        if segment["type"] != "climb":
            continue
        start = int(segment["start_index"])
        start_s = series.time[min(start, series.length - 1)] - series.time[0]
        watts, heart = _num(segment.get("avg_watts")), _num(segment.get("avg_hr"))
        climbs.append({
            "start_s": round(start_s), "late": start_s >= after_s, "duration_s": segment.get("duration_s"),
            "moving_s": segment.get("moving_s"), "gain_m": segment.get("elevation_gain_m"),
            "distance_m": segment.get("distance_m"), "avg_grade_pct": segment.get("avg_grade_pct"),
            "avg_watts": watts, "normalized_power": segment.get("normalized_power"), "avg_hr": heart,
            "w_per_bpm": _rnd(watts / heart, 3) if watts and heart else None,
            "cadence": segment.get("avg_cadence_nonzero"), "vam_m_per_h": segment.get("vam_m_per_h"),
            "stamina_start": _stamina_of(segment, stamina.get("stamina"), "first"),
            "stamina_end": _stamina_of(segment, stamina.get("stamina"), "last"),
            "potential_start": _stamina_of(segment, stamina.get("potential"), "first"),
            "potential_end": _stamina_of(segment, stamina.get("potential"), "last"),
            "prior_work": prior_work(series, start, weight),
            "grade_confidence": (segment.get("grade_confidence") or {}).get("level"),
        })
    return climbs, None


def ride_profile(  # pylint: disable=too-many-arguments,too-many-locals
    streams: list[dict[str, Any]],
    *,
    ftp: float | None,
    weight: float | None,
    thresholds_kj: list[float],
    bands: list[tuple[float, float]],
    stamina: dict[str, str] | None = None,
    sport: Any = None,
    min_segment_secs: int = DEFAULT_MIN_SEGMENT_S,
    min_climb_gain_m: float = 100.0,
    climb_after_s: float = 7200.0,
    with_climbs: bool = True,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Fatigue profile of one ride: work, threshold crossings, steady segments per band and phase, climbs.

    Returns ``{"error": ...}`` when time or power is missing (never raises).
    """
    codes = stamina or {}
    series = build_series(streams, codes)
    if isinstance(series, str):
        return {"error": series}
    accumulate_work(series, ftp)
    crossings = threshold_crossings(series, thresholds_kj, weight)
    boundaries = [row["index"] for row in crossings if row["index"] is not None]
    labels = labels or phase_labels(thresholds_kj)
    band_rows = []
    all_segments: list[dict[str, Any]] = []
    for band in bands:
        segments = steady_segments(series, band, boundaries, weight, min_secs=min_segment_secs)
        all_segments.extend({**s, "band": f"{band[0]:g}-{band[1]:g} W"} for s in segments)
        band_rows.append({"band": f"{band[0]:g}-{band[1]:g} W", **band_profile(segments, labels)})
    climbs: list[dict[str, Any]] = []
    climb_note = None
    if with_climbs:
        climbs, climb_note = ride_climbs(streams, series, codes, weight, sport, min_climb_gain_m, climb_after_s)
    total_kj = series.kj[-1]
    hr_present = sum(1 for value in series.heartrate if value is not None and value > 0)
    return {
        "samples": series.length,
        "elapsed_s": round(series.time[-1] - series.time[0]),
        "total_kj": round(total_kj),
        "total_kj_per_kg": _rnd(total_kj / weight, 1) if weight else None,
        "kj_above_ftp": round(series.kj_above[-1], 1) if ftp else None,
        "secs_above_ftp": round(series.secs_above[-1]) if ftp else None,
        "efforts_above_ftp": len(series.efforts) if ftp else None,
        "hr_coverage_pct": round(hr_present / series.length * 100, 1) if series.length else None,
        "stamina_streams": codes or None,
        "thresholds": [{k: v for k, v in row.items() if k != "index"} for row in crossings],
        "bands": band_rows,
        "segments": all_segments,
        "climbs": climbs,
        "climb_note": climb_note,
    }


def _describe(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median": None, "min": None, "max": None}
    return {"n": len(values), "median": round(statistics.median(values), 1), "min": round(min(values), 1),
            "max": round(max(values), 1)}


def _split_by_intensity(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Rides split at the median share of work above FTP before the threshold (needs 4 rides)."""
    rated = [e for e in entries if e["above_ftp_share_pct"] is not None]
    if len(rated) < 4:
        return None
    rated.sort(key=lambda e: e["above_ftp_share_pct"])
    half = len(rated) // 2
    groups = {"lower_share": rated[:half], "higher_share": rated[-half:]}
    return {
        name: {"rides": len(group), "above_ftp_share_pct": _describe([e["above_ftp_share_pct"] for e in group]),
               "hr_bpm": _describe([e["hr_bpm"] for e in group if e["hr_bpm"] is not None]),
               "w_per_bpm_pct": _describe([e["w_per_bpm_pct"] for e in group if e["w_per_bpm_pct"] is not None])}
        for name, group in groups.items()
    }


def across_rides(rides: list[dict[str, Any]], labels: list[str]) -> list[dict[str, Any]]:
    """Per band and later phase: the per-ride changes against the reference phase (median, range, n).

    Each ride counts once; a phase needs data in both the reference and the later phase.
    Fewer than 3 rides, or more than half of the rides with a small phase sample, make the row a
    small sample; rides whose cadence differs by more than 15 rpm between the phases (terrain,
    gearing) are counted. With at least 4 rides the rides are also split at
    the median share of work above FTP done before the threshold (context, not a predictor).
    """
    out = []
    band_names = sorted({band["band"] for ride in rides for band in ride["profile"].get("bands", [])})
    for band_name in band_names:
        for phase_index in range(1, len(labels)):
            entries = []
            for ride in rides:
                band = next((b for b in ride["profile"].get("bands", []) if b["band"] == band_name), None)
                change = next((c for c in (band or {}).get("changes", []) if c["phase"] == phase_index), None)
                if not change or not change["comparable"]:
                    continue
                crossing = ride["profile"]["thresholds"][phase_index - 1]
                entries.append({
                    "id": ride["id"], "hr_bpm": change["hr_bpm"], "w_per_bpm_pct": change["w_per_bpm_pct"],
                    "cadence_rpm": change["cadence_rpm"], "watts_w": change["watts_w"], "small_sample": change["small_sample"],
                    "cadence_shift": change["cadence_shift"],
                    "above_ftp_share_pct": ((crossing.get("prior_work") or {}).get("above_ftp_share_pct")),
                })
            small_phases = sum(1 for e in entries if e["small_sample"])
            out.append({
                "band": band_name, "phase": phase_index, "label": labels[phase_index], "rides": len(entries),
                "small_sample": len(entries) < MIN_ACTIVITIES or small_phases > len(entries) / 2,
                "rides_with_small_phase_samples": small_phases,
                "rides_with_cadence_shift": sum(1 for e in entries if e["cadence_shift"]),
                "hr_bpm": _describe([e["hr_bpm"] for e in entries if e["hr_bpm"] is not None]),
                "w_per_bpm_pct": _describe([e["w_per_bpm_pct"] for e in entries if e["w_per_bpm_pct"] is not None]),
                "cadence_rpm": _describe([e["cadence_rpm"] for e in entries if e["cadence_rpm"] is not None]),
                "watts_w": _describe([e["watts_w"] for e in entries if e["watts_w"] is not None]),
                "by_prior_intensity": _split_by_intensity(entries),
                "ride_ids": [e["id"] for e in entries],
            })
    return out


def default_band(ftp: float | None) -> tuple[float, float] | None:
    """75-85 % of FTP rounded outward to 5 W, None without FTP."""
    if not ftp or ftp <= 0:
        return None
    low = math.floor(ftp * DEFAULT_BAND_PCT[0] / 100 / 5) * 5
    high = math.ceil(ftp * DEFAULT_BAND_PCT[1] / 100 / 5) * 5
    return (float(low), float(high)) if 0 < low < high else None
