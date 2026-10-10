"""
Climb / descent segmentation tool for activities without structured intervals (long rides,
mountain hikes, trail runs). Segments are detected from the altitude, distance and time
streams; every segment is evaluated with power, NP, HR, cadence, speed, VAM, grade and
the custom streams present (e.g. stamina, gear selection).
"""

import json
from typing import Annotated

from pydantic import Field

from intervals_mcp_server.tenancy import default_athlete
from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.utils.custom_fields import ACTIVITY_STREAM
from intervals_mcp_server.utils.params import ActivityId, OutputFormat
from intervals_mcp_server.utils.segments import detect_segments, format_segments, segments_to_json
from intervals_mcp_server.utils.sports import format_start_times, start_times

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()


@tool("read")
async def analyze_climbs(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    activity_id: ActivityId,
    min_climb_gain_m: Annotated[float, Field(description="Minimum elevation gain of a climb, metres")] = 30.0,
    min_descent_loss_m: Annotated[float, Field(description="Minimum elevation loss of a descent, metres")] = 30.0,
    min_grade_pct: Annotated[float, Field(description="Minimum average grade of climbs and descents, %")] = 2.0,
    pause_min_secs: Annotated[int, Field(description="Minimum length of a reported stationary period, seconds")] = 60,
    extra_stream_types: Annotated[str | None, Field(
        description="Comma-separated standard streams to evaluate per segment besides the custom ones, e.g. "
        "temp,respiration,left_right_balance"
    )] = None,
    max_segments: Annotated[int, Field(description="Segments listed in text output")] = 40,
    output_format: OutputFormat = "text",
    show_raw_grade: Annotated[bool, Field(
        description="Also report the grade of the unsmoothed altitude (windows of at least min_grade_distance_m)"
    )] = False,
    grade_window_m: Annotated[float | None, Field(
        description="Distance window of the steepest grade, metres; default 50 on foot, 100 otherwise"
    )] = None,
    min_grade_distance_m: Annotated[float | None, Field(
        description="No grade below this horizontal distance, metres; default 30 on foot, 50 otherwise"
    )] = None,
    stationary_speed_m_s: Annotated[float | None, Field(
        description="Pause candidate below this speed, m/s; default 0.3 on foot, 0.5 otherwise"
    )] = None,
) -> str:
    """Use for activities without useful intervals (tours, mountain hikes, trail runs) to split the recording into climbs, descents, sections in between and pauses.

    Per segment: time and sample indices, moving time, distance, gain/loss, average and steepest
    grade, VAM, speed, power, NP, HR, cadence and start/end/min/max/mean/delta of every custom
    stream. Defaults follow the sport (foot vs bike); implausible grades are flagged and each
    segment gets a grade confidence. Slow scrambling counts as moving, real pauses as pause. Climb
    gains are not the activity's total elevation gain. Text lists up to max_segments segments.
    Read-only, 2 API calls (activity, all streams). Method: intervals://methods/climbs (get_guide).
    """
    result = await make_intervals_request(url=f"/activity/{seg(activity_id)}")
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching activity details: {result.get('message', 'Unknown error')}"
    activity = result[0] if isinstance(result, list) and result else result
    if not isinstance(activity, dict) or not activity:
        return f"No details found for activity {activity_id}."

    streams_result = await make_intervals_request(url=f"/activity/{seg(activity_id)}/streams")
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

    athlete = str(activity.get("icu_athlete_id") or default_athlete(config.athlete_id) or "")
    stream_defs = (await get_custom_item_index(athlete_id=athlete)).get(ACTIVITY_STREAM, {}) if athlete else {}
    for stream in streams:  # custom stream names help to recognise counters and other-sport streams
        if stream.get("custom") and not stream.get("name") and stream.get("type") in stream_defs:
            stream["name"] = stream_defs[stream["type"]].get("name")
    extras = tuple(t.strip() for t in (extra_stream_types or "").split(",") if t.strip())
    segments = detect_segments(
        streams,
        min_climb_gain_m=min_climb_gain_m,
        min_descent_loss_m=min_descent_loss_m,
        min_grade_pct=min_grade_pct,
        pause_min_secs=pause_min_secs,
        extra_stream_types=extras,
        sport=activity.get("type"),
        grade_window_m=grade_window_m,
        min_grade_distance_m=min_grade_distance_m,
        stationary_speed_m_s=stationary_speed_m_s,
        show_raw_grade=show_raw_grade,
    )
    header = (
        f"Climb analysis for {activity.get('name', 'Unnamed')} ({activity_id}, {activity.get('type', '?')}, "
        f"{format_start_times(activity)}); elapsed {activity.get('elapsed_time', 'n/a')} s, moving "
        f"{activity.get('moving_time', 'n/a')} s, elevation gain {activity.get('total_elevation_gain', 'n/a')} m "
        f"(Intervals.icu totals); thresholds: climb >= {min_climb_gain_m:g} m and >= {min_grade_pct:g}%, descent >= {min_descent_loss_m:g} m"
    )
    if output_format.strip().lower() == "json":
        return json.dumps(
            {
                "activity": {"id": activity.get("id"), "name": activity.get("name"), "type": activity.get("type"), **start_times(activity),
                             "total_elevation_gain": activity.get("total_elevation_gain"), "moving_time": activity.get("moving_time"),
                             "elapsed_time": activity.get("elapsed_time")},
                "thresholds": {
                    "min_climb_gain_m": min_climb_gain_m, "min_descent_loss_m": min_descent_loss_m,
                    "min_grade_pct": min_grade_pct, "pause_min_secs": pause_min_secs,
                },
                "result": segments_to_json(segments),
            },
            ensure_ascii=False,
        )
    return header + "\n\n" + format_segments(segments, stream_defs, max_segments=max_segments)
