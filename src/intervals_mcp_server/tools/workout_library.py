"""
Workout library MCP tools for Intervals.icu.

This module contains tools for listing the athlete's workout library (folders and plans
with their workouts), creating new library workouts (templates) and scheduling a library
workout on the calendar.
"""

import json
from typing import Annotated, Any

from pydantic import Field

from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.utils.params import AthleteId, OutputFormat
from intervals_mcp_server.utils.sports import hms
from intervals_mcp_server.utils.types import WorkoutDoc
from intervals_mcp_server.utils.validation import (
    resolve_activity_type,
    resolve_athlete_id,
    validate_date,
)
from intervals_mcp_server.utils.workout_validation import has_step_lines, warnings_note, workout_text_for_write, write_refusal

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

# Workout fields copied from a library workout when scheduling it on the calendar.
_WORKOUT_FIELDS_FOR_EVENT = (
    "name",
    "description",
    "type",
    "moving_time",
    "distance",
    "indoor",
    "color",
    "tags",
    "target",
    "sub_type",
    "carbs_per_hour",
    # Planned load and work, e.g. the manual load of a strength session.
    "icu_training_load",
    "joules",
)

# Above this many workouts, an unfiltered listing only shows per-folder counts.
_MAX_WORKOUTS_FULL_LISTING = 50


def _folder_matches(folder: dict[str, Any], key: str) -> bool:
    """Check whether a folder matches the given name (case-insensitive) or id."""
    return str(folder.get("id")) == key or str(folder.get("name", "")).lower() == key.lower()


def _format_workout(workout: dict[str, Any], folder_name: str) -> str:
    """Format a library workout as a short text block."""
    lines = [
        f"- id: {workout.get('id')} | name: {workout.get('name', 'Unnamed')} "
        f"| type: {workout.get('type', 'Unknown')} | folder: {folder_name}"
    ]
    details = []
    moving_time = workout.get("moving_time")
    if isinstance(moving_time, (int, float)) and moving_time:
        minutes = int(moving_time) // 60
        details.append(f"duration: {minutes // 60}h{minutes % 60:02d}m")
    if workout.get("icu_training_load") is not None:
        details.append(f"load: {workout['icu_training_load']}")
    if details:
        lines.append("  " + " | ".join(details))
    description = str(workout.get("description") or "").strip()
    if description:
        first_line = description.splitlines()[0]
        if len(first_line) > 100 or "\n" in description:
            first_line = first_line[:100] + "..."
        lines.append(f"  description: {first_line}")
    return "\n".join(lines)


def _format_folder(folder: dict[str, Any]) -> str:
    """Format a folder/plan with its workouts."""
    name = str(folder.get("name", "Unnamed"))
    children = [c for c in folder.get("children") or [] if isinstance(c, dict)]
    header = (
        f"{folder.get('type', 'FOLDER')}: {name} (id: {folder.get('id')}) "
        f"- {len(children)} workouts"
    )
    return "\n".join([header, *(_format_workout(child, name) for child in children)])


def _available_folders(folders: list[dict[str, Any]]) -> str:
    """List folder names and ids for error messages."""
    return ", ".join(f"{f.get('name')} (id {f.get('id')})" for f in folders)


async def _fetch_folders(
    athlete_id: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch all folders/plans. Returns (folders, error_message)."""
    result = await make_intervals_request(url=f"/athlete/{seg(athlete_id)}/folders")
    if isinstance(result, dict) and "error" in result:
        return [], str(result.get("message", "Unknown error"))
    folders = [f for f in result if isinstance(f, dict)] if isinstance(result, list) else []
    return folders, None


@tool("read")
async def get_workout_library(
    folder: Annotated[str | None, Field(description="Only this folder or plan: name (case-insensitive) or id")] = None,
    athlete_id: AthleteId = None,
) -> str:
    """Use to list the athlete's workout library: folders and plans with their workouts and ids (read-only). One workout in detail: get_library_workout."""
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    folders, error = await _fetch_folders(athlete_id_to_use)
    if error:
        return f"Error fetching workout library: {error}"

    if folder:
        folders = [f for f in folders if _folder_matches(f, folder)]
        if not folders:
            return f"No folder matching '{folder}' found for athlete {athlete_id_to_use}."

    if not folders:
        return f"No workout library found for athlete {athlete_id_to_use}."

    if not folder:
        total = sum(len(f.get("children") or []) for f in folders)
        if total > _MAX_WORKOUTS_FULL_LISTING:
            lines = [
                f"{f.get('type', 'FOLDER')}: {f.get('name', 'Unnamed')} (id: {f.get('id')}) "
                f"- {len(f.get('children') or [])} workouts"
                for f in folders
            ]
            return (
                f"Workout library ({total} workouts, too many to list at once). "
                "Pass `folder` (name or id) to see the workouts of one folder:\n\n"
                + "\n".join(lines)
            )

    return "Workout library:\n\n" + "\n\n".join(_format_folder(f) for f in folders)


async def _resolve_folder_id(
    athlete_id: str, folder: str | None
) -> tuple[int | None, str | None]:
    """Resolve a folder name/id to a folder id. Returns (folder_id, error_message)."""
    folders, error = await _fetch_folders(athlete_id)
    if error:
        return None, f"Error fetching folders: {error}"
    if not folders:
        return None, (
            "Error: the workout library has no folders. Create a folder or plan in "
            "Intervals.icu first, workouts must belong to a folder."
        )
    if folder:
        matches = [f for f in folders if _folder_matches(f, folder)]
        if not matches:
            return None, (
                f"Error: no folder matching '{folder}'. "
                f"Available folders: {_available_folders(folders)}"
            )
        if len(matches) > 1:
            return None, (
                f"Error: folder '{folder}' is ambiguous, use the id. "
                f"Matches: {_available_folders(matches)}"
            )
        if matches[0].get("type", "FOLDER") != "FOLDER":
            return None, (
                f"Error: '{matches[0].get('name')}' is a {matches[0].get('type')}; "
                "plans are not supported by this tool, choose a regular folder."
            )
        return int(matches[0]["id"]), None
    plain = [f for f in folders if f.get("type", "FOLDER") == "FOLDER"]
    if not plain:
        return None, "Error: the workout library has no regular folders (plans are not supported)."
    if len(plain) == 1:
        return int(plain[0]["id"]), None
    return None, (
        "Error: multiple folders exist, specify one with 'folder'. "
        f"Available folders: {_available_folders(plain)}"
    )


@tool("write")
async def create_library_workout(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements,too-many-branches
    name: Annotated[str, Field(description='Workout name, e.g. "Sweet Spot 3x10"')],
    sport_type: Annotated[str, Field(description="Sport, e.g. Ride, Run, Swim, Walk, Row, WeightTraining")],
    folder: Annotated[str | None, Field(description="Folder or plan by name (case-insensitive) or id; optional if the library has exactly one folder")] = None,
    description: Annotated[str | None, Field(description="Workout text in Intervals.icu syntax or free text; ignored when workout_doc has steps")] = None,
    workout_doc: Annotated[WorkoutDoc | None, Field(description="Structured steps, rendered into the text. Format: intervals://workout-syntax")] = None,
    moving_time: Annotated[int | None, Field(description="Planned moving time in seconds")] = None,
    distance: Annotated[int | None, Field(description="Planned distance in metres")] = None,
    athlete_id: AthleteId = None,
) -> str:
    """Use only when the athlete asks to save a workout template in the workout library. Writes to Intervals.icu.

    The workout_doc is validated like add_or_update_event (with errors nothing is created) and rendered into the workout text; an empty workout_doc is ignored and description is used. Format: intervals://workout-syntax (get_guide). Schedule it with add_event_from_library.
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if not (name or "").strip():
        return "Error: name must not be blank."
    for key, value in (("moving_time", moving_time), ("distance", distance)):
        if value is not None and value < 0:
            return f"Error: {key} must not be negative."
    workout_type = resolve_activity_type(name, sport_type)
    # A doc with steps wins; a blank doc ({} / {"steps": []}) falls back to the description.
    workout = workout_text_for_write(workout_doc, workout_type)
    if workout.problem:
        return write_refusal(workout.problem)
    text = workout.text
    if text is not None and workout.text_only and description is not None and description.strip():
        return "Error: pass the text either as description or as workout_doc, not both. Nothing was written."

    folder_id, error = await _resolve_folder_id(athlete_id_to_use, folder)
    if error:
        return error

    workout_data: dict[str, Any] = {
        "name": name,
        "type": workout_type,
        "folder_id": folder_id,
    }
    if text is not None:
        workout_data["description"] = text
    elif description is not None:
        workout_data["description"] = description
    if moving_time is not None:
        workout_data["moving_time"] = moving_time
    if distance is not None:
        workout_data["distance"] = distance

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/workouts",
        data=workout_data,
        method="POST",
    )
    if isinstance(result, dict) and "error" in result:
        return f"Error creating library workout: {result.get('message', 'Unknown error')}"
    if isinstance(result, dict) and result.get("id") is not None:
        return f"Successfully created library workout id: {result.get('id')} in folder {folder_id}" + warnings_note(workout.warnings)
    return f"No library workout created for athlete {athlete_id_to_use}."


@tool("write")
async def add_event_from_library(  # pylint: disable=too-many-return-statements
    workout_id: Annotated[str, Field(description="Library workout id (get_workout_library); small numbers such as 1 are normal")],
    date: Annotated[str, Field(description="Day YYYY-MM-DD")],
    athlete_id: AthleteId = None,
) -> str:
    """Use only when the athlete asks to schedule a workout from the library on a day. Writes a new WORKOUT event to Intervals.icu.

    Copies the library workout's name, steps, type, duration, distance, tags and planned load. A workout imported from a file (.zwo, .mrc, .erg, .fit) without workout steps in its text is created without steps; the answer says so.
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if not (workout_id.isascii() and workout_id.isdigit()):
        return "Error: workout_id must be a numeric library workout ID."
    try:
        validated_date = validate_date(date)
    except ValueError as e:
        return f"Error: {e}"

    workout = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/workouts/{seg(workout_id)}"
    )
    if isinstance(workout, dict) and "error" in workout:
        return f"Error fetching library workout: {workout.get('message', 'Unknown error')}"
    if not isinstance(workout, dict) or not workout:
        return f"No library workout found with id {workout_id}."

    event_data: dict[str, Any] = {
        key: workout[key] for key in _WORKOUT_FIELDS_FOR_EVENT if workout.get(key) is not None
    }
    event_data["category"] = "WORKOUT"
    event_data["start_date_local"] = validated_date + "T00:00:00"

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/events",
        data=event_data,
        method="POST",
    )
    if isinstance(result, dict) and "error" in result:
        return (
            f"Error creating event from library workout: {result.get('message', 'Unknown error')}"
        )
    if isinstance(result, dict) and result.get("id") is not None:
        note = ""
        steps = (workout.get("workout_doc") or {}).get("steps") if isinstance(workout.get("workout_doc"), dict) else None
        if steps and not has_step_lines(str(workout.get("description") or "")):
            note = (
                " Note: the library workout's structure is not in its description text (e.g. imported from a "
                "file), so the event was created WITHOUT steps; check it with get_event_by_id."
            )
        return (
            f"Successfully created event id: {result.get('id')} on {validated_date} "
            f"from library workout {workout_id}.{note}"
        )
    return f"No event created for athlete {athlete_id_to_use}."


@tool("destructive")
async def delete_library_workout(
    workout_id: Annotated[str, Field(description="Library workout id (get_workout_library); small numbers such as 1 are normal")],
    athlete_id: AthleteId = None,
) -> str:
    """Use only when the athlete explicitly asks to delete a workout from the library.

    DELETES it from Intervals.icu permanently (cannot be undone). Calendar events created from it are not affected; other workouts of the same plan are kept.
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if not (workout_id.isascii() and workout_id.isdigit()):
        return "Error: workout_id must be a numeric library workout ID."

    url = f"/athlete/{seg(athlete_id_to_use)}/workouts/{seg(workout_id)}"
    workout = await make_intervals_request(url=url)
    if isinstance(workout, dict) and "error" in workout:
        return f"Error fetching library workout: {workout.get('message', 'Unknown error')}"
    if not isinstance(workout, dict) or not workout:
        return f"No library workout found with id {workout_id}."

    result = await make_intervals_request(url=url, method="DELETE")
    if isinstance(result, dict) and "error" in result:
        return f"Error deleting library workout: {result.get('message', 'Unknown error')}"
    return f"Deleted library workout {workout_id} '{workout.get('name') or 'unnamed'}'."


@tool("read")
async def get_library_workout(
    workout_id: Annotated[str, Field(description="Library workout id (get_workout_library); small numbers such as 1 are normal")],
    athlete_id: AthleteId = None,
    output_format: Annotated[OutputFormat, Field(description="text or json (complete workout incl. workout_doc)")] = "text",
) -> str:
    """Use to read one library workout (steps, planned time, load, targets), e.g. to reuse it with add_or_update_event or validate_workout. Ids: get_workout_library."""
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    result = await make_intervals_request(url=f"/athlete/{seg(athlete_id_to_use)}/workouts/{seg(workout_id)}")
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching library workout: {result.get('message', 'Unknown error')}"
    if not isinstance(result, dict) or not result:
        return f"No library workout found with id {workout_id}."
    if output_format.strip().lower() == "json":
        return json.dumps(result, ensure_ascii=False)
    def shown(key: str) -> Any:
        value = result.get(key)
        if isinstance(value, list):
            return ", ".join(str(v) for v in value) or "none"
        return "n/a" if value is None else value

    lines = [
        f"Library workout {result.get('id')}: {result.get('name', 'unnamed')} ({result.get('type', '?')}, folder {shown('folder_id')})",
        f"Planned time {hms(result.get('moving_time'))}, load {shown('icu_training_load')}, intensity {shown('icu_intensity')}, "
        f"target {shown('target')}, indoor {shown('indoor')}, tags {shown('tags')}, updated {shown('updated')}",
    ]
    if result.get("description"):
        lines.append("Description / workout text:")
        lines.append(str(result["description"]))
    doc = result.get("workout_doc")
    if isinstance(doc, dict) and isinstance(doc.get("steps"), list):
        try:
            lines.append("Steps (rendered):")
            lines.append(str(WorkoutDoc.from_dict({"steps": doc["steps"]})).strip())
        except (ValueError, TypeError, KeyError, AttributeError) as exc:  # malformed or unsupported (nested repeats)
            lines.append(f"Steps could not be rendered ({exc}); raw steps: {json.dumps(doc['steps'], ensure_ascii=False)}")
    return "\n".join(lines)
