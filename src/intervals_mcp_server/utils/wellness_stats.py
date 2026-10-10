"""
Wellness statistics helpers for Intervals.icu MCP Server.

Wellness entries are one dict per calendar day whose ``id`` is the ISO date. Any
value can be null, float NaN or the string "NaN" (all 'no value', see
``is_missing``), whole days can be absent from the list, the list is not
necessarily sorted and the aggregates of the current day (steps, calories ...)
may still be incomplete.

This module computes descriptive statistics, trailing means, baselines, outliers,
lagged correlations, weight slopes and nutrition balances over such entries and
renders the results as compact text. Nothing is interpolated: a missing day stays
missing, every rolling, baseline or window statistic uses the available values
only and reports how many values it is based on, and a day without logged intake
is never counted as 0 kcal.
"""

import math
import statistics
from datetime import date, datetime, timedelta
from typing import Any, Literal

from intervals_mcp_server.utils.custom_fields import format_value, is_missing

CAUSATION_NOTE = "statistical association only, not causation"

NUTRITION_NOTES: tuple[str, ...] = (
    "Days without logged intake (kcalConsumed missing or 0) are excluded from every "
    "intake and balance statistic and counted in days_missing_intake; an unlogged day "
    "is never treated as 0 kcal.",
    "balance_kcal is the device balance field when present, otherwise kcal_consumed minus "
    "the burn field, and only on logged days.",
    "The burn field is a device estimate of daily energy expenditure, not a measurement.",
    "The most recent day may still be incomplete (device totals are updated during the day).",
)

# Days printed in the per-day table of the metric trend / nutrition formatters.
SERIES_TAIL_DAYS = 14
NUTRITION_TAIL_DAYS = 7
# Minimum number of values before z-score outliers are reported.
MIN_VALUES_FOR_OUTLIERS = 7
# Baselines with fewer values and correlations with fewer pairs are flagged as small samples.
MIN_BASELINE_VALUES = 14
MIN_RELIABLE_PAIRS = 30
# Native metrics for which a stored 0 is physiologically impossible and means "no value".
ZERO_MEANS_MISSING = frozenset({
    "hrv", "hrvSDNN", "restingHR", "avgSleepingHR", "respiration", "spO2", "weight", "bodyFat",
    "sleepSecs", "sleepScore", "readiness", "vo2max",
})
# Display units of native wellness metrics (custom fields carry their own units).
NATIVE_UNITS: dict[str, str] = {
    "hrv": "ms", "hrvSDNN": "ms", "restingHR": "bpm", "avgSleepingHR": "bpm", "respiration": "breaths/min",
    "spO2": "%", "readiness": "/100", "sleepScore": "/100", "weight": "kg", "bodyFat": "%", "sleepSecs": "s",
    "steps": "steps", "kcalConsumed": "kcal", "vo2max": "ml/kg/min", "systolic": "mmHg", "diastolic": "mmHg",
    "bloodGlucose": "mmol/L", "lactate": "mmol/L", "hydrationVolume": "l", "carbohydrates": "g", "protein": "g",
    "fatTotal": "g", "abdomen": "cm", "baevskySI": "", "ctl": "", "atl": "", "rampRate": "",
}


def metric_units(metric: str, definition_units: str | None = None) -> str | None:
    """Display units of a wellness metric: custom definition units, native map, eFTP in W."""
    if definition_units:
        return definition_units
    if metric.startswith(("eftp_", "pMax_")):
        return "W"
    if metric.startswith("wPrime_"):
        return "J"
    return NATIVE_UNITS.get(metric) or None

DateValue = tuple[date, float | None]


# ---------------------------------------------------------------------------
# Entry access
# ---------------------------------------------------------------------------


def _entry_date(entry: Any) -> date | None:
    """Calendar day of a wellness entry (its ``id``), None when unparsable."""
    if not isinstance(entry, dict):
        return None
    raw = entry.get("id")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    """Numeric value of a wellness field; None for null, NaN, booleans and non-numbers."""
    if is_missing(value) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _dated_entries(entries: list[dict[str, Any]]) -> list[tuple[date, dict[str, Any]]]:
    """(day, entry) pairs for the entries with a parsable id, oldest first (stable)."""
    dated: list[tuple[date, dict[str, Any]]] = []
    for entry in entries:
        day = _entry_date(entry)
        if day is not None:
            dated.append((day, entry))
    dated.sort(key=lambda item: item[0])
    return dated


def _entries_by_date(entries: list[dict[str, Any]]) -> dict[date, dict[str, Any]]:
    """Map calendar day -> entry; for duplicate days the last entry in input order wins."""
    return dict(_dated_entries(entries))


def _calendar(by_date: dict[date, Any]) -> list[date]:
    """Every calendar day from the first to the last day present (inclusive)."""
    if not by_date:
        return []
    first, last = min(by_date), max(by_date)
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def sort_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Entries sorted by their ``id`` date ascending; entries without a parsable id are dropped."""
    return [entry for _, entry in _dated_entries(entries)]


def metric_value(entry: dict[str, Any] | None, metric: str) -> float | None:
    """Numeric value of a wellness metric on one entry; for metrics in ``ZERO_MEANS_MISSING``
    a stored 0 is a placeholder ("no value") and returns None."""
    value = _number((entry or {}).get(metric))
    return None if value == 0 and metric in ZERO_MEANS_MISSING else value


def _zero_cleaned(series: list[DateValue], metric: str) -> tuple[list[DateValue], int]:
    """The series with stored 0 of a ``ZERO_MEANS_MISSING`` metric as None, and how many there were."""
    if metric not in ZERO_MEANS_MISSING:
        return series, 0
    zeros = sum(1 for _, value in series if value == 0)
    return [(day, None if value == 0 else value) for day, value in series], zeros


def metric_series(entries: list[dict[str, Any]], metric: str) -> list[DateValue]:
    """One (day, value) tuple per calendar day from the first to the last entry date.

    Days absent from the list and null/NaN/non-numeric values are reported as None.
    """
    by_date = _entries_by_date(entries)
    series: list[DateValue] = []
    for day in _calendar(by_date):
        entry = by_date.get(day)
        series.append((day, _number(entry.get(metric)) if entry is not None else None))
    return series


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _round(value: float | None, digits: int = 6) -> float | None:
    """Round a derived statistic (trims float noise), passing None through."""
    return None if value is None else round(value, digits)


def _values(series: list[DateValue]) -> list[float]:
    """The available (non-None) values of a series, in date order."""
    return [value for _, value in series if value is not None]


def _tail(series: list[DateValue], days: int) -> list[DateValue]:
    """The last ``days`` calendar days of a series (all of it when shorter)."""
    return series[-days:] if days > 0 else []


def _mean(values: list[float]) -> float | None:
    """Arithmetic mean, None for no values."""
    return statistics.fmean(values) if values else None


def _stdev(values: list[float]) -> float | None:
    """Sample standard deviation, None for fewer than two values."""
    return statistics.stdev(values) if len(values) >= 2 else None


def _pct(diff: float, reference: float | None) -> float | None:
    """``diff`` as a percentage of ``reference``; None when the reference is 0 or missing."""
    return _round(diff / reference * 100) if reference else None


def _point(item: DateValue | None) -> dict[str, Any] | None:
    """{"date", "value"} for a series point, None for no point."""
    if item is None:
        return None
    return {"date": item[0].isoformat(), "value": item[1]}


def describe(values: list[float]) -> dict[str, Any] | None:
    """Mean, median, min, max, sample stdev and quartiles; None for fewer than two values."""
    if len(values) < 2:
        return None
    p25, _, p75 = statistics.quantiles(values, n=4, method="inclusive")
    return {
        "mean": _round(statistics.fmean(values)),
        "median": _round(statistics.median(values)),
        "min": min(values),
        "max": max(values),
        "stdev": _round(statistics.stdev(values)),
        "p25": _round(p25),
        "p75": _round(p75),
    }


# ---------------------------------------------------------------------------
# Metric trend
# ---------------------------------------------------------------------------


def _window_summary(series: list[DateValue], days: int) -> dict[str, Any]:
    """Mean of the available values in the last ``days`` calendar days and their count."""
    values = _values(_tail(series, days))
    return {"latest_mean": _round(_mean(values)), "latest_n": len(values)}


def _trailing_means(
    series: list[DateValue], windows: tuple[int, ...]
) -> list[dict[int, float | None]]:
    """Per day and window: mean of the available values in the trailing window ending that day.

    Windows never reach before the first day of the series.
    """
    rows: list[dict[int, float | None]] = []
    for index in range(len(series)):
        row: dict[int, float | None] = {}
        for window in windows:
            low = max(0, index + 1 - window)
            row[window] = _round(_mean(_values(series[low : index + 1])))
        rows.append(row)
    return rows


def _baseline(series: list[DateValue], days: int) -> dict[str, Any]:
    """Mean, median and stdev of the available values in the last ``days`` calendar days."""
    values = _values(_tail(series, days))
    return {
        "days": days,
        "n": len(values),
        "mean": _round(_mean(values)),
        "median": _round(statistics.median(values)) if values else None,
        "stdev": _round(_stdev(values)),
    }


def _compare(value: float | None, mean: float | None, stdev: float | None) -> dict[str, Any] | None:
    """Difference of a value from a reference mean, as absolute, percent and z-score."""
    if value is None or mean is None:
        return None
    diff = value - mean
    return {
        "diff": _round(diff),
        "diff_pct": _pct(diff, mean),
        "z": _round(diff / stdev) if stdev else None,
    }


def _week_over_week(series: list[DateValue]) -> dict[str, Any] | None:
    """Mean of the last 7 calendar days against the mean of the 7 days before them."""
    recent = _values(series[-7:])
    previous = _values(series[-14:-7])
    if not recent or not previous:
        return None
    recent_mean = statistics.fmean(recent)
    previous_mean = statistics.fmean(previous)
    diff = recent_mean - previous_mean
    return {
        "recent_mean": _round(recent_mean),
        "recent_n": len(recent),
        "previous_mean": _round(previous_mean),
        "previous_n": len(previous),
        "diff": _round(diff),
        "diff_pct": _pct(diff, previous_mean),
    }


def _outliers(series: list[DateValue], threshold: float) -> list[dict[str, Any]]:
    """Days whose z-score against the whole-range mean/stdev is at least ``threshold``."""
    values = _values(series)
    if len(values) < MIN_VALUES_FOR_OUTLIERS:
        return []
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values)
    if not stdev:
        return []
    found: list[dict[str, Any]] = []
    for day, value in series:
        if value is None:
            continue
        z = (value - mean) / stdev
        if abs(z) >= threshold:
            found.append({"date": day.isoformat(), "value": value, "z": _round(z)})
    return found


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def compute_metric_trend(  # pylint: disable=too-many-arguments,too-many-locals
    entries: list[dict[str, Any]],
    metric: str,
    windows: tuple[int, ...] = (7, 14, 42),
    baseline_days: int = 42,
    outlier_z: float = 2.5,
    *,
    period_start: Any = None,
    period_end: Any = None,
) -> dict[str, Any]:
    """Trend statistics of one wellness metric.

    ``period_start`` / ``period_end`` (ISO dates) select the requested analysis period; the
    entries may reach further back (lookback) so that rolling windows and the baseline at
    the start of the period have data. Day counts, statistics, outliers, the latest value
    and the per-day series cover the requested period only; the baseline covers the last
    ``baseline_days`` calendar days ending at the period end (available values only, ``n``
    tells how many, ``small_sample`` flags fewer than 14); rolling means are trailing means
    over the available values of the last ``window`` calendar days, lookback included.
    Without a period the whole range of the entries is the period. For native metrics in
    ``ZERO_MEANS_MISSING`` a stored 0 is treated as missing and counted.
    """
    full, zeros_missing = _zero_cleaned(metric_series(entries, metric), metric)
    start, end = _as_date(period_start), _as_date(period_end)
    if end is not None:
        full = [item for item in full if item[0] <= end]
    fetched = {"start": full[0][0].isoformat() if full else None, "end": full[-1][0].isoformat() if full else None, "days": len(full)}
    if start is not None:
        full_start = full[0][0] if full else start
        if full_start > start:  # the entries start inside the period: count the empty days before
            full = [(start + timedelta(days=offset), None) for offset in range((full_start - start).days)] + full
    period = [item for item in full if start is None or item[0] >= start]
    if start is not None and end is not None:
        days_total = (end - start).days + 1
    else:
        days_total = len(period)
    values = _values(period)
    present = [(day, value) for day, value in period if value is not None]
    latest = _point(present[-1] if present else None)
    baseline = _baseline(full, baseline_days)
    baseline["start"] = (full[-1][0] - timedelta(days=baseline_days - 1)).isoformat() if full else None
    baseline["end"] = full[-1][0].isoformat() if full else None
    baseline["small_sample"] = baseline["n"] < MIN_BASELINE_VALUES
    rolling_rows = _trailing_means(full, windows)[len(full) - len(period):]
    return {
        "metric": metric,
        "start": start.isoformat() if start else (period[0][0].isoformat() if period else None),
        "end": end.isoformat() if end else (period[-1][0].isoformat() if period else None),
        "days_total": days_total,
        "days_with_value": len(values),
        "days_missing": days_total - len(values),
        "zeros_treated_as_missing": zeros_missing,
        "fetched": fetched,
        "latest": latest,
        "stats": describe(values),
        "baseline": baseline,
        "latest_vs_baseline": _compare(
            latest["value"] if latest else None, baseline["mean"], baseline["stdev"]
        ),
        "windows": list(windows),
        "rolling": {window: _window_summary(full, window) for window in windows},
        "trend_7d_vs_prev_7d": _week_over_week(full),
        "outlier_z": outlier_z,
        "outliers": _outliers(period, outlier_z),
        "series": [
            {"date": day.isoformat(), "value": value, "rolling": rolling}
            for (day, value), rolling in zip(period, rolling_rows, strict=True)
        ],
    }


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def _correlation(
    xs: list[float], ys: list[float], method: Literal["linear", "ranked"]
) -> float | None:
    """Pearson ("linear") or Spearman ("ranked") coefficient; None when undefined."""
    try:
        return _round(statistics.correlation(xs, ys, method=method))
    except statistics.StatisticsError:  # fewer than two pairs or a constant input
        return None


def compute_correlation(  # pylint: disable=too-many-locals
    entries: list[dict[str, Any]],
    metric_a: str,
    metric_b: str,
    lag_days: int = 0,
    min_pairs: int = 10,
) -> dict[str, Any]:
    """Pearson r and Spearman rho between ``metric_a`` on a day and ``metric_b`` ``lag_days`` later.

    Only days where both values exist are paired; the coefficients are None when
    fewer than ``min_pairs`` pairs exist. A stored 0 of a ``ZERO_MEANS_MISSING`` metric is
    no value (as in the trend statistics) and counted in ``zeros_treated_as_missing``.
    """
    by_date = _entries_by_date(entries)
    pairs: list[tuple[float, float]] = []
    for day, entry in by_date.items():
        other = by_date.get(day + timedelta(days=lag_days))
        value_a = metric_value(entry, metric_a)
        value_b = metric_value(other, metric_b) if other is not None else None
        if value_a is not None and value_b is not None:
            pairs.append((value_a, value_b))
    zeros = sum(
        1 for entry in by_date.values() for metric in dict.fromkeys((metric_a, metric_b))
        if metric in ZERO_MEANS_MISSING and _number(entry.get(metric)) == 0
    )
    xs = [a for a, _ in pairs]
    ys = [b for _, b in pairs]
    enough = len(pairs) >= min_pairs
    return {
        "metric_a": metric_a,
        "metric_b": metric_b,
        "lag_days": lag_days,
        "n": len(pairs),
        "min_pairs": min_pairs,
        "small_sample": len(pairs) < MIN_RELIABLE_PAIRS,
        "pearson_r": _correlation(xs, ys, "linear") if enough else None,
        "spearman_rho": _correlation(xs, ys, "ranked") if enough else None,
        "zeros_treated_as_missing": zeros,
        "note": CAUSATION_NOTE,
    }


# ---------------------------------------------------------------------------
# Weight
# ---------------------------------------------------------------------------


def _weight_window(series: list[DateValue], days: int) -> dict[str, Any]:
    """Mean, first/last value, change and OLS slope over the weights of the last ``days`` days."""
    points = [(day, value) for day, value in _tail(series, days) if value is not None]
    values = [value for _, value in points]
    result: dict[str, Any] = {
        "days": days,
        "n": len(points),
        "mean": _round(_mean(values)),
        "first": _point(points[0] if points else None),
        "last": _point(points[-1] if points else None),
        "change_kg": None,
        "slope_kg_per_week": None,
    }
    if len(points) >= 2:
        result["change_kg"] = _round(points[-1][1] - points[0][1])
    if len(points) >= 3:
        xs = [float((day - points[0][0]).days) for day, _ in points]
        result["slope_kg_per_week"] = _round(statistics.linear_regression(xs, values).slope * 7)
    return result


def weight_trend(
    entries: list[dict[str, Any]], windows: tuple[int, ...] = (7, 14, 28)
) -> dict[str, Any]:
    """Weight statistics per window: mean, first/last value, change and OLS slope per week.

    The slope needs at least three weighed days in the window, the change at least two. A
    stored 0 is no value (``zeros_treated_as_missing``).
    """
    series, zeros = _zero_cleaned(metric_series(entries, "weight"), "weight")
    present = [(day, value) for day, value in series if value is not None]
    return {
        "metric": "weight",
        "start": series[0][0].isoformat() if series else None,
        "end": series[-1][0].isoformat() if series else None,
        "days_total": len(series),
        "days_with_value": len(present),
        "zeros_treated_as_missing": zeros,
        "latest": _point(present[-1] if present else None),
        "windows": {window: _weight_window(series, window) for window in windows},
    }


# ---------------------------------------------------------------------------
# Nutrition
# ---------------------------------------------------------------------------


def _nutrition_day(
    day: date, entry: dict[str, Any] | None, fields: tuple[str, str, str]
) -> dict[str, Any]:
    """Intake, burn and balance of one calendar day (all None for a day without entry)."""
    burn_field, active_field, balance_field = fields
    get = (entry or {}).get
    kcal = _number(get("kcalConsumed"))
    logged = kcal is not None and kcal > 0
    burn = _number(get(burn_field))
    balance = _number(get(balance_field))
    if balance is None and kcal is not None and kcal > 0 and burn is not None:
        balance = kcal - burn
    return {
        "date": day.isoformat(),
        "kcal_consumed": kcal,
        "carbs_g": _number(get("carbohydrates")),
        "protein_g": _number(get("protein")),
        "fat_g": _number(get("fatTotal")),
        "burn_kcal": burn,
        "active_kcal": _number(get(active_field)),
        "balance_kcal": balance,
        "logged": logged,
        "weight": metric_value(entry, "weight"),
    }


def _present(rows: list[dict[str, Any]], key: str) -> list[float]:
    """The non-None values of one key over the day records, in date order."""
    return [row[key] for row in rows if row[key] is not None]


def _nutrition_window(day_rows: list[dict[str, Any]], days: int) -> dict[str, Any]:
    """Window statistics over the last ``days`` day records; intake stats use logged days only."""
    rows = day_rows[-days:] if days > 0 else []
    logged = [row for row in rows if row["logged"]]
    intake = _present(logged, "kcal_consumed")
    burns = _present(rows, "burn_kcal")
    balances = _present(logged, "balance_kcal")
    weights = _present(rows, "weight")
    return {
        "days": len(rows),
        "days_logged": len(logged),
        "days_missing_intake": len(rows) - len(logged),
        "days_with_burn": len(burns),
        "days_with_balance": len(balances),
        "kcal_consumed_total": _round(sum(intake)) if intake else None,
        "kcal_consumed_mean_logged_days": _round(_mean(intake)),
        "carbs_mean_g": _round(_mean(_present(logged, "carbs_g"))),
        "protein_mean_g": _round(_mean(_present(logged, "protein_g"))),
        "fat_mean_g": _round(_mean(_present(logged, "fat_g"))),
        "burn_mean_kcal": _round(_mean(burns)),
        "balance_total_kcal_logged_days": _round(sum(balances)) if balances else None,
        "balance_mean_kcal_logged_days": _round(_mean(balances)),
        "weight_change_kg": _round(weights[-1] - weights[0]) if len(weights) >= 2 else None,
    }


def nutrition_summary(
    entries: list[dict[str, Any]],
    windows: tuple[int, ...] = (7, 14, 28),
    burn_field: str = "GarminTotalCalories",
    active_field: str = "GarminActiveCalories",
    balance_field: str = "GarminKcalBalance",
) -> dict[str, Any]:
    """Per-day intake/burn/balance records and per-window nutrition statistics.

    A day counts as logged when ``kcalConsumed`` is a number greater than 0. Unlogged
    days (including days absent from the list) are excluded from every intake and
    balance statistic and counted in ``days_missing_intake``.
    """
    by_date = _entries_by_date(entries)
    fields = (burn_field, active_field, balance_field)
    rows = [_nutrition_day(day, by_date.get(day), fields) for day in _calendar(by_date)]
    return {
        "start": rows[0]["date"] if rows else None,
        "end": rows[-1]["date"] if rows else None,
        "days_total": len(rows),
        "fields": {"burn": burn_field, "active": active_field, "balance": balance_field},
        "days": rows,
        "windows": {window: _nutrition_window(rows, window) for window in windows},
        "notes": list(NUTRITION_NOTES),
    }


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 2, signed: bool = False) -> str:
    """Number rounded for display ('n/a' for None, '+' prefix for positive when signed)."""
    if value is None or is_missing(value):
        return "n/a"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    text = format_value(round(float(value), digits))
    return f"+{text}" if signed and value > 0 else text


def _pct_text(value: float | None) -> str:
    """Signed percentage for display, 'n/a' for None."""
    return "n/a" if value is None else f"{_fmt(value, 1, signed=True)}%"


def _format_stats(stats: dict[str, Any] | None, unit: str) -> str:
    """Range statistics on one line."""
    if stats is None:
        return "n/a (fewer than 2 values)"
    return (
        f"mean {_fmt(stats['mean'])}{unit}, median {_fmt(stats['median'])}{unit}, "
        f"min {_fmt(stats['min'])}{unit}, max {_fmt(stats['max'])}{unit}, "
        f"stdev {_fmt(stats['stdev'])}, p25 {_fmt(stats['p25'])}{unit}, p75 {_fmt(stats['p75'])}{unit}"
    )


def _format_compare(compare: dict[str, Any] | None, unit: str) -> str:
    """Latest-vs-baseline comparison on one line."""
    if compare is None:
        return "n/a"
    return (
        f"diff {_fmt(compare['diff'], signed=True)}{unit} ({_pct_text(compare['diff_pct'])}), "
        f"z {_fmt(compare['z'], signed=True)}"
    )


def _format_week(week: dict[str, Any] | None, unit: str) -> str:
    """Last-7-days-vs-previous-7-days comparison on one line."""
    if week is None:
        return "n/a"
    return (
        f"{_fmt(week['recent_mean'])}{unit} (n={week['recent_n']}) vs "
        f"{_fmt(week['previous_mean'])}{unit} (n={week['previous_n']}), "
        f"diff {_fmt(week['diff'], signed=True)}{unit} ({_pct_text(week['diff_pct'])})"
    )


def _format_outliers(result: dict[str, Any], unit: str) -> str:
    """Outlier days with their z-scores on one line."""
    outliers = result["outliers"]
    if not outliers:
        return f"Outliers in the period (|z| >= {_fmt(result['outlier_z'], 1)}): none"
    items = ", ".join(
        f"{item['date']} = {_fmt(item['value'])}{unit} (z {_fmt(item['z'], signed=True)})"
        for item in outliers
    )
    return f"Outliers in the period (|z| >= {_fmt(result['outlier_z'], 1)}): {items}"


def _format_series_tail(result: dict[str, Any], unit: str) -> list[str]:
    """Compact table of the last SERIES_TAIL_DAYS days plus a line about the hidden rest."""
    series = result["series"]
    if not series:
        return ["Series: no days in range"]
    windows = result.get("windows") or []
    window = 7 if 7 in windows else (windows[0] if windows else None)
    rows = series[-SERIES_TAIL_DAYS:]
    lines = [f"Last {len(rows)} days (date, value{unit}, {window}d mean):"]
    for row in rows:
        rolling = row["rolling"].get(window) if window is not None else None
        lines.append(f"  {row['date']}  {_fmt(row['value'])}  {_fmt(rolling)}")
    hidden = len(series) - len(rows)
    if hidden:
        lines.append(f"  ... {hidden} earlier days of the period not shown (period covers {len(series)} days)")
    return lines


def _unit_text(units: str | None) -> str:
    if not units:
        return ""
    return units if units.startswith("/") else f" {units}"


def format_metric_trend(result: dict[str, Any], units: str | None = None) -> str:
    """Render a ``compute_metric_trend`` result as compact text."""
    unit = _unit_text(units)
    latest = result["latest"]
    baseline = result["baseline"]
    head = (
        f"{result['metric']}{f' ({units})' if units else ''}: period {result['start'] or 'n/a'} to {result['end'] or 'n/a'}, "
        f"{result['days_total']} days, {result['days_with_value']} with values, {result['days_missing']} missing"
    )
    if result.get("zeros_treated_as_missing"):
        head += f" ({result['zeros_treated_as_missing']} stored 0 treated as missing)"
    baseline_head = (
        f"Personal baseline ({baseline['days']} days"
        + (f" {baseline['start']} to {baseline['end']}" if baseline.get("start") else "")
        + f", n={baseline['n']})"
    )
    if baseline.get("small_sample"):
        baseline_head += f" - small sample (fewer than {MIN_BASELINE_VALUES} values), not reliable"
    lines = [
        head,
        f"Latest: {latest['date']} = {_fmt(latest['value'])}{unit}" if latest else "Latest: n/a",
        f"Period stats: {_format_stats(result['stats'], unit)}",
        f"{baseline_head}: mean {_fmt(baseline['mean'])}{unit}, median {_fmt(baseline['median'])}{unit}, "
        f"stdev {_fmt(baseline['stdev'])}",
        f"Latest vs baseline: {_format_compare(result['latest_vs_baseline'], unit)}",
    ]
    rolling_parts = [
        f"{window}d {_fmt(rolling['latest_mean'])}{unit} (n={rolling['latest_n']})"
        for window, rolling in result["rolling"].items()
    ]
    lines.append(f"Rolling means at {result['end'] or 'n/a'} (lookback included): " + ", ".join(rolling_parts))
    lines.append(f"Last 7d vs previous 7d: {_format_week(result['trend_7d_vs_prev_7d'], unit)}")
    lines.append(_format_outliers(result, unit))
    lines.extend(_format_series_tail(result, unit))
    return "\n".join(lines)


def format_correlation(result: dict[str, Any]) -> str:
    """Render a ``compute_correlation`` result as compact text."""
    lag = result["lag_days"]
    if lag:
        lag_text = f"{result['metric_b']} {abs(lag)} day(s) {'later' if lag > 0 else 'earlier'}"
    else:
        lag_text = "same day"
    header = f"Correlation {result['metric_a']} vs {result['metric_b']} ({lag_text}): n={result['n']}"
    if result["n"] < result["min_pairs"]:
        header += f" (fewer than {result['min_pairs']} paired days, no coefficients)"
    elif result.get("small_sample"):
        header += f" (small sample, fewer than {MIN_RELIABLE_PAIRS} paired days: indicative only)"
    if result.get("zeros_treated_as_missing"):
        header += f" ({result['zeros_treated_as_missing']} stored 0 treated as missing)"
    return "\n".join(
        [
            header,
            f"Pearson r {_fmt(result['pearson_r'], 3)}, Spearman rho {_fmt(result['spearman_rho'], 3)}",
            f"Note: {result['note']}",
        ]
    )


def _format_weight_window(window: int, stats: dict[str, Any]) -> str:
    """One weight window on one line."""
    first, last = stats["first"], stats["last"]
    first_text = f"{_fmt(first['value'])} kg ({first['date']})" if first else "n/a"
    last_text = f"{_fmt(last['value'])} kg ({last['date']})" if last else "n/a"
    return (
        f"{window}d: n={stats['n']}, mean {_fmt(stats['mean'])} kg, first {first_text} -> "
        f"last {last_text}, change {_fmt(stats['change_kg'], signed=True)} kg, "
        f"slope {_fmt(stats['slope_kg_per_week'], signed=True)} kg/week"
    )


def format_weight_trend(result: dict[str, Any]) -> str:
    """Render a ``weight_trend`` result as compact text (one line per window)."""
    latest = result["latest"]
    latest_text = f"{latest['date']} = {_fmt(latest['value'])} kg" if latest else "n/a"
    lines = [
        f"Weight: {result['start'] or 'n/a'} to {result['end'] or 'n/a'}, "
        f"{result['days_total']} days, {result['days_with_value']} weighed; latest {latest_text}"
        + (f" ({result['zeros_treated_as_missing']} stored 0 treated as missing)" if result.get("zeros_treated_as_missing") else "")
    ]
    lines.extend(_format_weight_window(w, s) for w, s in result["windows"].items())
    if not result["windows"]:
        lines.append("Windows: n/a")
    return "\n".join(lines)


def _format_nutrition_window(window: int, stats: dict[str, Any]) -> str:
    """One nutrition window on one line."""
    return (
        f"{window}d: logged {stats['days_logged']}/{stats['days']} "
        f"({stats['days_missing_intake']} without intake), "
        f"intake mean {_fmt(stats['kcal_consumed_mean_logged_days'], 0)} kcal "
        f"(total {_fmt(stats['kcal_consumed_total'], 0)}), "
        f"carbs {_fmt(stats['carbs_mean_g'], 0)} / protein {_fmt(stats['protein_mean_g'], 0)} / "
        f"fat {_fmt(stats['fat_mean_g'], 0)} g, "
        f"burn mean {_fmt(stats['burn_mean_kcal'], 0)} kcal ({stats['days_with_burn']} days), "
        f"balance on logged days mean {_fmt(stats['balance_mean_kcal_logged_days'], 0)} kcal "
        f"(total {_fmt(stats['balance_total_kcal_logged_days'], 0)}, "
        f"{stats['days_with_balance']} days), "
        f"weight change {_fmt(stats['weight_change_kg'], signed=True)} kg"
    )


def _format_nutrition_day(row: dict[str, Any]) -> str:
    """One day record as a compact table row."""
    if not row["logged"]:
        intake = "not logged"
    else:
        intake = (
            f"{_fmt(row['kcal_consumed'], 0)} kcal, "
            f"{_fmt(row['carbs_g'], 0)}/{_fmt(row['protein_g'], 0)}/{_fmt(row['fat_g'], 0)} g"
        )
    return (
        f"  {row['date']}  {intake}  burn {_fmt(row['burn_kcal'], 0)}  "
        f"balance {_fmt(row['balance_kcal'], 0, signed=True)}"
    )


def format_nutrition_summary(result: dict[str, Any]) -> str:
    """Render a ``nutrition_summary`` result as compact text (one line per window)."""
    fields = result["fields"]
    lines = [
        f"Nutrition: {result['start'] or 'n/a'} to {result['end'] or 'n/a'}, "
        f"{result['days_total']} days (burn: {fields['burn']}, active: {fields['active']}, "
        f"balance: {fields['balance']})"
    ]
    lines.extend(_format_nutrition_window(w, s) for w, s in result["windows"].items())
    rows = result["days"][-NUTRITION_TAIL_DAYS:]
    if rows:
        lines.append(f"Last {len(rows)} days (date, intake, carbs/protein/fat, burn, balance kcal):")
        lines.extend(_format_nutrition_day(row) for row in rows)
        hidden = len(result["days"]) - len(rows)
        if hidden:
            lines.append(f"  ... {hidden} earlier days not shown")
    lines.append("Notes:")
    lines.extend(f"- {note}" for note in result["notes"])
    return "\n".join(lines)
