"""
Activity-related MCP tools for Intervals.icu.

This module contains tools for retrieving and managing athlete activities.
"""

# pylint: disable=too-many-lines

import json
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.athlete import assigned_field_ids
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.tools.gear import (
    resolve_gear_for_activities,
    resolve_gear_for_activity,
)
from intervals_mcp_server.utils.custom_fields import (
    ACTIVITY_FIELD,
    ACTIVITY_STREAM,
    INTERVAL_FIELD,
    CustomFieldDefs,
    assigned_codes,
    custom_fields_json,
    format_custom_field_lines,
    is_missing,
)
from intervals_mcp_server.utils.execution import plan_position, plan_steps, planned_step_map, planned_step_text
from intervals_mcp_server.utils.sports import cadence_spm, cadence_text, format_local_start, format_start_times, hms, start_times
from intervals_mcp_server.utils.formatting import (
    format_activity_details,
    format_activity_message,
    format_activity_summary,
    format_intervals,
)
from intervals_mcp_server.utils.streams import (
    DEFAULT_STREAM_TYPES,
    NON_METRIC_STREAM_TYPES,
    describe_stream,
    find_stream,
    format_stats,
    format_streams_summary,
    order_streams,
    range_stats,
    render_streams_json,
    render_streams_table,
    resolve_sample_range,
    stream_label,
    stream_length,
)
from intervals_mcp_server.utils.validation import resolve_athlete_id, resolve_date_params
from intervals_mcp_server.tool_guard import output_budget

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

STREAM_OUTPUT_FORMATS = ("summary", "full", "json")
# Samples per full/json response: default and hard maximum (a 24 h ride has ~86,400 samples).
DEFAULT_STREAM_POINTS = 2000
MAX_STREAM_POINTS = 20000
# Fields requested from the API for compact / JSON activity listings (keeps payloads small).
ACTIVITY_LIST_FIELDS = (
    "id,start_date_local,start_date,timezone,type,sub_type,name,description,moving_time,elapsed_time,"
    "distance,total_elevation_gain,icu_training_load,hr_load,power_load,pace_load,icu_intensity,"
    "icu_average_watts,icu_weighted_avg_watts,average_heartrate,max_heartrate,average_cadence,"
    "average_speed,feel,icu_rpe,perceived_exertion,gear,gear_id,trainer,device_name,power_meter,tags,"
    "icu_ctl,icu_atl,icu_ftp,paired_event_id,compliance"
)
SORT_KEYS = ("date_desc", "date_asc", "distance", "moving_time", "load")
DETAIL_LEVELS = ("summary", "compact")
DETAIL_LEVELS_3 = ("compact", "standard", "full")


def _compact_details(activity: dict[str, Any], defs: CustomFieldDefs, assigned: set[str] | None) -> str:
    """Token-efficient activity view: key numbers, thresholds, assigned custom fields, data quality."""
    gear = activity.get("_resolved_gear_name") or (activity.get("gear") or {}).get("id") if isinstance(activity.get("gear"), dict) else activity.get("_resolved_gear_name")
    lines = [
        f"{activity.get('name', 'Unnamed')} ({activity.get('id')}, {activity.get('type', '?')}) {format_local_start(activity)}",
        f"Time {hms(activity.get('moving_time'))} moving / {hms(activity.get('elapsed_time'))} elapsed | "
        f"{(activity.get('distance') or 0) / 1000:.1f} km | +{activity.get('total_elevation_gain') or 0:.0f} m"
        + (f" | gear {gear}" if gear else ""),
        f"Load {_n(activity.get('icu_training_load'))} (Intervals.icu; power {_n(activity.get('power_load'))} / HR {_n(activity.get('hr_load'))} / pace {_n(activity.get('pace_load'))}) | "
        f"IF {_n(activity.get('icu_intensity'))}% | NP {_n(activity.get('icu_weighted_avg_watts'))} W | avg {_n(activity.get('icu_average_watts'))} W | "
        f"HR avg {_n(activity.get('average_heartrate'))} max {_n(activity.get('max_heartrate'))} | "
        f"cadence {cadence_text(activity.get('average_cadence'), activity.get('type'))}",
        f"Feel {_n(activity.get('feel'))}/5 | RPE {_n(activity.get('icu_rpe'))}/10 | compliance "
        + (f"{_n(activity.get('compliance'))}%" if _n(activity.get("compliance")) != "n/a" else "n/a") + " | "
        f"FTP used {_n(activity.get('icu_ftp'))} W, eFTP {_n(activity.get('icu_rolling_ftp'))} W, LTHR {_n(activity.get('lthr'))} | "
        f"device {activity.get('device_name') or 'unknown'}, power meter {activity.get('power_meter') or 'unknown'}"
        + (f", power fields {', '.join(str(p) for p in activity['power_field_names'])}" if activity.get("power_field_names") else ""),
    ]
    custom = format_custom_field_lines(activity, defs, prefix="", only=assigned)
    if custom:
        lines.append("Custom fields" + (" (assigned to this sport)" if assigned is not None else "") + ": " + "; ".join(custom[:14]) + (" ..." if len(custom) > 14 else ""))
    quality = []
    if activity.get("icu_intervals_edited"):
        quality.append("intervals edited")
    if activity.get("icu_sync_error"):
        quality.append(f"sync error {activity['icu_sync_error']}")
    streams = activity.get("stream_types")
    if isinstance(streams, list):
        quality.append(f"{len(streams)} streams ({sum(1 for s in streams if s in defs or s not in STANDARD_STREAM_NAMES)} custom)")
    if activity.get("analyzed"):
        quality.append(f"analysed {activity['analyzed']}")
    if quality:
        lines.append("Data: " + ", ".join(quality))
    return "\n".join(lines)


STANDARD_STREAM_NAMES = {
    "time", "watts", "raw_watts", "fixed_watts", "secondary_power", "heartrate", "cadence", "distance", "altitude",
    "fixed_altitude", "latlng", "velocity_smooth", "grade_smooth", "temp", "torque", "left_right_balance", "hrv",
    "respiration", "left_pedal_smoothness", "right_pedal_smoothness", "left_torque_effectiveness",
    "right_torque_effectiveness", "stance_time", "vertical_oscillation", "vertical_ratio", "step_length", "moving",
}


def _n(value: Any, digits: int = 0) -> str:
    """Number for compact lines: rounded, 'n/a' when missing."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or is_missing(value):
        return "n/a"
    return f"{value:.{digits}f}"


def format_start_times_short(activity: dict[str, Any]) -> str:
    """'2026-10-06 17:36 local (UTC+02:00)' for compact views (see utils.sports.format_local_start)."""
    return format_local_start(activity)


def _json_safe(value: Any) -> Any:
    """Recursively replace NaN with None so json.dumps emits valid JSON."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and is_missing(value):
        return None
    return value


def activity_record(activity: dict[str, Any]) -> dict[str, Any]:
    """Machine-readable activity summary with explicit units and both start times."""
    gear = activity.get("gear")
    gear_id = gear.get("id") if isinstance(gear, dict) else activity.get("gear_id")
    return _json_safe(
        {
            "id": activity.get("id"),
            "name": activity.get("name"),
            "type": activity.get("type"),
            "sub_type": activity.get("sub_type"),
            **start_times(activity),
            "moving_time_s": activity.get("moving_time"),
            "elapsed_time_s": activity.get("elapsed_time"),
            "distance_m": activity.get("distance"),
            "elevation_gain_m": activity.get("total_elevation_gain"),
            "training_load_intervals": activity.get("icu_training_load"),
            "power_load": activity.get("power_load"),
            "hr_load": activity.get("hr_load"),
            "pace_load": activity.get("pace_load"),
            "intensity_pct": activity.get("icu_intensity"),
            "average_power_w": activity.get("icu_average_watts"),
            "weighted_avg_power_w": activity.get("icu_weighted_avg_watts"),
            "average_hr_bpm": activity.get("average_heartrate"),
            "max_hr_bpm": activity.get("max_heartrate"),
            "average_cadence": activity.get("average_cadence"),
            "average_cadence_spm": cadence_spm(activity.get("average_cadence"), activity.get("type")),
            "average_speed_m_s": activity.get("average_speed"),
            "feel": activity.get("feel"),
            "rpe": activity.get("icu_rpe", activity.get("perceived_exertion")),
            "gear_id": gear_id,
            "gear_name": activity.get("_resolved_gear_name"),
            "trainer": activity.get("trainer"),
            "device_name": activity.get("device_name"),
            "power_meter": activity.get("power_meter"),
            "tags": activity.get("tags"),
            "ftp_w": activity.get("icu_ftp"),
            "paired_event_id": activity.get("paired_event_id"),
            "compliance_pct": activity.get("compliance"),
        }
    )


def _compact_line(activity: dict[str, Any]) -> str:
    """One line per activity for token-efficient listings."""
    parts = [
        format_local_start(activity),
        str(activity.get("id")),
        str(activity.get("type", "?")),
        f"'{activity.get('name', 'unnamed')}'",
        hms(activity.get("moving_time")),
    ]
    distance = activity.get("distance")
    if isinstance(distance, (int, float)):
        parts.append(f"{distance / 1000:.1f} km")
    if isinstance(activity.get("total_elevation_gain"), (int, float)):
        parts.append(f"+{activity['total_elevation_gain']:.0f} m")
    if activity.get("icu_training_load") is not None:
        loads = "/".join(
            f"{label}{activity[key]}" for key, label in (("power_load", "P"), ("hr_load", "H"), ("pace_load", "Pa")) if activity.get(key) is not None
        )
        parts.append(f"load {activity['icu_training_load']}" + (f" ({loads})" if loads else ""))
    if activity.get("icu_average_watts") is not None:
        parts.append(f"avg {activity['icu_average_watts']} W" + (f" NP {activity['icu_weighted_avg_watts']}" if activity.get("icu_weighted_avg_watts") else ""))
    if activity.get("average_heartrate") is not None:
        parts.append(f"HR {activity['average_heartrate']}")
    if activity.get("feel") is not None or activity.get("icu_rpe") is not None:
        parts.append(f"feel {activity.get('feel', '-')}/5 RPE {activity.get('icu_rpe', '-')}")
    gear_name = activity.get("_resolved_gear_name")
    if gear_name:
        parts.append(f"gear {gear_name}")
    if activity.get("power_meter"):
        parts.append(f"PM {activity['power_meter']}")
    if activity.get("trainer"):
        parts.append("trainer")
    return " | ".join(parts)


def _sort_activities(activities: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    key = {
        "date_desc": lambda a: str(a.get("start_date_local", "")),
        "date_asc": lambda a: str(a.get("start_date_local", "")),
        "distance": lambda a: a.get("distance") or 0,
        "moving_time": lambda a: a.get("moving_time") or 0,
        "load": lambda a: a.get("icu_training_load") or 0,
    }[sort_by]
    return sorted(activities, key=key, reverse=sort_by != "date_asc")


def _activity_gear_id(activity: dict[str, Any]) -> str:
    gear = activity.get("gear")
    if isinstance(gear, dict) and gear.get("id"):
        return str(gear["id"])
    return str(activity.get("gear_id") or "")


async def _list_activities_filtered(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements,too-many-branches
    athlete_id: str,
    api_key: str | None,
    dates: tuple[str, str],
    limit: int,
    include_unnamed: bool,
    sport_types: str | None,
    gear_id: str | None,
    sort_by: str,
    offset: int,
    detail_level: str,
    output_format: str,
    power_meter: str | None = None,
) -> str:
    """Filtered, sorted and paginated activity listing (compact / summary / json)."""
    if sort_by not in SORT_KEYS:
        return f"Error: sort_by must be one of {', '.join(SORT_KEYS)}."
    if detail_level not in DETAIL_LEVELS:
        return f"Error: detail_level must be one of {', '.join(DETAIL_LEVELS)}."
    if output_format not in ("text", "json"):
        return "Error: output_format must be 'text' or 'json'."
    if offset < 0 or limit < 1:
        return "Error: offset must be >= 0 and limit >= 1."
    params: dict[str, Any] = {"oldest": dates[0], "newest": dates[1]}
    if detail_level == "compact" or output_format == "json":
        params["fields"] = ACTIVITY_LIST_FIELDS
    result = await make_intervals_request(url=f"/athlete/{seg(athlete_id)}/activities", api_key=api_key, params=params)
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching activities: {result.get('message', 'Unknown error')}"
    activities = _in_window(_parse_activities_from_result(result), dates[0], dates[1])
    if not include_unnamed:
        activities = _filter_named_activities(activities)
    wanted = {t.strip().lower() for t in (sport_types or "").split(",") if t.strip()}
    if wanted:
        activities = [a for a in activities if str(a.get("type", "")).lower() in wanted]
    if gear_id:
        activities = [a for a in activities if _activity_gear_id(a) == str(gear_id)]
    if power_meter:
        needle = power_meter.strip().lower()
        activities = [a for a in activities if needle in str(a.get("power_meter") or "").lower()]
    total = len(activities)
    page = _sort_activities(activities, sort_by)[offset : offset + limit]
    await resolve_gear_for_activities(page, athlete_id=athlete_id, api_key=api_key)
    filters = ", ".join(
        f for f in (f"types {sport_types}" if sport_types else "", f"gear {gear_id}" if gear_id else "",
                    f"power meter contains '{power_meter}'" if power_meter else "", f"sort {sort_by}") if f
    )
    next_offset = offset + limit if offset + limit < total else None
    if output_format == "json":
        return json.dumps(
            {"start_date": dates[0], "end_date": dates[1], "total": total, "offset": offset, "limit": limit,
             "next_offset": next_offset, "activities": [activity_record(a) for a in page]},
            ensure_ascii=False,
        )
    if not page:
        return f"No activities match ({filters}) for athlete {athlete_id} between {dates[0]} and {dates[1]}."
    header = f"Activities {offset + 1}-{offset + len(page)} of {total} ({dates[0]} to {dates[1]}; {filters}):\n"
    if detail_level == "compact":
        body = "\n".join(_compact_line(a) for a in page)
    else:
        body = "\n".join(format_activity_summary(a) for a in page)
    footer = f"\nNext page: offset={next_offset}" if next_offset is not None else ""
    return header + body + footer
# Selectors of get_activity_streams / get_activity_intervals that fetch every stream.
_ALL_STREAM_SELECTORS = ("all", "custom")


def _parse_activities_from_result(result: Any) -> list[dict[str, Any]]:
    """Extract a list of activity dictionaries from the API result."""
    activities: list[dict[str, Any]] = []

    if isinstance(result, list):
        activities = [item for item in result if isinstance(item, dict)]
    elif isinstance(result, dict):
        # Result is a single activity or a container
        for _key, value in result.items():
            if isinstance(value, list):
                activities = [item for item in value if isinstance(item, dict)]
                break
        # If no list was found but the dict has typical activity fields, treat it as a single activity
        if not activities and any(key in result for key in ["name", "startTime", "distance"]):
            activities = [result]

    return activities


def _filter_named_activities(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter out unnamed activities from the list."""
    return [
        activity
        for activity in activities
        if activity.get("name") and activity.get("name") != "Unnamed"
    ]


def _in_window(activities: list[dict[str, Any]], start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Activities whose local start date lies in [start_date, end_date] (upstream #134: nothing outside the range).

    An activity without a local start date cannot be placed and is kept as returned by the API.
    """
    kept = []
    for activity in activities:
        day = str(activity.get("start_date_local") or "")[:10]
        if day and not start_date <= day <= end_date:
            continue
        kept.append(activity)
    return kept


def _format_activities_response(
    activities: list[dict[str, Any]],
    athlete_id: str,
    include_unnamed: bool,
    note: str = "",
) -> str:
    """Format the activities response based on the results (``note`` is appended)."""
    if not activities:
        if include_unnamed:
            return (
                f"No valid activities found for athlete {athlete_id} in the specified date range."
            )
        return f"No named activities found for athlete {athlete_id} in the specified date range. Try with include_unnamed=True to see all activities."

    # Format the output
    activities_summary = "Activities:\n\n"
    for activity in activities:
        if isinstance(activity, dict):
            activities_summary += format_activity_summary(activity) + "\n"
        else:
            activities_summary += f"Invalid activity format: {activity}\n\n"

    return activities_summary + note


async def _custom_defs(
    item_type: str, api_key: str | None, athlete_id: Any = None
) -> CustomFieldDefs:
    """Custom item definitions of one type for the athlete (cached per process).

    The athlete is taken from the payload (``icu_athlete_id``) when given, otherwise
    from the configured ATHLETE_ID. Without an athlete there are no definitions.
    """
    athlete_id_to_use = str(athlete_id) if athlete_id else config.athlete_id
    if not athlete_id_to_use:
        return {}
    index = await get_custom_item_index(athlete_id=athlete_id_to_use, api_key=api_key)
    return index.get(item_type, {})


def _split_stream_types(stream_types: str) -> list[str]:
    """Split a comma-separated list, trimming blanks and duplicates (order kept)."""
    selected: list[str] = []
    for part in stream_types.split(","):
        name = part.strip()
        if name and name not in selected:
            selected.append(name)
    return selected


def _stream_request_params(
    stream_types: str | None, *, ensure_time: bool
) -> dict[str, str] | None:
    """Translate the stream_types argument into query parameters for the streams endpoint.

    None selects the default streams, "all"/"custom" fetch every stream of the
    activity (no filter), anything else is passed through as given. The time
    stream is added when ensure_time is set, because it carries the timestamps.
    """
    if stream_types is None:
        selected = _split_stream_types(DEFAULT_STREAM_TYPES)
    elif stream_types.strip().lower() in _ALL_STREAM_SELECTORS:
        return None
    else:
        selected = _split_stream_types(stream_types)
    if ensure_time and "time" not in selected:
        selected.insert(0, "time")
    return {"types": ",".join(selected)}


async def _fetch_streams(
    activity_id: str, api_key: str | None, params: dict[str, str] | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch streams for an activity. Returns (streams, error_message)."""
    result = await make_intervals_request(
        url=f"/activity/{seg(activity_id)}/streams",
        api_key=api_key,
        params=params,
    )

    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return [], f"Error fetching activity streams: {error_message}"

    streams = [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []
    if not streams:
        return [], f"No stream data found for activity {activity_id}."
    return streams, None


@tool("read")
async def get_activities(  # pylint: disable=too-many-arguments,too-many-return-statements,too-many-branches,too-many-positional-arguments,too-many-locals
    athlete_id: str | None = None,
    api_key: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 10,
    include_unnamed: bool = False,
    sport_types: str | None = None,
    gear_id: str | None = None,
    sort_by: str = "date_desc",
    offset: int = 0,
    detail_level: str = "summary",
    output_format: str = "text",
    power_meter: str | None = None,
) -> str:
    """Get a list of activities for an athlete from Intervals.icu

    Supports filtering by sport type and gear, sorting, pagination and compact or JSON
    output for large date ranges (e.g. comparing all rides on one bike over a season).
    Without the optional filters the classic summary listing is returned.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        start_date: Start date in YYYY-MM-DD format (optional, defaults to 30 days ago)
        end_date: End date in YYYY-MM-DD format (optional, defaults to today)
        limit: Maximum number of activities to return per page (optional, defaults to 10)
        include_unnamed: Whether to include unnamed activities (optional, defaults to False)
        sport_types: Comma-separated activity types to keep, e.g. "Ride,GravelRide" (optional)
        gear_id: Only activities done on this gear, e.g. "b12472159" (optional; see get_gear_list)
        sort_by: "date_desc" (default), "date_asc", "distance", "moving_time" or "load"
        offset: Number of matching activities to skip for pagination (optional, default 0)
        detail_level: "summary" (default, full text block per activity) or "compact" (one line each)
        output_format: "text" (default) or "json" (records with explicit units, local and UTC start)
        power_meter: Only activities whose power meter name (from the device file) contains this
            text, e.g. "Rally" or "Shimano"; activities without power meter data are excluded (optional)
    """
    # Resolve athlete ID and date parameters
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    start_date, end_date = resolve_date_params(start_date, end_date)

    if any((sport_types, gear_id, power_meter, sort_by != "date_desc", offset, detail_level != "summary", output_format != "text")):
        return await _list_activities_filtered(
            athlete_id_to_use, api_key, (start_date, end_date), limit, include_unnamed,
            sport_types, gear_id, sort_by, offset, detail_level, output_format, power_meter,
        )

    # Fetch more activities if we need to filter out unnamed ones
    api_limit = limit * 3 if not include_unnamed else limit

    # Call the Intervals.icu API
    params = {"oldest": start_date, "newest": end_date, "limit": api_limit}
    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/activities", api_key=api_key, params=params
    )

    # Check for error
    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error fetching activities: {error_message}"

    if not result:
        return f"No activities found for athlete {athlete_id_to_use} in the specified date range."

    # Parse activities from result
    raw = _parse_activities_from_result(result)
    activities = _in_window(raw, start_date, end_date)

    if not activities:
        return f"No valid activities found for athlete {athlete_id_to_use} in the specified date range."

    # Unnamed activities are dropped. When the API stopped at the request limit, the window may hold
    # more named ones: fetch the whole window once more. Activities before start_date are never
    # added (upstream #134); fewer than `limit` are returned when the range holds fewer.
    note = ""
    if not include_unnamed:
        named = _filter_named_activities(activities)
        if len(named) < limit and len(raw) >= api_limit:
            whole = await make_intervals_request(
                url=f"/athlete/{seg(athlete_id_to_use)}/activities", api_key=api_key,
                params={"oldest": start_date, "newest": end_date},
            )
            if isinstance(whole, list):
                named = _filter_named_activities(_in_window(_parse_activities_from_result(whole), start_date, end_date))
        hidden = len(activities) - len(named)
        activities = named
        if len(activities) < limit:
            note = (
                f"Note: {len(activities)} named activities between {start_date} and {end_date} (fewer than the limit "
                f"{limit}; nothing outside the range is added"
                + (f"; {hidden} unnamed hidden, include_unnamed=True shows them" if hidden > 0 else "") + ").\n"
            )

    # Limit to requested count
    activities = activities[:limit]

    # Resolve gear names (in-place injection of `_resolved_gear_name`)
    await resolve_gear_for_activities(
        activities, athlete_id=athlete_id_to_use, api_key=api_key
    )

    return _format_activities_response(activities, athlete_id_to_use, include_unnamed, note)


@tool("read")
async def get_activity_details(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements,too-many-locals
    activity_id: str,
    api_key: str | None = None,
    include_custom_fields: bool = True,
    include_all_fields: bool = False,
    output_format: str = "text",
    detail_level: str = "standard",
) -> str:
    """Get detailed information for a specific activity from Intervals.icu

    Besides the standard summary the result lists every custom activity field the
    athlete has defined on Intervals.icu that has a value on this activity, with
    display name, technical code, value and units (for select fields also the option
    label). This covers metrics that devices write into custom fields, for example
    aerobic/anaerobic training effect, training load, EPOC, recovery time, VO2max,
    performance condition, stamina at start/end, sweat loss and any other field the
    athlete has configured. The fields are read dynamically from the athlete's custom
    item definitions; nothing is hard-coded. 'no value' means null/NaN on Intervals.icu.

    Args:
        activity_id: The Intervals.icu activity ID
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        include_custom_fields: Include the custom activity fields section (optional, default True)
        include_all_fields: Also list every other non-empty field of the raw activity payload
            that is not part of the standard summary, e.g. time in zones, running dynamics,
            power meter details, available stream types (optional, default False)
        output_format: "text" (default) or "json" (raw activity, explicit start times, gear name,
            thresholds snapshot and every custom field with value/units/status)
        detail_level: "compact" (key numbers, thresholds, custom fields assigned to the sport, data
            quality; ~8 lines), "standard" (default, the full summary) or "full" (standard plus every
            other payload field, same as include_all_fields=True)
    """
    if detail_level not in DETAIL_LEVELS_3:
        return f"Error: detail_level must be one of {', '.join(DETAIL_LEVELS_3)}."
    # Call the Intervals.icu API
    result = await make_intervals_request(url=f"/activity/{seg(activity_id)}", api_key=api_key)

    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error fetching activity details: {error_message}"

    # Format the response
    if not result:
        return f"No details found for activity {activity_id}."

    # If result is a list, use the first item if available
    activity_data = result[0] if isinstance(result, list) and result else result
    if not isinstance(activity_data, dict):
        return f"Invalid activity format for activity {activity_id}."

    # Resolve gear name against the activity owner's catalog (falls back to ATHLETE_ID)
    await resolve_gear_for_activity(
        activity_data, athlete_id=activity_data.get("icu_athlete_id"), api_key=api_key
    )

    custom_field_defs: CustomFieldDefs | None = None
    assigned: set[str] | None = None
    if include_custom_fields:
        custom_field_defs = await _custom_defs(
            ACTIVITY_FIELD, api_key, activity_data.get("icu_athlete_id")
        )
        field_ids = await assigned_field_ids(
            str(activity_data.get("icu_athlete_id") or config.athlete_id or ""), api_key, activity_data.get("type")
        )
        assigned = assigned_codes(custom_field_defs, field_ids)

    if output_format.strip().lower() == "json":
        return json.dumps(
            _json_safe(
                {
                    "activity": {k: v for k, v in activity_data.items() if k not in ("skyline_chart_bytes", "_resolved_gear_name")},
                    "times": start_times(activity_data),
                    "gear_name": activity_data.get("_resolved_gear_name"),
                    "custom_fields": custom_fields_json(activity_data, custom_field_defs or {}, assigned),
                    "detail_level": detail_level,
                    "thresholds": {
                        k: activity_data.get(k)
                        for k in ("icu_ftp", "icu_rolling_ftp", "icu_pm_ftp", "icu_pm_cp", "icu_w_prime", "icu_pm_w_prime",
                                  "icu_pm_p_max", "lthr", "athlete_max_hr", "icu_resting_hr", "threshold_pace", "icu_weight",
                                  "icu_power_zones", "icu_hr_zones", "pace_zones", "power_meter", "power_meter_serial",
                                  "power_field_names", "device_name")
                    },
                }
            ),
            ensure_ascii=False,
        )

    if detail_level == "compact":
        return _compact_details(activity_data, custom_field_defs or {}, assigned)
    return format_activity_details(
        activity_data,
        custom_field_defs=custom_field_defs,
        include_all_fields=include_all_fields or detail_level == "full",
        assigned=assigned,
    )


def _interval_stream_metrics(
    interval: dict[str, Any], streams: list[dict[str, Any]], stream_defs: CustomFieldDefs
) -> dict[str, Any]:
    """Per-stream statistics over an interval's sample range (JSON output)."""
    start, end = interval.get("start_index"), interval.get("end_index")
    if not (isinstance(start, int) and isinstance(end, int) and end > start):
        return {}
    out: dict[str, Any] = {}
    for stream in streams:
        if stream.get("type") in NON_METRIC_STREAM_TYPES:
            continue
        info = describe_stream(stream, stream_defs)
        stats = range_stats(stream.get("data") or [], [(start, end)])
        out[info["type"]] = {"name": info["label"], "units": info["units"], **stats}
    return out


def _compact_intervals(
    result: dict[str, Any],
    interval_defs: CustomFieldDefs,
    streams: list[dict[str, Any]] | None,
    stream_defs: CustomFieldDefs,
    plan_map: dict[int, dict[str, Any]] | None = None,
    activity_type: Any = None,
) -> str:
    """One line per interval and group with the key numbers and compact custom data (and the planned step).

    Cadence is shown on the sport's basis when ``activity_type`` is known (steps per minute for
    foot sports, rpm otherwise).
    """
    lines = [f"Intervals of {result.get('id')} (analysed {result.get('analyzed', 'n/a')}):"]
    intervals = [i for i in result.get("icu_intervals") or [] if isinstance(i, dict)]
    for index, interval in enumerate(intervals, 1):
        parts = [
            f"[{index}] {interval.get('type', '?')}" + (f" '{interval['label']}'" if interval.get("label") else ""),
            f"{hms(interval.get('elapsed_time'))} ({hms(interval.get('start_time'))}-{hms(interval.get('end_time'))}, idx {interval.get('start_index')}-{interval.get('end_index')})",
        ]
        if interval.get("average_watts") is not None:
            parts.append(f"avg {interval['average_watts']} W NP {interval.get('weighted_average_watts', 'n/a')} max {interval.get('max_watts', 'n/a')}")
        if interval.get("average_heartrate") is not None:
            parts.append(f"HR {interval['average_heartrate']}/{interval.get('max_heartrate', 'n/a')}")
        if interval.get("average_cadence") is not None:
            parts.append(f"cad {cadence_text(interval['average_cadence'], activity_type)}")
        if interval.get("average_speed"):
            parts.append(f"{interval['average_speed'] * 3.6:.1f} km/h")
        if interval.get("intensity") is not None:
            parts.append(f"IF {interval['intensity']}%")
        custom = format_custom_field_lines(interval, interval_defs, prefix="")
        if custom:
            parts.append("custom " + ", ".join(custom))
        if streams:
            metrics = _interval_stream_metrics(interval, streams, stream_defs)
            compact = [
                f"{t} {_fmt_short(m.get('first'))}→{_fmt_short(m.get('last'))} (min {_fmt_short(m.get('min'))})"
                for t, m in metrics.items() if m.get("non_null")
            ]
            if compact:
                parts.append("streams " + ", ".join(compact))
        if plan_map:
            parts.append(planned_step_text(plan_map.get(index - 1), plan_position(plan_map, index - 1)))
        lines.append(" | ".join(parts))
    for group in result.get("icu_groups") or []:
        if isinstance(group, dict):
            lines.append(
                f"group {group.get('id')}: {group.get('count', '?')} intervals, {hms(group.get('elapsed_time'))}, "
                f"avg {group.get('average_watts', 'n/a')} W, HR {group.get('average_heartrate', 'n/a')}"
            )
    return "\n".join(lines)


def _fmt_short(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "n/a"
    return f"{value:.0f}" if float(value).is_integer() or abs(value) >= 100 else f"{value:.1f}"


async def _activity_payload(activity_id: str, api_key: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """The activity (for its sport, thresholds and owner) and why it could not be loaded."""
    result = await make_intervals_request(url=f"/activity/{seg(activity_id)}", api_key=api_key)
    if isinstance(result, dict) and "error" in result:
        return None, str(result.get("message", "Unknown error"))
    activity = result[0] if isinstance(result, list) and result else result
    if isinstance(activity, dict) and activity:
        return activity, None
    return None, "empty response"


async def _planned_step_types(
    activity: dict[str, Any] | None, intervals: list[dict[str, Any]], api_key: str | None, planned_workout_doc: dict[str, Any] | None
) -> tuple[dict[int, dict[str, Any]], str]:
    """Planned step matched to each interval index (alignment by order, duration and target) and the plan source."""
    from intervals_mcp_server.tools.analysis import _get_event, _threshold_context  # pylint: disable=import-outside-toplevel,protected-access

    if activity is None:
        return {}, "activity could not be loaded"
    steps: Any = None
    source = ""
    if isinstance(planned_workout_doc, dict) and isinstance(planned_workout_doc.get("steps"), list):
        steps, source = planned_workout_doc["steps"], "workout document provided by the caller"
    elif activity.get("paired_event_id"):
        athlete = str(activity.get("icu_athlete_id") or config.athlete_id or "")
        event = await _get_event(athlete, activity["paired_event_id"], api_key) if athlete else None
        if event:
            steps = (event.get("workout_doc") or {}).get("steps")
            source = f"event {event.get('id')} ('{event.get('name')}')"
    if not isinstance(steps, list):
        return {}, "no planned workout paired with this activity"
    return planned_step_map(plan_steps(steps, _threshold_context(activity)), intervals), source


def _plan_mapping_lines(intervals: list[dict[str, Any]], mapping: dict[int, dict[str, Any]], source: str) -> list[str]:
    if not mapping:
        return [f"Planned step types: not available ({source})."]
    lines = [
        f"Planned step per interval ({source}; the Intervals.icu type is kept as stored; an interval longer than its "
        "planned step is split as in analyze_workout_execution - the first part is the plan, the rest is reported as "
        "beyond the plan):"
    ]
    for index, interval in enumerate(intervals):
        step = mapping.get(index)
        expected = "WORK" if step and step["kind"] == "work" else "RECOVERY"
        flag = " <- type differs from the plan" if step and step["kind"] in ("work", "rest") and interval.get("type") != expected else ""
        lines.append(f"  [{index + 1}] Intervals.icu {interval.get('type', '?')} | {planned_step_text(step, plan_position(mapping, index))}{flag}")
    return lines


@tool("read")
async def get_activity_intervals(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements,too-many-locals,too-many-branches,too-many-statements
    activity_id: str,
    api_key: str | None = None,
    stream_types: str | None = None,
    include_custom_fields: bool = True,
    output_format: str = "text",
    detail_level: str = "standard",
    include_planned_types: bool = False,
    planned_workout_doc: dict[str, Any] | None = None,
) -> str:
    """Get interval data for a specific activity from Intervals.icu

    This endpoint returns detailed metrics for each interval in an activity, including power, heart rate,
    cadence, speed, and environmental data. It also includes grouped intervals if applicable.
    Cadence follows the sport (one extra call for the activity): foot sports show steps per
    minute (2 x the per-leg value Intervals.icu stores, which is labelled as stored), other
    sports rpm; running dynamics are only shown for foot sports. Missing temperatures are n/a.

    Custom interval fields defined by the athlete are listed per interval with display
    name, technical code, value and units. With stream_types the raw samples of the given
    streams between each interval's start_index and end_index are evaluated (start, end,
    min, max, mean, delta and the number of non-null samples), which yields per-interval
    values for any stream Intervals.icu does not summarise itself: custom streams such as
    stamina (delta = stamina drop during the interval), performance condition or grade
    adjusted speed, a second power meter (secondary_power), gear selection and so on.
    Groups are evaluated over all their member intervals. Statistics are computed from
    the recorded samples only; missing samples are never interpolated.

    Args:
        activity_id: The Intervals.icu activity ID
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        stream_types: Comma-separated stream types to evaluate per interval, e.g.
            "Stamina,PotentialStamina,secondary_power"; "custom" = every custom stream of the
            activity plus secondary_power; "all" = every stream. Use list_activity_streams to
            see what is available. (optional, default: no stream metrics)
        include_custom_fields: List custom interval fields per interval (optional, default True)
        output_format: "text" (default) or "json" (raw intervals and groups plus custom fields
            and per-interval stream statistics)
        detail_level: "compact" (one line per interval with the key numbers; custom streams as
            start→end), "standard" (default, the full block per interval) or "full" (standard plus
            every custom stream when stream_types is not given)
        include_planned_types: Also show the planned step (type) matched to each interval from the
            paired event or planned_workout_doc; the Intervals.icu WORK/RECOVERY type is kept as
            stored. An interval longer than its planned step (plus the tolerance of
            analyze_workout_execution) shows the planned duration and the time beyond the plan
            on its line and in JSON (planned_step.beyond_plan_s) (optional, default False; one or
            two extra API calls)
        planned_workout_doc: Workout document with "steps" to match against (optional; implies
            include_planned_types)
    """
    if detail_level not in DETAIL_LEVELS_3:
        return f"Error: detail_level must be one of {', '.join(DETAIL_LEVELS_3)}."
    if detail_level == "full" and not stream_types:
        stream_types = "custom"
    # Call the Intervals.icu API
    result = await make_intervals_request(url=f"/activity/{seg(activity_id)}/intervals", api_key=api_key)

    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error fetching intervals: {error_message}"

    # Format the response
    if not result:
        return f"No interval data found for activity {activity_id}."

    # If the result is empty or doesn't contain expected fields
    if not isinstance(result, dict) or not any(
        key in result for key in ["icu_intervals", "icu_groups"]
    ):
        return f"No interval data or unrecognized format for activity {activity_id}."

    if not (result.get("icu_intervals") or result.get("icu_groups")):
        return (
            f"Intervals.icu has no intervals or groups for activity {activity_id} (no laps/structure detected). "
            "Use analyze_climbs for an automatic segmentation into climbs, descents and pauses, or "
            "get_activity_streams for the raw samples."
        )

    # The intervals carry no sport and no athlete: the activity tells how to show cadence (steps
    # per minute on foot) and whose custom definitions label the fields (the owner, e.g. for a coach).
    activity, activity_error = await _activity_payload(activity_id, api_key)
    activity_type = activity.get("type") if activity else None
    owner = activity.get("icu_athlete_id") if activity else None

    interval_field_defs: CustomFieldDefs = {}
    if include_custom_fields:
        interval_field_defs = await _custom_defs(INTERVAL_FIELD, api_key, owner)

    streams: list[dict[str, Any]] | None = None
    stream_defs: CustomFieldDefs = {}
    note = ""
    if stream_types:
        streams, error = await _fetch_streams(
            activity_id, api_key, _stream_request_params(stream_types, ensure_time=True)
        )
        if error:
            note = f"\nNote: stream metrics unavailable. {error}\n"
            streams = None
        else:
            selector = stream_types.strip().lower()
            if selector == "custom":
                streams = [
                    s for s in streams if s.get("custom") or s.get("type") == "secondary_power"
                ]
            elif selector != "all":
                requested = set(_split_stream_types(stream_types)) | {"time"}
                streams = [s for s in streams if s.get("type") in requested]
            stream_defs = await _custom_defs(ACTIVITY_STREAM, api_key, owner)
    if activity_error:
        note += (
            f"\nNote: the activity itself could not be loaded ({activity_error}); cadence is shown as stored "
            "(per leg for runs) and custom labels come from the configured athlete.\n"
        )

    plan_map: dict[int, dict[str, Any]] = {}
    plan_source = ""
    if include_planned_types or planned_workout_doc:
        plan_map, plan_source = await _planned_step_types(
            activity, [i for i in result.get("icu_intervals") or [] if isinstance(i, dict)], api_key, planned_workout_doc
        )

    if output_format.strip().lower() == "json":
        intervals = [i for i in result.get("icu_intervals") or [] if isinstance(i, dict)]
        payload = {
            "id": result.get("id"),
            "analyzed": result.get("analyzed"),
            "activity_type": activity_type,
            "cadence_note": (
                "average/min/max_cadence are stored per leg (rpm); average_cadence_spm = steps per minute (2 x)"
                if cadence_spm(1, activity_type) else None
            ),
            "intervals": [
                {
                    **interval,
                    "average_cadence_spm": cadence_spm(interval.get("average_cadence"), activity_type),
                    "custom_fields": [r for r in custom_fields_json(interval, interval_field_defs) if r["status"] in ("value", "zero")],
                    "stream_metrics": _interval_stream_metrics(interval, streams or [], stream_defs),
                    **(
                        {"planned_step": plan_map.get(index), "plan_position": plan_position(plan_map, index)}
                        if include_planned_types or planned_workout_doc else {}
                    ),
                }
                for index, interval in enumerate(intervals)
            ],
            "groups": [g for g in result.get("icu_groups") or [] if isinstance(g, dict)],
            "note": note.strip() or None,
        }
        return json.dumps(_json_safe(payload), ensure_ascii=False)

    plan_text = ""
    if include_planned_types or planned_workout_doc:
        listed = [i for i in result.get("icu_intervals") or [] if isinstance(i, dict)]
        plan_text = "\n" + "\n".join(_plan_mapping_lines(listed, plan_map, plan_source))
    if detail_level == "compact":
        return _compact_intervals(result, interval_field_defs, streams, stream_defs, plan_map, activity_type) + note + plan_text

    # Format the intervals data
    return (
        format_intervals(
            result,
            interval_field_defs=interval_field_defs,
            streams=streams,
            stream_defs=stream_defs,
            plan_lines={
                index: planned_step_text(plan_map.get(index), plan_position(plan_map, index))
                for index in range(len(result.get("icu_intervals") or []))
            } if plan_map else None,
            activity_type=activity_type,
        )
        + note
        + plan_text
    )


def _render_stream_rows(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    activity_id: str,
    streams: list[dict[str, Any]],
    stream_defs: CustomFieldDefs,
    output_format: str,
    window: tuple[int | None, int | None, int | None, int | None],
    downsample: int,
    max_points: int,
) -> str:
    """Render the selected sample range of the streams as CSV table or one JSON object.

    The output is cut at max_points samples and at the size budget of a tool result; the
    answer says where to continue (start_index), never silently.
    """
    start_index, end_index, start_time, end_time = window
    length = stream_length(streams)
    time_stream = find_stream(streams, "time")
    start, end = resolve_sample_range(
        length,
        time_stream.get("data") if time_stream else None,
        start_index,
        end_index,
        start_time,
        end_time,
    )

    total = len(range(start, end, downsample))
    cut_end = min(end, start + max_points * downsample)

    def render(stop: int) -> str:
        if output_format == "json":
            return json.dumps(render_streams_json(streams, stream_defs, start, stop, downsample), ensure_ascii=False)
        return render_streams_table(streams, start, stop, downsample)

    body = render(cut_end) if cut_end > start else ""
    budget = output_budget()
    size_cut = False
    for _ in range(4):  # shrink until the body fits the size budget of one tool result
        rows = len(range(start, cut_end, downsample))
        if len(body) <= budget or rows <= 1:
            break
        keep = max(1, int(rows * budget / len(body) * 0.9))
        cut_end = start + keep * downsample
        body = render(cut_end)
        size_cut = True

    shown = len(range(start, cut_end, downsample)) if cut_end > start else 0
    next_start = cut_end if cut_end < end else None
    note = ""
    if next_start is not None:
        reason = "the size limit of one tool result" if size_cut else f"max_points={max_points}"
        note = (
            f"Note: output cut to {shown} of {total} samples (indices {start}-{cut_end - 1}) by {reason}. "
            f"Continue with start_index={cut_end}, or select fewer stream_types or raise downsample."
        )
    resolution = "full sample resolution" if downsample == 1 else f"every {downsample}. sample"
    labels = [stream_label(describe_stream(s, stream_defs)) for s in order_streams(streams)]

    if output_format == "json":
        payload = json.loads(body) if body else {"index_start": start, "index_end": start, "downsample": downsample, "streams": {}}
        return json.dumps(
            {
                "activity_id": activity_id,
                "activity_samples": length,
                "selected_samples": total,
                "returned_samples": shown,
                "resolution": resolution,
                "next_start_index": next_start,
                "note": note or ("No samples in the selected range." if not shown else None),
                "time_note": "time = seconds since activity start; null = no value",
                "stream_labels": labels,
                **payload,
            },
            ensure_ascii=False,
        )

    header = (
        f"Activity Streams for {activity_id}: samples {start}-{max(start, cut_end - 1)} of {length} "
        f"({resolution}). time = seconds since activity start; empty/null = no value.\n"
    )
    header += "Streams: " + ", ".join(labels) + "\n"
    if cut_end <= start:
        return header + "No samples in the selected range.\n"
    return header + body + (f"\n{note}\n" if note else "")


@tool("read")
async def get_activity_streams(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    activity_id: str,
    api_key: str | None = None,
    stream_types: str | None = None,
    output_format: str = "summary",
    start_index: int | None = None,
    end_index: int | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
    downsample: int = 1,
    max_points: int = DEFAULT_STREAM_POINTS,
) -> str:
    """Get time-series (stream) data for a specific activity from Intervals.icu

    Any stream the activity has can be requested by its technical type: the standard
    streams (time, watts, heartrate, cadence, altitude, distance, velocity_smooth, temp,
    torque, left_right_balance, hrv, respiration, secondary_power, ...) and every custom
    stream the athlete has defined, addressed by its code (for example Stamina,
    PotentialStamina, GarminGASpeed, FrontGear, RearGear ...). Use list_activity_streams
    to discover what an activity offers. All streams of an activity are sample-aligned:
    index i of every stream belongs to the same recorded sample and time[i] is its offset
    in seconds from the activity start. Recording pauses appear as jumps in time, so use
    the time column, not the index, as the timestamp.

    Args:
        activity_id: The Intervals.icu activity ID
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        stream_types: Comma-separated stream types to retrieve, or "all" for every stream of
            the activity (optional, defaults to time,watts,heartrate,cadence,altitude,distance,velocity_smooth)
        output_format: "summary" (default) = per-stream metadata, statistics and a short preview;
            "full" = CSV table with one row per sample (index, time, one column per stream,
            empty cell = no value); "json" = ONE JSON object with the same selection as arrays
            (null = no value), the sample range, next_start_index and a note.
            The time stream is always included in full/json output.
        start_index: First sample index of the full/json output (optional, default 0; matches
            start_index of get_activity_intervals)
        end_index: Sample index to stop before in full/json output (optional, default = end;
            matches end_index of get_activity_intervals, which is exclusive)
        start_time: Only samples with time >= start_time seconds (optional, full/json)
        end_time: Only samples with time < end_time seconds (optional, full/json)
        downsample: Keep every n-th recorded sample in full/json output (optional, default 1 =
            full sample resolution; samples are skipped, never averaged)
        max_points: Maximum number of samples in full/json output (optional, default 2000,
            at most 20000). Longer selections, and selections too large for one tool result
            (many streams), are cut and the response says how to continue (page with
            start_index, or raise downsample / select fewer streams).
    """
    output_format = (output_format or "summary").strip().lower()
    if output_format not in STREAM_OUTPUT_FORMATS:
        return f"Error: output_format must be one of {', '.join(STREAM_OUTPUT_FORMATS)}."
    if downsample < 1 or max_points < 1:
        return "Error: downsample and max_points must be positive integers."
    max_points = min(max_points, MAX_STREAM_POINTS)

    params = _stream_request_params(stream_types, ensure_time=output_format != "summary")
    streams, error = await _fetch_streams(activity_id, api_key, params)
    if error:
        return error

    owner = None
    if any(s.get("custom") for s in streams):
        # Custom streams are labelled with the definitions of the activity's owner.
        activity, _ = await _activity_payload(activity_id, api_key)
        owner = activity.get("icu_athlete_id") if activity else None
    stream_defs = await _custom_defs(ACTIVITY_STREAM, api_key, owner)

    if output_format == "summary":
        return format_streams_summary(activity_id, streams, stream_defs)

    return _render_stream_rows(
        activity_id,
        streams,
        stream_defs,
        output_format,
        (start_index, end_index, start_time, end_time),
        downsample,
        max_points,
    )


def _format_stream_listing(
    activity_id: str,
    activity: dict[str, Any],
    available: list[Any],
    streams: list[dict[str, Any]],
    stream_defs: CustomFieldDefs,
) -> str:
    """Render the stream listing of list_activity_streams."""
    by_type = {s.get("type"): s for s in streams}
    standard: list[str] = []
    custom: list[str] = []
    for stream_type in available:
        stream = by_type.get(stream_type) or {
            "type": stream_type,
            "custom": stream_type in stream_defs,
        }
        info = describe_stream(stream, stream_defs)
        line = f"- {stream_label(info)}"
        if info["description"]:
            line += f": {info['description']}"
        data = stream.get("data")
        if isinstance(data, list) and stream_type != "latlng":
            line += f" | {format_stats(range_stats(data, [(0, len(data))]))}"
        (custom if info["custom"] else standard).append(line)

    output = (
        f"Streams available for activity {activity_id} ('{activity.get('name', 'Unnamed')}', "
        f"start {format_start_times(activity)}, "
        f"elapsed {activity.get('elapsed_time', 'N/A')} s):\n"
    )
    power_fields = activity.get("power_field_names")
    if isinstance(power_fields, list) and power_fields:
        output += f"Power fields: {', '.join(str(p) for p in power_fields)}\n"
    output += f"\nStandard streams ({len(standard)}):\n" + "\n".join(standard or ["- (none)"]) + "\n"
    output += f"\nCustom streams ({len(custom)}):\n" + "\n".join(custom or ["- (none)"]) + "\n"
    output += (
        "\nRequest samples with get_activity_streams(activity_id, stream_types='time,watts,<type>', "
        "output_format='full') and evaluate streams per interval with "
        "get_activity_intervals(activity_id, stream_types='<type>').\n"
    )
    return output


@tool("read")
async def list_activity_streams(
    activity_id: str,
    api_key: str | None = None,
    include_stats: bool = False,
) -> str:
    """List every data stream available for an activity on Intervals.icu

    Returns the standard streams and the custom streams (defined by the athlete, for
    example streams a device records such as stamina, potential stamina, performance
    condition, grade adjusted speed, gear selection, battery ...) present on the activity,
    with display name, units and description. Use the listed stream types with
    get_activity_streams (full sample data) or get_activity_intervals(stream_types=...)
    (per-interval statistics).

    Args:
        activity_id: The Intervals.icu activity ID
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        include_stats: Also download the streams and report sample count, non-null count and
            start/end/min/max/mean per stream (optional, default False)
    """
    result = await make_intervals_request(url=f"/activity/{seg(activity_id)}", api_key=api_key)

    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error fetching activity details: {error_message}"

    activity = result[0] if isinstance(result, list) and result else result
    if not isinstance(activity, dict) or not activity:
        return f"No details found for activity {activity_id}."

    stream_defs = await _custom_defs(ACTIVITY_STREAM, api_key, activity.get("icu_athlete_id"))

    available = activity.get("stream_types")
    streams: list[dict[str, Any]] = []
    if include_stats or not isinstance(available, list) or not available:
        streams, error = await _fetch_streams(activity_id, api_key, None)
        if error and not available:
            return error
        if streams:
            available = [s.get("type") for s in streams]

    if not isinstance(available, list) or not available:
        return f"No streams found for activity {activity_id}."

    return _format_stream_listing(activity_id, activity, available, streams, stream_defs)


@tool("read")
async def get_activity_messages(activity_id: str, api_key: str | None = None) -> str:
    """Get messages (notes/comments) for a specific activity from Intervals.icu

    Args:
        activity_id: The Intervals.icu activity ID
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
    """
    result = await make_intervals_request(
        url=f"/activity/{seg(activity_id)}/messages",
        api_key=api_key,
    )

    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error fetching activity messages: {error_message}"

    if not result:
        return f"No messages found for activity {activity_id}."

    messages = result if isinstance(result, list) else []
    if not messages:
        return f"No messages found for activity {activity_id}."

    output = f"Messages for activity {activity_id}:\n\n"
    for msg in messages:
        if isinstance(msg, dict):
            output += format_activity_message(msg) + "\n\n"

    return output


@tool("write")
async def add_activity_message(
    activity_id: str,
    content: str,
    api_key: str | None = None,
) -> str:
    """Add a message (note/comment) to an activity on Intervals.icu

    Args:
        activity_id: The Intervals.icu activity ID
        content: The message text to add
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
    """
    if not (content or "").strip():
        return "Error: content must not be blank; nothing was posted."
    result = await make_intervals_request(
        url=f"/activity/{seg(activity_id)}/messages",
        api_key=api_key,
        method="POST",
        data={"content": content},
    )

    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error adding message to activity: {error_message}"

    if not result or not isinstance(result, dict):
        return "Error: Unexpected response when adding message."

    msg_id = result.get("id")
    if msg_id is not None:
        return f"Successfully added message (ID: {msg_id}) to activity {activity_id}."
    return f"Message appears to have been added to activity {activity_id}, but no ID was returned. Please verify manually."


@tool("write", overwrites=True)
async def update_activity(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements
    activity_id: str,
    rpe: int | None = None,
    feel: int | None = None,
    name: str | None = None,
    description: str | None = None,
    api_key: str | None = None,
) -> str:
    """WRITE TOOL: modifies an existing activity in Intervals.icu (PUT /activity/{id}).

    Only the fields that are passed are sent; all other activity values stay untouched.
    name and description REPLACE the current text (read it first with get_activity_details to
    extend it). At least one of rpe, feel, name or description must be provided.
    Activities imported from Strava cannot be updated via the API; the API returns an error.

    Args:
        activity_id: The Intervals.icu activity ID
        rpe: Rate of perceived exertion, integer 1-10 (sent as `icu_rpe`; 1 = very easy, 10 = maximal)
        feel: How the athlete felt, integer 1-5 (1 = Strong, 2 = Good, 3 = Normal, 4 = Poor, 5 = Weak)
        name: New activity name
        description: New activity description
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
    """
    if rpe is not None and (isinstance(rpe, bool) or not 1 <= rpe <= 10):
        return "Error: rpe must be an integer between 1 and 10."
    if feel is not None and (isinstance(feel, bool) or not 1 <= feel <= 5):
        return "Error: feel must be an integer between 1 and 5 (1 = Strong ... 5 = Weak)."

    if name is not None and not name.strip():
        return "Error: name must not be blank."

    payload: dict[str, Any] = {}
    if rpe is not None:
        payload["icu_rpe"] = rpe
    if feel is not None:
        payload["feel"] = feel
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description

    if not payload:
        return "Error: at least one of rpe, feel, name or description must be provided."

    result = await make_intervals_request(
        url=f"/activity/{seg(activity_id)}",
        api_key=api_key,
        method="PUT",
        data=payload,
    )

    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error updating activity: {error_message}"

    activity_data = result[0] if isinstance(result, list) and result else result
    if not activity_data or not isinstance(activity_data, dict):
        return f"Error: Unexpected response when updating activity {activity_id}."

    await resolve_gear_for_activity(activity_data, api_key=api_key)
    # List the changed fields with the values returned by the API, because the summary
    # formatter prefers `perceived_exertion` over `icu_rpe` and could show a stale RPE.
    updated = ", ".join(f"{field}={activity_data.get(field)!r}" for field in payload)
    return (
        f"Successfully updated activity {activity_id}.\nUpdated: {updated}\n\n"
        f"{format_activity_summary(activity_data)}"
    )
