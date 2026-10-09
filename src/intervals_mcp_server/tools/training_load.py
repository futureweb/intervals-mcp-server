"""
Training load MCP tools for Intervals.icu (read-only):

- get_training_load: acute and chronic load, acute:chronic ratio, Foster monotony and
  strain, deload-like weeks, per sport family, with CTL/ATL/form/ramp for context.
- get_load_projection: CTL/ATL/form projected over the planned workouts in the calendar.

The metrics follow the coach metrics proposed by morritter in upstream pull request
mvilanova/intervals-mcp-server#150 and are computed in ``utils.load_metrics``. Only the
Intervals.icu training load is used; device loads (custom fields such as a Garmin training
load) are listed separately on their own scale. Values are statistics with sample sizes and
commonly cited reference ranges, never a verdict.

The data helpers here (activities, wellness and events with a field selection) are shared
with the intensity, durability and coach context tools.
"""

import json
from datetime import date, timedelta
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.athlete import field_assignments
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.utils.custom_fields import ACTIVITY_FIELD, CustomFieldDefs
from intervals_mcp_server.utils.field_policy import aggregate_field, aggregation_policy
from intervals_mcp_server.utils.load_metrics import (
    ATL_DAYS,
    CTL_DAYS,
    DEFINITIONS,
    REFERENCES,
    activity_day,
    activity_load,
    between,
    daily_loads,
    day_range,
    fitness_status,
    iso_week,
    load_metrics,
    model_check,
    num,
    project_fitness,
    rnd,
    sport_breakdown,
    wellness_by_day,
    weekly_rows,
)
from intervals_mcp_server.utils.validation import resolve_athlete_id, validate_date

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

DETAIL_LEVELS = ("compact", "standard", "full")
LOAD_FIELDS = "id,start_date_local,type,name,moving_time,icu_training_load,icu_intensity"
ZONE_FIELDS = "icu_zone_times,icu_hr_zone_times,pace_zone_times"
DURABILITY_FIELDS = (
    "elapsed_time,decoupling,icu_efficiency_factor,icu_variability_index,average_temp,average_heartrate,trainer,gear"
)
FITNESS_FIELDS = "id,ctl,atl,rampRate,ctlLoad,atlLoad"
RACE_CATEGORIES = ("RACE_A", "RACE_B", "RACE_C")
MAX_PROJECTION_DAYS = 180
DEFAULT_PROJECTION_DAYS = 28
NO_VERDICT = (
    "Reference ranges are population heuristics from the cited sources, not individual limits; "
    "no assessment is made."
)


def current_day() -> date:
    """Today's date (one function for all load tools so tests can patch it)."""
    return date.today()


# ---------------------------------------------------------------------------
# Shared data helpers
# ---------------------------------------------------------------------------


def _error(result: Any, what: str) -> str | None:
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching {what}: {result.get('message', 'Unknown error')}"
    return None


async def fetch_activities(
    athlete_id: str, api_key: str | None, start: date, end: date, fields: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Activities from start to end (local days, inclusive) with the given field selection."""
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id}/activities", api_key=api_key,
        params={"oldest": start.isoformat(), "newest": end.isoformat(), "fields": fields},
    )
    error = _error(result, "activities")
    if error:
        return [], error
    return [a for a in result if isinstance(a, dict)] if isinstance(result, list) else [], None


async def fetch_wellness(
    athlete_id: str, api_key: str | None, start: date, end: date, fields: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Wellness records from start to end with the given field selection."""
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id}/wellness", api_key=api_key,
        params={"oldest": start.isoformat(), "newest": end.isoformat(), "fields": fields},
    )
    error = _error(result, "wellness data")
    if error:
        return [], error
    if isinstance(result, dict):
        return [dict(v, id=k) for k, v in result.items() if isinstance(v, dict)], None
    return [w for w in result if isinstance(w, dict)] if isinstance(result, list) else [], None


async def fetch_events(
    athlete_id: str, api_key: str | None, start: date, end: date
) -> tuple[list[dict[str, Any]], str | None]:
    """Calendar events from start to end (all categories)."""
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id}/events", api_key=api_key,
        params={"oldest": start.isoformat(), "newest": end.isoformat()},
    )
    error = _error(result, "events")
    if error:
        return [], error
    return [e for e in result if isinstance(e, dict)] if isinstance(result, list) else [], None


async def device_load_fields(athlete_id: str, api_key: str | None) -> CustomFieldDefs:
    """Custom activity fields that hold a device training load (policy device_load_sum)."""
    defs = (await get_custom_item_index(athlete_id=athlete_id, api_key=api_key)).get(ACTIVITY_FIELD, {})
    overrides = get_config().custom_aggregate_overrides
    return {
        code: definition for code, definition in defs.items()
        if aggregation_policy(definition, overrides.get(code))["policy"] == "device_load_sum"
    }


def resolve_request(athlete_id: str | None, detail_level: str) -> tuple[str, str | None]:
    """Athlete id to use and an error message for a missing athlete or an unknown detail level."""
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return "", error_msg
    if detail_level not in DETAIL_LEVELS:
        return "", f"Error: detail_level must be one of {', '.join(DETAIL_LEVELS)}."
    return athlete_id_to_use, None


def parse_end(end_date: str | None, today: date) -> date | str:
    """The end date (default today) or an error message."""
    if not end_date:
        return today
    try:
        validate_date(end_date)
    except ValueError as exc:
        return f"Error: {exc}"
    return date.fromisoformat(end_date)


def past_end(end_date: str | None) -> tuple[date, date] | str:
    """(today, end) for tools that look back from an end date that must not lie in the future."""
    today = current_day()
    end = parse_end(end_date, today)
    if isinstance(end, str):
        return end
    if end > today:
        return "Error: end_date lies in the future; use get_load_projection for planned load."
    return today, end


def resolve_period(
    start_date: str | None, end_date: str | None, default_days: int, max_days: int
) -> tuple[date, date] | str:
    """(start, end) from optional ISO dates (end default today, start default_days back) or an error."""
    end = parse_end(end_date, current_day())
    if isinstance(end, str):
        return end
    if start_date:
        try:
            validate_date(start_date)
        except ValueError as exc:
            return f"Error: {exc}"
        start = date.fromisoformat(start_date)
    else:
        start = end - timedelta(days=default_days - 1)
    if start > end:
        return "Error: start_date must not be after end_date."
    if (end - start).days + 1 > max_days:
        return f"Error: the period must not exceed {max_days} days."
    return start, end


def wanted_types(sport_types: str | None) -> set[str]:
    """Lower-case activity types of a comma-separated filter (empty = all)."""
    return {t.strip().lower() for t in (sport_types or "").split(",") if t.strip()}


def filter_types(activities: list[dict[str, Any]], wanted: set[str]) -> list[dict[str, Any]]:
    """Activities whose type is in ``wanted`` (all when empty)."""
    return [a for a in activities if not wanted or str(a.get("type") or "").lower() in wanted]


def load_end_for(end_date: str | None, end: date, activities: list[dict[str, Any]]) -> tuple[date, str | None]:
    """Last day of the load windows and a note.

    With the default end date (today) and no activity recorded today yet, the windows end
    yesterday so an untrained morning does not count as a rest day (approach of #150).
    """
    if end_date or any(activity_day(a) == end for a in activities):
        return end, None
    previous = end - timedelta(days=1)
    return previous, (
        f"load windows end {previous.isoformat()}: no activity is recorded today ({end.isoformat()}) yet, so today "
        f"is not counted as a rest day; pass end_date='{end.isoformat()}' to include it"
    )


def fmt(value: Any, digits: int = 0, suffix: str = "", signed: bool = False) -> str:
    """Number as text with fixed digits; 'n/a' for None."""
    if value is None:
        return "n/a"
    return f"{value:{'+' if signed else ''}.{digits}f}{suffix}"


def fitness_text(fitness: dict[str, Any]) -> str:
    """'CTL 65.3 | ATL 74.0 | form -8.7 | ramp +4.9/7 d (2026-10-09, intervals.icu)'."""
    if not fitness.get("available"):
        return f"n/a ({fitness.get('note')})"
    text = (
        f"CTL {fmt(fitness['ctl'], 1)} | ATL {fmt(fitness['atl'], 1)} | form {fmt(fitness['form'], 1, signed=True)} "
        f"({fmt(fitness['form_pct_of_ctl'], 0, ' %', signed=True)} of CTL) | ramp {fmt(fitness['ramp'], 1, signed=True)} per 7 d "
        f"({fitness['date']}, {fitness['source']})"
    )
    if fitness.get("note"):
        text += f"; {fitness['note']}"
    return text


# ---------------------------------------------------------------------------
# get_training_load
# ---------------------------------------------------------------------------


def _device_loads(
    activities: list[dict[str, Any]], defs: CustomFieldDefs, assigned: dict[str, set[str] | None],
    windows: dict[str, tuple[date, date]],
) -> list[dict[str, Any]]:
    overrides = get_config().custom_aggregate_overrides
    rows = []
    for code, definition in defs.items():
        row: dict[str, Any] = {"code": code, "name": definition.get("name"), "units": definition.get("units")}
        any_value = False
        for label, (start, end) in windows.items():
            agg = aggregate_field(between(activities, start, end), code, definition, overrides.get(code), assigned)
            row[label] = {"sum": rnd(agg["sum"], 0), "n": agg["n"]} if agg else {"sum": None, "n": 0}
            if agg and agg.get("values_from_unassigned_sports"):
                row[label]["from_unassigned_sports"] = agg["values_from_unassigned_sports"]
                row[label]["unassigned_sports"] = agg.get("unassigned_sports") or []
            any_value = any_value or bool(agg)
        if any_value:
            rows.append(row)
    return rows


def _window_line(label: str, window: dict[str, Any]) -> str:
    extra = f", {window['sessions_without_load']} without load" if window["sessions_without_load"] else ""
    weekly = f" = {fmt(window['weekly_mean'])}/week" if "weekly_mean" in window else ""
    return (
        f"{label} {window['days']} d ({window['start']} to {window['end']}): load {fmt(window['load'])} in "
        f"{window['sessions']} sessions{extra} on {window['days_with_activity']} days ({window['zero_load_days']} days "
        f"with load 0), {fmt(window['daily_mean'], 1)}/day{weekly}"
    )


def _sport_line(family: str, metrics: dict[str, Any]) -> str:
    week = metrics["week"]
    mono = fmt(week["monotony"], 2) if week["monotony"] is not None else f"n/a, {week['monotony_note']}"
    return (
        f"  {family}: acute {fmt(metrics['acute']['load'])} / chronic {fmt(metrics['chronic']['load'])} "
        f"({metrics['chronic']['sessions']} sessions), ratio {fmt(metrics['acwr'], 2)}, 7-day monotony {mono} "
        f"({week['days_with_load']} of 7 days with load)"
    )


def _week_line(row: dict[str, Any]) -> str:
    partial = "" if row["complete"] else f", partial {row['days']} d"
    sports = ", ".join(f"{f} {fmt(v)}" for f, v in row["by_sport"].items()) or "none"
    ratio = (
        f" | {fmt(row['vs_chronic_weekly_mean_pct'])} % of the trailing weekly mean"
        + (" (deload-like)" if row["deload_like"] else "")
        if row["vs_chronic_weekly_mean_pct"] is not None else ""
    )
    return (
        f"  {row['week']} ({row['start']} to {row['end']}{partial}): load {fmt(row['load'])} ({sports}), "
        f"{row['sessions']} sessions, {fmt(row['hours'], 1)} h, {row['rest_days']} rest days | monotony "
        f"{fmt(row['monotony'], 2)}, strain {fmt(row['strain'])}{ratio}"
    )


def _load_text(payload: dict[str, Any], detail_level: str) -> str:  # pylint: disable=too-many-locals
    total = payload["total"]
    week = total["week"]
    lines = [
        f"Training load for athlete {payload['athlete_id']}, windows ending {payload['load_end']} "
        "(Intervals.icu training load; device loads are listed separately and never added).",
    ]
    if payload["load_end_note"]:
        lines.append(f"Note: {payload['load_end_note']}.")
    lines += [
        _window_line("Acute", total["acute"]),
        _window_line("Chronic", total["chronic"]),
        f"Acute:chronic ratio {fmt(total['acwr'], 2)} (daily means, coupled): "
        + (f"{total['acwr_position']} the commonly cited range 0.8-1.3 [1]" if total["acwr_position"] else "n/a (no chronic load)")
        + (f"; small sample: only {total['chronic']['days_with_load']} days with load in the chronic window" if total["acwr_small_sample"] else ""),
        f"Last 7 days ({week['start']} to {week['end']}): load {fmt(week['load'])}, monotony {fmt(week['monotony'], 2)}"
        + (f" ({week['monotony_note']})" if week["monotony_note"] else "")
        + f" (above 2.0 is commonly cited as high [2]), strain {fmt(week['strain'])}; "
        f"{fmt(week['vs_chronic_weekly_mean_pct'])} % of the chronic weekly mean"
        + (" - deload-like (<= 80 %)" if week["deload_like"] else ""),
        f"Fitness: {fitness_text(payload['fitness'])}",
    ]
    sports = payload["sports"]
    if sports["by_sport"]:
        lines.append(f"By sport family (primary: {sports['primary_sport'] or 'n/a'}):")
        shown = list(sports["by_sport"].items())
        if detail_level == "compact":
            shown = shown[:3]
        lines.extend(_sport_line(family, metrics) for family, metrics in shown)
        if sports["multi_sport"]:
            lines.append(
                "  With several sports the total monotony includes cross-training: a steady low load from other sports "
                f"raises it. Primary sport ({sports['primary_sport']}) monotony alone: {fmt(sports['primary_monotony'], 2)}."
            )
    if detail_level != "compact":
        lines.append("ISO weeks (rest days = days without any activity; % of the weekly mean of the trailing chronic window, complete weeks only):")
        lines.extend(_week_line(row) for row in payload["weeks"])
        for row in payload["device_loads"]:
            units = f" {row['units']}" if row.get("units") else ""
            unassigned = row["chronic"].get("from_unassigned_sports")
            note = (
                f"; incl. {unassigned} value(s) from sports without field assignment ({', '.join(row['chronic'].get('unassigned_sports') or [])})"
                if unassigned else ""
            )
            lines.append(
                f"Device load {row['name']} [{row['code']}] (own scale, not comparable with or added to the Intervals.icu load): "
                f"acute {fmt(row['acute']['sum'])}{units} (n {row['acute']['n']}), chronic {fmt(row['chronic']['sum'])}{units} "
                f"(n {row['chronic']['n']}); activities without a value are not counted{note}"
            )
    if detail_level == "full":
        lines.append("Daily load (chronic window): " + ", ".join(f"{d['date'][5:]} {fmt(d['load'])}" for d in payload["daily"]))
        lines.append("Definitions: " + "; ".join(f"{k}: {v}" for k, v in DEFINITIONS.items()))
    without = total["chronic"]["sessions_without_load"]
    if without:
        lines.append(
            f"Coverage: {without} session(s) in the chronic window have no Intervals.icu load; they do not add to their "
            "days (as in Intervals.icu's fitness model)."
        )
    lines.append(f"[1] {REFERENCES['acwr']['source']}; [2] {REFERENCES['monotony']['source']}. {NO_VERDICT}")
    return "\n".join(lines)


@tool("read")
async def get_training_load(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    end_date: str | None = None,
    acute_days: int = 7,
    chronic_days: int = 28,
    weeks: int = 4,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
    detail_level: str = "standard",
) -> str:
    """Acute and chronic training load, acute:chronic ratio, monotony, strain and deload weeks (read-only)

    From the Intervals.icu training load of every activity (rest days count as 0): the acute
    (default 7 days) and chronic (default 28 days) load with sessions and days, the
    acute:chronic ratio (daily means, coupled: the acute window is part of the chronic one),
    Foster's monotony (mean / SD of the last 7 daily loads, rest days included; undefined
    when the SD is 0) and strain (7-day load x monotony), and the 7-day load as a percentage
    of the chronic weekly mean (deload-like at <= 80 %). The same per sport family, with the
    primary sport (highest 7-day load) and its own monotony, because a steady low load from
    cross-training raises the total monotony. ISO weeks with load per sport, rest days,
    weekly monotony and strain and the share of the trailing weekly mean. CTL, ATL, form and
    ramp from Intervals.icu at the end date for context (today's values are recomputed
    without planned workouts that are not done yet). Device loads (custom fields such as a
    Garmin training load) are summed separately on their own scale. Reference ranges (ACWR
    0.8-1.3, monotony 2.0) are shown with their sources as context only; small samples are
    flagged; no verdict is made. With the default end date and no activity recorded today
    yet, the windows end yesterday. Metric set after morritter's upstream PR #150.

    Args:
        end_date: Last day YYYY-MM-DD (optional, default today; not in the future, see get_load_projection)
        acute_days: Acute window in days, 3-28 (optional, default 7)
        chronic_days: Chronic window in days, 14-120 and longer than acute_days (optional, default 28)
        weeks: Number of ISO weeks in the weekly table, 1-26 (optional, default 4)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
        detail_level: "compact" (windows, ratio, monotony, fitness, top sports), "standard" (default, plus
            all sports, the weekly table and device loads) or "full" (plus the daily loads and definitions;
            JSON includes the daily loads only at full)
    """
    athlete_id_to_use, error_msg = resolve_request(athlete_id, detail_level)
    if error_msg:
        return error_msg
    if not 3 <= acute_days <= 28 or not 14 <= chronic_days <= 120 or acute_days >= chronic_days:
        return "Error: acute_days must be 3-28 and chronic_days 14-120, with acute_days < chronic_days."
    if not 1 <= weeks <= 26:
        return "Error: weeks must be between 1 and 26."
    days = past_end(end_date)
    if isinstance(days, str):
        return days
    today, end = days

    # One extra week: the windows may end yesterday (see load_end_for), which can move the weeks back.
    first_monday = end - timedelta(days=end.weekday() + 7 * weeks)
    fetch_start = min(end - timedelta(days=chronic_days + 1), first_monday - timedelta(days=chronic_days))
    device_defs = await device_load_fields(athlete_id_to_use, api_key)
    fields = LOAD_FIELDS + "".join(f",{code}" for code in device_defs)
    activities, error = await fetch_activities(athlete_id_to_use, api_key, fetch_start, end, fields)
    if error:
        return error
    wellness_list, error = await fetch_wellness(athlete_id_to_use, api_key, end - timedelta(days=14), end, FITNESS_FIELDS)
    if error:
        return error
    load_end, note = load_end_for(end_date, end, activities)
    wellness = wellness_by_day(wellness_list)
    completed_today = sum(activity_load(a) or 0.0 for a in between(activities, end, end))
    total = load_metrics(activities, load_end, acute_days, chronic_days)
    chronic_start = load_end - timedelta(days=chronic_days - 1)
    acute_start = load_end - timedelta(days=acute_days - 1)
    assigned = await field_assignments(
        athlete_id_to_use, api_key, device_defs, {str(a.get("type") or "") for a in activities} - {""}
    ) if device_defs else {}
    daily = daily_loads(activities, chronic_start, load_end)
    payload: dict[str, Any] = {
        "athlete_id": athlete_id_to_use,
        "end": end.isoformat(),
        "load_end": load_end.isoformat(),
        "load_end_note": note,
        "total": total,
        "sports": sport_breakdown(activities, load_end, acute_days, chronic_days),
        "fitness": fitness_status(wellness, end, completed_today, today),
        "weeks": weekly_rows(activities, load_end, weeks, chronic_days),
        "device_loads": _device_loads(
            activities, device_defs, assigned, {"acute": (acute_start, load_end), "chronic": (chronic_start, load_end)}
        ),
        "references": REFERENCES,
        "definitions": DEFINITIONS,
    }
    if detail_level == "full":
        counts = {day: len(between(activities, day, day)) for day in daily}
        payload["daily"] = [
            {"date": day.isoformat(), "load": rnd(load, 0), "sessions": counts[day]} for day, load in daily.items()
        ]
    if output_format.strip().lower() == "json":
        return json.dumps(payload, ensure_ascii=False)
    return _load_text(payload, detail_level)


# ---------------------------------------------------------------------------
# get_load_projection
# ---------------------------------------------------------------------------


def _planned(events: list[dict[str, Any]], today: date, end: date) -> tuple[dict[date, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Unpaired WORKOUT events per day (today .. end) and the races."""
    workouts: dict[date, list[dict[str, Any]]] = {}
    races = []
    for event in events:
        day = activity_day(event)
        if day is None or not today <= day <= end:
            continue
        if event.get("category") == "WORKOUT" and not event.get("paired_activity_id"):
            workouts.setdefault(day, []).append(event)
        elif event.get("category") in RACE_CATEGORIES:
            races.append(event)
    return workouts, races


def _projection_weeks(
    rows: list[dict[str, Any]], workouts: dict[date, list[dict[str, Any]]], intervals_icu: dict[date, dict[str, Any]]
) -> list[dict[str, Any]]:
    weeks: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        weeks.setdefault(iso_week(date.fromisoformat(row["date"])), []).append(row)
    out = []
    for label, days in weeks.items():
        planned = [e for d in days for e in workouts.get(date.fromisoformat(d["date"]), [])]
        last = days[-1]
        lowest = min(days, key=lambda r: r["form"])
        icu = intervals_icu.get(date.fromisoformat(last["date"]), {})
        out.append({
            "week": label, "start": days[0]["date"], "end": last["date"], "days": len(days),
            "load": rnd(sum(d["load"] or 0 for d in days), 0),
            "planned_sessions": len(planned),
            "planned_without_load": sum(1 for e in planned if activity_load(e) is None),
            "ctl": last["ctl"], "atl": last["atl"], "form": last["form"], "ramp": last["ramp"],
            "lowest_form": lowest["form"], "lowest_form_date": lowest["date"],
            "intervals_icu_ctl": rnd(num(icu.get("ctl")), 1), "intervals_icu_atl": rnd(num(icu.get("atl")), 1),
        })
    return out


def projection_basis(planned: dict[str, Any]) -> dict[str, Any]:
    """Whether a projection contains planned training: 'planned_load', 'no_planned_load' or 'no_planned_workouts'."""
    if not planned["sessions"]:
        return {"kind": "no_planned_workouts", "label": "PROJECTION WITHOUT PLANNED TRAINING (no planned workouts in the calendar)"}
    if not planned["with_load"]:
        return {
            "kind": "no_planned_load",
            "label": f"PROJECTION WITHOUT PLANNED LOAD ({planned['sessions']} planned workouts, none with a planned load)",
        }
    return {"kind": "planned_load", "label": "PROJECTION"}


def _projection_text(payload: dict[str, Any], detail_level: str) -> str:
    start, today_info, plan = payload["start"], payload["today"], payload["planned"]
    end_row = payload["days"][-1] if payload["days"] else None
    basis = payload["projection_basis"]
    assumption = (
        "assumes every planned workout is done with its planned Intervals.icu load and nothing else"
        if basis["kind"] == "planned_load" else "only the decay of CTL and ATL without any training after today's completed load"
    )
    lines = [
        f"{basis['label']}: load projection for athlete {payload['athlete_id']}, {payload['from']} to {payload['to']} "
        f"({assumption}; model CTL {payload['ctl_days']} d / ATL {payload['atl_days']} d).",
        f"Start (end of {start['date']}, Intervals.icu): CTL {fmt(start['ctl'], 1)} | ATL {fmt(start['atl'], 1)} | "
        f"form {fmt(start['form'], 1, signed=True)}",
        f"Today {today_info['date']}: completed load {fmt(today_info['completed_load'])} + planned, not yet done "
        f"{fmt(today_info['planned_load'])}",
        (f"Planned workouts: {plan['sessions']} ({plan['with_load']} with a planned load, sum {fmt(plan['load'])}); "
         + (f"{plan['without_load']} without a planned load are not included, so the projection understates those days"
            if plan["without_load"] else "all have a planned load")
         if plan["sessions"] else "Planned workouts: none in the calendar, so the projection shows the decay without training"),
    ]
    if end_row:
        icu = payload["intervals_icu_end"]
        lines.append(
            f"End {end_row['date']}: CTL {fmt(end_row['ctl'], 1)} | ATL {fmt(end_row['atl'], 1)} | form "
            f"{fmt(end_row['form'], 1, signed=True)}"
            + (f" (Intervals.icu's own projection: CTL {fmt(icu['ctl'], 1)}, ATL {fmt(icu['atl'], 1)})" if icu else "")
        )
        lowest = payload["lowest_form"]
        lines.append(f"Lowest projected form {fmt(lowest['form'], 1, signed=True)} on {lowest['date']}; highest 7-day ramp "
                     f"{fmt(payload['highest_ramp']['ramp'], 1, signed=True)} on {payload['highest_ramp']['date']}")
    if detail_level != "compact":
        lines.append("Weeks (values at the last day of the week):")
        for week in payload["weeks"]:
            missing = f", {week['planned_without_load']} without load" if week["planned_without_load"] else ""
            lines.append(
                f"  {week['week']} ({week['start']} to {week['end']}): load {fmt(week['load'])} ({week['planned_sessions']} planned"
                f"{missing}) | CTL {fmt(week['ctl'], 1)} | ATL {fmt(week['atl'], 1)} | form {fmt(week['form'], 1, signed=True)} "
                f"(lowest {fmt(week['lowest_form'], 1, signed=True)} on {week['lowest_form_date']}) | ramp {fmt(week['ramp'], 1, signed=True)}"
            )
    if payload["races"]:
        lines.append("Races: " + "; ".join(
            f"{r['date']} {r['category']} '{r['name']}': CTL {fmt(r['ctl'], 1)}, ATL {fmt(r['atl'], 1)}, form {fmt(r['form'], 1, signed=True)}"
            for r in payload["races"]
        ))
    if detail_level == "full":
        lines.append("Days: " + ", ".join(
            f"{d['date'][5:]} load {fmt(d['load'])} CTL {fmt(d['ctl'], 1)} form {fmt(d['form'], 1, signed=True)}" for d in payload["days"]
        ))
    check = payload["model_check"]
    if check:
        lines.append(
            f"Model check: replaying the last {check['days']} days of Intervals.icu loads reproduces the stored CTL within "
            f"{fmt(check['max_ctl_diff'], 2)} and ATL within {fmt(check['max_atl_diff'], 2)}."
        )
    else:
        lines.append("Model check: not possible (wellness records of the last 14 days incomplete).")
    lines.append("A projection is arithmetic on the plan, not a forecast; no assessment is made.")
    return "\n".join(lines)


@tool("read")
async def get_load_projection(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    end_date: str | None = None,
    ctl_days: int = CTL_DAYS,
    atl_days: int = ATL_DAYS,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
    detail_level: str = "standard",
) -> str:
    """Project CTL, ATL and form over the planned workouts in the calendar (read-only, projection)

    Starts from the Intervals.icu CTL and ATL at the end of yesterday and applies the
    exponential fitness model day by day (CTL time constant 42 days, ATL 7 days, as in
    Intervals.icu): today counts the load already completed plus planned workouts of today
    that are not done yet, the following days the planned Intervals.icu load of the WORKOUT
    events. Planned workouts without a planned load are not counted and are reported, so
    the projection then understates those days; nothing else (unplanned training) is
    assumed. Reports the values at the end date, the lowest form and highest 7-day ramp,
    per ISO week (load, sessions, CTL/ATL/form at the end of the week, lowest form, ramp),
    projected values on race days (RACE_A/B/C) and, for comparison, Intervals.icu's own
    projection from the wellness records. Without planned workouts (or without any planned
    load) the header says so at every detail level ("PROJECTION WITHOUT PLANNED TRAINING";
    JSON projection_basis). A model check replays the last 14 days of
    Intervals.icu loads to show that the time constants match. Clearly a projection, not a
    forecast; no verdict.

    Args:
        end_date: Last projected day YYYY-MM-DD (optional, default 28 days ahead, at most 180 days ahead)
        ctl_days: CTL time constant in days (optional, default 42)
        atl_days: ATL time constant in days (optional, default 7)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
        detail_level: "compact" (start, end, extremes, races), "standard" (default, plus weeks) or "full"
            (plus every day; JSON includes the days at every level)
    """
    athlete_id_to_use, error_msg = resolve_request(athlete_id, detail_level)
    if error_msg:
        return error_msg
    if not 1 <= atl_days < ctl_days <= 365:
        return "Error: ctl_days and atl_days must satisfy 1 <= atl_days < ctl_days <= 365."
    today = current_day()
    end = parse_end(end_date, today + timedelta(days=DEFAULT_PROJECTION_DAYS))
    if isinstance(end, str):
        return end
    if not today < end <= today + timedelta(days=MAX_PROJECTION_DAYS):
        return f"Error: end_date must lie after today and at most {MAX_PROJECTION_DAYS} days ahead."

    wellness_list, error = await fetch_wellness(athlete_id_to_use, api_key, today - timedelta(days=21), end, FITNESS_FIELDS)
    if error:
        return error
    events, error = await fetch_events(athlete_id_to_use, api_key, today, end)
    if error:
        return error
    activities, error = await fetch_activities(athlete_id_to_use, api_key, today, today, LOAD_FIELDS)
    if error:
        return error
    wellness = wellness_by_day(wellness_list)
    base_day = next(
        (d for d in (today - timedelta(days=o) for o in range(1, 15))
         if num(wellness.get(d, {}).get("ctl")) is not None and num(wellness.get(d, {}).get("atl")) is not None),
        None,
    )
    if base_day is None:
        return "No CTL/ATL found in the wellness records of the last 14 days; nothing to project from."
    workouts, races = _planned(events, today, end)
    loads: dict[date, float] = {}
    for day in day_range(base_day + timedelta(days=1), today - timedelta(days=1)):
        loads[day] = num(wellness.get(day, {}).get("ctlLoad")) or 0.0
    completed = sum(activity_load(a) or 0.0 for a in activities if activity_day(a) == today)
    planned_all = [e for day_events in workouts.values() for e in day_events]
    for day, day_events in workouts.items():
        loads[day] = loads.get(day, 0.0) + sum(activity_load(e) or 0.0 for e in day_events)
    loads[today] = loads.get(today, 0.0) + completed
    history = {d: v for d, rec in wellness.items() if d <= base_day and (v := num(rec.get("ctl"))) is not None}
    base = wellness[base_day]
    start_ctl, start_atl = num(base.get("ctl")) or 0.0, num(base.get("atl")) or 0.0
    rows = project_fitness(start_ctl, start_atl, base_day, loads, end, history, ctl_days, atl_days)
    projected = [r for r in rows if r["date"] >= today.isoformat()]
    by_date = {r["date"]: r for r in rows}
    end_icu = wellness.get(end, {})
    payload: dict[str, Any] = {
        "athlete_id": athlete_id_to_use, "from": today.isoformat(), "to": end.isoformat(),
        "ctl_days": ctl_days, "atl_days": atl_days, "mode": "projection",
        "start": {"date": base_day.isoformat(), "ctl": rnd(start_ctl, 1), "atl": rnd(start_atl, 1), "form": rnd(start_ctl - start_atl, 1)},
        "today": {"date": today.isoformat(), "completed_load": rnd(completed, 0),
                  "planned_load": rnd(sum(activity_load(e) or 0.0 for e in workouts.get(today, [])), 0)},
        "planned": {"sessions": len(planned_all), "with_load": sum(1 for e in planned_all if activity_load(e) is not None),
                    "without_load": sum(1 for e in planned_all if activity_load(e) is None),
                    "load": rnd(sum(activity_load(e) or 0.0 for e in planned_all), 0)},
        "intervals_icu_end": ({"ctl": rnd(num(end_icu.get("ctl")), 1), "atl": rnd(num(end_icu.get("atl")), 1)}
                              if num(end_icu.get("ctl")) is not None else None),
        "lowest_form": min(({"date": r["date"], "form": r["form"]} for r in projected), key=lambda r: r["form"]),
        "highest_ramp": max(({"date": r["date"], "ramp": r["ramp"]} for r in projected if r["ramp"] is not None),
                            key=lambda r: r["ramp"], default={"date": None, "ramp": None}),
        "weeks": _projection_weeks(projected, workouts, wellness),
        "races": [
            {"date": str(r.get("start_date_local"))[:10], "category": r.get("category"), "name": r.get("name"),
             **{k: by_date.get(str(r.get("start_date_local"))[:10], {}).get(k) for k in ("ctl", "atl", "form")}}
            for r in sorted(races, key=lambda r: str(r.get("start_date_local")))
        ],
        "model_check": model_check(wellness, today - timedelta(days=1), 14, ctl_days, atl_days),
        "days": projected,
    }
    payload["projection_basis"] = projection_basis(payload["planned"])
    if output_format.strip().lower() == "json":
        return json.dumps(payload, ensure_ascii=False)
    return _projection_text(payload, detail_level)
