"""
Wellness-related MCP tools for Intervals.icu.

This module contains tools for retrieving and updating athlete wellness data.
"""

from datetime import date as calendar_date, timedelta
from typing import Annotated, Any

from pydantic import Field

from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.utils.custom_fields import INPUT_FIELD, CustomFieldDefs
from intervals_mcp_server.utils.dates import athlete_today
from intervals_mcp_server.utils.formatting import format_wellness_entry
from intervals_mcp_server.utils.params import AthleteId, EndDate, StartDate
from intervals_mcp_server.utils.validation import (
    resolve_athlete_id,
    resolve_date_params,
    validate_date,
)
from intervals_mcp_server.utils.wellness_completeness import (
    completeness_line,
    completeness_start,
    has_missing_custom_fields,
    today_completeness,
)
from intervals_mcp_server.tool_guard import output_budget

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

COMPLETENESS_NAMES = 15  # names per group in the line on today's missing usual fields


@tool("read")
async def get_wellness_data(  # pylint: disable=too-many-locals,too-many-branches
    athlete_id: AthleteId = None,
    start_date: Annotated[StartDate, Field(description="First day YYYY-MM-DD; default 30 days before today")] = None,
    end_date: EndDate = None,
    include_all_fields: Annotated[
        bool, Field(description="Also list every other field, custom wellness fields with name and units")
    ] = False,
) -> str:
    """Use for the raw daily wellness records of a date range (default the last 30 days; read-only).

    Per day as stored: CTL/ATL and eFTP per sport, vitals (weight, resting HR, HRV, SpO2 ...),
    sleep, subjective scores, nutrition, steps and comments; include_all_fields adds every other
    field, custom wellness fields labelled with name and units. When the range includes today, a
    line names the usual fields not yet in today's record, grouped by when they usually arrive:
    not yet available, not normal. Long ranges are paged by day: a note gives the start_date to
    continue with. Baselines and trends: get_recovery_snapshot, get_wellness_trends,
    get_nutrition_summary.
    Method: intervals://methods/wellness (get_guide).
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

    # Today's completeness needs the 14 days before today: widen the same request.
    today = athlete_today()
    check_today = start_date <= today.isoformat() <= end_date
    oldest = min(start_date, completeness_start(today).isoformat()) if check_today else start_date
    params = {"oldest": oldest, "newest": end_date}

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/wellness", params=params
    )

    if isinstance(result, dict) and "error" in result:
        return f"Error fetching wellness data: {result.get('message')}"

    fetched: list[dict[str, Any]] = []
    if isinstance(result, dict):
        for date_str, data in result.items():
            if isinstance(data, dict):
                if "date" not in data:
                    data["date"] = date_str
                fetched.append(data)
    elif isinstance(result, list):
        fetched = [entry for entry in result if isinstance(entry, dict)]

    def day_of(entry: dict[str, Any]) -> str:
        return str(entry.get("id") or entry.get("date") or "")

    # Only the days the completeness check added before start_date are left out.
    entries = sorted((e for e in fetched if not oldest <= day_of(e)[:10] < start_date), key=day_of)

    field_definitions: CustomFieldDefs | None = None
    if include_all_fields and entries:
        index = await get_custom_item_index(athlete_id=athlete_id_to_use)
        field_definitions = index.get(INPUT_FIELD) or None

    completeness = today_completeness(fetched, today, field_definitions) if check_today else None
    if not (include_all_fields and entries) and has_missing_custom_fields(completeness):
        # Display names of the missing custom fields (the definitions are cached per athlete).
        index = await get_custom_item_index(athlete_id=athlete_id_to_use)
        completeness = today_completeness(fetched, today, index.get(INPUT_FIELD))
    today_line = completeness_line(completeness, COMPLETENESS_NAMES, " (all: get_recovery_snapshot detail_level=full)")

    if not entries:
        return (
            f"No wellness data found for athlete {athlete_id_to_use} in the specified date range."
            + (f"\n\n{today_line}" if today_line else "")
        )

    # Page by day so the answer stays within the size of one tool result (a year with all
    # fields is about 1 MB); the note says where to continue.
    budget = output_budget()
    wellness_summary = "Wellness Data:\n\n" + (f"{today_line}\n\n" if today_line else "")
    for position, entry in enumerate(entries):
        block = (
            format_wellness_entry(
                entry,
                include_all_fields=include_all_fields,
                field_definitions=field_definitions,
                header=False,
            )
            + "\n\n"
        )
        if position and len(wellness_summary) + len(block) > budget:
            last_day = day_of(entries[position - 1])[:10]
            try:
                next_day = (calendar_date.fromisoformat(last_day) + timedelta(days=1)).isoformat()
            except ValueError:
                next_day = day_of(entry)[:10]
            wellness_summary += (
                f"Note: output stopped after {position} of {len(entries)} days (through {last_day}) to keep the "
                f"response small. Continue with start_date={next_day} (end_date={end_date}).\n"
            )
            break
        wellness_summary += block

    return wellness_summary


# Subjective scales accepted by Intervals.icu (all 1-4, see the update_wellness parameters).
_SUBJECTIVE_SCALE_MIN = 1
_SUBJECTIVE_SCALE_MAX = 4


@tool("write", overwrites=True)
async def update_wellness(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    date: Annotated[str, Field(description="Day to update, YYYY-MM-DD")],
    soreness: Annotated[int | None, Field(description="Soreness 1-4: 1=Low, 2=Avg, 3=High, 4=Extreme", json_schema_extra={"minimum": 1, "maximum": 4})] = None,
    fatigue: Annotated[int | None, Field(description="Fatigue 1-4: 1=Low, 2=Avg, 3=High, 4=Extreme", json_schema_extra={"minimum": 1, "maximum": 4})] = None,
    stress: Annotated[int | None, Field(description="Stress 1-4: 1=Low, 2=Avg, 3=High, 4=Extreme", json_schema_extra={"minimum": 1, "maximum": 4})] = None,
    mood: Annotated[int | None, Field(description="Mood 1-4: 1=Great, 2=Good, 3=OK, 4=Grumpy", json_schema_extra={"minimum": 1, "maximum": 4})] = None,
    motivation: Annotated[int | None, Field(description="Motivation 1-4: 1=Extreme, 2=High, 3=Avg, 4=Low", json_schema_extra={"minimum": 1, "maximum": 4})] = None,
    injury: Annotated[int | None, Field(description="Injury 1-4: 1=None, 2=Niggle, 3=Poor, 4=Injured", json_schema_extra={"minimum": 1, "maximum": 4})] = None,
    comments: Annotated[
        str | None, Field(description="Free text; REPLACES the day's comment (no append); empty text is ignored")
    ] = None,
    athlete_id: AthleteId = None,
    clear_comments: Annotated[bool, Field(description="Empty the day's comment (not together with comments)")] = False,
) -> str:
    """Use only when the athlete asks to record or change subjective wellness scores or the comment of one day.

    Writes the wellness record of that day in Intervals.icu (created if missing): only the fields
    passed are changed; device values (weight, HRV, sleep, resting HR ...) stay untouched. At
    least one field is required. Scales are integers 1-4 as in the Intervals.icu wellness dialog
    (labels per parameter). A scale value once set cannot be cleared through the API (null is
    ignored, 0 rejected): use the web app, so double-check before writing. comments replaces the
    existing comment: to append, read it with get_wellness_data and send the combined text.
    Returns the updated record.
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
    if clear_comments and comments is not None and comments.strip():
        return "Error: pass either comments or clear_comments=true, not both."
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
