"""
One-call activity report: overview, plan-vs-execution (or intervals), power meter check and
climb segmentation from a single set of API requests (activity, intervals, streams, event).

Designed for token- and API-efficient coaching conversations: four requests at most, every
section compact, and data-quality notes instead of guesses.
"""

import json
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.activities import _compact_details, _compact_intervals  # pylint: disable=protected-access
from intervals_mcp_server.tools.analysis import PACE_SPORTS, _get_event, _threshold_context  # pylint: disable=protected-access
from intervals_mcp_server.tools.athlete import assigned_field_ids
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.tools.gear import resolve_gear_for_activity
from intervals_mcp_server.utils.custom_fields import ACTIVITY_FIELD, ACTIVITY_STREAM, INTERVAL_FIELD, assigned_codes
from intervals_mcp_server.utils.execution import analyze, format_execution, plan_steps
from intervals_mcp_server.utils.power_compare import compare_power_streams as compute_power_comparison
from intervals_mcp_server.utils.segments import detect_segments
from intervals_mcp_server.utils.sports import hms
from intervals_mcp_server.utils.streams import find_stream

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

CORE_STREAMS = ("time", "watts", "heartrate", "cadence", "velocity_smooth", "distance", "altitude", "secondary_power")
MAX_CLIMBS = 6


def _power_check(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    first, second, time_stream = (find_stream(streams, t) for t in ("watts", "secondary_power", "time"))
    if first is None or second is None or time_stream is None:
        return None
    result = compute_power_comparison(first.get("data") or [], second.get("data") or [], time_stream.get("data") or [])
    overall = result.get("overall") or {}
    stable = (result.get("stable_windows") or {}).get(60) or {}
    return {
        "paired_valid": result.get("paired_valid"),
        "mean_diff_pct": overall.get("mean_diff_pct"),
        "ratio": overall.get("ratio_secondary_to_primary"),
        "stable_60s_diff_pct": stable.get("mean_diff_pct"),
        "stable_60s_windows": stable.get("windows"),
        "lag_s": (result.get("lag") or {}).get("best_lag_s"),
    }


def _climb_summary(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    result = detect_segments(streams)
    if result.get("error"):
        return None
    climbs = [s for s in result.get("segments", []) if s.get("type") == "climb"]
    climbs.sort(key=lambda s: s.get("elevation_gain_m") or 0, reverse=True)
    return {"summary": result.get("summary"), "climbs": climbs[:MAX_CLIMBS], "pauses": len(result.get("pauses", []))}


def _format_climbs(data: dict[str, Any]) -> list[str]:
    summary = data.get("summary") or {}
    lines = [
        f"Climbs: {summary.get('climbs', 0)} (+{summary.get('total_climb_gain_m', 0):.0f} m), descents {summary.get('descents', 0)}, "
        f"pause time {hms(summary.get('pause_time_s'))}, {data.get('pauses', 0)} pause(s)"
    ]
    for climb in data.get("climbs", []):
        lines.append(
            f"  {hms(climb.get('start_time'))}-{hms(climb.get('end_time'))}: +{climb.get('elevation_gain_m') or 0:.0f} m over "
            f"{(climb.get('distance_m') or 0) / 1000:.1f} km, {climb.get('avg_grade_pct') or 0:.1f}%, VAM {climb.get('vam_m_per_h') or 'n/a'} m/h, "
            f"avg {climb.get('avg_watts') or 'n/a'} W, NP {climb.get('normalized_power') or 'n/a'} W, HR {climb.get('avg_hr') or 'n/a'}"
        )
    return lines


@tool("read")
async def get_activity_report(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-arguments,too-many-positional-arguments
    activity_id: str,
    api_key: str | None = None,
    planned_workout_doc: dict[str, Any] | None = None,
    include_climbs: bool | None = None,
    output_format: str = "text",
) -> str:
    """Complete compact analysis of one activity in a single call (overview, plan vs execution, power meters, climbs)

    Loads the activity, its intervals, one set of streams (time, power, HR, cadence, speed,
    distance, altitude, second power meter and every custom stream) and, when paired or
    provided, the planned workout, then reports: a compact overview with thresholds, device
    data and the custom fields assigned to the sport; the plan-vs-execution analysis (or the
    intervals when there is no plan); a short second-power-meter check when two power streams
    exist; a climb summary for activities without intervals (or on request); and data-quality
    notes. Use the specialised tools for the full detail of any section. Read-only.

    Args:
        activity_id: The Intervals.icu activity ID
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        planned_workout_doc: Workout document with "steps" to compare against when the calendar
            event no longer exists (optional)
        include_climbs: Force (True) or suppress (False) the climb section; default: only when
            the activity has no intervals or more than 500 m of elevation gain
        output_format: "text" (default) or "json"
    """
    result = await make_intervals_request(url=f"/activity/{activity_id}", api_key=api_key)
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching activity details: {result.get('message', 'Unknown error')}"
    activity = result[0] if isinstance(result, list) and result else result
    if not isinstance(activity, dict) or not activity:
        return f"No details found for activity {activity_id}."
    athlete_id = str(activity.get("icu_athlete_id") or config.athlete_id or "")
    await resolve_gear_for_activity(activity, athlete_id=athlete_id or None, api_key=api_key)
    index = await get_custom_item_index(athlete_id=athlete_id, api_key=api_key) if athlete_id else {}
    field_defs, stream_defs, interval_defs = (index.get(t, {}) for t in (ACTIVITY_FIELD, ACTIVITY_STREAM, INTERVAL_FIELD))
    assigned = assigned_codes(field_defs, await assigned_field_ids(athlete_id, api_key, activity.get("type")))

    intervals_result = await make_intervals_request(url=f"/activity/{activity_id}/intervals", api_key=api_key)
    intervals_payload = intervals_result if isinstance(intervals_result, dict) and "error" not in intervals_result else {}
    intervals = [i for i in intervals_payload.get("icu_intervals") or [] if isinstance(i, dict)]

    available = [str(t) for t in (activity.get("stream_types") or [])]
    wanted = [t for t in CORE_STREAMS if t in available or t == "time"] + [t for t in available if t in stream_defs]
    streams_result = await make_intervals_request(
        url=f"/activity/{activity_id}/streams", api_key=api_key, params={"types": ",".join(wanted)}
    )
    streams = [s for s in streams_result if isinstance(s, dict)] if isinstance(streams_result, list) else []

    steps: Any = None
    plan_source = ""
    if isinstance(planned_workout_doc, dict) and isinstance(planned_workout_doc.get("steps"), list):
        steps, plan_source = planned_workout_doc["steps"], "workout document provided by the caller"
    elif activity.get("paired_event_id") and athlete_id:
        event = await _get_event(athlete_id, activity["paired_event_id"], api_key)
        if event:
            steps = (event.get("workout_doc") or {}).get("steps")
            plan_source = f"event {event.get('id')} ('{event.get('name')}')"
    planned = plan_steps(steps, _threshold_context(activity)) if isinstance(steps, list) else []
    execution = analyze(planned, intervals, streams) if intervals else None
    pace_based = str(activity.get("type")) in PACE_SPORTS
    power = _power_check(streams)
    want_climbs = include_climbs if include_climbs is not None else (not intervals or (activity.get("total_elevation_gain") or 0) > 500)
    climbs = _climb_summary(streams) if want_climbs and streams else None

    notes: list[str] = []
    if not activity.get("device_name"):
        notes.append("device unknown (no device data in the file)")
    if "watts" in available and not activity.get("power_meter"):
        notes.append("power meter identity unknown: the file carries no power meter name/serial")
    if "watts" in available and "secondary_power" not in available:
        notes.append("only one power stream recorded; no power meter comparison possible")
    if not streams:
        notes.append("no streams returned by Intervals.icu (file not retained?)")
    if not intervals:
        notes.append("no intervals detected by Intervals.icu")
    if activity.get("icu_intervals_edited"):
        notes.append("intervals were edited (Intervals.icu will not regenerate them)")
    if activity.get("compliance") == 0 and not activity.get("paired_event_id"):
        notes.append("compliance 0 only means the activity is not paired with a planned workout")

    if output_format.strip().lower() == "json":
        payload = {
            "activity": {k: activity.get(k) for k in ("id", "name", "type", "start_date_local", "start_date", "moving_time", "elapsed_time", "distance", "total_elevation_gain", "icu_training_load", "icu_intensity", "icu_ftp", "device_name", "power_meter", "power_field_names", "compliance", "paired_event_id", "_resolved_gear_name")},
            "plan_source": plan_source or None,
            "execution": {"summary": execution["summary"], "rows": execution["rows"], "extension": execution.get("extension")} if execution else None,
            "intervals": intervals if not planned else None,
            "power_check": power,
            "climbs": climbs,
            "notes": notes,
            "api_calls": 3 + (1 if plan_source.startswith("event") else 0),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    sections = ["== Overview", _compact_details(activity, field_defs, assigned)]
    if execution and planned:
        sections.append("== Plan vs execution (" + plan_source + ")")
        sections.append(format_execution(execution, "", pace_based).strip())
    elif intervals:
        sections.append("== Intervals")
        sections.append(_compact_intervals(intervals_payload, interval_defs, streams, stream_defs))
    if power:
        sections.append("== Second power meter check (watts vs secondary_power, no calibration)")
        sections.append(
            f"valid pairs {power['paired_valid']}, mean diff {power['mean_diff_pct']:+.1f}% (ratio {power['ratio']:.3f}), "
            f"stable 60 s windows {power['stable_60s_windows']}: {power['stable_60s_diff_pct']:+.1f}%, lag {power['lag_s']} s"
            if power.get("mean_diff_pct") is not None else "not enough valid paired samples"
        )
    if climbs:
        sections.append("== Climbs")
        sections.extend(_format_climbs(climbs))
    if notes:
        sections.append("== Data quality")
        sections.extend(f"- {n}" for n in notes)
    return "\n".join(sections)
