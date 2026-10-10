"""
Workout validation and preview tools (read-only). They check a workout document before
add_or_update_event / add_events_bulk / create_library_workout write it, and can look up
the calendar day for existing events to avoid duplicates.
"""

import json
from typing import Annotated, Any

from pydantic import Field

from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.utils.formatting import event_type_label
from intervals_mcp_server.utils.params import AthleteId, OutputFormat
from intervals_mcp_server.utils.sports import hms
from intervals_mcp_server.utils.validation import resolve_athlete_id, validate_date
from intervals_mcp_server.utils.workout_validation import format_validation, validate_workout_doc

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()


@tool("read")
async def preview_workout(
    workout_doc: Annotated[dict[str, Any], Field(description="Workout document with steps, as for add_or_update_event. Format: intervals://workout-syntax")],
    workout_type: Annotated[str, Field(description="Sport of the workout, e.g. Ride, GravelRide, Run")] = "Ride",
    moving_time: Annotated[int | None, Field(description="Expected moving time in seconds, compared with the step sum")] = None,
    output_format: OutputFormat = "text",
) -> str:
    """Use to show the athlete how a drafted workout will look before writing it: renders the workout document as Intervals.icu workout text with its totals (no API call, nothing written).

    Expands repeats, sums planned duration and distance, counts steps with targets and open-ended steps, and shows the text add_or_update_event would send. Full check list: validate_workout.
    """
    result = validate_workout_doc(workout_doc, workout_type, moving_time)
    if output_format.strip().lower() == "json":
        return json.dumps(result, ensure_ascii=False)
    return format_validation(result, workout_type)


@tool("read")
async def validate_workout(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-nested-blocks
    workout_doc: Annotated[dict[str, Any], Field(description="Workout document with steps, as for add_or_update_event. Format: intervals://workout-syntax")],
    workout_type: Annotated[str, Field(description="Sport of the workout, e.g. Ride, GravelRide, Run, Swim")],
    name: Annotated[str | None, Field(description="Planned event name, for the duplicate check")] = None,
    start_date: Annotated[str | None, Field(description="Planned day YYYY-MM-DD, for the calendar check")] = None,
    moving_time: Annotated[int | None, Field(description="Expected moving time in seconds, compared with the step sum")] = None,
    check_calendar: Annotated[bool, Field(description="Look up the events already on start_date")] = True,
    athlete_id: AthleteId = None,
    output_format: OutputFormat = "text",
) -> str:
    """Use before every workout write (add_or_update_event, add_events_bulk, create_library_workout) to check a drafted workout document; read-only, nothing is written.

    Checks durations or distances per step, repeat blocks, target units per sport and plausible values, ramps and ranges, warm-up and cool-down, the step sum against moving_time and the sport; with start_date it lists the events already on that day and flags a same-name event as a possible duplicate. Format: intervals://workout-syntax (get_guide). Show the result to the athlete before writing.
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
                        url=f"/athlete/{seg(athlete_id_to_use)}/events",
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
