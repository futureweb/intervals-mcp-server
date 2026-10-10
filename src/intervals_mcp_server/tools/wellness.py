"""
Wellness-related MCP tools for Intervals.icu.

This module contains tools for retrieving and updating athlete wellness data.
"""

from datetime import date as calendar_date, timedelta
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.utils.custom_fields import INPUT_FIELD, CustomFieldDefs
from intervals_mcp_server.utils.formatting import format_wellness_entry
from intervals_mcp_server.utils.validation import (
    resolve_athlete_id,
    resolve_date_params,
    validate_date,
)
from intervals_mcp_server.tool_guard import output_budget

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()


@tool("read")
async def get_wellness_data(  # pylint: disable=too-many-locals,too-many-branches
    athlete_id: str | None = None,
    api_key: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_all_fields: bool = False,
) -> str:
    """Get wellness data for an athlete from Intervals.icu.

    By default returns standard wellness fields (training metrics, vitals, sleep,
    subjective scores, etc.). Set include_all_fields=True to also include any
    additional or custom fields configured by the user in Intervals.icu; custom
    wellness fields are labelled with their display name and units from the
    athlete's custom item definitions.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        start_date: Start date in YYYY-MM-DD format (optional, defaults to 30 days ago)
        end_date: End date in YYYY-MM-DD format (optional, defaults to today)
        include_all_fields: If True, include additional and custom fields beyond the standard set (optional, defaults to False)

    Long ranges are paged by day: when the answer would get too large it stops with a note
    giving the start_date to continue with.
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    start_date, end_date = resolve_date_params(start_date, end_date)
    try:
        validate_date(start_date)
        validate_date(end_date)
    except ValueError as exc:
        return f"Error: {exc}"

    params = {"oldest": start_date, "newest": end_date}

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/wellness", api_key=api_key, params=params
    )

    if isinstance(result, dict) and "error" in result:
        return f"Error fetching wellness data: {result.get('message')}"

    if not result:
        return (
            f"No wellness data found for athlete {athlete_id_to_use} in the specified date range."
        )

    field_definitions: CustomFieldDefs | None = None
    if include_all_fields:
        index = await get_custom_item_index(athlete_id=athlete_id_to_use, api_key=api_key)
        field_definitions = index.get(INPUT_FIELD) or None

    entries: list[dict[str, Any]] = []
    if isinstance(result, dict):
        for date_str, data in result.items():
            if isinstance(data, dict):
                if "date" not in data:
                    data["date"] = date_str
                entries.append(data)
    elif isinstance(result, list):
        entries = [entry for entry in result if isinstance(entry, dict)]
    entries.sort(key=lambda e: str(e.get("id") or e.get("date") or ""))

    # Page by day so the answer stays within the size of one tool result (a year with all
    # fields is about 1 MB); the note says where to continue.
    budget = output_budget()
    wellness_summary = "Wellness Data:\n\n"
    for position, entry in enumerate(entries):
        block = (
            format_wellness_entry(
                entry,
                include_all_fields=include_all_fields,
                field_definitions=field_definitions,
            )
            + "\n\n"
        )
        if position and len(wellness_summary) + len(block) > budget:
            last_day = str(entries[position - 1].get("id") or entries[position - 1].get("date") or "")[:10]
            try:
                next_day = (calendar_date.fromisoformat(last_day) + timedelta(days=1)).isoformat()
            except ValueError:
                next_day = str(entry.get("id") or entry.get("date") or "")[:10]
            wellness_summary += (
                f"Note: output stopped after {position} of {len(entries)} days (through {last_day}) to keep the "
                f"response small. Continue with start_date={next_day} (end_date={end_date}).\n"
            )
            break
        wellness_summary += block

    return wellness_summary


# Subjective scales accepted by Intervals.icu (all 1-4, see update_wellness docstring).
_SUBJECTIVE_SCALE_MIN = 1
_SUBJECTIVE_SCALE_MAX = 4


@tool("write", overwrites=True)
async def update_wellness(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    date: str,
    soreness: int | None = None,
    fatigue: int | None = None,
    stress: int | None = None,
    mood: int | None = None,
    motivation: int | None = None,
    injury: int | None = None,
    comments: str | None = None,
    athlete_id: str | None = None,
    api_key: str | None = None,
    clear_comments: bool = False,
) -> str:
    """WRITE: Update (modify) the subjective wellness fields of one day in Intervals.icu.

    This tool MODIFIES data in Intervals.icu. Only the fields you pass are sent and
    changed; all other values of that day (weight, HRV, sleep, resting HR, etc., which
    are usually synced from devices) are left untouched. At least one field is required.
    If no wellness record exists for the date, it is created.

    Scales (integers 1-4, as in the Intervals.icu wellness dialog):
        soreness:   1=Low, 2=Avg, 3=High, 4=Extreme
        fatigue:    1=Low, 2=Avg, 3=High, 4=Extreme
        stress:     1=Low, 2=Avg, 3=High, 4=Extreme
        mood:       1=Great, 2=Good, 3=OK, 4=Grumpy
        motivation: 1=Extreme, 2=High, 3=Avg, 4=Low
        injury:     1=None, 2=Niggle, 3=Poor, 4=Injured

    A scale value cannot be cleared again through the API once it is set (null is
    ignored and 0 is rejected); use the Intervals.icu web app for that. Double-check
    the values before writing.

    NOTE: comments REPLACES the day's existing comment, it does not append. To append,
    read the existing record first (get_wellness_data) and send the combined text.
    An empty string is ignored (it never wipes the comment); clear_comments=true empties it.

    Args:
        date: The day to update in YYYY-MM-DD format
        soreness: Muscle soreness, 1-4 (optional)
        fatigue: Fatigue, 1-4 (optional)
        stress: Stress, 1-4 (optional)
        mood: Mood, 1-4 (optional)
        motivation: Motivation, 1-4 (optional)
        injury: Injury level, 1-4 (optional)
        comments: Free-text comment for the day; replaces any existing comment (optional)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        clear_comments: Empty the day's comment (optional, default false)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    try:
        validate_date(date)
    except ValueError as exc:
        return f"Error: {exc}"

    scales = {
        "soreness": soreness,
        "fatigue": fatigue,
        "stress": stress,
        "mood": mood,
        "motivation": motivation,
        "injury": injury,
    }
    body: dict[str, int | str] = {}
    for name, value in scales.items():
        if value is None:
            continue
        # bool is a subclass of int; reject it explicitly
        if isinstance(value, bool) or not _SUBJECTIVE_SCALE_MIN <= value <= _SUBJECTIVE_SCALE_MAX:
            return (
                f"Error: {name} must be an integer between {_SUBJECTIVE_SCALE_MIN} "
                f"and {_SUBJECTIVE_SCALE_MAX}."
            )
        body[name] = value
    if clear_comments:
        body["comments"] = ""
    elif comments is not None and comments.strip():
        body["comments"] = comments  # a placeholder "" never wipes the comment

    if not body:
        return (
            "Error: No wellness fields provided. Pass at least one of soreness, fatigue, "
            "stress, mood, motivation, injury or comments."
        )

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/wellness/{seg(date)}",
        api_key=api_key,
        method="PUT",
        data=body,
    )

    if isinstance(result, dict) and "error" in result:
        return f"Error updating wellness data: {result.get('message')}"

    if not isinstance(result, dict) or not result:
        return f"Error updating wellness data: unexpected empty response for {date}."

    if "date" not in result:
        result["date"] = result.get("id", date)

    return "Wellness updated:\n\n" + format_wellness_entry(result)
