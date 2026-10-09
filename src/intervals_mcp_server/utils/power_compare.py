"""
Power meter comparison for Intervals.icu MCP Server.

A ride can carry two power streams: ``watts`` from the power meter paired as the
primary sensor of the head unit and ``secondary_power`` from a second meter (or a
custom stream). The two sensors measure at different points of the drive train and
have their own calibration, so the athlete wants to know how far apart they read,
whether the gap depends on the power level, whether it drifts during the ride and
whether one sensor lags behind the other, without changing any calibration or FTP.

The streams are sample-aligned (see ``streams``): index ``i`` of both streams
belongs to the same recorded sample at ``time[i]`` seconds since the start.
Recording pauses show up as jumps in ``time``, so every window-based statistic
walks the time stream and only uses windows that are contiguous in time (no gap
larger than ``MAX_SAMPLE_GAP_S`` between consecutive samples).

Conventions (also repeated in the ``notes`` of every result):

- Differences are ``secondary - primary``; percentages are relative to the primary.
  ``mean_diff_pct`` of a group of samples is the difference of the group means
  relative to the primary mean, i.e. ``(ratio - 1) * 100``; ``median_diff_pct`` and
  ``stdev_diff_pct`` are over the per-sample percentages.
- Samples where either stream is missing (null, NaN, "NaN") or below
  ``min_power_w`` (coasting, zeros, sensor drop to 0) are excluded. Paired samples
  whose absolute difference exceeds ``outlier_pct`` of the primary are counted as
  outliers and excluded from the statistics.
- Bins group the paired samples by the primary value, ``[lo, hi)``.
- Lag: a positive ``best_lag_s`` means the secondary lags behind the primary, i.e.
  the secondary at ``time + lag`` matches the primary at ``time``. The other
  statistics are computed at lag 0 (as recorded).
- No calibration, scaling or smoothing is applied to either stream.

Several rides (``ride_summary`` / ``summarize_rides``): each ride is compared on its own,
then the per-ride numbers are summarised per bike and power meter identity with n, mean,
median, the standard deviation between rides and the range, so the stability of the
relation between rides becomes visible. No correction factor is ever derived.
"""

import bisect
import math
import statistics
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from intervals_mcp_server.utils.custom_fields import is_missing

DEFAULT_BINS: tuple[tuple[int, int], ...] = (
    (50, 100),
    (100, 150),
    (150, 200),
    (200, 250),
    (250, 300),
    (300, 400),
)
DEFAULT_STABILITY_WINDOWS: tuple[int, ...] = (30, 60)
DEFAULT_BEST_EFFORT_SECS: tuple[int, ...] = (5, 30, 60, 300, 1200, 3600)

# Largest gap between consecutive samples (seconds) that still counts as contiguous.
MAX_SAMPLE_GAP_S = 2
# Minimum number of paired samples for a correlation to be reported.
MIN_LAG_SAMPLES = 60
# Minimum number of paired samples for a drift quarter mean to be reported.
MIN_QUARTER_SAMPLES = 30
# Fraction of the samples of a stability window that must be usable pairs.
WINDOW_MIN_COVERAGE = 0.9

Pair = tuple[float, float]


@dataclass
class _Prepared:
    """Cleaned streams plus the per-sample validity shared by all statistics."""

    primary: list[float | None]
    secondary: list[float | None]
    times: list[float]
    valid: list[bool]  # both numeric, >= min power and not an outlier
    paired_valid: int
    excluded: dict[str, int]


def _as_number(value: Any) -> float | None:
    """Float value of a sample; None for null, NaN, "NaN", booleans and non-numbers."""
    if is_missing(value) or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _clean(values: list[Any], length: int) -> list[float | None]:
    """Numeric samples of a stream, truncated or padded with None to ``length``."""
    cleaned = [_as_number(value) for value in values[:length]]
    cleaned.extend([None] * (length - len(cleaned)))
    return cleaned


def _clean_time(time: list[Any], length: int) -> tuple[list[float], str | None]:
    """Time stream as floats; falls back to the sample index when it is unusable."""
    times: list[float] = []
    for value in _clean(time, length):
        if value is None:
            break
        times.append(value)
    if len(times) == length and all(b >= a for a, b in zip(times, times[1:], strict=False)):
        return times, None
    note = "Time stream missing, incomplete or not monotonic: the sample index was used as time."
    return [float(index) for index in range(length)], note


def _classify(
    primary: list[float | None],
    secondary: list[float | None],
    min_power_w: float,
    outlier_pct: float,
) -> tuple[list[bool], int, dict[str, int]]:
    """Per-sample usability flags, number of paired samples and the exclusion counts."""
    valid = [False] * len(primary)
    excluded = {"coasting_or_zero": 0, "missing": 0, "outliers": 0}
    paired = 0
    for index, (first, second) in enumerate(zip(primary, secondary, strict=True)):
        if first is None or second is None:
            excluded["missing"] += 1
        elif first < min_power_w or second < min_power_w:
            excluded["coasting_or_zero"] += 1
        else:
            paired += 1
            if abs(second - first) > outlier_pct / 100.0 * first:
                excluded["outliers"] += 1
            else:
                valid[index] = True
    return valid, paired, excluded


def _prepare(
    primary: list[Any],
    secondary: list[Any],
    time: list[Any],
    min_power_w: float,
    outlier_pct: float,
) -> tuple[_Prepared, list[str]]:
    """Clean the streams and classify every sample (missing, coasting, outlier, valid)."""
    length = max(len(primary), len(secondary))
    primary_values = _clean(primary, length)
    secondary_values = _clean(secondary, length)
    times, time_note = _clean_time(time, length)
    valid, paired, excluded = _classify(primary_values, secondary_values, min_power_w, outlier_pct)
    prepared = _Prepared(primary_values, secondary_values, times, valid, paired, excluded)
    return prepared, [time_note] if time_note else []


def _valid_pairs(prepared: _Prepared, start: int, end: int) -> list[Pair]:
    """(primary, secondary) of the usable samples in an index range (end exclusive)."""
    pairs: list[Pair] = []
    for index in range(start, end):
        first, second = prepared.primary[index], prepared.secondary[index]
        if prepared.valid[index] and first is not None and second is not None:
            pairs.append((first, second))
    return pairs


def _pct(diff: float, base: float) -> float | None:
    """``diff`` as a percentage of ``base`` (None when the base is 0)."""
    return diff / base * 100.0 if base else None


def _pair_stats(pairs: list[Pair]) -> dict[str, Any]:
    """Means of both sides, mean difference and its percentage of a group of pairs."""
    count = len(pairs)
    if count == 0:
        return {
            "n": 0,
            "mean_primary_w": None,
            "mean_secondary_w": None,
            "mean_diff_w": None,
            "mean_diff_pct": None,
        }
    mean_primary = sum(first for first, _ in pairs) / count
    mean_secondary = sum(second for _, second in pairs) / count
    diff = mean_secondary - mean_primary
    return {
        "n": count,
        "mean_primary_w": mean_primary,
        "mean_secondary_w": mean_secondary,
        "mean_diff_w": diff,
        "mean_diff_pct": _pct(diff, mean_primary),
    }


def _overall(pairs: list[Pair]) -> dict[str, Any]:
    """Overall statistics over the usable pairs."""
    stats = _pair_stats(pairs)
    pcts = [
        pct for pct in (_pct(second - first, first) for first, second in pairs) if pct is not None
    ]
    ratio = None
    if stats["n"] and stats["mean_primary_w"]:
        ratio = stats["mean_secondary_w"] / stats["mean_primary_w"]
    stats.update(
        median_diff_pct=statistics.median(pcts) if pcts else None,
        ratio_secondary_to_primary=ratio,
        stdev_diff_pct=statistics.stdev(pcts) if len(pcts) > 1 else None,
    )
    return stats


def _bins(pairs: list[Pair], bins: tuple[tuple[int, int], ...]) -> list[dict[str, Any]]:
    """Pair statistics per primary power bin ``[lo, hi)``; empty bins are left out."""
    out: list[dict[str, Any]] = []
    for low, high in bins:
        members = [(first, second) for first, second in pairs if low <= first < high]
        if members:
            out.append({"range_w": [low, high], **_pair_stats(members)})
    return out


def _first_gap(times: list[float], start: int, end: int) -> int | None:
    """Index of the first sample in ``(start, end)`` that follows a time gap, if any."""
    for index in range(start + 1, end):
        if times[index] - times[index - 1] > MAX_SAMPLE_GAP_S:
            return index
    return None


def _contiguous_windows(times: list[float], width: int) -> Iterator[tuple[int, int]]:
    """Index ranges (end exclusive) of consecutive, non-overlapping ``width`` second windows.

    Each window starts at the first sample after the previous one. A window that
    contains a time gap is skipped and the next window starts right after the gap;
    a window cut short by the end of the ride is skipped as well.
    """
    start = 0
    count = len(times)
    while start < count:
        end = bisect.bisect_left(times, times[start] + width, start)
        gap = _first_gap(times, start, end)
        if gap is not None:
            start = gap
            continue
        if times[end - 1] - times[start] >= width - MAX_SAMPLE_GAP_S:
            yield start, end
        start = end


def _stable_windows(prepared: _Prepared, width: int, cv_limit_pct: float) -> dict[str, Any]:
    """Compare the window means of the steady-state windows of ``width`` seconds."""
    diffs: list[float] = []
    means: list[float] = []
    for start, end in _contiguous_windows(prepared.times, width):
        pairs = _valid_pairs(prepared, start, end)
        if len(pairs) < 2 or len(pairs) < WINDOW_MIN_COVERAGE * (end - start):
            continue
        stats = _pair_stats(pairs)
        spread = statistics.pstdev([first for first, _ in pairs])
        if spread / stats["mean_primary_w"] * 100.0 > cv_limit_pct:
            continue
        diffs.append(stats["mean_diff_pct"])
        means.append(stats["mean_primary_w"])
    return {
        "windows": len(diffs),
        "mean_diff_pct": statistics.fmean(diffs) if diffs else None,
        "median_diff_pct": statistics.median(diffs) if diffs else None,
        "mean_primary_w": statistics.fmean(means) if means else None,
    }


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation, None with too few samples or a constant series."""
    if len(xs) < MIN_LAG_SAMPLES:
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


def _lag(prepared: _Prepared, max_lag_s: int) -> dict[str, Any]:
    """Lag of the secondary relative to the primary that maximises their correlation.

    For lag ``L`` the primary at ``t`` is paired with the secondary at ``t + L``,
    so a positive lag means the secondary lags behind. Ties go to the smaller lag.
    """
    index_at = {moment: index for index, moment in enumerate(prepared.times)}
    correlations: dict[int, float] = {}
    for lag in range(-max_lag_s, max_lag_s + 1):
        xs: list[float] = []
        ys: list[float] = []
        for index, moment in enumerate(prepared.times):
            other = index_at.get(moment + lag)
            if other is None or not (prepared.valid[index] and prepared.valid[other]):
                continue
            first, second = prepared.primary[index], prepared.secondary[other]
            if first is not None and second is not None:
                xs.append(first)
                ys.append(second)
        correlation = _pearson(xs, ys)
        if correlation is not None:
            correlations[lag] = correlation
    if not correlations:
        return {"best_lag_s": None, "correlation_at_best": None, "correlation_at_zero": None}
    best = max(correlations, key=lambda lag: (correlations[lag], -abs(lag)))
    return {
        "best_lag_s": best,
        "correlation_at_best": correlations[best],
        "correlation_at_zero": correlations.get(0),
    }


def _drift(prepared: _Prepared) -> list[dict[str, Any]]:
    """Mean difference per quarter of the elapsed time, to make a changing offset visible."""
    times = prepared.times
    if not times:
        return [
            {"quarter": quarter, "from_s": None, "to_s": None, "n": 0, "mean_diff_pct": None}
            for quarter in range(1, 5)
        ]
    span = times[-1] - times[0]
    quarters: list[dict[str, Any]] = []
    for quarter in range(4):
        low = times[0] + span * quarter / 4
        high = times[0] + span * (quarter + 1) / 4
        start = bisect.bisect_left(times, low)
        end = len(times) if quarter == 3 else bisect.bisect_left(times, high)
        pairs = _valid_pairs(prepared, start, end)
        mean_pct = (
            _pair_stats(pairs)["mean_diff_pct"] if len(pairs) >= MIN_QUARTER_SAMPLES else None
        )
        quarters.append(
            {
                "quarter": quarter + 1,
                "from_s": round(low),
                "to_s": round(high),
                "n": len(pairs),
                "mean_diff_pct": mean_pct,
            }
        )
    return quarters


def _best_rolling_mean(
    values: list[float | None], times: list[float], secs: int
) -> tuple[float, int, int] | None:
    """Highest mean over a contiguous window of ``secs`` seconds: ``(mean, start, end)``.

    A window is invalid when it contains a missing sample or a time gap, or when it
    is cut short by the end of the ride. Prefix sums keep this linear in the ride.
    """
    sums = [0.0]
    missing = [0]
    gaps = [0]
    for index, value in enumerate(values):
        sums.append(sums[-1] + (value or 0.0))
        missing.append(missing[-1] + (1 if value is None else 0))
        gapped = index > 0 and times[index] - times[index - 1] > MAX_SAMPLE_GAP_S
        gaps.append(gaps[-1] + (1 if gapped else 0))
    best_mean = -math.inf
    best_range: tuple[int, int] | None = None
    for start in range(len(values)):
        end = bisect.bisect_left(times, times[start] + secs, start)
        if times[end - 1] - times[start] < secs - MAX_SAMPLE_GAP_S:
            continue
        if missing[end] - missing[start] or gaps[end] - gaps[start + 1]:
            continue
        mean = (sums[end] - sums[start]) / (end - start)
        if mean > best_mean:
            best_mean = mean
            best_range = (start, end)
    if best_range is None:
        return None
    return best_mean, best_range[0], best_range[1]


def _window_mean(values: list[float | None], start: int, end: int) -> float | None:
    """Mean of an index range, None when it contains a missing sample."""
    window = values[start:end]
    if not window or any(value is None for value in window):
        return None
    return sum(value for value in window if value is not None) / len(window)


def _best_effort(prepared: _Prepared, secs: int) -> dict[str, Any]:
    """Best rolling mean of both streams over ``secs`` seconds, found independently."""
    entry: dict[str, Any] = {
        "secs": secs,
        "primary_w": None,
        "secondary_w": None,
        "diff_w": None,
        "diff_pct": None,
        "same_window_diff_pct": None,
    }
    best_secondary = _best_rolling_mean(prepared.secondary, prepared.times, secs)
    if best_secondary is not None:
        entry["secondary_w"] = best_secondary[0]
    best_primary = _best_rolling_mean(prepared.primary, prepared.times, secs)
    if best_primary is not None:
        primary_w, start, end = best_primary
        entry["primary_w"] = primary_w
        same = _window_mean(prepared.secondary, start, end)
        if same is not None:
            entry["same_window_diff_pct"] = _pct(same - primary_w, primary_w)
        if entry["secondary_w"] is not None:
            entry["diff_w"] = entry["secondary_w"] - primary_w
            entry["diff_pct"] = _pct(entry["diff_w"], primary_w)
    return entry


def _best_efforts(prepared: _Prepared, secs_list: tuple[int, ...]) -> list[dict[str, Any]]:
    """One best-effort entry per duration."""
    return [_best_effort(prepared, secs) for secs in secs_list]


def _notes(
    prepared: _Prepared,
    used: int,
    min_power_w: float,
    outlier_pct: float,
    stability_windows: tuple[int, ...],
    stability_cv_pct: float,
) -> list[str]:
    """Human readable record of the exclusions and conventions."""
    excluded = prepared.excluded
    windows = ", ".join(f"{width} s" for width in stability_windows) or "none"
    return [
        f"Used {used} of {len(prepared.valid)} samples. Excluded: {excluded['missing']} with a "
        f"missing value (null/NaN) on either side, {excluded['coasting_or_zero']} below "
        f"{min_power_w:g} W on either side (coasting/zero), {excluded['outliers']} outliers "
        f"(|secondary - primary| > {outlier_pct:g}% of primary).",
        "Differences are secondary - primary and percentages are relative to the primary; "
        "mean_diff_pct = difference of the means / primary mean, median and stdev are over "
        "the per-sample percentages.",
        "Bins group the paired samples by the primary value [lo, hi).",
        f"Stable windows: non-overlapping {windows} windows without time gaps > "
        f"{MAX_SAMPLE_GAP_S} s whose primary coefficient of variation is <= "
        f"{stability_cv_pct:g}%; their window means are compared.",
        "Lag: a positive best_lag_s means the secondary lags behind the primary (secondary at "
        "t + lag matches primary at t); all other statistics are computed as recorded (lag 0).",
        "Best efforts: highest rolling mean of each stream over contiguous windows, found "
        "independently (the windows may differ); same_window_diff_pct uses the primary's window.",
        "No calibration, scaling or smoothing was applied; both streams are compared as recorded.",
    ]


def compare_power_streams(  # pylint: disable=too-many-arguments
    primary: list[Any],
    secondary: list[Any],
    time: list[Any],
    *,
    bins: tuple[tuple[int, int], ...] = DEFAULT_BINS,
    stability_windows: tuple[int, ...] = DEFAULT_STABILITY_WINDOWS,
    stability_cv_pct: float = 10.0,
    max_lag_s: int = 5,
    min_power_w: float = 10.0,
    outlier_pct: float = 50.0,
    best_effort_secs: tuple[int, ...] = DEFAULT_BEST_EFFORT_SECS,
) -> dict[str, Any]:
    """Compare two sample-aligned power streams of one activity.

    ``primary`` and ``secondary`` are power samples in watts (entries may be None,
    NaN or "NaN"), ``time`` the matching seconds since the start (not contiguous:
    recording pauses appear as jumps). Returns a dict with the keys ``samples``,
    ``paired_valid``, ``excluded``, ``overall``, ``bins``, ``stable_windows`` (keyed
    by window length in seconds), ``lag``, ``drift``, ``best_efforts`` and ``notes``;
    see the module docstring for the conventions. Statistics are None when there is
    nothing to compute, no exception is raised for empty or all-missing input.
    """
    prepared, notes = _prepare(primary, secondary, time, min_power_w, outlier_pct)
    pairs = _valid_pairs(prepared, 0, len(prepared.valid))
    lag = _lag(prepared, max_lag_s)
    notes.extend(
        _notes(prepared, len(pairs), min_power_w, outlier_pct, stability_windows, stability_cv_pct)
    )
    if lag["best_lag_s"]:
        notes.append(
            f"The secondary is offset by {lag['best_lag_s']} s from the primary; this widens the "
            "per-sample spread and skews the bins, the stable windows and best efforts are robust."
        )
    return {
        "samples": len(prepared.valid),
        "paired_valid": prepared.paired_valid,
        "excluded": dict(prepared.excluded),
        "overall": _overall(pairs),
        "bins": _bins(pairs, bins),
        "stable_windows": {
            width: _stable_windows(prepared, width, stability_cv_pct) for width in stability_windows
        },
        "lag": lag,
        "drift": _drift(prepared),
        "best_efforts": _best_efforts(prepared, best_effort_secs),
        "notes": notes,
    }


def _fmt(value: Any, digits: int = 1) -> str:
    """Number rounded to ``digits`` decimals (integers as they are), "n/a" for None/NaN."""
    if value is None:
        return "n/a"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, int):
        return str(value)
    return "n/a" if math.isnan(value) else f"{value:.{digits}f}"


def _format_overview(result: dict[str, Any], primary_label: str, secondary_label: str) -> list[str]:
    """Header, sample counts and overall statistics."""
    excluded = result["excluded"]
    overall = result["overall"]
    return [
        f"Power comparison: {secondary_label} vs {primary_label} "
        f"(diff = {secondary_label} - {primary_label}, % of {primary_label})",
        f"Samples: {result['samples']}, paired valid: {result['paired_valid']}, used: "
        f"{overall['n']}; excluded: missing {excluded['missing']}, coasting/zero "
        f"{excluded['coasting_or_zero']}, outliers {excluded['outliers']}",
        f"Overall: {primary_label} {_fmt(overall['mean_primary_w'])} W, {secondary_label} "
        f"{_fmt(overall['mean_secondary_w'])} W, diff {_fmt(overall['mean_diff_w'])} W "
        f"({_fmt(overall['mean_diff_pct'])}%), median {_fmt(overall['median_diff_pct'])}%, "
        f"ratio {_fmt(overall['ratio_secondary_to_primary'], 3)}, "
        f"stdev {_fmt(overall['stdev_diff_pct'])}%",
    ]


def _format_bins(result: dict[str, Any]) -> list[str]:
    """One line per non-empty primary power bin."""
    lines = ["Bins by primary power:"]
    for entry in result["bins"]:
        low, high = entry["range_w"]
        lines.append(
            f"  {low}-{high} W: n {entry['n']}, {_fmt(entry['mean_primary_w'])} vs "
            f"{_fmt(entry['mean_secondary_w'])} W, diff {_fmt(entry['mean_diff_w'])} W "
            f"({_fmt(entry['mean_diff_pct'])}%)"
        )
    if len(lines) == 1:
        lines.append("  (no paired samples in any bin)")
    return lines


def _format_windows_and_lag(result: dict[str, Any]) -> list[str]:
    """Stable window and lag sections."""
    lines = ["Stable windows (primary CV within limit):"]
    for width, entry in result["stable_windows"].items():
        lines.append(
            f"  {width} s: {entry['windows']} windows, mean diff {_fmt(entry['mean_diff_pct'])}%, "
            f"median {_fmt(entry['median_diff_pct'])}%, mean primary "
            f"{_fmt(entry['mean_primary_w'])} W"
        )
    lag = result["lag"]
    lines.append(
        f"Lag: best {_fmt(lag['best_lag_s'])} s (r {_fmt(lag['correlation_at_best'], 3)}), "
        f"r at 0 s {_fmt(lag['correlation_at_zero'], 3)}; positive = secondary lags behind"
    )
    return lines


def _format_drift_and_efforts(result: dict[str, Any]) -> list[str]:
    """Drift quarters and best efforts."""
    lines = ["Drift by quarter of elapsed time:"]
    for entry in result["drift"]:
        lines.append(
            f"  Q{entry['quarter']} {_fmt(entry['from_s'])}-{_fmt(entry['to_s'])} s: "
            f"n {entry['n']}, diff {_fmt(entry['mean_diff_pct'])}%"
        )
    lines.append("Best efforts (max rolling mean per stream, windows found independently):")
    for entry in result["best_efforts"]:
        lines.append(
            f"  {entry['secs']} s: {_fmt(entry['primary_w'])} vs {_fmt(entry['secondary_w'])} W, "
            f"diff {_fmt(entry['diff_w'])} W ({_fmt(entry['diff_pct'])}%), same window "
            f"{_fmt(entry['same_window_diff_pct'])}%"
        )
    return lines


def format_power_comparison(
    result: dict[str, Any], primary_label: str = "watts", secondary_label: str = "secondary_power"
) -> str:
    """Compact text report of a ``compare_power_streams`` result.

    One line per bin, window length, quarter and effort; values are rounded to 1
    decimal (ratio and correlations to 3), None is rendered as "n/a".
    """
    lines = _format_overview(result, primary_label, secondary_label)
    lines.extend(_format_bins(result))
    lines.extend(_format_windows_and_lag(result))
    lines.extend(_format_drift_and_efforts(result))
    lines.append("Notes:")
    lines.extend(f"  - {note}" for note in result["notes"])
    return "\n".join(lines)


def _json_key(key: Any) -> str:
    """Dict key as a string; tuple keys are joined with '-'."""
    if isinstance(key, tuple):
        return "-".join(str(part) for part in key)
    return str(key)


def _json_value(value: Any) -> Any:
    """Recursive copy with string keys, lists for tuples and None for NaN/inf."""
    if isinstance(value, dict):
        return {_json_key(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def comparison_to_json(result: dict[Any, Any]) -> dict[str, Any]:
    """JSON-serialisable copy of a comparison result (NaN -> None, non-string keys -> strings)."""
    out: dict[str, Any] = _json_value(result)
    return out


# ------------------------------------------------------------- several rides
# A ride enters the between-ride statistics with at least this many usable pairs (10 min at 1 Hz).
MIN_RIDE_PAIRS = 600
# A power bin of a ride counts with at least this many usable pairs.
MIN_BIN_PAIRS = 60
# Fewer rides than this in a group are flagged as a small sample.
MIN_RIDES = 3
# A ride whose mean difference lies further than max(this, 3 x MAD) from the group median is listed.
FAR_FROM_MEDIAN_PP = 3.0


def _spread(values: list[float]) -> dict[str, Any]:
    """n, mean, median, standard deviation and range of per-ride values (None when empty)."""
    clean = [value for value in values if value is not None and math.isfinite(value)]
    if not clean:
        return {"n": 0, "mean": None, "median": None, "sd": None, "min": None, "max": None}
    return {
        "n": len(clean),
        "mean": round(statistics.fmean(clean), 2),
        "median": round(statistics.median(clean), 2),
        "sd": round(statistics.stdev(clean), 2) if len(clean) > 1 else None,
        "min": round(min(clean), 2),
        "max": round(max(clean), 2),
    }


def ride_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Key numbers of one ``compare_power_streams`` result for the comparison between rides."""
    overall = result["overall"]
    drift = [entry["mean_diff_pct"] for entry in result["drift"]]
    first = next((value for value in drift if value is not None), None)
    last = next((value for value in reversed(drift) if value is not None), None)
    windows = result["stable_windows"] or {}
    stable: dict[str, Any] = windows.get(60) or windows.get("60") or next(iter(windows.values()), {})
    paired = result["paired_valid"]
    return {
        "samples": result["samples"],
        "used": overall["n"],
        "mean_primary_w": overall["mean_primary_w"],
        "mean_diff_pct": overall["mean_diff_pct"],
        "median_diff_pct": overall["median_diff_pct"],
        "stdev_diff_pct": overall["stdev_diff_pct"],
        "outlier_pct": result["excluded"]["outliers"] / paired * 100 if paired else None,
        "best_lag_s": result["lag"]["best_lag_s"],
        "correlation_at_best": result["lag"]["correlation_at_best"],
        "drift_pct": drift,
        "drift_last_minus_first_pp": last - first if first is not None and last is not None else None,
        "stable_windows": (stable or {}).get("windows"),
        "stable_diff_pct": (stable or {}).get("mean_diff_pct"),
        "bins": {
            f"{entry['range_w'][0]}-{entry['range_w'][1]}": {"n": entry["n"], "mean_diff_pct": entry["mean_diff_pct"]}
            for entry in result["bins"]
        },
    }


def _far_from_median(rides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rides whose mean difference lies more than max(3 pp, 3 x MAD) from the group median (needs 3 rides)."""
    values = [(ride["id"], ride["summary"]["mean_diff_pct"]) for ride in rides if ride["summary"]["mean_diff_pct"] is not None]
    if len(values) < MIN_RIDES:
        return []
    median = statistics.median(value for _, value in values)
    mad = statistics.median(abs(value - median) for _, value in values)
    limit = max(FAR_FROM_MEDIAN_PP, 3 * mad)
    return [
        {"id": ride_id, "diff_pct": round(value, 2), "deviation_pp": round(value - median, 2)}
        for ride_id, value in values if abs(value - median) > limit
    ]


def _group_summary(label: str, rides: list[dict[str, Any]], bins: tuple[tuple[int, int], ...]) -> dict[str, Any]:
    summaries = [ride["summary"] for ride in rides]
    lags = [s["best_lag_s"] for s in summaries if s["best_lag_s"] is not None]
    bin_rows = []
    for low, high in bins:
        key = f"{low}-{high}"
        members = [s["bins"][key] for s in summaries if key in s["bins"] and s["bins"][key]["n"] >= MIN_BIN_PAIRS]
        if members:
            bin_rows.append({
                "range_w": [low, high], "rides": len(members), "samples": sum(m["n"] for m in members),
                "small_sample": len(members) < MIN_RIDES, "diff_pct": _spread([m["mean_diff_pct"] for m in members]),
            })
    return {
        "group": label,
        "rides": len(rides),
        "ride_ids": [ride["id"] for ride in rides],
        "small_sample": len(rides) < MIN_RIDES,
        "samples_used": sum(s["used"] for s in summaries),
        "overall_diff_pct": _spread([s["mean_diff_pct"] for s in summaries]),
        "within_ride_sd_pct": _spread([s["stdev_diff_pct"] for s in summaries]),
        "stable_diff_pct": _spread([s["stable_diff_pct"] for s in summaries]),
        "drift_last_minus_first_pp": _spread([s["drift_last_minus_first_pp"] for s in summaries]),
        "outlier_pct": _spread([s["outlier_pct"] for s in summaries]),
        "lag_s": {"counts": {str(lag): lags.count(lag) for lag in sorted(set(lags))},
                  "median": statistics.median(lags) if lags else None},
        "bins": bin_rows,
        "far_from_median": _far_from_median(rides),
    }


def summarize_rides(
    rides: list[dict[str, Any]], bins: tuple[tuple[int, int], ...] = DEFAULT_BINS
) -> dict[str, Any]:
    """Between-ride statistics of several dual power rides, per group (bike / power meters).

    ``rides`` are ``{"id", "date", "group", "summary"}`` with ``summary`` from
    ``ride_summary``. Rides with fewer than ``MIN_RIDE_PAIRS`` usable pairs are listed as
    excluded. Each ride counts once: per group the per-ride mean differences (overall, per
    power bin with at least ``MIN_BIN_PAIRS`` pairs, stable windows), the drift between the
    first and last quarter, the outlier share and the lag are summarised with n, mean,
    median, standard deviation (between rides) and range. No correction factor is derived.
    """
    excluded = [
        {"id": ride["id"], "reason": f"only {ride['summary']['used']} usable pairs (minimum {MIN_RIDE_PAIRS})"}
        for ride in rides if ride["summary"]["used"] < MIN_RIDE_PAIRS
    ]
    included = [ride for ride in rides if ride["summary"]["used"] >= MIN_RIDE_PAIRS]
    groups: dict[str, list[dict[str, Any]]] = {}
    for ride in included:
        groups.setdefault(ride["group"], []).append(ride)
    notes = [
        "Each ride counts once; sd is the standard deviation between rides of the per-ride mean difference "
        "(secondary - primary in % of primary), within_ride_sd_pct the per-sample spread inside the rides.",
        "Drift = last minus first quarter of the ride (percentage points); lag counts rides per best lag "
        "(positive: secondary lags behind); rides further than max(3 pp, 3 x MAD) from the group median are listed.",
        "No correction or calibration factor is derived or applied; the numbers describe the recorded streams.",
    ]
    if len(groups) > 1:
        notes.append("Rides are grouped by bike and power meter identity; groups are not pooled.")
    return {
        "groups": [_group_summary(label, members, bins) for label, members in groups.items()],
        "excluded": excluded,
        "notes": notes,
    }


def _spread_text(spread: dict[str, Any], unit: str = "%") -> str:
    if not spread["n"]:
        return "n/a"
    text = f"median {_fmt(spread['median'], 2)}{unit} (mean {_fmt(spread['mean'], 2)}{unit}"
    if spread["sd"] is not None:
        text += f", sd {_fmt(spread['sd'], 2)}{unit}"
    return text + f", range {_fmt(spread['min'], 2)}..{_fmt(spread['max'], 2)}{unit}, n {spread['n']})"


def _rides(count: int) -> str:
    return f"{count} ride{'' if count == 1 else 's'}"


def format_rides_summary(summary: dict[str, Any]) -> list[str]:
    """Text lines of ``summarize_rides`` (one block per group)."""
    lines: list[str] = []
    for group in summary["groups"]:
        flag = " - small sample, not reliable" if group["small_sample"] else ""
        lines.append(f"Group {group['group']}: {_rides(group['rides'])}, {group['samples_used']} usable pairs{flag}")
        lines.append(f"  Overall difference per ride: {_spread_text(group['overall_diff_pct'])}")
        lines.append(f"  Stable windows per ride: {_spread_text(group['stable_diff_pct'])}")
        lines.append(f"  Per-sample spread inside the rides (sd): {_spread_text(group['within_ride_sd_pct'])}")
        lines.append(f"  Drift last - first quarter: {_spread_text(group['drift_last_minus_first_pp'], ' pp')}")
        lines.append(f"  Outliers: {_spread_text(group['outlier_pct'])}")
        counts = ", ".join(f"{lag} s: {count}" for lag, count in group["lag_s"]["counts"].items()) or "n/a"
        lines.append(f"  Best lag (rides): {counts}")
        for ride in group["far_from_median"]:
            lines.append(
                f"  Ride {ride['id']} lies {ride['deviation_pp']:+.2f} pp from the group median "
                f"(diff {ride['diff_pct']:.2f}%)"
            )
        for entry in group["bins"]:
            low, high = entry["range_w"]
            lines.append(
                f"  {low}-{high} W: {_rides(entry['rides'])}, {entry['samples']} pairs, diff {_spread_text(entry['diff_pct'])}"
                + (" - small sample" if entry["small_sample"] else "")
            )
    for item in summary["excluded"]:
        lines.append(f"Excluded {item['id']}: {item['reason']}")
    return lines
