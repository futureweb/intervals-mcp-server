"""
Event-related MCP tools for Intervals.icu.

This module contains tools for retrieving, creating, updating, and deleting athlete events.
"""

# pylint: disable=too-many-lines

import json
from datetime import date
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.utils.dates import (
    athlete_today,
    get_default_end_date,
    get_default_future_end_date,
    get_default_start_date,
)
from intervals_mcp_server.utils.formatting import (
    event_type_label,
    format_event_details,
    format_event_summary,
)
from intervals_mcp_server.utils.sports import hms
from intervals_mcp_server.utils.types import WorkoutDoc
from intervals_mcp_server.utils.validation import (
    infer_activity_type,
    resolve_athlete_id,
    validate_date,
)
from intervals_mcp_server.utils.workout_validation import (
    coerce_workout_doc,
    is_blank_workout_doc,
    is_structured_workout,
    warnings_note,
    workout_text_for_write,
    write_refusal,
)

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()


# Categories add_or_update_event can create or set. TARGET is not offered: a weekly target
# needs load/time/distance target fields this tool does not have (it would be an empty target).
EVENT_CATEGORIES = (
    "WORKOUT", "RACE_A", "RACE_B", "RACE_C", "NOTE", "HOLIDAY", "SICK", "INJURED",
)
# Categories that carry a sport (type); notes and sick/holiday/injury days do not.
SPORT_CATEGORIES = ("WORKOUT", "RACE_A", "RACE_B", "RACE_C")
# Categories delete_events_by_date_range may delete. Plan phases, season markers and the
# fitness-model events (SET_EFTP, SET_FITNESS, FITNESS_DAYS) change the plan or past CTL/ATL
# and can only be deleted one by one with delete_event.
DELETABLE_CATEGORIES = ("WORKOUT", "RACE_A", "RACE_B", "RACE_C", "NOTE", "TARGET", "HOLIDAY", "SICK", "INJURED")
MAX_DELETE_RANGE_DAYS = 31
MAX_DELETE_EVENTS = 100
MAX_BULK_EVENTS = 100


def _prepare_event_data(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    name: str | None,
    event_type: str | None,
    start_date_local: str | None,
    description: str | None,
    moving_time: int | None,
    distance: int | None,
    indoor: bool | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """The event payload; only fields that are set are sent (an update changes nothing else)."""
    event_data: dict[str, Any] = {
        "start_date_local": start_date_local,
        "category": category,
        "name": name,
        "description": description,
        "type": event_type,
        "moving_time": moving_time,
        "distance": distance,
        "indoor": indoor,
    }
    return {key: value for key, value in event_data.items() if value is not None}


def _prepare_note_data(
    name: str | None, description: str | None, start_date: str | None, color: str | None, *, is_update: bool = False
) -> dict[str, Any]:
    """Prepare note (category NOTE) data; fields that are not set are not sent.

    The category is only sent when creating: an update must never turn another event into a note.
    """
    note = {
        "category": None if is_update else "NOTE",
        "name": name,
        "description": description,
        "start_date_local": start_date + "T00:00:00" if start_date else None,
        "color": color,
    }
    return {key: value for key, value in note.items() if value is not None}


def _handle_event_response(
    result: dict[str, Any] | list[dict[str, Any]] | None,
    action: str,
    athlete_id: str,
    start_date: str | None,
) -> str:
    """Handle API response and format appropriate message."""
    verb = {"created": "creating", "updated": "updating"}.get(action, action)
    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error {verb} event: {error_message}"
    if not result:
        return f"No events {action} for athlete {athlete_id}."
    if isinstance(result, dict):
        return f"Successfully {action} event id: {result.get('id')}"
    return f"Event {action} successfully" + (f" at {start_date}" if start_date else "")


def _check_amounts(moving_time: Any, distance: Any) -> str | None:
    """moving_time and distance must be non-negative integers when given."""
    for key, value in (("moving_time", moving_time), ("distance", distance)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            return f"'{key}' must be an integer"
        if value < 0:
            return f"'{key}' must not be negative"
    return None


EVENT_JSON_FIELDS = (
    "id",
    "start_date_local",
    "end_date_local",
    "category",
    "type",
    "name",
    "description",
    "moving_time",
    "distance",
    "icu_training_load",
    "icu_intensity",
    "indoor",
    "paired_activity_id",
    "tags",
    "color",
    "plan_name",
    "updated",
)


def _event_json(event: dict[str, Any], include_workout_doc: bool = False) -> dict[str, Any]:
    """Compact machine-readable view of an event (workout_doc optional)."""
    row = {key: event.get(key) for key in EVENT_JSON_FIELDS if key in event}
    row["type_label"] = event_type_label(event)
    if include_workout_doc and "workout_doc" in event:
        row["workout_doc"] = event.get("workout_doc")
    return row


@tool("read")
async def get_events(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    athlete_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    categories: str | None = None,
    output_format: str = "text",
) -> str:
    """Get events for an athlete from Intervals.icu

    Events are planned workouts, races, notes and other calendar items. Each event is
    reported with its category (Workout, Race A/B/C, Note ...) and sport (Ride, Run ...),
    planned time and load, and the paired activity once it has been done.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        start_date: Start date in YYYY-MM-DD format (optional, defaults to today)
        end_date: End date in YYYY-MM-DD format (optional, defaults to 30 days from today)
        categories: Comma-separated categories to return, e.g. "WORKOUT" or "WORKOUT,RACE_A,RACE_B"
            (optional, default all categories)
        output_format: "text" (default) or "json" (list of events with technical fields)
    """
    # Resolve athlete ID
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    # Parse date parameters (events use different defaults)
    if not start_date:
        start_date = get_default_end_date()
    if not end_date:
        end_date = get_default_future_end_date()

    # Call the Intervals.icu API
    params: dict[str, str] = {"oldest": start_date, "newest": end_date}
    if categories:
        params["category"] = ",".join(c.strip().upper() for c in categories.split(",") if c.strip())

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/events", params=params
    )

    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error fetching events: {error_message}"

    # Format the response
    if not result:
        return f"No events found for athlete {athlete_id_to_use} in the specified date range."

    # Ensure result is a list
    events = result if isinstance(result, list) else []

    if not events:
        return f"No events found for athlete {athlete_id_to_use} in the specified date range."

    if output_format.strip().lower() == "json":
        return json.dumps(
            {"events": [_event_json(e) for e in events if isinstance(e, dict)]},
            ensure_ascii=False,
        )

    events_summary = "Events:\n\n"
    for event in events:
        if not isinstance(event, dict):
            continue

        events_summary += format_event_summary(event) + "\n\n"

    return events_summary


@tool("read")
async def get_event_by_id(
    event_id: str,
    athlete_id: str | None = None,
    output_format: str = "text",
    resolve_targets: bool = False,
) -> str:
    """Get detailed information for a specific event from Intervals.icu

    Returns category and sport, planned time/load, the paired activity and the full
    workout document (steps, planned time in zones). Use it to read back a workout
    right after creating or updating it.

    Args:
        event_id: The Intervals.icu event ID
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        output_format: "text" (default) or "json" (complete event including workout_doc)
        resolve_targets: Ask Intervals.icu to resolve %FTP / %LTHR / pace targets to watts, bpm
            and m/s inside the workout steps (optional, default False)
    """
    # Resolve athlete ID
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    # Call the Intervals.icu API (the endpoint is /events/{id}, plural)
    params = {"resolve": "true"} if resolve_targets else None
    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/events/{seg(event_id)}", params=params
    )

    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error fetching event details: {error_message}"

    # Format the response
    if not result:
        return f"No details found for event {event_id}."

    if not isinstance(result, dict):
        return f"Invalid event format for event {event_id}."

    if output_format.strip().lower() == "json":
        return json.dumps(_event_json(result, include_workout_doc=True), ensure_ascii=False)

    return format_event_details(result)


@tool("destructive")
async def delete_event(
    event_id: str,
    athlete_id: str | None = None,
) -> str:
    """Delete event for an athlete from Intervals.icu
    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        event_id: The Intervals.icu event ID
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if not event_id:
        return "Error: No event ID provided."
    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/events/{seg(event_id)}", method="DELETE"
    )
    if isinstance(result, dict) and "error" in result:
        return f"Error deleting event: {result.get('message')}"
    return json.dumps(result, indent=2)


def _parse_categories(categories: str | None) -> list[str] | str:
    """Categories to delete (default WORKOUT) or an error string."""
    wanted = [c.strip().upper() for c in (categories or "WORKOUT").split(",") if c.strip()]
    if not wanted:
        wanted = ["WORKOUT"]
    unknown = [c for c in wanted if c not in DELETABLE_CATEGORIES]
    if unknown:
        return (
            f"Error: categories {', '.join(unknown)} cannot be deleted by date range; use one or more of "
            f"{', '.join(DELETABLE_CATEGORIES)} (plan phases and fitness-model events: delete_event by id)."
        )
    return list(dict.fromkeys(wanted))


def _deletion_row(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event.get("id"),
        "date": str(event.get("start_date_local") or "")[:10],
        "category": event.get("category"),
        "type": event.get("type"),
        "name": event.get("name"),
        "paired_activity_id": event.get("paired_activity_id"),
    }


def _select_for_deletion(
    events: list[dict[str, Any]], wanted: list[str], start: str, end: str, include_paired: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(events to delete, events kept with the reason) after the client-side checks.

    Only events whose START day lies in the range are deleted (a holiday or plan phase that
    started earlier and only overlaps the range is kept), only the requested categories, and
    planned workouts already paired with a completed activity only with include_paired.
    """
    selected: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or event.get("id") is None:
            continue
        row = _deletion_row(event)
        if event.get("category") not in wanted:
            continue  # the API filter should already have excluded it
        if not start <= row["date"] <= end:
            kept.append({**row, "reason": "starts outside the date range"})
        elif event.get("paired_activity_id") and not include_paired:
            kept.append({**row, "reason": "paired with a completed activity (pass include_paired=true to delete)"})
        else:
            selected.append(row)
    return selected, kept


@tool("destructive")
async def delete_events_by_date_range(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements,too-many-locals,too-many-branches,too-many-statements
    start_date: str,
    end_date: str,
    categories: str = "WORKOUT",
    dry_run: bool = True,
    confirm_ids: str | None = None,
    include_paired: bool = False,
    athlete_id: str | None = None,
) -> str:
    """DELETES calendar events of the given categories in a date range, in two steps.

    1. Preview (dry_run=true, the default): nothing is deleted; the answer lists the events
       that match (id, date, category, name). Show that list to the athlete.
    2. Delete (dry_run=false) ONLY after the athlete confirmed the list: pass the confirmed ids
       as confirm_ids. Only events that are in confirm_ids AND still match the filters are
       deleted, one by one; events added since the preview are never deleted, and confirmed
       ids that no longer match are reported, not deleted.

    Only the listed categories are touched (default WORKOUT = planned workouts; races, notes,
    sick days etc. only when named), only events that START in the range, at most 31 days per
    call, and planned workouts already paired with a completed activity are kept unless
    include_paired=true.

    Args:
        start_date: First day YYYY-MM-DD (inclusive)
        end_date: Last day YYYY-MM-DD (inclusive, at most 31 days after start_date)
        categories: Comma-separated categories to delete (default "WORKOUT"); allowed:
            WORKOUT, RACE_A, RACE_B, RACE_C, NOTE, TARGET, HOLIDAY, SICK, INJURED
        dry_run: true (default) = only list what would be deleted; false = delete confirm_ids
        confirm_ids: Comma-separated event ids from the preview that the athlete confirmed
            (required with dry_run=false), e.g. "101,102,107"
        include_paired: Also delete planned workouts that are paired with a completed activity
            (optional, default false)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)

    Returns:
        JSON with dry_run, the range and categories, "events" (id, date, category, type, name)
        that match now, "kept" (in the API result but not matching, with the reason) and, when
        deleting, "deleted", "already_gone", "failed" (id and reason), "not_confirmed" (matching
        events that were not in confirm_ids) and "no_longer_matching" (confirmed ids that do not
        match any more).
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    try:
        start = validate_date(start_date)
        end = validate_date(end_date)
    except ValueError as exc:
        return f"Error: {exc}"
    if end < start:
        return "Error: end_date must not be before start_date."
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if span > MAX_DELETE_RANGE_DAYS:
        return (
            f"Error: the range covers {span} days; at most {MAX_DELETE_RANGE_DAYS} days can be deleted per call. "
            "Split it into several calls."
        )
    wanted = _parse_categories(categories)
    if isinstance(wanted, str):
        return wanted
    confirmed: list[str] = []
    if not dry_run:
        confirmed = [part.strip() for part in (confirm_ids or "").split(",") if part.strip()]
        if not confirmed:
            return (
                "Error: dry_run=false needs confirm_ids, the ids from the preview that the athlete confirmed. "
                "Call with dry_run=true first and show the list. Nothing was deleted."
            )
        if not all(part.isascii() and part.isdigit() for part in confirmed):
            return "Error: confirm_ids must be comma-separated numeric event ids. Nothing was deleted."
        if len(confirmed) > MAX_DELETE_EVENTS:
            return f"Error: at most {MAX_DELETE_EVENTS} events per call. Nothing was deleted."

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/events",
        params={"oldest": start, "newest": end, "category": ",".join(wanted)},
    )
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching events to delete: {result.get('message')}"
    events = [e for e in result if isinstance(e, dict)] if isinstance(result, list) else []
    selected, kept = _select_for_deletion(events, wanted, start, end, include_paired)
    payload: dict[str, Any] = {
        "dry_run": dry_run,
        "start_date": start,
        "end_date": end,
        "categories": wanted,
        "events": selected,
        "kept": kept,
    }
    if dry_run:
        ids = ",".join(str(row["id"]) for row in selected)
        payload["message"] = (
            f"Preview only, nothing was deleted: {len(selected)} event(s) match. After the athlete confirmed "
            f"this list, call again with dry_run=false and confirm_ids=\"{ids}\"."
            if selected else "Nothing to delete in this range for these categories."
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    current = {str(row["id"]): row for row in selected}
    to_delete = [current[i] for i in dict.fromkeys(confirmed) if i in current]
    payload["not_confirmed"] = [row for key, row in current.items() if key not in confirmed]
    payload["no_longer_matching"] = [i for i in dict.fromkeys(confirmed) if i not in current]
    deleted: list[dict[str, Any]] = []
    already_gone: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for row in to_delete:
        answer = await make_intervals_request(
            url=f"/athlete/{seg(athlete_id_to_use)}/events/{seg(row['id'])}", method="DELETE"
        )
        if isinstance(answer, dict) and "error" in answer:
            if answer.get("status_code") == 404:
                already_gone.append(row)  # e.g. deleted by a retried request or in the web app
            else:
                failed.append({**row, "error": answer.get("message")})
        else:
            deleted.append(row)
    payload.update({"deleted": deleted, "already_gone": already_gone, "failed": failed})
    parts = [f"Deleted {len(deleted)} of {len(dict.fromkeys(confirmed))} confirmed event(s)"]
    if already_gone:
        parts.append(f"{len(already_gone)} were already gone")
    if failed:
        parts.append(f"{len(failed)} failed (see failed; check with get_events before retrying)")
    if payload["no_longer_matching"]:
        parts.append(f"{len(payload['no_longer_matching'])} confirmed id(s) no longer match the range/filters and were not deleted")
    if payload["not_confirmed"]:
        parts.append(f"{len(payload['not_confirmed'])} matching event(s) were not confirmed and were kept")
    payload["message"] = "; ".join(parts) + "."
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _fetch_event(athlete_id: str, event_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """The current event (for checks before an update) or an error message."""
    result = await make_intervals_request(url=f"/athlete/{seg(athlete_id)}/events/{seg(event_id)}")
    if isinstance(result, dict) and "error" in result:
        return None, str(result.get("message", "Unknown error"))
    if not isinstance(result, dict) or not result:
        return None, f"event {event_id} not found"
    return result, None


def _time_of_day(event: dict[str, Any] | None) -> str:
    """The event's time component ("T07:30:00"), kept when the event is moved to another day."""
    value = str((event or {}).get("start_date_local") or "")
    return value[10:19] if len(value) >= 19 and value[10] == "T" else "T00:00:00"


def _blank(value: str | None) -> bool:
    return value is not None and not value.strip()


@tool("write", overwrites=True)
async def add_or_update_event(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements,too-many-branches
    workout_type: str | None = None,
    name: str | None = None,
    athlete_id: str | None = None,
    event_id: str | None = None,
    start_date: str | None = None,
    workout_doc: WorkoutDoc | None = None,
    moving_time: int | None = None,
    distance: int | None = None,
    indoor: bool | None = None,
    category: str | None = None,
    description: str | None = None,
    replace_workout: bool = False,
) -> str:
    """Post event for an athlete to Intervals.icu this follows the event api from intervals.icu
    If event_id is provided, the event will be updated instead of created.

    Updates are partial: only the parameters you pass are changed (name, type, date,
    workout, time, distance, indoor, category); everything else on the event stays as it is.
    Moving an event to another day keeps its time of day. Passing workout_doc REPLACES the
    planned workout; an empty workout_doc ({} or no steps) is ignored and never clears it.
    The workout is validated first (same checks as validate_workout); with errors nothing is
    written, warnings are listed in the answer. Text-only workouts (strength, yoga: an exercise
    list without durations) go into `description` (or a workout_doc with text steps only). On an
    update, a description or a workout_doc without timed steps never silently replaces a
    structured (timed) workout: that needs replace_workout=true. Creating an event needs a name; the date defaults to today (athlete's time zone)
    and the category to WORKOUT. Workouts and races need a sport: pass workout_type unless the
    name names exactly one sport ("Easy run" -> Run).

    Many arguments are required as this MCP tool function maps directly to the Intervals.icu API parameters.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        event_id: The Intervals.icu event ID (optional; given = update this event)
        start_date: Start date in YYYY-MM-DD format (optional; defaults to today when creating,
            unchanged when updating)
        name: Name of the activity (required when creating; must not be blank)
        workout_doc: steps as a list of Step objects (optional, but necessary to define workout steps)
        workout_type: Workout type (e.g. Ride, Run, Swim, Walk, Row); required to create a workout or
            race unless the name names exactly one sport; unchanged when updating without it
        category: Event category, e.g. WORKOUT (default when creating), RACE_A, RACE_B, RACE_C,
            NOTE, HOLIDAY, SICK, INJURED (optional; unchanged when updating without it)
        moving_time: Total expected moving time of the workout in seconds (optional, >= 0)
        distance: Total expected distance of the workout in meters (optional, >= 0)
        indoor: Mark the event as an indoor/trainer session. Optional; omit to leave
            unchanged when updating an existing event.
        description: Workout text instead of workout_doc (optional): plain text such as
            "Squats 5x5, Deadlifts 3x5" or native Intervals.icu workout text, sent as-is.
            Replaces the event's description; an empty string is ignored (never clears it).
        replace_workout: Allow a description or a workout_doc without timed steps to replace an
            event that holds a structured workout (optional, default false)

    Example:
        "workout_doc": {
            "description": "High-intensity workout for increasing VO2 max",
            "steps": [
                {"power": {"value": 80, "units": "%ftp"}, "duration": 900, "warmup": true},
                {"reps": 2, "text": "Intervals", "steps": [
                    {"power": {"value": 110, "units": "%ftp"}, "distance": 500, "text": "Hard"},
                    {"power": {"value": 80, "units": "%ftp"}, "duration": 90, "text": "Recovery"}
                ]},
                {"power": {"value": 80, "units": "%ftp"}, "duration": 600, "cooldown": true},
                {"text": ""}
            ]
        }

    Step properties:
        distance: Distance of step in meters (positive)
            {"distance": 5000}
        duration: Duration of step in whole seconds (positive); give duration OR distance, not both
            {"duration": 1800}
        power/hr/pace/cadence: Define step intensity (units must fit the kind)
            Percentage of FTP: {"power": {"value": 80, "units": "%ftp"}}
            Absolute power: {"power": {"value": 200, "units": "w"}}
            Heart rate: {"hr": {"value": 75, "units": "%hr"}}
            Heart rate (LTHR): {"hr": {"value": 85, "units": "%lthr"}}
            Cadence: {"cadence": {"value": 90, "units": "cadence"}}
            Pace by ftp: {"pace": {"value": 80, "units": "%pace"}}
            Pace by zone: {"pace": {"value": 2, "units": "pace_zone"}}
            Absolute pace: MINS_KM / MINS_MILE in seconds (335) or decimal minutes (5.583) per km/mile;
            SECS_100M, SECS_100Y, SECS_500M in seconds:
                {"pace": {"value": 335, "units": "MINS_KM"}}  -> "5:35/km Pace"
            Zone by power: {"power": {"value": 2, "units": "power_zone"}}
            Zone by heart rate: {"hr": {"value": 2, "units": "hr_zone"}}
        Ranges: Specify ranges for power, heart rate, pace, or cadence (start AND end, no value):
            {"power": {"start": 80, "end": 90, "units": "%ftp"}}
        Ramps: Instead of a range, indicate a gradual change in intensity (useful for ERG workouts):
            {"ramp": true, "power": {"start": 80, "end": 90, "units": "%ftp"}}
        Repeats: include the reps property and add nested steps (no nested repeats)
            {"reps": 3,
             "steps": [
                {"power": {"value": 110, "units": "%ftp"}, "distance": 500, "text": "Hard"},
                {"power": {"value": 80, "units": "%ftp"}, "duration": 90, "text": "Recovery"}
            ]}
        Free Ride: Include freeride to indicate a segment without ERG control (needs a duration),
            optionally with a suggested power range:
            {"freeride": true, "duration": 1200, "power": {"value": 80, "units": "%ftp"}}
        Comments and Labels: Add descriptive text to label steps:
            {"text": "Warmup"}
            A step's text becomes the cue at the start of its line, as in Intervals.icu's builder:
            {"text": "Sprint", "distance": 40, "hr": {"value": 5, "units": "hr_zone"}} -> "- Sprint 40mtr Z5 HR".
            Labels must be plain words: durations, distances, %, watts, rpm, zones (Z2), repeat
            counts (3x), m:ss and the keywords ramp/freeride/max effort are refused, because
            Intervals.icu would read them as part of the step.

    How to use steps:
    - Set distance or duration as appropriate for step
    - Use "reps" with nested steps to define repeat intervals (as in example above)
    - Define one of "power", "hr" or "pace" to define step intensity
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    is_update = bool(event_id)
    if not is_update and not (name or "").strip():
        return "Error: name is required when creating an event."
    if _blank(name):
        return "Error: name must not be blank."
    if category is not None:
        category = category.strip().upper()
        if category not in EVENT_CATEGORIES:
            return f"Error: category must be one of {', '.join(EVENT_CATEGORIES)}."
    amount_error = _check_amounts(moving_time, distance)
    if amount_error:
        return f"Error: {amount_error}."
    try:
        validated_date = validate_date(start_date) if start_date else None
    except ValueError as e:
        return f"Error: {e}"

    event_type = workout_type.strip() if workout_type and workout_type.strip() else None
    if not is_update:
        category = category or "WORKOUT"
        if category in SPORT_CATEGORIES:
            event_type = event_type or infer_activity_type(name)
            if not event_type:
                return (
                    f"Error: workout_type is required for a {category} whose name does not name exactly one "
                    "sport (e.g. Ride, Run, Swim, Walk, Row, WeightTraining)."
                )
        else:
            event_type = None  # notes, holidays, sick and injured days have no sport
        validated_date = validated_date or athlete_today().isoformat()

    workout = workout_text_for_write(workout_doc, event_type)
    if workout.problem:
        return write_refusal(workout.problem)
    if description is not None and not description.strip():
        description = None  # "" from a client filling optional fields: not given, never clears
    if workout.text is not None and description is not None:
        return "Error: pass either workout_doc or description, not both. Nothing was written."
    text = workout.text
    # Text (a description, or a workout_doc without timed steps) replacing a structured workout
    # needs replace_workout=true, however the text looks: "- legs heavy" is not a workout.
    replaces_with_text = workout.text_only if text is not None else description is not None
    if text is None:
        text = description

    start_local = None
    existing: dict[str, Any] | None = None
    if is_update and (validated_date or replaces_with_text):
        existing, fetch_error = await _fetch_event(athlete_id_to_use, str(event_id))
        if fetch_error:
            return f"Error: could not read event {event_id} before changing it: {fetch_error}. Nothing was changed."
        if replaces_with_text and is_structured_workout(existing) and not replace_workout:
            return (
                f"Error: event {event_id} holds a structured workout with timed steps; this text would replace it. "
                "Pass the new workout as steps in workout_doc, or replace_workout=true to replace it with text. "
                "Nothing was changed."
            )
    if validated_date:
        start_local = validated_date + (_time_of_day(existing) if is_update else "T00:00:00")

    event_data = _prepare_event_data(
        name, event_type, start_local, text, moving_time, distance, indoor, category=category
    )
    if is_update and not event_data:
        return "Error: nothing to update; pass at least one field to change."
    answer = await _create_or_update_event_request(
        athlete_id_to_use, event_data, validated_date, event_id
    )
    return answer + (warnings_note(workout.warnings) if answer.startswith("Successfully") else "")


@tool("write", overwrites=True)
async def add_or_update_note(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements,too-many-locals,too-many-branches
    name: str | None = None,
    description: str | None = None,
    start_date: str | None = None,
    color: str | None = None,
    athlete_id: str | None = None,
    event_id: str | None = None,
    clear_description: bool = False,
) -> str:
    """Add or update a plain text note (category NOTE) on the Intervals.icu calendar.

    Updates are partial: only the parameters you pass are changed, so renaming a note keeps
    its text, colour and date. An empty description is ignored on update (it never wipes the
    text); pass clear_description=true to empty it on purpose. Only notes can be updated with
    this tool: the event is read first and anything else (a planned workout, a race) is
    refused, never turned into a note.

    Args:
        name: Title of the note (required when creating; must not be blank)
        description: Plain text content of the note
        start_date: Date in YYYY-MM-DD format (optional; defaults to today when creating,
            unchanged when updating)
        color: Color of the note (e.g. green, orange, red, blue; default green when creating)
        athlete_id: The Intervals.icu athlete ID (optional)
        event_id: The Intervals.icu event ID of a NOTE (optional, for updates)
        clear_description: Empty the note's text on update (optional, default false)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    is_update = bool(event_id)
    if _blank(name):
        return "Error: name must not be blank."
    if not is_update:
        if not (name or "").strip():
            return "Error: name is required when creating a note."
        start_date = start_date or athlete_today().isoformat()
        color = color or "green"
        description = description if description is not None else ""

    elif clear_description:
        if description is not None and description.strip():
            return "Error: pass either description or clear_description=true, not both. Nothing was changed."
        description = ""
    elif description is not None and not description.strip():
        description = None  # a placeholder "" never wipes the note's text

    try:
        validated_date = validate_date(start_date) if start_date else None
    except ValueError as e:
        return f"Error: {e}"
    event_data = _prepare_note_data(name, description, validated_date, color, is_update=is_update)
    if is_update:
        if not event_data:
            return "Error: nothing to update; pass at least one field to change."
        existing, fetch_error = await _fetch_event(athlete_id_to_use, str(event_id))
        if fetch_error:
            return f"Error: could not read event {event_id}: {fetch_error}. Nothing was changed."
        if (existing or {}).get("category") != "NOTE":
            return (
                f"Error: event {event_id} is a {(existing or {}).get('category') or 'non-note'} event, not a NOTE; "
                "add_or_update_note only changes notes (use add_or_update_event for other events). Nothing was changed."
            )
        if "start_date_local" in event_data:
            event_data["start_date_local"] = str(validated_date) + _time_of_day(existing)
    return await _create_or_update_event_request(
        athlete_id_to_use, event_data, validated_date, event_id
    )


async def _create_or_update_event_request(
    athlete_id: str,
    event_data: dict[str, Any],
    start_date: str | None,
    event_id: str | None,
) -> str:
    """Create or update an event via API request.

    Args:
        athlete_id: The athlete ID.
        event_data: Prepared event data dictionary.
        start_date: Start date string for response formatting.
        event_id: Optional event ID for updates.

    Returns:
        Formatted response string.
    """
    url = f"/athlete/{seg(athlete_id)}/events"
    if event_id:
        url += f"/{seg(event_id)}"
    result = await make_intervals_request(
        url=url,
        data=event_data,
        method="PUT" if event_id else "POST",
    )
    action = "updated" if event_id else "created"
    return _handle_event_response(result, action, athlete_id, start_date)


_BULK_COMMON_KEYS = {"category", "name", "start_date"}
_BULK_WORKOUT_KEYS = _BULK_COMMON_KEYS | {
    "workout_type",
    "workout_doc",
    "description",
    "moving_time",
    "distance",
}
_BULK_NOTE_KEYS = _BULK_COMMON_KEYS | {"description", "color"}


def _build_bulk_event_entry(entry: Any, warnings: list[str] | None = None) -> dict[str, Any]:  # pylint: disable=too-many-branches,too-many-statements,too-many-locals
    """Validate one bulk entry and build the API event body using the shared builders.

    All problems of the entry are collected and reported together; validation warnings of its
    workout are appended to *warnings*.

    Raises:
        ValueError: If the entry is invalid; the message lists every problem found.
    """
    if not isinstance(entry, dict):
        raise ValueError("entry must be an object")

    problems: list[str] = []
    category = str(entry.get("category") or "WORKOUT").upper()
    if category not in ("WORKOUT", "NOTE"):
        raise ValueError("'category' must be WORKOUT or NOTE")
    allowed = _BULK_NOTE_KEYS if category == "NOTE" else _BULK_WORKOUT_KEYS
    not_applicable = sorted(set(entry) - allowed)
    if not_applicable:
        problems.append(f"keys not supported for category {category}: {', '.join(not_applicable)}")

    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("'name' is required")
    validated_date = ""
    if not entry.get("start_date"):
        problems.append("'start_date' is required")
    else:
        try:
            validated_date = validate_date(str(entry["start_date"]))
        except ValueError as e:
            problems.append(str(e))

    description = entry.get("description")
    if category == "NOTE":
        if not isinstance(description, str) or not description.strip():
            problems.append("'description' is required for category NOTE")
        color = entry.get("color", "green")
        if not isinstance(color, str) or not color.strip():
            problems.append("'color' must be a colour name such as green")
        if problems:
            raise ValueError("; ".join(problems))
        return _prepare_note_data(str(name), str(description), validated_date, color)

    workout_type = entry.get("workout_type")
    if not isinstance(workout_type, str) or not workout_type.strip():
        problems.append("'workout_type' is required for category WORKOUT")
    if description is not None and not isinstance(description, str):
        problems.append("'description' must be a string")
    raw_doc = entry.get("workout_doc")
    text: str | None = None
    if raw_doc is not None and description:
        problems.append("provide either 'workout_doc' or 'description', not both")
    elif isinstance(raw_doc, dict) and is_blank_workout_doc(raw_doc):
        pass  # {} or {"steps": []}: not given
    elif raw_doc is not None:
        workout_doc, doc_error = coerce_workout_doc(raw_doc)
        if doc_error:
            problems.append(doc_error)
        else:
            workout = workout_text_for_write(workout_doc, workout_type if isinstance(workout_type, str) else None)
            if workout.problem:
                problems.append(workout.problem)
            text = workout.text
            if warnings is not None:
                warnings.extend(workout.warnings)
    amount_error = _check_amounts(entry.get("moving_time"), entry.get("distance"))
    if amount_error:
        problems.append(amount_error)
    if problems:
        raise ValueError("; ".join(problems))

    body = _prepare_event_data(
        str(name),
        str(workout_type),
        validated_date + "T00:00:00",
        text,
        entry.get("moving_time"),
        entry.get("distance"),
        category="WORKOUT",
    )
    if description:
        # Native Intervals.icu workout text, sent as-is.
        body["description"] = description
    return body


@tool("admin")
async def add_events_bulk(  # pylint: disable=too-many-locals
    events: list[dict[str, Any]],
    athlete_id: str | None = None,
) -> str:
    """WRITES to Intervals.icu: create many calendar events (planned workouts and/or notes) in one call.

    All entries are validated first (workout documents with the same checks as validate_workout).
    If ANY entry is invalid, nothing is sent and all errors are returned (fix them and retry).
    Otherwise all entries are sent in a single POST to /athlete/{id}/events/bulk. This tool only
    creates new events (it never updates existing ones; use add_or_update_event for that).
    At most 100 entries per call.

    Args:
        events: List of entry objects (see below), at most 100.
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)

    Entry keys (keys that do not apply to the entry's category are rejected, not ignored):
        name (str, required, not blank): Name/title of the event
        start_date (str, required): Date in YYYY-MM-DD format
        category (str, optional): "WORKOUT" (default) or "NOTE"
        WORKOUT entries:
            workout_type (str, REQUIRED here, unlike add_or_update_event where it is inferred
                from the name): e.g. Ride, Run, Swim, Walk, Row
            workout_doc (object, optional): structured workout with "steps", same structure as in
                add_or_update_event, e.g.
                {"description": "...", "steps": [{"power": {"value": 80, "units": "%ftp"}, "duration": 900}]}
            description (str, optional): workout as native Intervals.icu workout text, sent as-is.
                Mutually exclusive with workout_doc.
            moving_time (int, optional): expected moving time in seconds (>= 0)
            distance (int, optional): expected distance in meters (>= 0)
        NOTE entries:
            description (str, required): plain text content of the note
            color (str, optional): note color name, defaults to "green"

    Example:
        [
            {"name": "Easy run", "start_date": "2025-01-06", "workout_type": "Run", "moving_time": 2700},
            {"name": "Rest day", "start_date": "2025-01-07", "category": "NOTE", "description": "Full rest"}
        ]

    Returns:
        JSON with "created" (per event: input index, event id, and the name/start date as returned
        by the API), "errors" (index and all problems found per invalid entry, joined with "; ")
        and counts ("created_count" = events the API returned with an id). If the request
        fails, an error string is returned and events may have been created.
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if not events:
        return "Error: No events provided."
    if len(events) > MAX_BULK_EVENTS:
        return f"Error: {len(events)} entries; at most {MAX_BULK_EVENTS} events can be created per call. Split the list."

    bodies: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for index, entry in enumerate(events):
        entry_warnings: list[str] = []
        try:
            bodies.append(_build_bulk_event_entry(entry, entry_warnings))
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            errors.append({"index": index, "error": str(e)})
        if entry_warnings:
            warnings.append({"index": index, "warnings": entry_warnings})

    if errors:
        return json.dumps(
            {
                "message": "No events were sent because some entries are invalid.",
                "created_count": 0,
                "error_count": len(errors),
                "created": [],
                "errors": errors,
            },
            indent=2,
        )

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/events/bulk",
        method="POST",
        params={"upsert": False, "upsertOnUid": False, "updatePlanApplied": False},
        data=bodies,
    )
    if isinstance(result, dict) and "error" in result:
        return (
            f"Error creating events in bulk: {str(result.get('message', 'Unknown error')).rstrip('.')}. "
            "The events may have been partially or fully created; check get_events for the "
            "date range before retrying."
        )
    returned = result if isinstance(result, list) else []
    created: list[dict[str, Any]] = []
    for index, body in enumerate(bodies):
        item = returned[index] if index < len(returned) else {}
        if not isinstance(item, dict):
            item = {}
        created.append(
            {
                "index": index,
                "id": item.get("id"),
                "name": item.get("name", body.get("name")),
                "start_date_local": item.get("start_date_local"),
            }
        )
    if len(returned) != len(bodies):
        errors.append(
            {
                "index": None,
                "error": f"API returned {len(returned)} events for {len(bodies)} sent; ids may be missing. "
                "Check get_events for the dates before retrying.",
            }
        )
    return json.dumps(
        {
            "created_count": sum(1 for row in created if row["id"] is not None),
            "sent_count": len(bodies),
            "error_count": len(errors),
            "created": created,
            "errors": errors,
            "warnings": warnings,
        },
        indent=2,
    )


PLAN_CATEGORIES = ("PLAN", "TARGET", "RACE_A", "RACE_B", "RACE_C", "SEASON_START")


def _format_plan_event(event: dict[str, Any]) -> str:
    """One line for a plan phase, weekly target or race."""
    day = str(event.get("start_date_local", ""))[:10]
    end = str(event.get("end_date_local", ""))[:10]
    text = f"- {day}" + (f" to {end}" if end and end != day else "") + f" {event_type_label(event)}: {event.get('name', 'unnamed')} (event {event.get('id')})"
    targets = []
    if event.get("load_target") is not None:
        targets.append(f"load {event['load_target']}")
    if event.get("time_target") is not None:
        targets.append(f"time {hms(event['time_target'])}")
    if event.get("distance_target") is not None:
        targets.append(f"distance {event['distance_target'] / 1000:.1f} km")
    if event.get("for_week"):
        targets.append("weekly target")
    if targets:
        text += " | " + ", ".join(targets)
    if event.get("description"):
        text += f" | {str(event['description'])[:120]}"
    return text


@tool("read")
async def get_training_plan(  # pylint: disable=too-many-locals
    start_date: str | None = None,
    end_date: str | None = None,
    athlete_id: str | None = None,
    output_format: str = "text",
) -> str:
    """Annual training plan view: plan phases, weekly targets, races and fitness-model events

    Reads the training plan assigned to the athlete (``/training-plan``), the calendar entries
    that make up the annual plan (categories PLAN = phases/blocks, TARGET = weekly load, time
    or distance targets, RACE_A/B/C = races with priority, SEASON_START) and the events that
    influence the fitness model (set eFTP, set fitness/fatigue, fitness days). Nothing is
    changed. Default range: 30 days back to 180 days ahead.

    Args:
        start_date: Start date YYYY-MM-DD (optional, default 30 days ago)
        end_date: End date YYYY-MM-DD (optional, default 180 days ahead)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        output_format: "text" (default) or "json"
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    start = start_date or get_default_start_date(30)
    end = end_date or get_default_future_end_date(180)
    try:
        validate_date(start)
        validate_date(end)
    except ValueError as exc:
        return f"Error: {exc}"

    plan = await make_intervals_request(url=f"/athlete/{seg(athlete_id_to_use)}/training-plan")
    plan_info = plan if isinstance(plan, dict) and "error" not in plan else {}
    plan_error = plan.get("message", "unknown error") if isinstance(plan, dict) and "error" in plan else None
    events = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/events",
        params={"oldest": start, "newest": end, "category": ",".join(PLAN_CATEGORIES)},
    )
    if isinstance(events, dict) and "error" in events:
        # Older API versions reject unknown category names: fall back to all events.
        events = await make_intervals_request(
            url=f"/athlete/{seg(athlete_id_to_use)}/events", params={"oldest": start, "newest": end}
        )
    plan_events = [
        e for e in (events if isinstance(events, list) else []) if isinstance(e, dict) and e.get("category") in PLAN_CATEGORIES
    ]
    model_events = await make_intervals_request(url=f"/athlete/{seg(athlete_id_to_use)}/fitness-model-events")
    model_list = [e for e in (model_events if isinstance(model_events, list) else []) if isinstance(e, dict)]

    phases = [e for e in plan_events if e.get("category") in ("PLAN", "SEASON_START")]
    targets = [e for e in plan_events if e.get("category") == "TARGET"]
    races = [e for e in plan_events if str(e.get("category", "")).startswith("RACE")]
    if output_format.strip().lower() == "json":
        return json.dumps(
            {"start": start, "end": end, "training_plan": plan_info, "training_plan_error": plan_error, "phases": [_event_json(e) for e in phases],
             "targets": [_event_json(e) for e in targets], "races": [_event_json(e) for e in races],
             "fitness_model_events": model_list},
            ensure_ascii=False,
        )
    lines = [f"Training plan for athlete {athlete_id_to_use}, {start} to {end}:"]
    if plan_info.get("training_plan_id") or plan_info.get("training_plan"):
        lines.append(
            f"Assigned plan: {plan_info.get('training_plan_alias') or plan_info.get('training_plan') or plan_info.get('training_plan_id')}, "
            f"start {plan_info.get('training_plan_start_date')}, last applied {plan_info.get('training_plan_last_applied')}"
        )
    elif plan_error:
        lines.append(f"Assigned plan: unknown (the training plan could not be read: {plan_error})")
    else:
        lines.append("Assigned plan: none (no Intervals.icu training plan is applied to this athlete)")
    if not (phases or targets or races or model_list):
        lines.append("No plan phases, weekly targets, races or fitness-model events in this range.")
    lines.append(f"Phases / season markers ({len(phases)}):")
    lines.extend([_format_plan_event(e) for e in phases] or ["- none"])
    lines.append(f"Weekly targets ({len(targets)}):")
    lines.extend([_format_plan_event(e) for e in targets] or ["- none"])
    lines.append(f"Races ({len(races)}):")
    lines.extend([_format_plan_event(e) for e in races] or ["- none"])
    lines.append(f"Fitness model events ({len(model_list)}):")
    lines.extend(
        [
            f"- {str(e.get('start_date_local', ''))[:10]} {e.get('category')}: {e.get('name') or ''} "
            f"{json.dumps({k: v for k, v in e.items() if k in ('ctl', 'atl', 'eftp', 'ctl_days', 'atl_days', 'type') and v is not None})}"
            for e in model_list
        ]
        or ["- none"]
    )
    return "\n".join(lines)
