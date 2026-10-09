"""
Analysis MCP tools for Intervals.icu: dual power meter comparison and planned-vs-executed
workout analysis. Climb/descent segmentation lives in tools/climbs.py.

All tools are read-only; they combine the activity, its intervals, its streams and (when
paired) the planned workout, and compute statistics from recorded samples only.
"""

import difflib
import json
from datetime import date, timedelta
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
from intervals_mcp_server.utils.sports import format_start_times, hms, start_times
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


async def _match_candidates(  # pylint: disable=too-many-locals
    athlete_id: str, activity: dict[str, Any], api_key: str | None
) -> list[dict[str, Any]]:
    """Read-only suggestion of planned workouts that could belong to an unpaired activity.

    Looks at WORKOUT events on the activity's day and the day before/after and scores them
    by sport, planned vs actual moving time and name similarity. Nothing is paired or changed.
    """
    day = str(activity.get("start_date_local", ""))[:10]
    try:
        start = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        end = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    except ValueError:
        return []
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id}/events", api_key=api_key,
        params={"oldest": start, "newest": end, "category": "WORKOUT"},
    )
    if not isinstance(result, list):
        return []
    actual = activity.get("moving_time") or activity.get("elapsed_time") or 0
    name = str(activity.get("name") or "").lower()
    candidates: list[dict[str, Any]] = []
    for event in result:
        if not isinstance(event, dict):
            continue
        score = 0.0
        reasons = []
        if str(event.get("type")) == str(activity.get("type")):
            score += 0.4
            reasons.append("same sport")
        planned = event.get("moving_time") or 0
        if planned and actual:
            ratio = min(planned, actual) / max(planned, actual)
            score += 0.4 * ratio
            reasons.append(f"duration {hms(planned)} planned vs {hms(actual)} actual")
        similarity = difflib.SequenceMatcher(None, name, str(event.get("name") or "").lower()).ratio()
        score += 0.2 * similarity
        if similarity > 0.5:
            reasons.append(f"name similarity {similarity:.0%}")
        if str(event.get("start_date_local", ""))[:10] != day:
            score -= 0.2
            reasons.append("different day")
        if event.get("paired_activity_id"):
            reasons.append(f"already paired with {event['paired_activity_id']}")
            score -= 0.3
        candidates.append({"event_id": event.get("id"), "name": event.get("name"), "date": str(event.get("start_date_local", ""))[:10],
                           "type": event.get("type"), "moving_time": planned, "score": round(score, 2), "reasons": reasons})
    candidates.sort(key=lambda c: float(c["score"]), reverse=True)
    return candidates[:3]


def _stream_types_for_execution(activity: dict[str, Any], stream_defs: dict[str, Any]) -> list[str]:
    available = [str(t) for t in (activity.get("stream_types") or [])]
    wanted = [t for t in CORE_STREAMS if t in available or t == "time"]
    wanted += [t for t in available if t in stream_defs]
    return wanted


@tool("read")
async def analyze_workout_execution(  # pylint: disable=too-many-locals,too-many-branches,too-many-arguments,too-many-positional-arguments,too-many-statements
    activity_id: str,
    api_key: str | None = None,
    event_id: str | None = None,
    planned_workout_doc: dict[str, Any] | None = None,
    suggest_matches: bool = True,
    output_format: str = "text",
) -> str:
    """Compare a planned workout with how it was actually executed (or analyse the intervals alone)

    Uses, in this order, the given planned_workout_doc, the given event_id, or the event
    paired with the activity, together with the intervals Intervals.icu detected. Planned
    steps (repeats expanded) are aligned with the actual intervals by order, duration and
    target; for each step it reports planned vs actual duration, the target range (resolved
    to W, bpm or pace from the thresholds stored with the activity), the actual average,
    below/in/above target and the time within the target range (±5%), HR start/end and the
    HR drop in the first minute of the following interval, cadence, power/speed fade,
    Pw:HR drift for steady efforts of 10 min or more, and the change of every custom stream
    (e.g. stamina) during the step. Training done after the last planned step (a longer
    ride home, an extra effort) is reported as additional training with its own metrics
    and is not counted against the plan. Without a plan the same metrics are reported per
    detected interval; for an unpaired activity up to three planned workouts of the same
    days are suggested as possible matches (read-only, nothing is paired or changed). The
    content of a deleted event is never reconstructed; pass planned_workout_doc instead.

    Args:
        activity_id: The Intervals.icu activity ID
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        event_id: Planned workout (event) to compare against (optional; default: the event
            paired with the activity, if any)
        planned_workout_doc: Workout document with "steps" (same format as add_or_update_event)
            to compare against, e.g. when the calendar event no longer exists (optional)
        suggest_matches: For unpaired activities without a plan, list candidate events of the
            same days as a read-only suggestion (optional, default True)
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
    steps: Any = None
    plan_source = ""
    if isinstance(planned_workout_doc, dict) and isinstance(planned_workout_doc.get("steps"), list):
        steps = planned_workout_doc["steps"]
        plan_source = "workout document provided by the caller"
    paired = event_id or activity.get("paired_event_id")
    if steps is None and paired and athlete_id:
        event = await _get_event(athlete_id, paired, api_key)
        steps = (event or {}).get("workout_doc", {}).get("steps") if event else None
        if event:
            plan_source = f"event {event.get('id')} ('{event.get('name')}', {str(event.get('start_date_local', ''))[:10]})"
    planned = plan_steps(steps, _threshold_context(activity)) if isinstance(steps, list) else []
    candidates: list[dict[str, Any]] = []
    if not planned and not paired and suggest_matches and athlete_id:
        candidates = await _match_candidates(athlete_id, activity, api_key)

    result = analyze(planned, intervals, streams)
    pace_based = str(activity.get("type")) in PACE_SPORTS or (event or {}).get("target") == "PACE"

    device_lines = format_custom_field_lines(activity, field_defs, prefix="")
    header = f"Workout execution for {_activity_header(activity)}"
    if planned:
        header += f"\nPlan source: {plan_source}"
    elif paired:
        header += f"\nPlanned workout {paired} could not be loaded; analysing intervals only."
    else:
        header += "\nNo planned workout paired with this activity (paired_event_id is empty); analysing intervals only."
        if candidates:
            header += "\nPossible planned workouts (read-only suggestion, nothing was paired):"
            for cand in candidates:
                header += f"\n  - event {cand['event_id']} '{cand['name']}' {cand['date']} {cand['type']} {hms(cand['moving_time'])}, score {cand['score']} ({'; '.join(cand['reasons'])})"
        elif suggest_matches:
            header += "\nNo planned workouts found on the activity's day or the days around it."
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
            "plan_source": plan_source or None,
            "match_candidates": candidates,
            "thresholds": _threshold_context(activity),
            "custom_fields": [row for row in custom_fields_json(activity, field_defs) if row["status"] in ("value", "zero")],
            "summary": result["summary"],
            "rows": result["rows"],
            "extension": result.get("extension"),
        }
        return json.dumps(payload, ensure_ascii=False)
    return format_execution(result, header, pace_based)
