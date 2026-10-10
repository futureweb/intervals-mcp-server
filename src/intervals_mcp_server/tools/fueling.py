"""
Fueling MCP tool for Intervals.icu (read-only).

One activity: the activity (one request) and, only when a custom stream carries intake over
time, that stream (one request). A period: one activity list request with a field selection.
Custom item definitions come from the per-process cache. The logic lives in ``utils.fueling``.
"""

import json
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.tools.training_load import filter_types, resolve_period, wanted_types
from intervals_mcp_server.utils.custom_fields import ACTIVITY_FIELD, ACTIVITY_STREAM, CustomFieldDefs
from intervals_mcp_server.utils.field_policy import field_words
from intervals_mcp_server.utils.fueling import (
    CARB_WORDS,
    CARBS_NOTE,
    FUELING_LIST_FIELDS,
    INTAKE_WORDS,
    MIN_CORRELATION_N,
    TIMING_NOTE,
    activity_fueling,
    correlation_text,
    fueling_fields,
    fueling_line,
    group_text,
    intake_distribution,
    period_fueling,
    row_text,
)
from intervals_mcp_server.utils.sports import format_local_start, hms
from intervals_mcp_server.utils.validation import resolve_athlete_id

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

DETAIL_LEVELS = ("compact", "standard", "full")
DEFAULT_PERIOD_DAYS = 90
MAX_PERIOD_DAYS = 366
STANDARD_ROWS = 15


def intake_streams(stream_defs: CustomFieldDefs, listed: list[Any]) -> list[str]:
    """Custom streams of the activity whose definition names carbohydrate or fluid intake."""
    return [code for code, d in stream_defs.items() if code in listed and field_words(d) & (CARB_WORDS | INTAKE_WORDS)]


async def _single(activity_id: str, athlete_id: str | None, api_key: str | None) -> dict[str, Any] | str:  # pylint: disable=too-many-locals
    result = await make_intervals_request(url=f"/activity/{activity_id}", api_key=api_key)
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching the activity: {result.get('message', 'Unknown error')}"
    activity = result[0] if isinstance(result, list) and result else result
    if not isinstance(activity, dict) or not activity:
        return f"No activity {activity_id} found."
    owner = str(activity.get("icu_athlete_id") or athlete_id or config.athlete_id or "")
    index = await get_custom_item_index(athlete_id=owner, api_key=api_key) if owner else {}
    defs, stream_defs = index.get(ACTIVITY_FIELD, {}), index.get(ACTIVITY_STREAM, {})
    figures = activity_fueling(activity, defs)
    timing: dict[str, Any] = {"available": False, "note": TIMING_NOTE, "streams": {}}
    codes = intake_streams(stream_defs, [str(t) for t in activity.get("stream_types") or []])
    calls = 1
    if codes:
        streams = await make_intervals_request(url=f"/activity/{activity_id}/streams", api_key=api_key,
                                               params={"types": ",".join(["time", *codes])})
        calls += 1
        by_type = {s.get("type"): s.get("data") or [] for s in streams if isinstance(s, dict)} if isinstance(streams, list) else {}
        for code in codes:
            distribution = intake_distribution(by_type.get("time") or [], by_type.get(code) or [])
            if distribution:
                timing["streams"][code] = {"name": stream_defs[code].get("name"), "units": stream_defs[code].get("units"), **distribution}
        if timing["streams"]:
            timing.update(available=True, note="intake per hour from the custom stream(s) below (cumulative or per event as detected)")
    return {"activity": {"id": activity.get("id"), "name": activity.get("name"), "type": activity.get("type"),
                         "start": format_local_start(activity)},
            "fueling": figures, "timing": timing, "fields_found": fueling_fields(defs),
            "notes": [CARBS_NOTE], "api_calls": calls}


def _single_text(payload: dict[str, Any], detail_level: str) -> str:
    activity, figures = payload["activity"], payload["fueling"]
    lines = [f"Fueling of {activity['id']} '{activity['name']}' ({activity['type']}) {activity['start']}, "
             f"moving {hms(figures['moving_time_s'])}" + (f", IF {figures['intensity_factor']:.2f}" if figures["intensity_factor"] is not None else "")
             + "; rates per moving hour"]
    lines.append(fueling_line(figures) or "Fueling: no carbohydrate, fluid or energy values stored")
    found = payload["fields_found"]
    missing = [label for key, label in (("fluid_intake", "fluid intake"), ("sodium", "sodium"), ("sweat_loss", "sweat loss")) if not found[key]]
    if missing and detail_level != "compact":
        lines.append(f"No custom field for {', '.join(missing)} (looked for by units and name: ml/l with fluid/drink/bottle/intake, "
                     "mg with sodium/salt, ml/l with sweat)")
    if figures["custom_carb_fields"]:
        lines.append(f"Custom carbohydrate fields present: {', '.join(figures['custom_carb_fields'])}")
    timing = payload["timing"]
    if timing["available"]:
        for code, stream in timing["streams"].items():
            lines.append(f"Intake over time from {stream['name']} [{code}] ({stream['mode']}): " + ", ".join(
                f"hour {h['hour']} {h['amount']:g}{' ' + stream['units'] if stream['units'] else ''}" for h in stream["per_hour"]))
    else:
        lines.append(timing["note"])
    if detail_level != "compact":
        lines.extend(f"Note: {note}" for note in payload["notes"])
    return "\n".join(lines)


async def _period(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    athlete_id: str, api_key: str | None, start_date: str | None, end_date: str | None, sport_types: str | None, min_minutes: float,
) -> dict[str, Any] | str:
    period = resolve_period(start_date, end_date, DEFAULT_PERIOD_DAYS, MAX_PERIOD_DAYS)
    if isinstance(period, str):
        return period
    start, end = period
    defs = (await get_custom_item_index(athlete_id=athlete_id, api_key=api_key)).get(ACTIVITY_FIELD, {})
    roles = fueling_fields(defs)
    codes = sorted({code for group in roles.values() for code in group})
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id}/activities", api_key=api_key,
        params={"oldest": start.isoformat(), "newest": end.isoformat(), "fields": ",".join([FUELING_LIST_FIELDS, *codes])},
    )
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching activities: {result.get('message', 'Unknown error')}"
    activities = filter_types([a for a in result if isinstance(a, dict)] if isinstance(result, list) else [], wanted_types(sport_types))
    summary = period_fueling(activities, defs, min_minutes * 60)
    return {"athlete_id": athlete_id, "start": start.isoformat(), "end": end.isoformat(), "sport_types": sport_types,
            "min_minutes": min_minutes, "activities_in_period": len(activities), **summary, "fields_found": roles,
            "notes": [CARBS_NOTE, TIMING_NOTE, f"Spearman rank correlation per sport family, from {MIN_CORRELATION_N} sessions with "
                      "intake logged on; association only, not causation."], "api_calls": 1}


def _period_text(payload: dict[str, Any], detail_level: str) -> str:
    rows = payload["rows"]
    lines = [f"Fueling for athlete {payload['athlete_id']}, {payload['start']} to {payload['end']}: {len(rows)} sessions of at least "
             f"{payload['min_minutes']:g} min (of {payload['activities_in_period']} activities"
             + (f", types {payload['sport_types']}" if payload["sport_types"] else "") + "); rates per moving hour"]
    if not rows:
        return lines[0] + "."
    families = payload["by_sport_family"]
    if len(families) > 1:
        lines.append(group_text("Overall (all sports)", payload["overall"]))
    for family, block in families.items():
        lines.append(group_text(family, block))
        if detail_level == "compact":
            continue
        lines.extend(group_text(f"  {label} (moving time)", group) for label, group in block["by_duration"].items() if group["sessions"])
        lines.extend(group_text(f"  {label}", group) for label, group in block["by_intensity"].items() if group["sessions"])
        lines.append(f"  {correlation_text(block['correlations'])}")
    if detail_level != "compact":
        shown = rows if detail_level == "full" else rows[:STANDARD_ROWS]
        lines.append("Sessions:")
        lines.extend(f"  {row_text(row)}" for row in shown)
        if len(rows) > len(shown):
            lines.append(f"  ... {len(rows) - len(shown)} more (detail_level=full)")
    lines.extend(f"Note: {note}" for note in payload["notes"][:2])
    return "\n".join(lines)


@tool("read")
async def get_fueling_analysis(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements
    activity_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sport_types: str | None = None,
    min_minutes: float = 90,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
    detail_level: str = "standard",
) -> str:
    """Fueling of one activity or of the long sessions of a period (read-only)

    Carbs used (Intervals.icu estimate) and ingested (logged total) in g and g/h, ingested share
    of used, kcal, kJ and, where custom fields exist (found by units and name), fluid intake,
    sodium and sweat loss per hour. Period mode: sessions of at least min_minutes, g/h per sport
    family, duration and intensity with sample sizes. Used vs ingested is no 1:1 energy deficit
    (body stores contribute). No targets.

    Args:
        activity_id: One activity; omit for the period mode
        start_date: YYYY-MM-DD (default end_date - 89 days)
        end_date: YYYY-MM-DD (default today)
        sport_types: e.g. "Ride,GravelRide"
        min_minutes: Minimum moving time, period mode (default 90)
        output_format: "text" or "json"
        detail_level: "compact", "standard" or "full"
    """
    if detail_level not in DETAIL_LEVELS:
        return f"Error: detail_level must be one of {', '.join(DETAIL_LEVELS)}."
    if min_minutes < 0:
        return "Error: min_minutes must not be negative."
    as_json = output_format.strip().lower() == "json"
    if activity_id:
        payload = await _single(activity_id, athlete_id, api_key)
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, ensure_ascii=False, default=str) if as_json else _single_text(payload, detail_level)
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    result = await _period(athlete_id_to_use, api_key, start_date, end_date, sport_types, min_minutes)
    if isinstance(result, str):
        return result
    if as_json:
        if detail_level == "compact":
            result.pop("rows")
        return json.dumps(result, ensure_ascii=False, default=str)
    return _period_text(result, detail_level)
