"""
Workout validation and preview tools (read-only). They check a workout document before
add_or_update_event / add_events_bulk / create_library_workout write it, and can look up
the calendar day for existing events to avoid duplicates.
"""

import json
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.utils.formatting import event_type_label
from intervals_mcp_server.utils.sports import hms
from intervals_mcp_server.utils.validation import resolve_athlete_id, validate_date
from intervals_mcp_server.utils.workout_validation import format_validation, validate_workout_doc

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()


@tool("read")
async def preview_workout(
    workout_doc: dict[str, Any],
    workout_type: str = "Ride",
    moving_time: int | None = None,
    output_format: str = "text",
) -> str:
    """Render a workout document as Intervals.icu workout text and report its totals (no API call)

    Expands repeat blocks, sums the planned duration and distance, counts steps with
    targets and open-ended (lap-press / free-ride) steps, and shows the workout text that
    add_or_update_event would send. Use validate_workout for the full check list.

    Args:
        workout_doc: Structured workout with "steps" (same format as add_or_update_event)
        workout_type: Activity type the workout is for, e.g. Ride, GravelRide, Run (default Ride)
        moving_time: Expected moving time in seconds to compare against the step sum (optional)
        output_format: "text" (default) or "json"
    """
    result = validate_workout_doc(workout_doc, workout_type, moving_time)
    if output_format.strip().lower() == "json":
        return json.dumps(result, ensure_ascii=False)
    return format_validation(result, workout_type)


@tool("read")
async def validate_workout(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-nested-blocks
    workout_doc: dict[str, Any],
    workout_type: str,
    name: str | None = None,
    start_date: str | None = None,
    moving_time: int | None = None,
    check_calendar: bool = True,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
) -> str:
    """Validate a workout document before writing it to the calendar (read-only)

    Checks: every step has a duration or distance (open-ended steps are flagged), repeat
    blocks have a positive count and nested steps, targets use supported units with
    plausible values, ramps have start/end ranges, pace targets on rides and power targets
    on runs are flagged, warm-up and cool-down presence, the step sum versus moving_time,
    and the activity type. With start_date and check_calendar the events already on that
    day are listed and a same-name event is flagged as a possible duplicate. Nothing is
    written.

    Args:
        workout_doc: Structured workout with "steps" (same format as add_or_update_event)
        workout_type: Activity type, e.g. Ride, GravelRide, Run, Swim
        name: Planned event name, used for the duplicate check (optional)
        start_date: Planned date YYYY-MM-DD, used for the calendar check (optional)
        moving_time: Expected moving time in seconds to compare against the step sum (optional)
        check_calendar: Look up existing events on start_date (optional, default True)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
    """
    result = validate_workout_doc(workout_doc, workout_type, moving_time)
    calendar: list[dict[str, Any]] = []
    calendar_note: str | None = None
    if start_date:
        try:
            validate_date(start_date)
        except ValueError as exc:
            result["errors"].append(f"start_date: {exc}")
            result["ok"] = False
        else:
            if check_calendar:
                athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
                if error_msg:
                    calendar_note = error_msg
                else:
                    events = await make_intervals_request(
                        url=f"/athlete/{athlete_id_to_use}/events", api_key=api_key,
                        params={"oldest": start_date, "newest": start_date},
                    )
                    if isinstance(events, dict) and "error" in events:
                        calendar_note = f"calendar check failed: {events.get('message')}"
                    else:
                        calendar = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
                        for event in calendar:
                            if name and str(event.get("name", "")).strip().lower() == name.strip().lower():
                                result["warnings"].append(
                                    f"an event named '{name}' already exists on {start_date} (id {event.get('id')}); "
                                    "pass event_id to add_or_update_event to update it instead of creating a duplicate"
                                )
    result["calendar"] = [
        {"id": e.get("id"), "name": e.get("name"), "type": event_type_label(e), "moving_time": e.get("moving_time")}
        for e in calendar
    ]
    if calendar_note:
        result["calendar_note"] = calendar_note
    if output_format.strip().lower() == "json":
        return json.dumps(result, ensure_ascii=False)
    text = format_validation(result, workout_type)
    if start_date and check_calendar:
        text += f"\nCalendar on {start_date}: "
        if calendar_note:
            text += calendar_note
        elif calendar:
            text += "; ".join(f"{c['type']} '{c['name']}' ({c['id']}, {hms(c['moving_time'])})" for c in result["calendar"])
        else:
            text += "no events"
    return text
