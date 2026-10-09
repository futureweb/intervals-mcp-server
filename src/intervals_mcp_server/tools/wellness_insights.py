"""
Wellness insight tools for Intervals.icu: recovery snapshot, multi-day trends with personal
baselines and correlations, nutrition / calorie / weight trends.

All tools are read-only and report statistics only; the interpretation is left to the
coach. Custom wellness fields (for example the daily Garmin values a bridge writes) are
picked up dynamically from the athlete's custom item definitions.
"""

import json
from datetime import date, datetime, timedelta
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.utils.custom_fields import (
    ACTIVITY_FIELD,
    INPUT_FIELD,
    CustomFieldDefs,
    format_field_value,
    format_value,
    is_missing,
)
from intervals_mcp_server.utils.dates import get_default_end_date
from intervals_mcp_server.utils.formatting import event_type_label
from intervals_mcp_server.utils.sports import hms
from intervals_mcp_server.utils.validation import resolve_athlete_id, validate_date
from intervals_mcp_server.utils.wellness_stats import (
    compute_correlation,
    compute_metric_trend,
    format_correlation,
    format_metric_trend,
    format_nutrition_summary,
    format_weight_trend,
    nutrition_summary,
    sort_entries,
    weight_trend,
)

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

# Native wellness fields shown in the snapshot: (key, label, units, divisor)
SNAPSHOT_FIELDS: tuple[tuple[str, str, str, float], ...] = (
    ("restingHR", "RHR", "bpm", 1),
    ("hrv", "HRV", "ms", 1),
    ("hrvSDNN", "HRV SDNN", "ms", 1),
    ("avgSleepingHR", "sleeping HR", "bpm", 1),
    ("sleepSecs", "sleep", "h", 3600),
    ("sleepScore", "sleep score", "", 1),
    ("readiness", "readiness", "", 1),
    ("spO2", "SpO2", "%", 1),
    ("respiration", "respiration", "/min", 1),
    ("weight", "weight", "kg", 1),
    ("bodyFat", "body fat", "%", 1),
    ("steps", "steps", "", 1),
    ("kcalConsumed", "kcal consumed", "kcal", 1),
    ("hydrationVolume", "hydration", "l", 1),
)
SUBJECTIVE_FIELDS = ("sleepQuality", "soreness", "fatigue", "stress", "mood", "motivation", "injury")
DEFAULT_BASELINE_METRICS = "hrv,restingHR,avgSleepingHR,sleepScore,readiness,respiration,spO2"
DEFAULT_TREND_METRICS = "hrv,restingHR,avgSleepingHR,sleepScore,readiness,respiration,spO2,weight"
BASELINE_DAYS = 42
MAX_DAYS_BACK = 14
MAX_TREND_METRICS = 16


def _split(csv: str | None) -> list[str]:
    return [part.strip() for part in (csv or "").split(",") if part.strip()]


def _windows(csv: str | None, default: tuple[int, ...]) -> tuple[int, ...] | str:
    if not csv:
        return default
    try:
        values = tuple(sorted({int(p) for p in _split(csv)}))
    except ValueError:
        return "Error: windows must be a comma-separated list of integers (days)."
    if not values or any(v < 2 or v > 365 for v in values):
        return "Error: windows must be between 2 and 365 days."
    return values


def _resolve_range(start_date: str | None, end_date: str | None, default_days: int) -> tuple[str, str] | str:
    end = end_date or get_default_end_date()
    try:
        validate_date(end)
        if start_date:
            validate_date(start_date)
    except ValueError as exc:
        return f"Error: {exc}"
    start = start_date or (date.fromisoformat(end) - timedelta(days=default_days - 1)).isoformat()
    if start > end:
        return "Error: start_date must not be after end_date."
    return start, end


async def _fetch_wellness(
    athlete_id: str, api_key: str | None, start: str, end: str, fields: str | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    params: dict[str, str] = {"oldest": start, "newest": end}
    if fields:
        params["fields"] = fields
    result = await make_intervals_request(url=f"/athlete/{athlete_id}/wellness", api_key=api_key, params=params)
    if isinstance(result, dict) and "error" in result:
        return [], f"Error fetching wellness data: {result.get('message', 'Unknown error')}"
    if isinstance(result, dict):
        entries = [dict(v, id=k) for k, v in result.items() if isinstance(v, dict)]
    else:
        entries = [e for e in result if isinstance(e, dict)] if isinstance(result, list) else []
    return sort_entries(entries), None


async def _fetch_activities(
    athlete_id: str, api_key: str | None, start: str, end: str, fields: str | None = None
) -> list[dict[str, Any]]:
    params: dict[str, str] = {"oldest": start, "newest": end}
    if fields:
        params["fields"] = fields
    result = await make_intervals_request(url=f"/athlete/{athlete_id}/activities", api_key=api_key, params=params)
    if not isinstance(result, list):
        return []
    return [a for a in result if isinstance(a, dict)]


async def _defs(athlete_id: str, api_key: str | None, item_type: str) -> CustomFieldDefs:
    return (await get_custom_item_index(athlete_id=athlete_id, api_key=api_key)).get(item_type, {})


def flatten_sport_info(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose per-sport eFTP/W'/Pmax of the wellness sportInfo as eftp_<Type> ... keys."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        row = dict(entry)
        for info in entry.get("sportInfo") or []:
            if isinstance(info, dict) and info.get("type"):
                for key in ("eftp", "wPrime", "pMax"):
                    if info.get(key) is not None:
                        row[f"{key}_{info['type']}"] = info[key]
        out.append(row)
    return out


# ------------------------------------------------------------- recovery snapshot
def _day_line(entry: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, label, units, divisor in SNAPSHOT_FIELDS:
        value = entry.get(key)
        if is_missing(value) or not isinstance(value, (int, float)):
            continue
        shown = value / divisor if divisor != 1 else value
        parts.append(f"{label} {format_value(round(shown, 2))}{(' ' + units) if units else ''}")
    return " | ".join(parts) if parts else "no native values"


def _fitness_line(entry: dict[str, Any]) -> str | None:
    ctl, atl = entry.get("ctl"), entry.get("atl")
    if ctl is None and atl is None:
        return None
    form = (ctl - atl) if isinstance(ctl, (int, float)) and isinstance(atl, (int, float)) else None
    ramp = entry.get("rampRate")
    return (
        f"CTL {format_value(ctl)} | ATL {format_value(atl)} | form {format_value(round(form, 1)) if form is not None else 'n/a'}"
        f" | ramp {format_value(round(ramp, 2)) if isinstance(ramp, (int, float)) else 'n/a'}"
    )


def _subjective_line(entry: dict[str, Any]) -> str | None:
    parts = [f"{k} {entry[k]}" for k in SUBJECTIVE_FIELDS if entry.get(k) is not None]
    return ", ".join(parts) + " (1-4 scales, 1 = best)" if parts else None


def _custom_line(entry: dict[str, Any], defs: CustomFieldDefs) -> str | None:
    parts = [
        f"{definition['name']} {format_field_value(definition, entry[code])}"
        for code, definition in defs.items()
        if code in entry and not is_missing(entry[code])
    ]
    return "; ".join(parts) if parts else None


def _missing_line(entry: dict[str, Any]) -> str | None:
    missing = [label for key, label, _, _ in SNAPSHOT_FIELDS if is_missing(entry.get(key))]
    return ", ".join(missing) if missing else None


def _baseline_lines(entries: list[dict[str, Any]], metrics: list[str]) -> list[str]:
    lines: list[str] = []
    for metric in metrics:
        trend = compute_metric_trend(entries, metric, windows=(7, 42), baseline_days=BASELINE_DAYS)
        latest, base, rolling = trend.get("latest"), trend.get("baseline") or {}, trend.get("rolling") or {}
        if not latest or base.get("n", 0) < 3:
            lines.append(f"  {metric}: not enough data ({trend.get('days_with_value', 0)} days with values)")
            continue
        comp = trend.get("latest_vs_baseline") or {}
        seven = (rolling.get(7) or {}).get("latest_mean")
        lines.append(
            f"  {metric}: latest {format_value(latest['value'])} ({latest['date']}) | 7d mean {format_value(seven) if seven is not None else 'n/a'}"
            f" | {BASELINE_DAYS}d baseline mean {format_value(base.get('mean'))}, median {format_value(base.get('median'))}, sd {format_value(base.get('stdev'))} (n {base.get('n')})"
            + (f" | vs baseline {comp['diff']:+.2f} ({comp['diff_pct']:+.1f}%, z {comp['z']:+.2f})" if comp.get("diff") is not None and comp.get("z") is not None else "")
        )
    return lines


def _activity_line(activity: dict[str, Any], defs: CustomFieldDefs) -> str:
    loads = []
    for key, label in (("power_load", "P"), ("hr_load", "H"), ("pace_load", "Pa")):
        if activity.get(key) is not None:
            loads.append(f"{label}{activity[key]}")
    text = (
        f"  {str(activity.get('start_date_local', ''))[:16]} {activity.get('type', '?')} '{activity.get('name', 'unnamed')}' ({activity.get('id')}): "
        f"{hms(activity.get('moving_time'))}, load {activity.get('icu_training_load', 'n/a')} (Intervals.icu{', ' + '/'.join(loads) if loads else ''})"
    )
    if activity.get("icu_intensity") is not None:
        text += f", IF {activity['icu_intensity']:.0f}%"
    if activity.get("feel") is not None:
        text += f", feel {activity['feel']}/5"
    if activity.get("icu_rpe") is not None:
        text += f", RPE {activity['icu_rpe']}/10"
    device = [
        f"{definition['name']} {format_field_value(definition, activity[code])}"
        for code, definition in defs.items()
        if code in activity and not is_missing(activity[code]) and definition.get("value_type") == "numeric"
        and activity[code] not in (0, 0.0)
    ]
    if device:
        text += " | device fields: " + ", ".join(device[:12]) + (" ..." if len(device) > 12 else "")
    return text


def _event_line(event: dict[str, Any]) -> str:
    text = f"  {str(event.get('start_date_local', ''))[:10]} {event_type_label(event)} '{event.get('name', 'unnamed')}' (event {event.get('id')})"
    if event.get("moving_time"):
        text += f", planned {hms(event['moving_time'])}"
    if event.get("icu_training_load") is not None:
        text += f", planned load {event['icu_training_load']}"
    if event.get("paired_activity_id"):
        text += f", done: {event['paired_activity_id']}"
    return text


def _snapshot_json(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    target: str, entries: list[dict[str, Any]], input_defs: CustomFieldDefs, baselines: list[dict[str, Any]],
    activities: list[dict[str, Any]], events: list[dict[str, Any]],
) -> dict[str, Any]:
    days = []
    for entry in entries:
        day: dict[str, Any] = {"date": entry.get("id"), "updated": entry.get("updated"), "locked": entry.get("locked")}
        day["native"] = {key: entry.get(key) for key, _, _, _ in SNAPSHOT_FIELDS if not is_missing(entry.get(key))}
        day["fitness"] = {k: entry.get(k) for k in ("ctl", "atl", "rampRate") if entry.get(k) is not None}
        day["subjective"] = {k: entry.get(k) for k in SUBJECTIVE_FIELDS if entry.get(k) is not None}
        day["custom"] = {code: entry[code] for code in input_defs if code in entry and not is_missing(entry[code])}
        day["missing"] = [key for key, _, _, _ in SNAPSHOT_FIELDS if is_missing(entry.get(key))]
        day["preliminary"] = entry.get("id") == target
        days.append(day)
    return {"date": target, "days": days, "baselines": baselines, "activities": activities, "planned_events": events}


@tool("read")
async def get_recovery_snapshot(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments,too-many-branches
    date_str: str | None = None,
    days_back: int = 3,
    baseline_metrics: str = DEFAULT_BASELINE_METRICS,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
) -> str:
    """Compact recovery / readiness context for a day in one call (read-only, no verdict)

    Returns, for the given day and the days before it: the native wellness values (RHR,
    HRV, sleeping HR, sleep, sleep score, readiness, SpO2, respiration, weight, steps,
    kcal), CTL/ATL/form/ramp rate, subjective scores, every custom wellness field with a
    value (e.g. device values such as Body Battery, training readiness, sleep stages,
    respiration or skin temperature written by a sync bridge), which values are missing
    and when the record was last updated; personal baselines (7-day mean, 42-day mean,
    median, SD and the latest value's deviation) for the selected metrics; the activities
    of those days with their loads and device fields; and the planned events of the day.
    The current day's aggregates (steps, calories) can still be incomplete and are flagged
    as preliminary. No readiness verdict is computed.

    Args:
        date_str: The day in YYYY-MM-DD format (optional, default today)
        days_back: How many days before date_str to include, 0-14 (optional, default 3)
        baseline_metrics: Comma-separated wellness metrics for the baseline block
            (optional, default "hrv,restingHR,avgSleepingHR,sleepScore,readiness,respiration,spO2")
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if not 0 <= days_back <= MAX_DAYS_BACK:
        return f"Error: days_back must be between 0 and {MAX_DAYS_BACK}."
    target = date_str or get_default_end_date()
    try:
        validate_date(target)
    except ValueError as exc:
        return f"Error: {exc}"
    start = (date.fromisoformat(target) - timedelta(days=days_back)).isoformat()
    baseline_start = (date.fromisoformat(target) - timedelta(days=BASELINE_DAYS + days_back)).isoformat()
    metrics = _split(baseline_metrics)

    entries, error = await _fetch_wellness(athlete_id_to_use, api_key, start, target)
    if error:
        return error
    baseline_entries, _ = await _fetch_wellness(
        athlete_id_to_use, api_key, baseline_start, target, fields="id," + ",".join(metrics)
    )
    input_defs = await _defs(athlete_id_to_use, api_key, INPUT_FIELD)
    field_defs = await _defs(athlete_id_to_use, api_key, ACTIVITY_FIELD)
    activities = await _fetch_activities(athlete_id_to_use, api_key, start, target)
    events_result = await make_intervals_request(
        url=f"/athlete/{athlete_id_to_use}/events", api_key=api_key, params={"oldest": target, "newest": target}
    )
    events = [e for e in events_result if isinstance(e, dict)] if isinstance(events_result, list) else []

    if output_format.strip().lower() == "json":
        baselines = [
            compute_metric_trend(baseline_entries, m, windows=(7, 42), baseline_days=BASELINE_DAYS) for m in metrics
        ]
        for trend in baselines:
            trend.pop("series", None)
        return json.dumps(
            _snapshot_json(target, entries, input_defs, baselines, activities, events), ensure_ascii=False
        )

    lines = [
        f"Recovery snapshot for athlete {athlete_id_to_use}: {target} and {days_back} day(s) before "
        f"(generated {datetime.now().strftime('%Y-%m-%dT%H:%M')} server local time)",
        "Values are reported as stored; today's aggregates (steps, calories, burn) may still be incomplete.",
        "",
    ]
    by_date = {str(e.get("id")): e for e in entries}
    for offset in range(days_back, -1, -1):
        day = (date.fromisoformat(target) - timedelta(days=offset)).isoformat()
        entry = by_date.get(day)
        tag = " (today, preliminary)" if day == target else ""
        if entry is None:
            lines.append(f"{day}{tag}: no wellness record")
            continue
        lines.append(f"{day}{tag} | updated {entry.get('updated', 'n/a')} | locked {'yes' if entry.get('locked') else 'no'}")
        lines.append("  " + _day_line(entry))
        for extra in (_fitness_line(entry), _subjective_line(entry)):
            if extra:
                lines.append("  " + extra)
        custom = _custom_line(entry, input_defs)
        if custom:
            lines.append("  custom: " + custom)
        missing = _missing_line(entry)
        if missing:
            lines.append("  missing: " + missing)
    lines.append("")
    lines.append(f"Baselines (last {BASELINE_DAYS} days before and including {target}):")
    lines.extend(_baseline_lines(baseline_entries, metrics))
    lines.append("")
    lines.append(f"Activities {start} to {target} ({len(activities)}):")
    lines.extend([_activity_line(a, field_defs) for a in activities] or ["  none"])
    lines.append("")
    lines.append(f"Planned on {target} ({len(events)}):")
    lines.extend([_event_line(e) for e in events] or ["  none"])
    return "\n".join(lines)


# ------------------------------------------------------------------- trends
@tool("read")
async def get_wellness_trends(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements,too-many-branches
    start_date: str | None = None,
    end_date: str | None = None,
    metrics: str = DEFAULT_TREND_METRICS,
    windows: str = "7,14,42",
    correlations: str | None = None,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
) -> str:
    """Wellness trends with rolling means, personal baselines, outliers and optional correlations

    For every requested wellness metric (native fields such as hrv, restingHR,
    avgSleepingHR, respiration, sleepScore, readiness, spO2, weight, ctl, atl, steps,
    or any custom wellness field by its code, and eFTP per sport as eftp_Ride /
    eftp_Run) it reports the daily values of the last two weeks with trailing means,
    rolling means for the requested windows, mean/median/min/max/SD, a 42-day baseline,
    the latest value versus the baseline (difference, percent, z-score), the last 7 days
    versus the 7 days before, and outliers (|z| >= 2.5). Weight additionally gets a
    7/14/28-day trend with the slope in kg per week. Optional correlations between
    metric pairs are statistical associations only, not causes. Missing days are never
    filled in.

    Args:
        start_date: Start date YYYY-MM-DD (optional, default 42 days before end_date)
        end_date: End date YYYY-MM-DD (optional, default today)
        metrics: Comma-separated metrics (optional, default
            "hrv,restingHR,avgSleepingHR,sleepScore,readiness,respiration,spO2,weight")
        windows: Comma-separated rolling windows in days (optional, default "7,14,42")
        correlations: Comma-separated pairs "a:b" or "a:b:lag_days", e.g.
            "hrv:readiness,restingHR:sleepScore:1" (optional)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    span = _resolve_range(start_date, end_date, BASELINE_DAYS)
    if isinstance(span, str):
        return span
    start, end = span
    window_tuple = _windows(windows, (7, 14, 42))
    if isinstance(window_tuple, str):
        return window_tuple
    metric_list = _split(metrics)
    if not metric_list:
        return "Error: at least one metric is required."
    if len(metric_list) > MAX_TREND_METRICS:
        return f"Error: at most {MAX_TREND_METRICS} metrics per call."

    fetch_start = (date.fromisoformat(start) - timedelta(days=BASELINE_DAYS)).isoformat()
    entries, error = await _fetch_wellness(athlete_id_to_use, api_key, fetch_start, end)
    if error:
        return error
    entries = flatten_sport_info(entries)
    in_range = [e for e in entries if start <= str(e.get("id")) <= end]
    input_defs = await _defs(athlete_id_to_use, api_key, INPUT_FIELD)

    trends = [compute_metric_trend(entries, m, windows=window_tuple, baseline_days=BASELINE_DAYS) for m in metric_list]
    for trend in trends:
        trend["series"] = [row for row in trend.get("series", []) if start <= row["date"] <= end]
    weight = weight_trend(in_range) if "weight" in metric_list else None
    pairs = []
    for spec in _split(correlations):
        bits = spec.split(":")
        if len(bits) < 2:
            return f"Error: correlation '{spec}' must be 'metric_a:metric_b' or 'metric_a:metric_b:lag_days'."
        lag = 0
        if len(bits) > 2:
            try:
                lag = int(bits[2])
            except ValueError:
                return f"Error: lag in '{spec}' must be an integer number of days."
        pairs.append(compute_correlation(in_range, bits[0], bits[1], lag_days=lag))

    if output_format.strip().lower() == "json":
        return json.dumps(
            {"start": start, "end": end, "metrics": trends, "weight": weight, "correlations": pairs},
            ensure_ascii=False,
        )
    blocks = [f"Wellness trends for athlete {athlete_id_to_use}, {start} to {end} (baseline window {BASELINE_DAYS} days):"]
    for metric, trend in zip(metric_list, trends, strict=True):
        units = (input_defs.get(metric) or {}).get("units")
        blocks.append(format_metric_trend(trend, units=units))
    if weight:
        blocks.append(format_weight_trend(weight))
    blocks.extend(format_correlation(pair) for pair in pairs)
    return "\n\n".join(blocks)


# ----------------------------------------------------------------- nutrition
@tool("read")
async def get_nutrition_summary(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    start_date: str | None = None,
    end_date: str | None = None,
    windows: str = "7,14,28",
    burn_field: str = "GarminTotalCalories",
    active_field: str = "GarminActiveCalories",
    balance_field: str = "GarminKcalBalance",
    include_training_load: bool = True,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
) -> str:
    """Nutrition, calorie balance and weight trends from the wellness records (read-only)

    Per day: logged kcal, carbohydrates, protein and fat (native Intervals.icu fields),
    the device's total and active calorie burn and balance (custom wellness fields whose
    codes are configurable), and weight. Per window (default 7/14/28 days): totals and
    means over logged days only, how many days have no logged intake, burn mean, balance
    total/mean on logged days and the weight change. A day without logged intake is
    never treated as 0 kcal; the device burn is an estimate; a calorie balance is not a
    measurement of fat change. Optionally the Intervals.icu training load per day is
    listed alongside.

    Args:
        start_date: Start date YYYY-MM-DD (optional, default 28 days before end_date)
        end_date: End date YYYY-MM-DD (optional, default today)
        windows: Comma-separated windows in days (optional, default "7,14,28")
        burn_field: Custom wellness field code holding the total daily burn (optional)
        active_field: Custom wellness field code holding the active burn (optional)
        balance_field: Custom wellness field code holding a device-computed balance (optional)
        include_training_load: Append the Intervals.icu training load per day (optional, default True)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    span = _resolve_range(start_date, end_date, 28)
    if isinstance(span, str):
        return span
    start, end = span
    window_tuple = _windows(windows, (7, 14, 28))
    if isinstance(window_tuple, str):
        return window_tuple

    entries, error = await _fetch_wellness(athlete_id_to_use, api_key, start, end)
    if error:
        return error
    summary = nutrition_summary(
        entries, windows=window_tuple, burn_field=burn_field, active_field=active_field, balance_field=balance_field
    )
    weight = weight_trend(entries, windows=window_tuple)
    loads: dict[str, float] = {}
    if include_training_load:
        for activity in await _fetch_activities(
            athlete_id_to_use, api_key, start, end, fields="id,start_date_local,icu_training_load"
        ):
            day = str(activity.get("start_date_local", ""))[:10]
            load = activity.get("icu_training_load")
            if day and isinstance(load, (int, float)):
                loads[day] = loads.get(day, 0) + load
    if output_format.strip().lower() == "json":
        return json.dumps(
            {"start": start, "end": end, "nutrition": summary, "weight": weight, "training_load_per_day": loads},
            ensure_ascii=False,
        )
    text = f"Nutrition summary for athlete {athlete_id_to_use}, {start} to {end}:\n\n"
    text += format_nutrition_summary(summary) + "\n\n" + format_weight_trend(weight)
    if include_training_load:
        last = sorted(loads.items())[-7:]
        total = sum(loads.values())
        text += (
            "\n\nTraining load per day (Intervals.icu, last 7 days with activities): "
            + (", ".join(f"{d} {v:.0f}" for d, v in last) if last else "none")
            + f"; total in range {total:.0f}"
        )
    return text
