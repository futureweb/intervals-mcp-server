"""
Analysis MCP tools for Intervals.icu: dual power meter comparison and planned-vs-executed
workout analysis. Climb/descent segmentation lives in tools/climbs.py.

All tools are read-only; they combine the activity, its intervals, its streams and (when
paired) the planned workout, and compute statistics from recorded samples only.
"""

import json
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.utils.custom_fields import (
    ACTIVITY_FIELD,
    ACTIVITY_STREAM,
    custom_fields_json,
    format_custom_field_lines,
)
from intervals_mcp_server.utils.execution import analyze, format_execution, plan_steps
from intervals_mcp_server.utils.power_compare import (
    compare_power_streams as compute_power_comparison,
    comparison_to_json,
    format_power_comparison,
)
from intervals_mcp_server.utils.sports import format_start_times, start_times
from intervals_mcp_server.utils.streams import find_stream

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

PACE_SPORTS = ("Run", "TrailRun", "VirtualRun", "Walk", "Hike", "Swim", "OpenWaterSwim")
CORE_STREAMS = ("time", "watts", "heartrate", "cadence", "velocity_smooth", "distance", "altitude")


async def _get_activity(activity_id: str, api_key: str | None) -> tuple[dict[str, Any] | None, str | None]:
    result = await make_intervals_request(url=f"/activity/{activity_id}", api_key=api_key)
    if isinstance(result, dict) and "error" in result:
        return None, f"Error fetching activity details: {result.get('message', 'Unknown error')}"
    activity = result[0] if isinstance(result, list) and result else result
    if not isinstance(activity, dict) or not activity:
        return None, f"No details found for activity {activity_id}."
    return activity, None


async def _get_streams(
    activity_id: str, api_key: str | None, types: list[str] | None
) -> tuple[list[dict[str, Any]], str | None]:
    params = {"types": ",".join(types)} if types else None
    result = await make_intervals_request(
        url=f"/activity/{activity_id}/streams", api_key=api_key, params=params
    )
    if isinstance(result, dict) and "error" in result:
        return [], f"Error fetching activity streams: {result.get('message', 'Unknown error')}"
    streams = [s for s in result if isinstance(s, dict)] if isinstance(result, list) else []
    return streams, None if streams else f"No stream data found for activity {activity_id}."


async def _defs(item_type: str, api_key: str | None, athlete_id: Any) -> dict[str, dict[str, Any]]:
    athlete = str(athlete_id) if athlete_id else config.athlete_id
    if not athlete:
        return {}
    return (await get_custom_item_index(athlete_id=athlete, api_key=api_key)).get(item_type, {})


def _activity_header(activity: dict[str, Any]) -> str:
    return (
        f"{activity.get('name', 'Unnamed')} ({activity.get('id')}, {activity.get('type', '?')}, "
        f"{format_start_times(activity)})"
    )


# ------------------------------------------------------------------ power meters
@tool("read")
async def compare_power_streams(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    activity_id: str,
    api_key: str | None = None,
    primary: str = "watts",
    secondary: str = "secondary_power",
    start_index: int | None = None,
    end_index: int | None = None,
    output_format: str = "text",
) -> str:
    """Compare two power streams of an activity sample by sample (e.g. head unit vs. second power meter)

    Compares the primary power stream with a second one recorded on the same activity
    (Intervals.icu stores a second power meter as stream type "secondary_power", shown as
    "Power2" in power_field_names). Reports the overall offset in W and %, the offset per
    power band, stable 30 s / 60 s windows only, drift over the ride quarters, a lag estimate,
    best efforts per duration from each stream and how many samples were excluded as
    coasting, missing or outliers. No calibration is performed and nothing is written; which
    physical sensor each stream belongs to must be taken from the device data shown in the
    header (power meter name/serial), not assumed.

    Args:
        activity_id: The Intervals.icu activity ID
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        primary: Stream type of the primary power source (default "watts")
        secondary: Stream type of the second power source (default "secondary_power")
        start_index: First sample index to compare (optional, e.g. an interval's start_index)
        end_index: Sample index to stop before (optional, e.g. an interval's end_index)
        output_format: "text" (default) or "json"
    """
    activity, error = await _get_activity(activity_id, api_key)
    if error or activity is None:
        return error or "Error"
    streams, error = await _get_streams(activity_id, api_key, ["time", primary, secondary])
    if error:
        return error
    first, second, time_stream = (find_stream(streams, t) for t in (primary, secondary, "time"))
    if first is None or second is None:
        available = ", ".join(str(t) for t in (activity.get("stream_types") or []))
        return (
            f"Activity {activity_id} has no '{primary if first is None else secondary}' stream. "
            f"Available streams: {available or 'unknown (use list_activity_streams)'}."
        )
    time_data = (time_stream or {}).get("data") or []
    sl = slice(start_index or 0, end_index)
    result = compute_power_comparison(
        (first.get("data") or [])[sl], (second.get("data") or [])[sl], time_data[sl]
    )
    device = ", ".join(
        f"{label} {activity[key]}"
        for key, label in (
            ("device_name", "device"),
            ("power_meter", "power meter"),
            ("power_meter_serial", "serial"),
            ("power_field", "primary power field"),
        )
        if activity.get(key)
    )
    fields = activity.get("power_field_names")
    if isinstance(fields, list) and fields:
        device += f"; power fields in file: {', '.join(str(f) for f in fields)}"
    header = (
        f"Power stream comparison for {_activity_header(activity)}\n"
        f"Primary '{primary}' vs secondary '{secondary}'"
        + (f", samples {start_index or 0}-{(end_index or len(time_data)) - 1}" if start_index or end_index else "")
        + f"\nDevice data: {device or 'not available'}\n"
    )
    if output_format.strip().lower() == "json":
        payload = {
            "activity": {"id": activity.get("id"), "name": activity.get("name"), **start_times(activity)},
            "primary": primary,
            "secondary": secondary,
            "device": {k: activity.get(k) for k in ("device_name", "power_meter", "power_meter_serial", "power_field", "power_field_names")},
            "comparison": comparison_to_json(result),
        }
        return json.dumps(payload, ensure_ascii=False)
    return header + "\n" + format_power_comparison(result, primary, secondary)


# ------------------------------------------------------------- workout execution
def _threshold_context(activity: dict[str, Any]) -> dict[str, Any]:
    return {
        "ftp": activity.get("icu_ftp"),
        "lthr": activity.get("lthr"),
        "max_hr": activity.get("athlete_max_hr"),
        "threshold_pace": activity.get("threshold_pace"),
        "power_zones": activity.get("icu_power_zones"),
        "hr_zones": activity.get("icu_hr_zones"),
        "pace_zones": activity.get("pace_zones"),
    }


async def _get_event(athlete_id: str, event_id: Any, api_key: str | None) -> dict[str, Any] | None:
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id}/events/{event_id}", api_key=api_key, params={"resolve": "true"}
    )
    return result if isinstance(result, dict) and "error" not in result else None


def _stream_types_for_execution(activity: dict[str, Any], stream_defs: dict[str, Any]) -> list[str]:
    available = [str(t) for t in (activity.get("stream_types") or [])]
    wanted = [t for t in CORE_STREAMS if t in available or t == "time"]
    wanted += [t for t in available if t in stream_defs]
    return wanted


@tool("read")
async def analyze_workout_execution(  # pylint: disable=too-many-locals,too-many-branches
    activity_id: str,
    api_key: str | None = None,
    event_id: str | None = None,
    output_format: str = "text",
) -> str:
    """Compare a planned workout with how it was actually executed (or analyse the intervals alone)

    Uses the planned workout paired with the activity (or the given event_id) and the
    intervals Intervals.icu detected. Planned steps (repeats expanded) are aligned with the
    actual intervals in order; for each step it reports planned vs actual duration, the
    target range (resolved to W, bpm or pace from the thresholds stored with the activity),
    the actual average, below/in/above target and the time within the target range (±5%),
    HR start/end and the HR drop in the first minute of the following interval, cadence,
    power/speed fade (second half vs first half), Pw:HR drift for steady efforts of 10 min
    or more, and the change of every custom stream (e.g. stamina) during the step. Without
    a plan the same metrics are reported per detected interval. Intervals.icu's own
    compliance value, RPE/feel and the device metrics stored as custom fields (training
    effect, performance condition ...) are included. No interpretation is made.

    Args:
        activity_id: The Intervals.icu activity ID
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        event_id: Planned workout (event) to compare against (optional; default: the event
            paired with the activity, if any)
        output_format: "text" (default) or "json"
    """
    activity, error = await _get_activity(activity_id, api_key)
    if error or activity is None:
        return error or "Error"
    athlete_id = str(activity.get("icu_athlete_id") or config.athlete_id or "")
    intervals_result = await make_intervals_request(url=f"/activity/{activity_id}/intervals", api_key=api_key)
    intervals: list[dict[str, Any]] = []
    if isinstance(intervals_result, dict) and "error" not in intervals_result:
        intervals = [i for i in intervals_result.get("icu_intervals") or [] if isinstance(i, dict)]

    stream_defs = await _defs(ACTIVITY_STREAM, api_key, athlete_id)
    field_defs = await _defs(ACTIVITY_FIELD, api_key, athlete_id)
    streams, _ = await _get_streams(activity_id, api_key, _stream_types_for_execution(activity, stream_defs))

    event: dict[str, Any] | None = None
    paired = event_id or activity.get("paired_event_id")
    if paired and athlete_id:
        event = await _get_event(athlete_id, paired, api_key)
    steps = (event or {}).get("workout_doc", {}).get("steps") if event else None
    planned = plan_steps(steps, _threshold_context(activity)) if isinstance(steps, list) else []

    result = analyze(planned, intervals, streams)
    pace_based = str(activity.get("type")) in PACE_SPORTS or (event or {}).get("target") == "PACE"

    device_lines = format_custom_field_lines(activity, field_defs, prefix="")
    header = f"Workout execution for {_activity_header(activity)}"
    if event:
        header += f"\nPlanned workout: {event.get('name')} (event {event.get('id')}, {event.get('start_date_local', '')[:10]})"
    elif paired:
        header += f"\nPlanned workout {paired} could not be loaded; analysing intervals only."
    else:
        header += "\nNo planned workout paired with this activity; analysing intervals only."
    extras = []
    if activity.get("compliance") is not None:
        extras.append(f"Intervals.icu compliance {activity['compliance']:.0f}%")
    if activity.get("icu_rpe") is not None:
        extras.append(f"RPE {activity['icu_rpe']}/10")
    if activity.get("feel") is not None:
        extras.append(f"feel {activity['feel']}/5")
    if activity.get("icu_training_load") is not None:
        extras.append(f"load {activity['icu_training_load']} (Intervals.icu)")
    if extras:
        header += "\n" + ", ".join(extras)
    if device_lines:
        header += "\nDevice/custom fields: " + "; ".join(device_lines)

    if output_format.strip().lower() == "json":
        payload = {
            "activity": {
                "id": activity.get("id"), "name": activity.get("name"), "type": activity.get("type"),
                **start_times(activity), "compliance": activity.get("compliance"),
                "rpe": activity.get("icu_rpe"), "feel": activity.get("feel"),
                "training_load": activity.get("icu_training_load"),
            },
            "event": {"id": event.get("id"), "name": event.get("name"), "start_date_local": event.get("start_date_local")} if event else None,
            "thresholds": _threshold_context(activity),
            "custom_fields": [row for row in custom_fields_json(activity, field_defs) if row["status"] in ("value", "zero")],
            "summary": result["summary"],
            "rows": result["rows"],
        }
        return json.dumps(payload, ensure_ascii=False)
    return format_execution(result, header, pace_based)
