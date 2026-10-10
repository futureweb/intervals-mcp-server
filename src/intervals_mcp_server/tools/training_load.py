"""
Training load MCP tools for Intervals.icu (read-only):

- get_training_load: acute and chronic load, acute:chronic ratio, Foster monotony and
  strain, deload-like weeks, per sport family, with CTL/ATL/form/ramp for context.
- get_load_projection: CTL/ATL/form projected over the planned workouts in the calendar, or
  simulated for a what-if plan (sessions or weekly templates that are not in the calendar),
  with the form at a target day, a search for the load before it and plan statistics
  (``utils.plan_simulation``).

The metrics follow the coach metrics proposed by morritter in upstream pull request
mvilanova/intervals-mcp-server#150 and are computed in ``utils.load_metrics``. Only the
Intervals.icu training load is used; device loads (custom fields such as a Garmin training
load) are listed separately on their own scale. Values are statistics with sample sizes and
commonly cited reference ranges, never a verdict.

The data helpers here (activities, wellness and events with a field selection) are shared
with the intensity, durability and coach context tools.
"""

# pylint: disable=too-many-lines

import json
from datetime import date, timedelta
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.athlete import field_assignments
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.utils.dates import athlete_today
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
    load_metrics,
    model_check,
    num,
    project_fitness,
    rnd,
    sport_breakdown,
    wellness_by_day,
    weekly_rows,
)
from intervals_mcp_server.utils.plan_simulation import (
    IDENTICAL_LOADS,
    LOAD_FORMULA,
    PLAN_REFERENCES,
    ScenarioError,
    add_session_loads,
    apply_calendar,
    calendar_sessions,
    completed_sessions,
    flagged,
    parse_form_range,
    parse_scenario,
    plan_weeks,
    recent_weeks,
    session_view,
    solve_target,
    state_at_start,
)
from intervals_mcp_server.utils.validation import resolve_athlete_id, validate_date

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

DETAIL_LEVELS = ("compact", "standard", "full")
LOAD_FIELDS = "id,start_date_local,type,name,moving_time,icu_training_load,icu_intensity"
ZONE_FIELDS = "icu_zone_times,icu_hr_zone_times,pace_zone_times,gap_zone_times,use_gap_zone_times"
DURABILITY_FIELDS = (
    "elapsed_time,decoupling,icu_efficiency_factor,icu_variability_index,average_temp,average_heartrate,trainer,gear,power_meter,"
    "average_weather_temp,average_feels_like"
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
    """Today in the athlete's time zone (one function for all load tools so tests can patch it)."""
    return athlete_today()


# ---------------------------------------------------------------------------
# Shared data helpers
# ---------------------------------------------------------------------------


def _error(result: Any, what: str) -> str | None:
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching {what}: {result.get('message', 'Unknown error')}"
    return None


async def fetch_activities(
    athlete_id: str, start: date, end: date, fields: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Activities from start to end (local days, inclusive) with the given field selection."""
    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id)}/activities",
        params={"oldest": start.isoformat(), "newest": end.isoformat(), "fields": fields},
    )
    error = _error(result, "activities")
    if error:
        return [], error
    return [a for a in result if isinstance(a, dict)] if isinstance(result, list) else [], None


async def fetch_wellness(
    athlete_id: str, start: date, end: date, fields: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Wellness records from start to end with the given field selection (None: all fields)."""
    params = {"oldest": start.isoformat(), "newest": end.isoformat()}
    if fields is not None:
        params["fields"] = fields
    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id)}/wellness", params=params
    )
    error = _error(result, "wellness data")
    if error:
        return [], error
    if isinstance(result, dict):
        return [dict(v, id=k) for k, v in result.items() if isinstance(v, dict)], None
    return [w for w in result if isinstance(w, dict)] if isinstance(result, list) else [], None


async def fetch_events(
    athlete_id: str, start: date, end: date
) -> tuple[list[dict[str, Any]], str | None]:
    """Calendar events from start to end (all categories)."""
    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id)}/events",
        params={"oldest": start.isoformat(), "newest": end.isoformat()},
    )
    error = _error(result, "events")
    if error:
        return [], error
    return [e for e in result if isinstance(e, dict)] if isinstance(result, list) else [], None


async def device_load_fields(athlete_id: str) -> CustomFieldDefs:
    """Custom activity fields that hold a device training load (policy device_load_sum)."""
    defs = (await get_custom_item_index(athlete_id=athlete_id)).get(ACTIVITY_FIELD, {})
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
    device_defs = await device_load_fields(athlete_id_to_use)
    fields = LOAD_FIELDS + "".join(f",{code}" for code in device_defs)
    activities, error = await fetch_activities(athlete_id_to_use, fetch_start, end, fields)
    if error:
        return error
    wellness_list, error = await fetch_wellness(athlete_id_to_use, end - timedelta(days=14), end, FITNESS_FIELDS)
    if error:
        return error
    load_end, note = load_end_for(end_date, end, activities)
    wellness = wellness_by_day(wellness_list)
    completed_today = sum(activity_load(a) or 0.0 for a in between(activities, end, end))
    total = load_metrics(activities, load_end, acute_days, chronic_days)
    chronic_start = load_end - timedelta(days=chronic_days - 1)
    acute_start = load_end - timedelta(days=acute_days - 1)
    assigned = await field_assignments(
        athlete_id_to_use, device_defs, {str(a.get("type") or "") for a in activities} - {""}
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


def _planned_counts(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    loads = [s["load"] for s in sessions]
    return {"sessions": len(loads), "with_load": sum(1 for v in loads if v is not None),
            "without_load": sum(1 for v in loads if v is None), "load": rnd(sum(v or 0.0 for v in loads), 0)}


VALUE_BASIS = {
    "days": "end of day: includes that day's load, as Intervals.icu's wellness values (days, weeks, end, lowest form)",
    "races_and_target": "start of day: the end of the day before, so the race's own load does not count",
}
NO_STATE = dict.fromkeys(("ctl", "atl", "form", "form_pct_of_ctl"))


def _by_date(rows: list[dict[str, Any]], base: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Projection rows by ISO date, with the start values on the base day (the start of a race today)."""
    return {base["date"]: base, **{r["date"]: r for r in rows}}


def _variant(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    rows: list[dict[str, Any]], base: dict[str, Any], today: date, loads: dict[date, float],
    sessions: list[dict[str, Any]], races: list[dict[str, Any]], race_minutes: float | None,
) -> dict[str, Any]:
    """End values, extremes, plan weeks, race days and days of one projection (calendar plan or scenario).

    Days, weeks, the end and the lowest form are end-of-day values (the day's load included);
    race days are reported at the start of the day, like the target day.
    """
    projected = [r for r in rows if r["date"] >= today.isoformat()]
    by_date = _by_date(rows, base)
    last = projected[-1]
    return {
        "end": {**{k: last[k] for k in ("date", "ctl", "atl", "form")},
                "form_pct_of_ctl": rnd(last["form"] / last["ctl"] * 100, 0) if last["ctl"] else None},
        "lowest_form": min(({"date": r["date"], "form": r["form"]} for r in projected), key=lambda r: r["form"]),
        "highest_ramp": max(({"date": r["date"], "ramp": r["ramp"]} for r in projected if r["ramp"] is not None),
                            key=lambda r: r["ramp"], default={"date": None, "ramp": None}),
        "weeks": plan_weeks(projected, loads, sessions, race_minutes),
        "races": [
            {"date": day.isoformat(), "category": r.get("category"), "name": r.get("name"), "basis": "start_of_day",
             **(state_at_start(by_date, day) or NO_STATE)}
            for r in sorted(races, key=lambda r: str(r.get("start_date_local"))) if (day := activity_day(r)) is not None
        ],
        "days": projected,
    }


def _calendar_week_extras(
    weeks: list[dict[str, Any]], workouts: dict[date, list[dict[str, Any]]], intervals_icu: dict[date, dict[str, Any]]
) -> None:
    """Planned workouts per week and Intervals.icu's own CTL/ATL at the week's end (calendar plan only)."""
    for week in weeks:
        days = day_range(date.fromisoformat(week["start"]), date.fromisoformat(week["end"]))
        planned = [e for d in days for e in workouts.get(d, [])]
        icu = intervals_icu.get(days[-1], {})
        week.update({
            "planned_sessions": len(planned), "planned_without_load": sum(1 for e in planned if activity_load(e) is None),
            "intervals_icu_ctl": rnd(num(icu.get("ctl")), 1), "intervals_icu_atl": rnd(num(icu.get("atl")), 1),
        })


def _what_if_inputs(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    scenario: Any, target_date: str | None, target_form: Any, taper_days: int, today: date, last: date
) -> dict[str, Any] | str:
    """Parsed scenario, target day and form range, or an error message."""
    if not 1 <= taper_days <= 42:
        return "Error: taper_days must be between 1 and 42."
    try:
        parsed = None if scenario is None or (isinstance(scenario, str) and not scenario.strip()) else parse_scenario(scenario, today, last)
        absent = target_form is None or (isinstance(target_form, str) and not target_form.strip())
        form_range = None if absent else parse_form_range(target_form)
    except ScenarioError as exc:
        return f"Error: {exc}"
    target = None
    if target_date:
        try:
            validate_date(target_date)
        except ValueError as exc:
            return f"Error: {exc}"
        target = date.fromisoformat(target_date)
        if not today < target <= last:
            return f"Error: target_date must lie after today and not after {last.isoformat()}."
    return {"scenario": parsed, "form_range": form_range, "target": target,
            "active": parsed is not None or form_range is not None or target is not None}


def _target_event(events: list[dict[str, Any]], target: date | None, today: date, last: date) -> dict[str, Any] | None:
    """The target day: the given date (with a race event on it, if any) or the next RACE_A event."""
    races = sorted(
        ((d, e) for e in events if e.get("category") in RACE_CATEGORIES and (d := activity_day(e)) is not None and today < d <= last),
        key=lambda item: item[0],
    )
    if target is not None:
        event = next((e for d, e in races if d == target), None)
        source = "target_date"
    else:
        found = next(((d, e) for d, e in races if e.get("category") == "RACE_A"), None)
        if found is None:
            return None
        target, event = found
        source = "next_race_a"
    secs = num(event.get("moving_time")) if event else None
    return {"date": target, "source": source, "event": event.get("name") if event else None,
            "category": event.get("category") if event else None, "event_minutes": secs / 60 if secs else None}


def _in_range(state: dict[str, Any] | None, form_range: tuple[float, float, str]) -> bool | None:
    low, high, unit = form_range
    value = (state or {}).get("form_pct_of_ctl" if unit == "pct" else "form")
    return None if value is None else low <= value <= high


IGNORED_CATEGORIES = ("SICK", "INJURED", "HOLIDAY", "FITNESS_DAYS", "SET_FITNESS")


def _ignored_entries(events: list[dict[str, Any]], today: date, end: date) -> str | None:
    """Calendar entries in the period that Intervals.icu's fitness chart may use but this model ignores."""
    found = sorted(
        (str(e.get("start_date_local"))[:10], str(e.get("category"))) for e in events
        if e.get("category") in IGNORED_CATEGORIES and (d := activity_day(e)) is not None and today <= d <= end
    )
    if not found:
        return None
    return ("calendar entries in the period that the model ignores (no load change, constant time constants): "
            + ", ".join(f"{category} {day}" for day, category in found))


def _assumptions(
    ctl_days: int, atl_days: int, scenario: dict[str, Any] | None, target: dict[str, Any] | None, ignored: str | None
) -> list[str]:
    items = [
        f"model: CTL and ATL are exponentially weighted averages of the daily Intervals.icu load with constant time "
        f"constants of {ctl_days} and {atl_days} days",
        "every listed session is done on its day with exactly its load and nothing else is trained; no illness, travel "
        "or missed sessions",
    ]
    if scenario is not None:
        mode = scenario["calendar"]
        items.append({
            "add": "the scenario is added on top of the planned workouts in the calendar",
            "replace": "planned workouts from the first to the last scenario day are left out (template weeks count "
            "completely), the others stay",
            "none": "no planned workout of the calendar is used",
        }[mode])
        if any(s["estimated"] for s in scenario["sessions"]):
            items.append(f"estimated loads: {LOAD_FORMULA} (the TSS definition for a session at intensity factor IF; "
                         "for a planned IF or HR/pace-based sports an estimate)")
        if scenario["templates"]:
            items.append("weekly templates split the weekly load and hours equally over the session days, or give the "
                         "long day its long_session_share and split the rest equally")
    if target is not None:
        items.append("target values are at the start of the target day (end of the day before), so the target day's "
                     "own load does not count")
    if ignored:
        items.append(ignored)
    return items


def _comparison(baseline: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    end_b, end_s = baseline["end"], scenario["end"]
    return {
        "end": {"date": end_s["date"], "baseline": end_b, "scenario": end_s,
                "difference": {k: rnd(end_s[k] - end_b[k], 1) for k in ("ctl", "atl", "form")}},
        "weeks": [
            {"week": wb["week"], **{f"{label}_{k}": w[k] for label, w in (("baseline", wb), ("scenario", ws))
                                    for k in ("load", "ctl", "form", "ramp")}}
            for wb, ws in zip(baseline["weeks"], scenario["weeks"], strict=True)
        ],
    }


def _simulation_section(
    parsed: dict[str, Any], kept: list[dict[str, Any]], dropped: list[dict[str, Any]], simulated: dict[str, Any]
) -> dict[str, Any]:
    """The scenario part of the payload: inputs as understood, the calendar mode's effect and the projection."""
    proposed = parsed["sessions"]
    return {
        "label": "SIMULATION (what-if, nothing is written to the calendar)",
        "calendar": parsed["calendar"], "span": [d.isoformat() for d in parsed["span"]],
        "calendar_dropped": {"sessions": len(dropped), "load": rnd(sum(s["load"] or 0.0 for s in dropped), 0)},
        "templates": parsed["templates"],
        "proposed": {"sessions": sum(1 for s in proposed if s["source"] == "proposed"),
                     "template_sessions": sum(1 for s in proposed if s["source"] == "template"),
                     "load": rnd(sum(s["load"] for s in proposed if s["source"] == "proposed"), 0),
                     "estimated": sum(1 for s in proposed if s["estimated"]), "load_formula": LOAD_FORMULA},
        "planned": _planned_counts(kept + proposed),
        **simulated,
        "sessions": [session_view(s) for s in proposed],
    }


def _target_section(
    target: dict[str, Any], form_range: tuple[float, float, str] | None, rows: dict[str, dict[str, Any]],
    scenario_rows: dict[str, dict[str, Any]] | None, search: dict[str, Any] | None,
) -> dict[str, Any]:
    """Start-of-day values on the target day for the calendar plan (and the scenario), the range and the search."""
    day = target["date"]
    info: dict[str, Any] = {
        **target, "date": day.isoformat(), "event_minutes": rnd(target["event_minutes"], 0), "available": True,
        "basis": "start of the target day (end of the day before)",
        "baseline": state_at_start(rows, day),
    }
    if scenario_rows is not None:
        info["scenario"] = state_at_start(scenario_rows, day)
    if form_range and search is not None:
        info["range"] = {"low": form_range[0], "high": form_range[1], "unit": form_range[2]}
        for key in ("baseline", "scenario") if scenario_rows is not None else ("baseline",):
            info[f"{key}_in_range"] = _in_range(info[key], form_range)
        info["search"] = {**search, "basis": "scenario" if scenario_rows is not None else "calendar plan"}
    return info


def _trim_simulation(payload: dict[str, Any], detail_level: str) -> None:
    """Simulation JSON below full: no days and sessions; at compact also no week rows (comparison weeks stay)."""
    keys = ("days",) if detail_level == "standard" else ("days", "weeks")
    for key in keys:
        payload.pop(key, None)
        payload["simulation"].pop(key, None)
    payload["simulation"].pop("sessions", None)
    payload["omitted"] = [*keys, "simulation.sessions"]


# ------------------------------------------------------------------ text


RACE_BASIS = "start of day, before the race's own load"


def _state(state: dict[str, Any] | None) -> str:
    if not state:
        return "n/a"
    return (f"CTL {fmt(state['ctl'], 1)} | ATL {fmt(state['atl'], 1)} | form {fmt(state['form'], 1, signed=True)} "
            f"({fmt(state.get('form_pct_of_ctl'), 0, ' %', signed=True)} of CTL)")


def _span(first: Any, last: Any, suffix: str = "") -> str:
    return f"{fmt(first)}{suffix}" if first == last else f"{fmt(first)}-{fmt(last)}{suffix}"


def _search_lines(search: dict[str, Any]) -> list[str]:
    if not search["available"]:
        return [f"  Search: {search['note']}."]
    window = search["window"]
    lines = [f"  Search on the {search['basis']} (only the {window['days']} days {window['start']} to {window['end']} vary; "
             "grid search, everything else unchanged):"]

    def form(point: dict[str, Any]) -> str:
        return f"{fmt(point['form'], 1, signed=True)} ({fmt(point['form_pct_of_ctl'], 0, ' %', signed=True)})"

    def result(found: dict[str, Any]) -> str:
        first, last = found["from"], found["to"]
        return f"form {form(first)} to {form(last)} and CTL {fmt(first['ctl'], 1)} to {fmt(last['ctl'], 1)} at the target"

    def missed(found: dict[str, Any], unit: str) -> str:
        high, low = found["highest_form"], found["lowest_form"]
        return (f"not reached in the searched range; form from {form(high)} at {fmt(high['x'])}{unit} to "
                f"{form(low)} at {fmt(low['x'])}{unit}")

    pct = search["pct_of_planned"]
    if pct is None:
        lines.append("    (a) scaling the planned load: no planned load in those days")
    elif pct["reached"]:
        lines.append(f"    (a) {_span(pct['from']['x'], pct['to']['x'], ' %')} of the {fmt(search['planned_window_load'])} planned "
                     f"there ({_span(pct['from']['window_load'], pct['to']['window_load'])}) gives {result(pct)}")
    else:
        lines.append(f"    (a) scaling the planned {fmt(search['planned_window_load'])} by 0-200 %: {missed(pct, ' %')}")
    weekly = search["constant_weekly"]
    if weekly["reached"]:
        lines.append(f"    (b) a constant {_span(weekly['from']['x'], weekly['to']['x'])} per week "
                     f"({_span(weekly['from']['per_day'], weekly['to']['per_day'])} per day) gives {result(weekly)}")
    else:
        lines.append(f"    (b) a constant 0-{fmt(weekly['searched'][1])} per week: {missed(weekly, ' per week')}")
    return lines


def _target_lines(target: dict[str, Any], has_scenario: bool) -> list[str]:
    if not target.get("available", True):
        return [f"Target: {target['note']}."]
    event = f" {target['category']} '{target['event']}'" if target["event"] else ""
    if target["event_minutes"]:
        event += f", planned {fmt(target['event_minutes'] / 60, 1)} h"
    source = "next RACE_A in the calendar" if target["source"] == "next_race_a" else "target_date"
    states = (f"scenario {_state(target['scenario'])}; calendar plan {_state(target['baseline'])}" if has_scenario
              else _state(target["baseline"]))
    lines = [f"Target {target['date']}{event} ({source}), start of day: {states}"]
    form_range = target.get("range")
    if form_range:
        unit = " % of CTL" if form_range["unit"] == "pct" else " (CTL - ATL)"
        labels = (("scenario", "scenario"), ("baseline", "calendar plan")) if has_scenario else (("baseline", "calendar plan"),)
        inside = ", ".join(
            f"{name} {'inside' if target[f'{key}_in_range'] else 'outside' if target[f'{key}_in_range'] is False else 'n/a'}"
            for key, name in labels
        )
        lines.append(f"Target form range {form_range['low']:+g} to {form_range['high']:+g}{unit}: {inside}")
        lines.extend(_search_lines(target["search"]))
    return lines


def _hours_text(week: dict[str, Any]) -> str:
    missing = week["sessions_without_duration"]
    if week["hours"] is None:
        return f"hours n/a ({missing} session(s) without duration)"
    return f"{fmt(week['hours'], 1)} h" + (f" (+{missing} session(s) without duration)" if missing else "")


def _week_stats(week: dict[str, Any]) -> str:
    sports = ", ".join(f"{family} {fmt(value)}" for family, value in week["by_sport"].items()) or "none"
    longest = week["longest_session"]
    long_text = "longest n/a"
    if longest:
        share = f", {fmt(longest['pct_of_target_event'])} % of the target event" if longest["pct_of_target_event"] is not None else ""
        long_text = f"longest {fmt(longest['minutes'] / 60, 1)} h ({longest['date'][5:]}{share})"
    estimated = f", {week['estimated_sessions']} estimated" if week["estimated_sessions"] else ""
    mono = fmt(week["monotony"], 2)
    if week["monotony_note"] == IDENTICAL_LOADS:
        mono = "undefined (identical daily loads, maximal)"
    return (f"{week['sessions']} sessions{estimated} ({sports}), {_hours_text(week)}, {long_text}, "
            f"rest days {week['rest_days']}, monotony {mono}")


CHECK_TEXT = {
    "ramp": lambda e: f"ramp {fmt(e['value'], 1, signed=True)} (above 5-8 [1])",
    "monotony": lambda e: (f"monotony {fmt(e['value'], 2)}" if e["value"] is not None
                           else "monotony undefined, identical daily loads") + " (above 2.0 [2])",
    "rest_days": lambda e: f"{e['value']} rest days (below 1 [3])",
}


def _check_lines(checks: dict[str, Any], labels: tuple[tuple[str, str], ...]) -> list[str]:
    lines = ["Plan checks, weeks outside the commonly cited range (statistics, not individual limits; CTL ramp above "
             "5-8 per week, monotony above 2.0, no rest day in a complete week):"]
    for key, name in labels:
        entries = checks.get(key) or []
        lines.append(f"  {name}: " + ("; ".join(f"{e['week']} {CHECK_TEXT[e['metric']](e)}" for e in entries) or "none"))
    return lines


def _sources_line() -> str:
    return (f"[1] {PLAN_REFERENCES['ramp']['source']}; [2] {PLAN_REFERENCES['monotony']['source']}; "
            f"[3] {PLAN_REFERENCES['rest_days']['source']}. {NO_VERDICT}")


def _model_check_line(check: dict[str, Any] | None) -> str:
    if check:
        return (f"Model check: replaying the last {check['days']} days of Intervals.icu loads reproduces the stored CTL within "
                f"{fmt(check['max_ctl_diff'], 2)} and ATL within {fmt(check['max_atl_diff'], 2)}.")
    return "Model check: not possible (wellness records of the last 14 days incomplete)."


def _projection_text(payload: dict[str, Any], detail_level: str) -> str:  # pylint: disable=too-many-locals
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
            f"End of {end_row['date']}: CTL {fmt(end_row['ctl'], 1)} | ATL {fmt(end_row['atl'], 1)} | form "
            f"{fmt(end_row['form'], 1, signed=True)}"
            + (f" (Intervals.icu's own projection: CTL {fmt(icu['ctl'], 1)}, ATL {fmt(icu['atl'], 1)})" if icu else "")
        )
        lowest = payload["lowest_form"]
        lines.append(f"Lowest projected form (end of day) {fmt(lowest['form'], 1, signed=True)} on {lowest['date']}; highest 7-day ramp "
                     f"{fmt(payload['highest_ramp']['ramp'], 1, signed=True)} on {payload['highest_ramp']['date']}")
    if detail_level != "compact":
        lines.append("Weeks (values at the end of the week's last day):")
        for week in payload["weeks"]:
            missing = f", {week['planned_without_load']} without load" if week["planned_without_load"] else ""
            lines.append(
                f"  {week['week']} ({week['start']} to {week['end']}): load {fmt(week['load'])} ({week['planned_sessions']} planned"
                f"{missing}) | CTL {fmt(week['ctl'], 1)} | ATL {fmt(week['atl'], 1)} | form {fmt(week['form'], 1, signed=True)} "
                f"(lowest {fmt(week['lowest_form'], 1, signed=True)} on {week['lowest_form_date']}) | ramp {fmt(week['ramp'], 1, signed=True)}"
                f" | {_week_stats(week)}"
            )
    if payload["races"]:
        lines.append(f"Races ({RACE_BASIS}): " + "; ".join(
            f"{r['date']} {r['category']} '{r['name']}': CTL {fmt(r['ctl'], 1)}, ATL {fmt(r['atl'], 1)}, form {fmt(r['form'], 1, signed=True)}"
            for r in payload["races"]
        ))
    if "target" in payload:
        lines.extend(_target_lines(payload["target"], False))
    flagged_weeks = payload["checks"]["baseline"]
    if detail_level != "compact":
        lines.extend(_check_lines(payload["checks"], (("baseline", "calendar plan"),)))
    elif flagged_weeks:
        lines.append(f"Plan checks: {len(flagged_weeks)} week metric(s) outside the commonly cited range (details at standard).")
    if detail_level == "full":
        lines.append("Days (end of day): " + ", ".join(
            f"{d['date'][5:]} load {fmt(d['load'])} CTL {fmt(d['ctl'], 1)} form {fmt(d['form'], 1, signed=True)}" for d in payload["days"]
        ))
    lines.append(_model_check_line(payload["model_check"]))
    if detail_level != "compact" or flagged_weeks:
        lines.append(_sources_line())
    lines.append("A projection is arithmetic on the plan, not a forecast; no assessment is made.")
    return "\n".join(lines)


def _scenario_line(sim: dict[str, Any]) -> str:
    parts = []
    proposed = sim["proposed"]
    if proposed["sessions"]:
        parts.append(f"{proposed['sessions']} proposed session(s), load {fmt(proposed['load'])}")
    for index, template in enumerate(sim["templates"], 1):
        long_text = (f", long day {template['long_day']} {fmt(template['long_session_share'] * 100)} %"
                     if template["long_session_share"] is not None else "")
        hours = f", hours {', '.join(fmt(h, 1) for h in template['weekly_hours'])}" if template["weekly_hours"] else ""
        parts.append(
            f"template {template['name'] or index}: {template['weeks']} week(s) from {template['start']}, "
            f"{template['sessions_per_week']} {template['sport']} sessions/week on {', '.join(template['days'])}{long_text}, "
            f"weekly load {', '.join(fmt(v) for v in template['weekly_load'])}"
            + (" (estimated)" if template["load_estimated"] else "") + hours
        )
    dropped = sim["calendar_dropped"]
    calendar = {
        "add": "planned workouts kept",
        "replace": f"planned workouts {sim['span'][0]} to {sim['span'][1]} replaced ({dropped['sessions']} left out, load {fmt(dropped['load'])})",
        "none": f"planned workouts not used ({dropped['sessions']} left out, load {fmt(dropped['load'])})",
    }[sim["calendar"]]
    estimated = f"; {proposed['estimated']} load(s) estimated as {LOAD_FORMULA}" if proposed["estimated"] else ""
    return f"Scenario: {'; '.join(parts)}; calendar: {calendar}{estimated}."


def _simulation_text(payload: dict[str, Any], detail_level: str) -> str:  # pylint: disable=too-many-locals
    sim, start, plan = payload["simulation"], payload["start"], payload["planned"]
    end = payload["comparison"]["end"]
    lines = [
        f"{sim['label']}: scenario vs calendar plan for athlete {payload['athlete_id']}, {payload['from']} to {payload['to']} "
        f"(model CTL {payload['ctl_days']} d / ATL {payload['atl_days']} d).",
        _scenario_line(sim),
        "Calendar plan: " + (f"{plan['sessions']} planned workouts ({plan['with_load']} with a planned load, sum {fmt(plan['load'])})"
                             if plan["sessions"] else "no planned workouts (decay only)"),
        "Assumptions: " + "; ".join(payload["assumptions"]) + ".",
        f"Start (end of {start['date']}, Intervals.icu): CTL {fmt(start['ctl'], 1)} | ATL {fmt(start['atl'], 1)} | "
        f"form {fmt(start['form'], 1, signed=True)}",
        f"End of {end['date']}: scenario {_state(end['scenario'])}; calendar plan {_state(end['baseline'])}; difference CTL "
        f"{fmt(end['difference']['ctl'], 1, signed=True)}, form {fmt(end['difference']['form'], 1, signed=True)}",
        f"Lowest form (end of day, that day's load included): scenario {fmt(sim['lowest_form']['form'], 1, signed=True)} on "
        f"{sim['lowest_form']['date']}, calendar plan "
        f"{fmt(payload['lowest_form']['form'], 1, signed=True)} on {payload['lowest_form']['date']}; highest 7-day ramp: scenario "
        f"{fmt(sim['highest_ramp']['ramp'], 1, signed=True)}, calendar plan {fmt(payload['highest_ramp']['ramp'], 1, signed=True)}",
    ]
    if "target" in payload:
        lines.extend(_target_lines(payload["target"], True))
    recent = payload.get("recent_weeks")
    if recent and detail_level != "compact":
        longest = recent["longest_session"]
        lines.append(
            f"Recent {recent['weeks']} completed weeks ({recent['start']} to {recent['end']}) for comparison: mean load "
            f"{fmt(recent['mean_load'])}/week (max {fmt(recent['max_load'])}), {fmt(recent['mean_hours'], 1)} h/week, "
            f"{fmt(recent['mean_rest_days'], 1)} rest days/week"
            + (f", longest session {fmt(longest['minutes'] / 60, 1)} h ({longest['date']})" if longest else "")
        )
    if detail_level != "compact":
        lines.append("Weeks, scenario vs calendar plan (values at the end of the week's last day):")
        for scenario_week, plan_week in zip(sim["weeks"], payload["weeks"], strict=True):
            partial = "" if scenario_week["complete"] else f", partial {scenario_week['days']} d"
            lines.append(
                f"  {scenario_week['week']} ({scenario_week['start']} to {scenario_week['end']}{partial}): load "
                f"{fmt(scenario_week['load'])} vs {fmt(plan_week['load'])} | CTL {fmt(scenario_week['ctl'], 1)} vs "
                f"{fmt(plan_week['ctl'], 1)} | form {fmt(scenario_week['form'], 1, signed=True)} vs "
                f"{fmt(plan_week['form'], 1, signed=True)} | ramp {fmt(scenario_week['ramp'], 1, signed=True)} vs "
                f"{fmt(plan_week['ramp'], 1, signed=True)}"
            )
            lines.append(f"    scenario: {_week_stats(scenario_week)}")
        lines.extend(_check_lines(payload["checks"], (("scenario", "scenario"), ("baseline", "calendar plan"))))
    else:
        counts = {k: len(payload["checks"][k]) for k in ("scenario", "baseline")}
        lines.append(f"Plan checks: {counts['scenario']} week metric(s) of the scenario and {counts['baseline']} of the calendar "
                     "plan outside the commonly cited range (details at standard).")
    if sim["races"]:
        lines.append(f"Races ({RACE_BASIS}): " + "; ".join(
            f"{r['date']} {r['category']} '{r['name']}': scenario CTL {fmt(r['ctl'], 1)}, form {fmt(r['form'], 1, signed=True)}; "
            f"calendar plan CTL {fmt(b['ctl'], 1)}, form {fmt(b['form'], 1, signed=True)}"
            for r, b in zip(sim["races"], payload["races"], strict=True)
        ))
    if detail_level == "full":
        lines.append("Scenario days (end of day): " + ", ".join(
            f"{d['date'][5:]} load {fmt(d['load'])} CTL {fmt(d['ctl'], 1)} form {fmt(d['form'], 1, signed=True)}" for d in sim["days"]
        ))
        lines.append("Proposed sessions: " + ("; ".join(
            f"{s['date']} {s['sport'] or 'n/a'} load {fmt(s['load'])}" + (" (estimated)" if s["estimated"] else "")
            + (f", {fmt(s['minutes'])} min" if s["minutes"] is not None else "")
            for s in sim["sessions"]
        ) or "none"))
    lines.append(_model_check_line(payload["model_check"]))
    lines.append(_sources_line())
    lines.append("A simulation is arithmetic on the inputs, not a forecast; no assessment is made.")
    return "\n".join(lines)


# ------------------------------------------------------------------ tool


@tool("read")
async def get_load_projection(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements,too-many-statements,too-many-branches
    end_date: str | None = None,
    ctl_days: int = CTL_DAYS,
    atl_days: int = ATL_DAYS,
    athlete_id: str | None = None,
    output_format: str = "text",
    detail_level: str = "standard",
    scenario: dict[str, Any] | list[dict[str, Any]] | str | None = None,
    target_date: str | None = None,
    target_form: str | list[float] | None = None,
    taper_days: int = 7,
) -> str:
    """Project CTL, ATL and form over the planned workouts, or simulate a what-if plan (read-only, nothing is written)

    Exponential model from the Intervals.icu CTL/ATL at the end of yesterday (time constants 42/7 d): today's completed
    load, then the planned load of the WORKOUT events (workouts without one are reported). Reports end values, lowest
    form, highest ramp, ISO weeks (load per sport, sessions, hours, longest session, rest days, monotony, CTL/ATL/form/
    ramp), all end of day; race days at the start of the day; Intervals.icu's projection, a model check and weeks
    outside commonly cited ranges (ramp 5-8 CTL/week, monotony > 2.0, < 1 rest day; with sources). `scenario` simulates
    sessions NOT in the calendar and
    compares with the calendar plan. A target day (target_date or the next RACE_A) gets CTL/form at the start of the
    day; with `target_form`, a grid search over the load of the last `taper_days` days. Arithmetic, no verdict.

    Args:
        end_date: Last day YYYY-MM-DD (optional; default 28 days ahead, extended to the target and scenario; max 180 days)
        ctl_days: CTL time constant in days (optional, default 42)
        atl_days: ATL time constant in days (optional, default 7)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        output_format: "text" (default) or "json"
        detail_level: "compact", "standard" (default, plus weeks and checks) or "full" (plus days; JSON has the days
            always, with a scenario only at full)
        scenario: Optional object (or JSON text): {"calendar": "add" (default) | "replace" (drop planned workouts from
            the first to the last scenario day) | "none", "sessions": [{"date": "YYYY-MM-DD", "load": 120} or
            {"date": ..., "duration_min": 240, "intensity_factor": 0.7} (load estimated as hours x IF^2 x 100), optional
            "sport", "name"], "weekly": [{"start": "YYYY-MM-DD" (default next Monday), "weeks": 4, "load": 550 or
            [500, 550, 600, 400] or "hours" + "intensity_factor", "sessions": 5 or "days": ["Tue", "Thu", "Sat"],
            optional "long_session_share": 0.4, "long_day": "Sat", "sport": "Ride"}]}; a plain list = sessions
        target_date: Target day YYYY-MM-DD (optional; default the next RACE_A event)
        target_form: Form range at the target, "5,15" (CTL - ATL) or "5%,20%" (of CTL) (optional)
        taper_days: Days before the target varied by the search, 1-42 (optional, default 7)
    """
    athlete_id_to_use, error_msg = resolve_request(athlete_id, detail_level)
    if error_msg:
        return error_msg
    if not 1 <= atl_days < ctl_days <= 365:
        return "Error: ctl_days and atl_days must satisfy 1 <= atl_days < ctl_days <= 365."
    today = current_day()
    limit = today + timedelta(days=MAX_PROJECTION_DAYS)
    given_end = parse_end(end_date, today + timedelta(days=DEFAULT_PROJECTION_DAYS))
    if isinstance(given_end, str):
        return given_end
    if not today < given_end <= limit:
        return f"Error: end_date must lie after today and at most {MAX_PROJECTION_DAYS} days ahead."
    inputs = _what_if_inputs(scenario, target_date, target_form, taper_days, today, given_end if end_date else limit)
    if isinstance(inputs, str):
        return inputs
    parsed = inputs["scenario"]
    fetch_end = limit if inputs["active"] and not end_date else given_end

    wellness_list, error = await fetch_wellness(athlete_id_to_use, today - timedelta(days=21), fetch_end, FITNESS_FIELDS)
    if error:
        return error
    events, error = await fetch_events(athlete_id_to_use, today, fetch_end)
    if error:
        return error
    # With a scenario, the four completed ISO weeks before this one are summarised for comparison.
    recent_start = today - timedelta(days=today.weekday() + 28)
    activities, error = await fetch_activities(athlete_id_to_use, recent_start if parsed else today, today, LOAD_FIELDS)
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
    target = _target_event(events, inputs["target"], today, fetch_end) if inputs["active"] else None
    end = given_end
    if not end_date:
        end = max([end] + ([target["date"]] if target else []) + ([parsed["span"][1]] if parsed else []))
    workouts, races = _planned(events, today, end)
    history = {day: num(wellness.get(day, {}).get("ctlLoad")) or 0.0 for day in day_range(base_day + timedelta(days=1), today - timedelta(days=1))}
    completed_list = completed_sessions(activities, today)
    calendar = calendar_sessions(workouts)
    loads = add_session_loads(history, completed_list + calendar)
    completed = sum(s["load"] or 0.0 for s in completed_list)
    ctl_history = {d: v for d, rec in wellness.items() if d <= base_day and (v := num(rec.get("ctl"))) is not None}
    base = wellness[base_day]
    start_ctl, start_atl = num(base.get("ctl")) or 0.0, num(base.get("atl")) or 0.0
    rows = project_fitness(start_ctl, start_atl, base_day, loads, end, ctl_history, ctl_days, atl_days)
    race_minutes = target["event_minutes"] if target else None
    base_row = {"date": base_day.isoformat(), "ctl": rnd(start_ctl, 1), "atl": rnd(start_atl, 1), "form": rnd(start_ctl - start_atl, 1)}
    baseline = _variant(rows, base_row, today, loads, completed_list + calendar, races, race_minutes)
    _calendar_week_extras(baseline["weeks"], workouts, wellness)
    end_icu = wellness.get(end, {})
    payload: dict[str, Any] = {
        "athlete_id": athlete_id_to_use, "from": today.isoformat(), "to": end.isoformat(),
        "ctl_days": ctl_days, "atl_days": atl_days, "mode": "simulation" if parsed else "projection",
        "start": dict(base_row), "value_basis": VALUE_BASIS,
        "today": {"date": today.isoformat(), "completed_load": rnd(completed, 0),
                  "planned_load": rnd(sum(activity_load(e) or 0.0 for e in workouts.get(today, [])), 0)},
        "planned": _planned_counts(calendar),
        "intervals_icu_end": ({"ctl": rnd(num(end_icu.get("ctl")), 1), "atl": rnd(num(end_icu.get("atl")), 1)}
                              if num(end_icu.get("ctl")) is not None else None),
        "lowest_form": baseline["lowest_form"], "highest_ramp": baseline["highest_ramp"],
        "weeks": baseline["weeks"], "races": baseline["races"],
        "model_check": model_check(wellness, today - timedelta(days=1), 14, ctl_days, atl_days),
        "days": baseline["days"],
    }
    payload["projection_basis"] = projection_basis(payload["planned"])
    payload["checks"] = {"references": PLAN_REFERENCES, "baseline": flagged(baseline["weeks"])}
    if inputs["active"]:
        payload["assumptions"] = _assumptions(ctl_days, atl_days, parsed, target, _ignored_entries(events, today, end))
    scenario_loads, scenario_rows = loads, None
    if parsed:
        kept, dropped = apply_calendar(calendar, parsed)
        scenario_sessions = completed_list + kept + parsed["sessions"]
        scenario_loads = add_session_loads(history, scenario_sessions)
        scenario_rows = project_fitness(start_ctl, start_atl, base_day, scenario_loads, end, ctl_history, ctl_days, atl_days)
        simulated = _variant(scenario_rows, base_row, today, scenario_loads, scenario_sessions, races, race_minutes)
        payload["simulation"] = _simulation_section(parsed, kept, dropped, simulated)
        payload["comparison"] = _comparison(baseline, simulated)
        payload["checks"]["scenario"] = flagged(simulated["weeks"])
        last_sunday = today - timedelta(days=today.weekday() + 1)
        payload["recent_weeks"] = recent_weeks(activities, weekly_rows(activities, last_sunday, 4))
    form_range = inputs["form_range"]
    if target:
        search = solve_target(
            (start_ctl, start_atl), base_day, scenario_loads, target["date"], today + timedelta(days=1), taper_days,
            form_range, ctl_days, atl_days,
        ) if form_range else None
        payload["target"] = _target_section(
            target, form_range, _by_date(rows, base_row), _by_date(scenario_rows, base_row) if scenario_rows else None, search
        )
    elif form_range:
        payload["target"] = {"available": False,
                             "note": "no target day: pass target_date or add a RACE_A event to the calendar (within 180 days)"}
    if output_format.strip().lower() == "json":
        if parsed and detail_level != "full":
            _trim_simulation(payload, detail_level)
        return json.dumps(payload, ensure_ascii=False)
    return _simulation_text(payload, detail_level) if parsed else _projection_text(payload, detail_level)
