"""
Climb / descent segmentation tool for activities without structured intervals (long rides,
mountain hikes, trail runs). Segments are detected from the altitude, distance and time
streams; every segment is evaluated with power, NP, HR, cadence, speed, VAM, grade and
the custom streams present (e.g. stamina, gear selection).
"""

import json

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.utils.custom_fields import ACTIVITY_STREAM
from intervals_mcp_server.utils.segments import detect_segments, format_segments, segments_to_json
from intervals_mcp_server.utils.sports import format_start_times, start_times

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()


@tool("read")
async def analyze_climbs(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    activity_id: str,
    api_key: str | None = None,
    min_climb_gain_m: float = 30.0,
    min_descent_loss_m: float = 30.0,
    min_grade_pct: float = 2.0,
    pause_min_secs: int = 60,
    extra_stream_types: str | None = None,
    max_segments: int = 40,
    output_format: str = "text",
) -> str:
    """Detect climbs, descents and pauses of an activity from its streams and evaluate each segment

    For activities without defined intervals (tours, mountain hikes, trail runs) this
    splits the recording into climbs (sustained ascent of at least min_climb_gain_m with an
    average grade of at least min_grade_pct), descents and the sections in between, using
    the corrected altitude when available. Per segment: start/end time and sample indices,
    duration, distance, elevation gain/loss, average and maximum grade, VAM, average and
    normalized power, max power, average/max HR, cadence (non-zero samples), speed, and the
    start/end/min/max/mean/delta of every custom stream (e.g. stamina, gear ratio) plus any
    extra standard streams requested. Recording stops and stationary periods are listed
    as pauses. Thresholds are explicit parameters, nothing is interpolated, and Garmin
    sitting/standing or similar device classifications are not used.

    Args:
        activity_id: The Intervals.icu activity ID
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        min_climb_gain_m: Minimum elevation gain for a climb in metres (optional, default 30)
        min_descent_loss_m: Minimum elevation loss for a descent in metres (optional, default 30)
        min_grade_pct: Minimum average grade in percent for climbs/descents (optional, default 2)
        pause_min_secs: Minimum length of a stationary period to report (optional, default 60)
        extra_stream_types: Comma-separated standard stream types to evaluate per segment in
            addition to the custom streams, e.g. "temp,respiration,left_right_balance" (optional)
        max_segments: Maximum number of segments to print in text output (optional, default 40)
        output_format: "text" (default) or "json"
    """
    result = await make_intervals_request(url=f"/activity/{activity_id}", api_key=api_key)
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching activity details: {result.get('message', 'Unknown error')}"
    activity = result[0] if isinstance(result, list) and result else result
    if not isinstance(activity, dict) or not activity:
        return f"No details found for activity {activity_id}."

    streams_result = await make_intervals_request(url=f"/activity/{activity_id}/streams", api_key=api_key)
    if isinstance(streams_result, dict) and "error" in streams_result:
        return f"Error fetching activity streams: {streams_result.get('message', 'Unknown error')}"
    streams = [s for s in streams_result if isinstance(s, dict)] if isinstance(streams_result, list) else []
    if not streams:
        listed = ", ".join(str(t) for t in (activity.get("stream_types") or [])) or "none"
        return (
            f"No stream data returned for activity {activity_id} although the activity lists these stream types: "
            f"{listed}. Intervals.icu returns no streams for activities whose file is not retained (e.g. a "
            f"duplicate upload); check get_activities around {str(activity.get('start_date_local', ''))[:10]} for "
            "another activity with the same start time."
        )

    athlete = str(activity.get("icu_athlete_id") or config.athlete_id or "")
    stream_defs = (await get_custom_item_index(athlete_id=athlete, api_key=api_key)).get(ACTIVITY_STREAM, {}) if athlete else {}
    extras = tuple(t.strip() for t in (extra_stream_types or "").split(",") if t.strip())
    segments = detect_segments(
        streams,
        min_climb_gain_m=min_climb_gain_m,
        min_descent_loss_m=min_descent_loss_m,
        min_grade_pct=min_grade_pct,
        pause_min_secs=pause_min_secs,
        extra_stream_types=extras,
    )
    header = (
        f"Climb analysis for {activity.get('name', 'Unnamed')} ({activity_id}, {activity.get('type', '?')}, "
        f"{format_start_times(activity)}); elapsed {activity.get('elapsed_time', 'n/a')} s, "
        f"elevation gain {activity.get('total_elevation_gain', 'n/a')} m (Intervals.icu); "
        f"thresholds: climb >= {min_climb_gain_m:g} m and >= {min_grade_pct:g}%, descent >= {min_descent_loss_m:g} m"
    )
    if output_format.strip().lower() == "json":
        return json.dumps(
            {
                "activity": {"id": activity.get("id"), "name": activity.get("name"), "type": activity.get("type"), **start_times(activity)},
                "thresholds": {
                    "min_climb_gain_m": min_climb_gain_m, "min_descent_loss_m": min_descent_loss_m,
                    "min_grade_pct": min_grade_pct, "pause_min_secs": pause_min_secs,
                },
                "result": segments_to_json(segments),
            },
            ensure_ascii=False,
        )
    return header + "\n\n" + format_segments(segments, stream_defs, max_segments=max_segments)
