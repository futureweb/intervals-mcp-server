"""
Performance MCP tools for Intervals.icu: best efforts within one activity and across several,
searching for comparable intervals, power / heart rate / pace histograms, the same workout
over time, power-to-heart-rate efficiency per power band and fatigue resistance (fresh vs
fatigued power curves).

All tools are read-only. They report values computed by Intervals.icu or simple statistics
over them; nothing is calibrated, interpolated or judged. Every text output ends with the
number of API calls made so the caller can keep an eye on the rate limit.
"""

# pylint: disable=too-many-lines

import json
import math
import re
import statistics
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field

from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.gear import get_gear_map
from intervals_mcp_server.utils.dates import get_default_end_date
from intervals_mcp_server.utils.custom_fields import is_missing
from intervals_mcp_server.utils.sports import (
    SPORT_FAMILIES,
    cadence_text,
    default_pace_units,
    family_types,
    format_pace,
    hms,
    is_foot_sport,
    is_indoor,
    sport_family,
)
from intervals_mcp_server.utils.streams import find_stream
from intervals_mcp_server.utils.work_sets import INTENSITY_TOL_PTS, interval_secs, matches_pattern, pattern_of, set_summary, split_work
from intervals_mcp_server.utils.params import (
    ActivityId,
    AthleteId,
    EndDate,
    Environment,
    GearId,
    OptionalActivityId,
    OutputFormat,
    SportTypes,
    StartDate,
    lower_choice,
    upper_choice,
)
from intervals_mcp_server.utils.validation import resolve_athlete_id, validate_date

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

DEFAULT_RANGE_DAYS = 90
REFERENCE_LOOKBACK_DAYS = 365  # default search window before a reference activity
MAX_COMPARE_ACTIVITIES = 25
# compare_best_efforts makes one request per activity and duration (25 x 10 = 250 at most).
MAX_COMPARE_DURATIONS = 10
MAX_WORKOUTS = 12
MAX_EFFICIENCY_ACTIVITIES = 60
MAX_SEARCH_RESULTS = 100
MAX_EFFORT_COUNT = 20
MAX_INTENSITY = 300
TARGETS = ("POWER", "HR", "PACE")
ACTIVITY_FIELDS = (
    "id,name,start_date_local,type,gear,icu_ftp,icu_training_load,icu_intensity,moving_time,"
    "icu_rpe,feel,compliance,interval_summary,power_meter,trainer,device_name,power_field_names"
)
MIN_TREND_ACTIVITIES = 3
RECORDING_GAP_S = 5  # a jump in the time stream larger than this is a recording pause
STREAM_UNITS = {
    "watts": "W",
    "secondary_power": "W",
    "heartrate": "bpm",
    "cadence": "rpm",
    "velocity_smooth": "m/s",
    "altitude": "m",
    "torque": "Nm",
}
# metric -> (endpoint, units, default bucket size; None = fixed by Intervals.icu)
HISTOGRAMS: dict[str, tuple[str, str, int | None]] = {
    "power": ("power-histogram", "W", 25),
    "hr": ("hr-histogram", "bpm", 5),
    "pace": ("pace-histogram", "m/s", None),
    "gap": ("gap-histogram", "m/s", None),
}
POWER_METER_NOTE = (
    "Note: values are averages of the recorded stream as computed by Intervals.icu. Different bikes "
    "may carry different power meters that are not calibrated against each other, so compare "
    "across gear with care; 'n/a' means the activity is shorter than the duration or has no such stream."
)
EFFICIENCY_NOTE = (
    "Note: this is a statistical comparison of W per bpm in steady WORK intervals. Heat, fatigue, "
    "hydration, cadence, indoor vs outdoor, interval position in the ride and power meter "
    "differences between bikes all move the ratio; it is not a fitness verdict, and a single "
    "activity with a higher W/bpm does not show an improvement."
)
GEAR_CALL_NOTE = "(plus 1 for the gear catalog unless cached)"


class _Api:  # pylint: disable=too-few-public-methods
    """Issues GET requests for one tool call and counts them."""

    def __init__(self) -> None:
        self.calls = 0

    async def get(
        self, url: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """GET an endpoint (path relative to the API base) and count the call."""
        self.calls += 1
        return await make_intervals_request(url=url, params=params)


# ------------------------------------------------------------------ helpers
def _error(result: Any, what: str) -> str | None:
    """'Error fetching <what>: <message>' when the client returned an error dict, else None."""
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching {what}: {result.get('message', 'Unknown error')}"
    return None


def _split(csv: str | None) -> list[str]:
    return [part.strip() for part in (csv or "").split(",") if part.strip()]


def cap_ids(csv: str | None, cap: int) -> tuple[list[str], list[str], int]:
    """Comma-separated ids de-duplicated (first occurrence kept) and cut to ``cap`` before any request.

    Returns (ids to fetch, ids beyond the cap, number of duplicates ignored).
    """
    unique = list(dict.fromkeys(_split(csv)))
    duplicates = len(_split(csv)) - len(unique)
    return unique[:cap], unique[cap:], duplicates


def ids_note(dropped: list[str], duplicates: int, cap: int) -> str:
    """'; 2 ids beyond the limit of 10 not fetched (i5, i6); 1 duplicate id ignored' or ''."""
    parts = []
    if dropped:
        shown = ", ".join(dropped[:5]) + (", ..." if len(dropped) > 5 else "")
        parts.append(f"{len(dropped)} id{'s' if len(dropped) != 1 else ''} beyond the limit of {cap} not fetched ({shown})")
    if duplicates:
        parts.append(f"{duplicates} duplicate id{'s' if duplicates != 1 else ''} ignored")
    return "".join(f"; {part}" for part in parts)


def _numbers(csv: str | None, name: str) -> list[int] | str:
    """Comma-separated positive numbers as a de-duplicated int list, or an error string."""
    try:
        values = [int(float(part)) for part in _split(csv)]
    except ValueError:
        return f"Error: {name} must be a comma-separated list of numbers."
    if any(value <= 0 for value in values):
        return f"Error: {name} must be positive."
    return list(dict.fromkeys(values))


def _num(value: Any) -> float | None:
    """Float of a numeric value; None for null, NaN, bool or text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or is_missing(value):
        return None
    return float(value)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _fmt(value: float | None, digits: int = 0, units: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}{(' ' + units) if units else ''}"


def _pct_change(first: float | None, last: float | None) -> float | None:
    if first is None or last is None or first == 0:
        return None
    return (last - first) / abs(first) * 100


def _duration_label(secs: int) -> str:
    return f"{secs // 60} min" if secs >= 60 and secs % 60 == 0 else f"{secs} s"


def _resolve_range(start_date: str | None, end_date: str | None, default_days: int) -> tuple[str, str] | str:
    end = end_date or get_default_end_date()
    try:
        validate_date(end)
        if start_date:
            validate_date(start_date)
    except ValueError as exc:
        return f"Error: {exc}"
    start = start_date or (date.fromisoformat(end) - timedelta(days=default_days - 1)).isoformat()
    if start > end:
        return "Error: start_date must not be after end_date."
    return start, end


def _validate_optional_dates(start_date: str | None, end_date: str | None) -> str | None:
    try:
        for value in (start_date, end_date):
            if value:
                validate_date(value)
    except ValueError as exc:
        return f"Error: {exc}"
    if start_date and end_date and start_date > end_date:
        return "Error: start_date must not be after end_date."
    return None


def _day(activity: dict[str, Any]) -> str:
    return str(activity.get("start_date_local") or "")[:10]


def _gear_id(activity: dict[str, Any]) -> str | None:
    gear = activity.get("gear")
    if isinstance(gear, dict) and gear.get("id"):
        return str(gear["id"])
    return str(activity["gear_id"]) if activity.get("gear_id") else None


def _gear_label(activity: dict[str, Any], gear_map: dict[str, str]) -> str:
    gear_id = _gear_id(activity)
    if not gear_id:
        return "no gear"
    name = gear_map.get(gear_id)
    return f"{name} ({gear_id})" if name else gear_id


def _activity_label(activity: dict[str, Any]) -> str:
    return (
        f"{_day(activity)} {activity.get('type', '?')} '{activity.get('name', 'unnamed')}' "
        f"({activity.get('id')})"
    )


def _list_of_dicts(result: Any) -> list[dict[str, Any]]:
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


async def _activities_by_ids(api: _Api, ids: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    activities: list[dict[str, Any]] = []
    for activity_id in ids:
        result = await api.get(f"/activity/{seg(activity_id)}")
        error = _error(result, f"activity {activity_id}")
        if error:
            return [], error
        activity = result[0] if isinstance(result, list) and result else result
        if isinstance(activity, dict) and activity:
            activities.append(activity)
    return activities, None


def _name_matches(activity: dict[str, Any], query: str) -> bool:
    """Same rule as the search endpoint: case-insensitive name search, exact tag for "#tag"."""
    text = query.strip()
    if text.startswith("#"):
        return text[1:].lower() in {str(tag).lower() for tag in activity.get("tags") or []}
    return text.lower() in str(activity.get("name") or "").lower()


def truncation_note(found: list[dict[str, Any]], fetch_limit: int, start_date: str | None, what: str) -> str | None:
    """Note when an endpoint that answers newest first stopped at its limit, so older matches
    (e.g. before start_date) are missing; None when the result is complete."""
    if fetch_limit <= 0 or len(found) < fetch_limit:
        return None
    oldest = min((_day(a) for a in found if _day(a)), default="")
    if start_date and oldest and oldest <= start_date:
        return None
    return (
        f"the {what} returned its maximum of {fetch_limit} results, newest first (oldest {oldest or 'unknown'}); "
        "older matches are not included. Narrow the search (sport, dates via a date range, a more specific "
        "query or a reference activity) to reach them."
    )


async def _collect_activities(  # pylint: disable=too-many-arguments
    api: _Api,
    athlete_id: str,
    *,
    activity_ids: str | None = None,
    query: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    fetch_limit: int = MAX_SEARCH_RESULTS,
) -> tuple[list[dict[str, Any]], str | None, str]:
    """Activities by id, by name search or by date range; returns (activities, error, source text).

    A name search WITH a date range lists the range and matches the names locally: the search
    endpoint answers newest first with a limit, so older matches would be cut off.
    """
    if activity_ids:
        ids = _split(activity_ids)
        activities, error = await _activities_by_ids(api, ids)
        return activities, error, f"{len(ids)} activity id(s)"
    if query and not (start_date or end_date):
        result = await api.get(
            f"/athlete/{seg(athlete_id)}/activities/search-full", {"q": query, "limit": fetch_limit}
        )
        error = _error(result, "activity search")
        return _list_of_dicts(result), error, f"name search '{query}'"
    span = _resolve_range(start_date, end_date, DEFAULT_RANGE_DAYS)
    if isinstance(span, str):
        return [], span, ""
    result = await api.get(
        f"/athlete/{seg(athlete_id)}/activities",
        {"oldest": span[0], "newest": span[1], "fields": ACTIVITY_FIELDS + (",tags" if query else "")},
    )
    activities = _list_of_dicts(result)
    if query:
        activities = [a for a in activities if _name_matches(a, query)]
        return activities, _error(result, "activities"), f"name '{query}' in {span[0]} to {span[1]}"
    return activities, _error(result, "activities"), f"{span[0]} to {span[1]}"


def _select_activities(
    activities: list[dict[str, Any]],
    *,
    sport_types: str | None = None,
    gear_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Client-side filter by sport type (case-insensitive), gear id and local date; newest first."""
    wanted = {sport.lower() for sport in _split(sport_types)}
    selected: list[dict[str, Any]] = []
    for activity in activities:
        if wanted and str(activity.get("type") or "").lower() not in wanted:
            continue
        if gear_id and _gear_id(activity) != str(gear_id):
            continue
        day = _day(activity)
        if (start_date and day < start_date) or (end_date and day > end_date):
            continue
        selected.append(activity)
    selected.sort(key=lambda a: str(a.get("start_date_local") or ""), reverse=True)
    return selected[:limit] if limit else selected


def _filter_text(sport_types: str | None, gear_id: str | None, start_date: str | None, end_date: str | None) -> str:
    parts = []
    if start_date or end_date:
        parts.append(f"dates {start_date or '...'} to {end_date or '...'}")
    if sport_types:
        parts.append(f"sports {sport_types}")
    if gear_id:
        parts.append(f"gear {gear_id}")
    return ", ".join(parts)


async def _reference_activity(api: _Api, activity_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """The reference activity fetched by id: (activity, None), (None, error) or (None, None) when missing."""
    fetched, error = await _activities_by_ids(api, [activity_id])
    return (fetched[0] if fetched else None), error


def _reference_window(reference: dict[str, Any] | None, start_date: str | None) -> tuple[str | None, str | None]:
    """(effective start date, note) of a search anchored on a reference activity.

    Without an explicit start_date the search starts REFERENCE_LOOKBACK_DAYS before the
    reference activity's local date; an explicit start_date (any range) wins.
    """
    if start_date or reference is None:
        return start_date, None
    try:
        reference_day = date.fromisoformat(_day(reference))
    except ValueError:
        return start_date, None
    start = (reference_day - timedelta(days=REFERENCE_LOOKBACK_DAYS)).isoformat()
    return start, (
        f"default window: {REFERENCE_LOOKBACK_DAYS} days before the reference activity ({start} onwards); "
        "pass start_date for another range"
    )


async def _intervals_of(api: _Api, activity_id: Any) -> tuple[list[dict[str, Any]], str | None]:
    """All intervals of an activity (WORK and RECOVERY)."""
    result = await api.get(f"/activity/{seg(activity_id)}/intervals")
    error = _error(result, f"intervals of activity {activity_id}")
    if error:
        return [], error
    items = result.get("icu_intervals") if isinstance(result, dict) else None
    return [i for i in items or [] if isinstance(i, dict)], None


async def _work_intervals(api: _Api, activity_id: Any) -> tuple[list[dict[str, Any]], str | None]:
    result = await api.get(f"/activity/{seg(activity_id)}/intervals")
    error = _error(result, f"intervals of activity {activity_id}")
    if error:
        return [], error
    items = result.get("icu_intervals") if isinstance(result, dict) else None
    return [i for i in items or [] if isinstance(i, dict) and i.get("type") == "WORK"], None


def _activity_json(activity: dict[str, Any], gear_map: dict[str, str] | None = None) -> dict[str, Any]:
    gear_id = _gear_id(activity)
    return {
        "id": activity.get("id"),
        "name": activity.get("name"),
        "date": _day(activity),
        "start_date_local": activity.get("start_date_local"),
        "type": activity.get("type"),
        "gear_id": gear_id,
        "gear_name": (gear_map or {}).get(gear_id or ""),
        "ftp": activity.get("icu_ftp"),
        "training_load": activity.get("icu_training_load"),
        "intensity": activity.get("icu_intensity"),
        "moving_time": activity.get("moving_time"),
    }


# ------------------------------------------------------------- best efforts
def _time_stream(result: Any) -> list[Any]:
    stream = find_stream(_list_of_dicts(result), "time")
    data = (stream or {}).get("data")
    return data if isinstance(data, list) else []


def _time_at(time_data: list[Any], index: Any) -> float | None:
    """Seconds since start at a sample index from the time stream; None when not available."""
    if not time_data or isinstance(index, bool) or not isinstance(index, int):
        return None
    return _num(time_data[min(max(index, 0), len(time_data) - 1)])


def _end_time(time_data: list[Any], end: Any) -> float | None:
    """End of the window [start, end) (``end`` exclusive, as Intervals.icu best efforts).

    The time of the first sample after the window when it follows without a recording
    pause, else one second after the last sample of the window; a pause right after the
    window is therefore not counted as part of it.
    """
    if not time_data or isinstance(end, bool) or not isinstance(end, int) or end <= 0:
        return _time_at(time_data, end)
    last = _num(time_data[min(end, len(time_data)) - 1])
    following = _num(time_data[end]) if end < len(time_data) else None
    if last is None:
        return _time_at(time_data, end)
    if following is not None and 0 < following - last <= RECORDING_GAP_S:
        return following
    return last + 1


def _paused_secs(time_data: list[Any], start: Any, end: Any) -> float:
    """Seconds of recording pauses (time jumps > 5 s) inside the window [start, end)."""
    if not time_data or not isinstance(start, int) or not isinstance(end, int) or isinstance(start, bool):
        return 0.0
    paused = 0.0
    for index in range(max(start, 0), min(end, len(time_data)) - 1):
        current, following = _num(time_data[index]), _num(time_data[index + 1])
        if current is not None and following is not None and following - current > RECORDING_GAP_S:
            paused += following - current - 1
    return paused


def _position(time_data: list[Any], index: Any) -> str:
    secs = _time_at(time_data, index)
    return hms(secs) if secs is not None else f"index {index}"


def _request_label(requested: dict[str, int]) -> str:
    if "distance" in requested:
        return f"{requested['distance']} m"
    return _duration_label(requested["duration"])


def _effort_row(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    effort: dict[str, Any], stream: str, time_data: list[Any], rank: int, requested: dict[str, int], pace_units: str | None = None
) -> dict[str, Any]:
    average = _num(effort.get("average"))
    start, end = effort.get("start_index"), effort.get("end_index")
    end_secs = _end_time(time_data, end)
    return {
        "requested": requested,
        "available": True,
        "rank": rank,
        "average": round(average, 2) if average is not None else None,
        "units": STREAM_UNITS.get(stream),
        "pace": format_pace(average, pace_units) if stream == "velocity_smooth" and average else None,
        "duration": effort.get("duration"),
        "distance": effort.get("distance"),
        "start_index": start,
        "end_index": end,
        "start_secs": _time_at(time_data, start),
        "end_secs": end_secs,
        "start": _position(time_data, start),
        "end": hms(end_secs) if end_secs is not None else f"index {end}",
        "paused_s_in_window": _paused_secs(time_data, start, end),
    }


def _effort_text(row: dict[str, Any]) -> str:
    units = row.get("units") or ""
    value = _fmt(row["average"], 2 if units == "m/s" else 1, units)
    if row.get("pace"):
        value += f" ({row['pace']})"
    text = f"#{row['rank']} {value}"
    if "distance" in row["requested"] and row.get("duration") is not None:
        text += f" in {hms(row['duration'])}"
    text = f"{text} from {row['start']} to {row['end']} (samples {row['start_index']}-{row['end_index']})"
    if row.get("paused_s_in_window"):
        text += f" [window spans {hms(row['paused_s_in_window'])} of recording pause]"
    return text


@tool("read")
async def get_best_efforts(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-return-statements
    activity_id: ActivityId,
    stream: Annotated[str, Field(description="Stream, e.g. watts, heartrate, velocity_smooth (speed), cadence")] = "watts",
    durations: Annotated[str | None, Field(description='Comma-separated seconds; "" for distances only')] = "5,30,60,300,1200,3600",
    distances: Annotated[str | None, Field(description='Comma-separated metres, e.g. "1000,5000" (added to durations)')] = None,
    count: Annotated[int, Field(description="Non-overlapping best efforts per duration or distance, 1-20")] = 1,
    exclude_intervals: Annotated[bool, Field(description="Ignore samples inside detected intervals")] = False,
    start_index: Annotated[int | None, Field(description="First sample index searched")] = None,
    end_index: Annotated[int | None, Field(description="Sample index to stop before")] = None,
    output_format: OutputFormat = "text",
) -> str:
    """Use for the peak efforts of one activity: best average power, HR or speed over durations (s) or distances (m), e.g. peak 5 s to 20 min power or the fastest km of a run (velocity_smooth with distances).

    Returns per effort the average (W, bpm or m/s with pace) and its position as elapsed
    h:mm:ss and sample indices (usable as start_index / end_index); windows spanning a
    recording pause are flagged. Default durations 5 s to 60 min; longer than the activity =
    not available. Read-only; one API call per duration or distance plus 1-2. Several
    activities: compare_best_efforts; period bests: get_athlete_power_curves. Method:
    intervals://methods/comparisons (get_guide).
    """
    duration_list = _numbers(durations, "durations")
    if isinstance(duration_list, str):
        return duration_list
    distance_list = _numbers(distances, "distances")
    if isinstance(distance_list, str):
        return distance_list
    if not duration_list and not distance_list:
        return "Error: at least one duration or distance is required."
    if not 1 <= count <= MAX_EFFORT_COUNT:
        return f"Error: count must be between 1 and {MAX_EFFORT_COUNT}."

    api = _Api()
    time_data = _time_stream(await api.get(f"/activity/{seg(activity_id)}/streams", {"types": "time"}))
    pace_units = None
    if stream == "velocity_smooth":  # pace in the sport's units (per 100 m for swims)
        activity = await api.get(f"/activity/{seg(activity_id)}")
        pace_units = default_pace_units(activity.get("type") if isinstance(activity, dict) else None)
    base: dict[str, Any] = {"stream": stream, "count": count}
    if exclude_intervals:
        base["excludeIntervals"] = "true"
    if start_index is not None:
        base["startIndex"] = start_index
    if end_index is not None:
        base["endIndex"] = end_index
    requests = [("duration", d) for d in duration_list] + [("distance", d) for d in distance_list]
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for key, value in requests:
        result = await api.get(f"/activity/{seg(activity_id)}/best-efforts", {**base, key: value})
        error = _error(result, "best efforts")
        if error:
            errors.append(error)
            rows.append({"requested": {key: value}, "available": False, "reason": error})
            continue
        efforts = result.get("efforts") if isinstance(result, dict) else None
        if not isinstance(efforts, list) or not efforts:
            rows.append({
                "requested": {key: value}, "available": False,
                "reason": "not available (longer than the activity or no data in this stream)",
            })
            continue
        rows.extend(
            _effort_row(effort, stream, time_data, rank, {key: value}, pace_units)
            for rank, effort in enumerate(efforts, 1)
            if isinstance(effort, dict)
        )
    if errors and len(errors) == len(requests):
        return errors[0]

    if output_format.strip().lower() == "json":
        payload = {
            "activity_id": activity_id, "stream": stream, "count": count,
            "exclude_intervals": exclude_intervals, "start_index": start_index, "end_index": end_index,
            "time_stream": bool(time_data), "efforts": rows, "api_calls": api.calls,
        }
        return json.dumps(payload, ensure_ascii=False)
    header = f"Best efforts for activity {activity_id}, stream {stream} (count {count}"
    if exclude_intervals:
        header += ", intervals excluded"
    if start_index is not None or end_index is not None:
        header += f", samples {start_index or 0}-{end_index if end_index is not None else 'end'}"
    lines = [
        header + "):",
        "Positions are elapsed h:mm:ss from the time stream (the clock includes recording pauses; "
        "windows that span a pause are flagged)."
        if time_data else "Time stream not available; positions are sample indices.",
    ]
    for row in rows:
        detail = _effort_text(row) if row["available"] else row["reason"]
        lines.append(f"  {_request_label(row['requested'])}: {detail}")
    lines.append(f"API calls: {api.calls}")
    return "\n".join(lines)


# ------------------------------------------------------ compare best efforts
def _best_average(result: Any) -> float | None:
    efforts = result.get("efforts") if isinstance(result, dict) else None
    if isinstance(efforts, list) and efforts and isinstance(efforts[0], dict):
        return _num(efforts[0].get("average"))
    return None


@tool("read")
async def compare_best_efforts(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-return-statements
    activity_ids: Annotated[str | None, Field(description="Comma-separated activity ids; otherwise the date range is used")] = None,
    start_date: Annotated[StartDate, Field(description="First day YYYY-MM-DD; without activity_ids default 90 days before end_date")] = None,
    end_date: EndDate = None,
    sport_types: SportTypes = None,
    gear_id: GearId = None,
    stream: Annotated[str, Field(description="Stream, e.g. watts, heartrate, velocity_smooth")] = "watts",
    durations: Annotated[str, Field(description='Comma-separated seconds, at most 10, e.g. "60,300,1200"')] = "60,300,1200",
    limit: Annotated[int, Field(description="Maximum activities, newest first; capped to 1-25")] = 10,
    athlete_id: AthleteId = None,
    output_format: OutputFormat = "text",
) -> str:
    """Use to compare peak efforts (e.g. 1 / 5 / 20 min power) of several activities side by side: development over a block, races, or whether a new bike or power meter reads differently.

    Returns a matrix of activities (date, sport, gear, FTP at the time) by duration with the
    best average of the stream, and the best activity per duration. Activities come from
    activity_ids or a date range (default the last 90 days), filtered by sport and gear; newest
    first, limit 10 (max 25), at most 10 durations. Power meters of different bikes are not
    calibrated against each other; no verdict. Read-only; one API call per activity and
    duration (up to 250) plus the list and gear calls. One activity in detail: get_best_efforts.
    Method: intervals://methods/comparisons (get_guide).
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    duration_list = _numbers(durations, "durations")
    if isinstance(duration_list, str):
        return duration_list
    if not duration_list:
        return "Error: at least one duration is required."
    if len(duration_list) > MAX_COMPARE_DURATIONS:
        return (
            f"Error: {len(duration_list)} durations; at most {MAX_COMPARE_DURATIONS} per call "
            "(each activity needs one API request per duration)."
        )
    date_error = _validate_optional_dates(start_date, end_date)
    if date_error:
        return date_error
    capped = min(max(limit, 1), MAX_COMPARE_ACTIVITIES)
    # Explicit ids: duplicates dropped and cut to the limit BEFORE any request.
    ids, dropped, duplicates = cap_ids(activity_ids, capped) if activity_ids else ([], [], 0)

    api = _Api()
    activities, error, source = await _collect_activities(
        api, athlete_id_to_use, activity_ids=",".join(ids) or None, start_date=start_date, end_date=end_date
    )
    if error:
        return error
    source += ids_note(dropped, duplicates, capped)
    selected = _select_activities(
        activities, sport_types=sport_types, gear_id=gear_id, start_date=start_date, end_date=end_date, limit=capped
    )
    filters = _filter_text(sport_types, gear_id, start_date, end_date)
    if not selected:
        return f"No activities found ({source}{'; ' + filters if filters else ''})."
    gear_map = await get_gear_map(athlete_id=athlete_id_to_use)

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for activity in selected:
        efforts: list[dict[str, Any]] = []
        for duration in duration_list:
            result = await api.get(
                f"/activity/{seg(activity.get('id'))}/best-efforts",
                {"stream": stream, "duration": duration, "count": 1},
            )
            error = _error(result, f"best efforts of activity {activity.get('id')} ({_duration_label(duration)})")
            if error:
                errors.append(error)
            efforts.append({"duration": duration, "average": _best_average(result), "error": error})
        rows.append({**_activity_json(activity, gear_map), "efforts": efforts})
    if errors and len(errors) == len(selected) * len(duration_list):
        return errors[0]

    units = STREAM_UNITS.get(stream, "")
    best: list[dict[str, Any]] = []
    for position, duration in enumerate(duration_list):
        candidates = [(row["efforts"][position]["average"], row) for row in rows if row["efforts"][position]["average"] is not None]
        # A failed request is not "no such effort": the best of the rest may not be the real best.
        incomplete = any(row["efforts"][position]["error"] for row in rows)
        if candidates:
            value, row = max(candidates, key=lambda item: item[0])
            best.append({"duration": duration, "average": value, "activity_id": row["id"], "name": row["name"], "date": row["date"], "incomplete": incomplete})
        else:
            best.append({"duration": duration, "average": None, "activity_id": None, "name": None, "date": None, "incomplete": incomplete})

    if output_format.strip().lower() == "json":
        payload = {
            "athlete_id": athlete_id_to_use, "stream": stream, "units": units, "durations": duration_list,
            "source": source, "limit": capped, "activities": rows, "best": best, "errors": errors, "api_calls": api.calls,
            "note": POWER_METER_NOTE,
        }
        return json.dumps(payload, ensure_ascii=False)
    labels = [_duration_label(d) for d in duration_list]
    lines = [
        f"Best efforts comparison for athlete {athlete_id_to_use}, stream {stream}: {len(rows)} activities, "
        f"newest first (source {source}{'; ' + filters if filters else ''}; limit {capped}"
        + (f", capped from {limit}" if limit > capped else "") + "):",
        "Date | Sport | Gear | FTP | " + " | ".join(labels) + " | Activity",
    ]
    for row in rows:
        cells = ["error" if effort["error"] else _fmt(effort["average"], 0, units) for effort in row["efforts"]]
        gear = f"{row['gear_name']} ({row['gear_id']})" if row["gear_name"] else (row["gear_id"] or "no gear")
        lines.append(
            f"{row['date']} | {row['type']} | {gear} | {_fmt(_num(row['ftp']), 0, 'W')} | "
            + " | ".join(cells) + f" | '{row['name']}' ({row['id']})"
        )
    lines.append(
        "Best per duration: "
        + "; ".join(
            f"{label} {_fmt(item['average'], 0, units)}"
            + (f" ({item['date']} '{item['name']}', {item['activity_id']})" if item["activity_id"] else "")
            + (" (INCOMPLETE: not every activity could be loaded)" if item["incomplete"] else "")
            for label, item in zip(labels, best, strict=True)
        )
    )
    if errors:
        lines.append(
            f"Errors ({len(errors)} of {len(selected) * len(duration_list)} requests failed; 'error' cells are not "
            "missing efforts): " + "; ".join(errors[:5]) + (" ..." if len(errors) > 5 else "")
        )
    lines.append(POWER_METER_NOTE)
    lines.append(f"API calls: {api.calls} {GEAR_CALL_NOTE}")
    return "\n".join(lines)


# ------------------------------------------------------- interval search
def _interval_search_params(  # pylint: disable=too-many-arguments
    min_secs: int, max_secs: int, min_intensity: float, max_intensity: float,
    target: str | None, min_reps: int | None, max_reps: int | None, limit: int,
) -> dict[str, Any] | str:
    """Validated query parameters of the interval-search endpoint, or an error string."""
    if min_secs <= 0 or min_secs > max_secs:
        return "Error: min_secs must be greater than 0 and not greater than max_secs."
    if not 0 <= min_intensity <= max_intensity <= MAX_INTENSITY:
        return f"Error: intensities must satisfy 0 <= min_intensity <= max_intensity <= {MAX_INTENSITY} (% of FTP)."
    # The endpoint only accepts whole numbers ("90.0" is rejected with 422), and MCP clients
    # send 90 as 90.0 for a float parameter. Widen fractional bounds to whole percent.
    params: dict[str, Any] = {
        "minSecs": int(min_secs), "maxSecs": int(max_secs),
        "minIntensity": math.floor(min_intensity), "maxIntensity": math.ceil(max_intensity),
        "limit": int(limit),
    }
    if target:
        target_value = target.strip().upper()
        if target_value not in TARGETS:
            return f"Error: target must be one of {', '.join(TARGETS)} (the workout target, not the sport)."
        params["type"] = target_value
    reps = _reps_params(min_reps, max_reps)
    if isinstance(reps, str):
        return reps
    params.update(reps)
    if limit < 1:
        return "Error: limit must be at least 1."
    return params


def _reps_params(min_reps: int | None, max_reps: int | None) -> dict[str, int] | str:
    """minReps / maxReps query parameters, or an error string."""
    reps: dict[str, int] = {}
    for name, value in (("minReps", min_reps), ("maxReps", max_reps)):
        if value is not None:
            if value < 1:
                return "Error: min_reps and max_reps must be at least 1."
            reps[name] = value
    if min_reps is not None and max_reps is not None and min_reps > max_reps:
        return "Error: min_reps must not be greater than max_reps."
    return reps


SUMMARY_PATTERN = re.compile(r"^\s*(\d+)x\s+((?:\d+h)?(?:\d+m)?(?:\d+s)?)\s+(\d+(?:\.\d+)?)\s*([a-z/%]*)\s*$", re.IGNORECASE)


def _parse_duration(text: str) -> int:
    total = 0
    for value, unit in re.findall(r"(\d+)([hms])", text):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def parse_interval_summary(summary: Any) -> list[dict[str, Any]]:
    """Intervals.icu interval_summary entries ("3x 10m 250w") as count / secs / value / units."""
    groups = []
    for item in summary if isinstance(summary, list) else []:
        match = SUMMARY_PATTERN.match(str(item))
        if not match:
            continue
        groups.append({"count": int(match.group(1)), "secs": _parse_duration(match.group(2)),
                       "value": float(match.group(3)), "units": match.group(4).lower()})
    return groups


def _best_group(
    activity: dict[str, Any], params: dict[str, Any], reference: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The interval_summary group that best matches the search window (or the reference pattern)."""
    ftp = _num(activity.get("icu_ftp"))
    best_score = -1.0
    best: dict[str, Any] | None = None
    centre_secs = reference["secs"] if reference else (params["minSecs"] + params["maxSecs"]) / 2
    centre_pct = reference.get("pct_ftp") if reference else (params["minIntensity"] + params["maxIntensity"]) / 2
    for group in parse_interval_summary(activity.get("interval_summary")):
        if not params["minSecs"] * 0.9 <= group["secs"] <= params["maxSecs"] * 1.1:
            continue
        pct = group["value"] / ftp * 100 if group["units"] == "w" and ftp else None
        closeness = 1 - min(1.0, abs(group["secs"] - centre_secs) / max(centre_secs, 1))
        if pct is not None and centre_pct:
            closeness += 1 - min(1.0, abs(pct - centre_pct) / 15)
        if closeness > best_score:
            best_score, best = closeness, {**group, "pct_ftp": round(pct, 1) if pct is not None else None}
    return best


def _comparability(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    activity: dict[str, Any], params: dict[str, Any], reference: dict[str, Any] | None, anchor: dict[str, Any] | None,
    ftp_tolerance_pct: float, gear_map: dict[str, str],
) -> dict[str, Any]:
    """Score 0-100 of how comparable an activity is, with the reasons (pattern, sport, gear, FTP context)."""
    group = _best_group(activity, params, reference)
    parts: list[tuple[float, float]] = []  # (score, weight)
    notes: list[str] = []
    if group:
        centre = reference["secs"] if reference else (params["minSecs"] + params["maxSecs"]) / 2
        parts.append((1 - min(1.0, abs(group["secs"] - centre) / max(centre, 1)), 2.0))
        if reference and reference.get("count"):
            parts.append((1 - min(1.0, abs(group["count"] - reference["count"]) / reference["count"]), 1.0))
        if reference and reference.get("pct_ftp") is not None and group.get("pct_ftp") is not None:
            parts.append((1 - min(1.0, abs(group["pct_ftp"] - reference["pct_ftp"]) / 15), 2.0))
        notes.append(f"best match {group['count']}x {hms(group['secs'])} {group['value']:g}{group['units']}"
                     + (f" ({group['pct_ftp']:.0f}% FTP)" if group.get("pct_ftp") is not None else ""))
    else:
        parts.append((0.3, 2.0))
        notes.append("no interval group of the searched length in the summary")
    if anchor:
        same_type = str(activity.get("type")) == str(anchor.get("type"))
        parts.append((1.0 if same_type else 0.8, 1.0))
        if not same_type:
            notes.append(f"{activity.get('type')} vs {anchor.get('type')}")
        gear, anchor_gear = _gear_id(activity), _gear_id(anchor)
        if gear and anchor_gear:
            parts.append((1.0 if gear == anchor_gear else 0.6, 1.0))
            notes.append("same gear" if gear == anchor_gear else f"other gear {_gear_label(activity, gear_map)}: watts from another power meter, compare % FTP only")
        ftp, anchor_ftp = _num(activity.get("icu_ftp")), _num(anchor.get("icu_ftp"))
        if ftp and anchor_ftp:
            diff = (ftp - anchor_ftp) / anchor_ftp * 100
            parts.append((1.0 if abs(diff) <= ftp_tolerance_pct else 0.6, 1.0))
            if abs(diff) > ftp_tolerance_pct:
                notes.append(f"other FTP context {ftp:.0f} vs {anchor_ftp:.0f} W ({diff:+.0f}%)")
    score = sum(s * w for s, w in parts) / sum(w for _, w in parts)
    if anchor is not None and str(activity.get("id")) == str(anchor.get("id")):
        notes.insert(0, "reference" if reference else "newest result (context)")
    return {"score": round(score * 100), "notes": notes, "best_group": group}


def _interval_search_block(activity: dict[str, Any], gear_map: dict[str, str], comparability: dict[str, Any] | None = None) -> str:
    summary = activity.get("interval_summary")
    summary_text = "; ".join(str(s) for s in summary) if isinstance(summary, list) and summary else "n/a"
    details = [
        f"intervals: {summary_text}",
        f"moving {hms(activity.get('moving_time'))}",
        f"load {_fmt(_num(activity.get('icu_training_load')))}",
        f"intensity {_fmt(_num(activity.get('icu_intensity')), 0, '%')}",
        f"FTP {_fmt(_num(activity.get('icu_ftp')), 0, 'W')}",
        f"gear {_gear_label(activity, gear_map)}",
        f"power meter {activity.get('power_meter') or 'unknown'}",
        f"compliance {_fmt(_num(activity.get('compliance')), 0, '%')}",
    ]
    text = f"{_activity_label(activity)}\n  " + ", ".join(details)
    if comparability:
        text += f"\n  comparability {comparability['score']}/100: " + "; ".join(comparability["notes"])
    return text


async def _reference_pattern(api: _Api, activity_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """(activity, main-set pattern, error) of a reference activity."""
    fetched, error = await _activities_by_ids(api, [activity_id])
    if error or not fetched:
        return None, None, error or f"Reference activity {activity_id} not found."
    activity = fetched[0]
    intervals, error = await _intervals_of(api, activity_id)
    if error:
        return activity, None, error
    ftp = _num(activity.get("icu_ftp"))
    main = set_summary(split_work(intervals, ftp)["main"], ftp)
    if not main.get("count"):
        return activity, None, f"Reference activity {activity_id} has no WORK intervals to derive a pattern from."
    return activity, pattern_of(main), None


def _family_filter(
    found: list[dict[str, Any]], sport_types: str | None, reference: dict[str, Any] | None
) -> tuple[str | None, str, dict[str, int]]:
    """(sport filter, description, other families found) for the default sport family."""
    if sport_types and sport_types.strip().lower() == "all":
        return None, "all sports (explicit)", {}
    if sport_types:
        return sport_types, f"sports {sport_types}", {}
    families: dict[str, int] = {}
    for activity in found:
        family = sport_family(activity.get("type"))
        families[family] = families.get(family, 0) + 1
    if reference is not None:
        chosen = sport_family(reference.get("type"))
        source = "the reference activity"
    elif families:
        chosen = max(families, key=lambda f: families[f])
        source = "most results"
    else:
        return None, "all sports", {}
    others = {f: n for f, n in families.items() if f != chosen}
    types = ",".join(SPORT_FAMILIES.get(chosen, (chosen,)))
    return types, f"sport family {chosen} (default from {source})", others


@tool("read")
async def find_similar_intervals(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements,too-many-branches,too-many-statements
    min_secs: Annotated[int | None, Field(description="Shortest interval (s); required without reference_activity_id")] = None,
    max_secs: Annotated[int | None, Field(description="Longest interval (s); required without reference_activity_id")] = None,
    min_intensity: Annotated[float | None, Field(description="Lowest % of FTP, 0-300 (rounded down)")] = None,
    max_intensity: Annotated[float | None, Field(description="Highest % of FTP, 0-300 (rounded up)")] = None,
    target: Annotated[
        Literal["POWER", "HR", "PACE"] | None,
        BeforeValidator(upper_choice),
        Field(description="Workout target type (not the sport); omit for any"),
    ] = None,
    min_reps: Annotated[int | None, Field(description="Minimum matching repetitions per activity")] = None,
    max_reps: Annotated[int | None, Field(description="Maximum matching repetitions per activity")] = None,
    limit: Annotated[int, Field(description="Maximum activities returned")] = 20,
    start_date: Annotated[
        str | None, Field(description="First day YYYY-MM-DD; default 365 days before the reference, else no limit")
    ] = None,
    end_date: Annotated[str | None, Field(description="Last day YYYY-MM-DD; default no limit")] = None,
    sport_types: Annotated[
        str | None,
        Field(description='Comma-separated activity types or "all"; default the sport family of the reference or of most results'),
    ] = None,
    gear_id: GearId = None,
    athlete_id: AthleteId = None,
    output_format: OutputFormat = "text",
    reference_activity_id: Annotated[
        OptionalActivityId, Field(description="Activity whose main work set defines the search window and the ranking")
    ] = None,
    ftp_range: Annotated[str | None, Field(description='Only activities with FTP at the time in "low-high" W, e.g. "225-240"')] = None,
    ftp_tolerance_pct: Annotated[
        float, Field(description="FTP difference in % to the reference or newest result flagged as other FTP context")
    ] = 5.0,
    sort_by: Annotated[
        Literal["comparability", "date"] | None,
        BeforeValidator(lower_choice),
        Field(description="Default comparability with a reference, else date (newest first)"),
    ] = None,
) -> str:
    """Use to find earlier activities with intervals of a given length and intensity (% of FTP), or like the main set of a reference activity, ranked by comparability.

    Pass min/max_secs and min/max_intensity, or reference_activity_id (window from its main
    set: length ±25 %, intensity ±8 points of FTP; explicit values win; only the 365 days
    before it unless start_date is given). Returns per activity date, sport, interval summary,
    load, FTP at the time, gear, power meter, compliance and a comparability score 0-100 with
    reasons. Sport defaults to one family (other families are counted, "all" keeps every
    sport); watts of different power meters are not comparable, use % of FTP. Read-only; one
    API call (+2 with a reference, +1 gear catalog). Details: get_activity_intervals; same
    workout over time: compare_workouts. Method: intervals://methods/comparisons (get_guide).
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    date_error = _validate_optional_dates(start_date, end_date)
    if date_error:
        return date_error
    ftp_bounds = _parse_range(ftp_range, "ftp_range")
    if isinstance(ftp_bounds, str):
        return ftp_bounds
    order = (sort_by or ("comparability" if reference_activity_id else "date")).strip().lower()
    if order not in ("comparability", "date"):
        return "Error: sort_by must be 'comparability' or 'date'."
    api = _Api()
    reference: dict[str, Any] | None = None
    pattern: dict[str, Any] | None = None
    if reference_activity_id:
        reference, pattern, error = await _reference_pattern(api, reference_activity_id)
        if error or pattern is None:
            return error or f"Reference activity {reference_activity_id} has no pattern."
        min_secs = min_secs if min_secs is not None else max(1, round(pattern["secs"] * 0.75))
        max_secs = max_secs if max_secs is not None else round(pattern["secs"] * 1.25)
        if pattern.get("pct_ftp") is not None:
            min_intensity = min_intensity if min_intensity is not None else max(0.0, pattern["pct_ftp"] - INTENSITY_TOL_PTS)
            max_intensity = max_intensity if max_intensity is not None else pattern["pct_ftp"] + INTENSITY_TOL_PTS
        elif min_intensity is None or max_intensity is None:
            return (f"Error: reference activity {reference_activity_id} has no power-based work intensity; "
                    "pass min_intensity and max_intensity.")
    if min_secs is None or max_secs is None or min_intensity is None or max_intensity is None:
        return "Error: pass min_secs, max_secs, min_intensity and max_intensity, or a reference_activity_id."
    params = _interval_search_params(
        min_secs, max_secs, min_intensity, max_intensity, target, min_reps, max_reps, max(min(limit * 5, MAX_SEARCH_RESULTS), limit)
    )
    if isinstance(params, str):
        return params
    result = await api.get(f"/athlete/{seg(athlete_id_to_use)}/activities/interval-search", params)
    error = _error(result, "interval search")
    if error:
        return error
    found = _list_of_dicts(result)
    sport_filter, sport_text, other_families = _family_filter(found, sport_types, reference)
    window_start, window_note = _reference_window(reference, start_date)
    truncated = truncation_note(found, int(params["limit"]), window_start, "interval search")
    selected = _select_activities(found, sport_types=sport_filter, gear_id=gear_id, start_date=window_start, end_date=end_date)
    older = (
        len(_select_activities(found, sport_types=sport_filter, gear_id=gear_id, end_date=end_date)) - len(selected)
        if window_note else 0
    )
    if ftp_bounds:
        selected = [a for a in selected if (f := _num(a.get("icu_ftp"))) is not None and ftp_bounds[0] <= f <= ftp_bounds[1]]
    gear_map = await get_gear_map(athlete_id=athlete_id_to_use) if selected else {}
    anchor = reference or (selected[0] if selected else None)
    scored = [(a, _comparability(a, params, pattern, anchor, ftp_tolerance_pct, gear_map)) for a in selected]
    if order == "comparability":
        scored.sort(key=lambda item: (item[1]["score"], str(item[0].get("start_date_local") or "")), reverse=True)
    scored = scored[:limit]
    filters = ", ".join(p for p in (_filter_text(None, gear_id, window_start, end_date), sport_text, f"FTP {ftp_range}" if ftp_range else "") if p)
    window = {
        "start": window_start, "end": end_date,
        "default_lookback_days": REFERENCE_LOOKBACK_DAYS if window_note else None,
        "older_matches_outside_window": older,
    }

    criteria = (
        f"{params['minSecs']}-{params['maxSecs']} s at {params['minIntensity']}-{params['maxIntensity']}% of FTP"
        + (f", target {params['type']}" if "type" in params else "")
        + (f", reps {min_reps if min_reps is not None else 'any'}-{max_reps if max_reps is not None else 'any'}"
           if min_reps is not None or max_reps is not None else "")
    )
    if output_format.strip().lower() == "json":
        payload = {
            "athlete_id": athlete_id_to_use, "criteria": criteria, "api_params": params,
            "returned_by_api": len(found), "api_result_truncated": truncated,
            "filters": filters or None, "other_sport_families": other_families,
            "reference": {"activity_id": reference_activity_id, "pattern": pattern} if reference else None,
            "window": window,
            "sort_by": order,
            "activities": [
                {
                    **_activity_json(activity, gear_map),
                    "rpe": activity.get("icu_rpe"), "feel": activity.get("feel"), "power_meter": activity.get("power_meter"),
                    "compliance": activity.get("compliance"), "interval_summary": activity.get("interval_summary"),
                    "comparability": comp,
                }
                for activity, comp in scored
            ],
            "api_calls": api.calls,
        }
        return json.dumps(payload, ensure_ascii=False)
    lines = [f"Interval search for athlete {athlete_id_to_use}: {criteria}, limit {limit}"]
    if pattern:
        lines.append(
            f"Reference {reference_activity_id}: {pattern['count']} x {hms(pattern['secs'])}"
            + (f" @ {pattern['pct_ftp']:.0f}% FTP" if pattern.get("pct_ftp") is not None else "")
            + " (search window derived from its main work set unless given explicitly)"
        )
    lines.append(
        f"Date window: {window_start or 'any'} to {end_date or 'latest'}"
        + (f" ({window_note})" if window_note else "")
        + (f"; {older} older match(es) before {window_start} not shown" if older else "")
    )
    lines.append(
        f"API returned {len(found)} activities; filters ({filters}) keep {len(selected)}; "
        + ("ranked by comparability." if order == "comparability" else "newest first.")
    )
    if truncated:
        lines.append(f"Note: {truncated}")
    if other_families:
        lines.append(
            "Also found in other sports (not shown, % of FTP is sport-specific; sport_types='all' to include): "
            + ", ".join(f"{family} {count}" for family, count in sorted(other_families.items()))
        )
    lines.extend(_interval_search_block(activity, gear_map, comp) for activity, comp in scored)
    if not scored:
        lines.append("No matching activities.")
    lines.append("Absolute watts of different bikes / power meters are not comparable; compare % of FTP across gear.")
    lines.append("Call get_activity_intervals(activity_id) for the per-interval details (power, HR, cadence, timing).")
    lines.append(f"API calls: {api.calls} {GEAR_CALL_NOTE}")
    return "\n".join(lines)


# ------------------------------------------------------------ histograms
def _histogram_rows(result: Any, speed: bool) -> list[dict[str, Any]]:
    buckets: list[dict[str, Any]] = []
    for item in _list_of_dicts(result):
        low, high, secs = _num(item.get("min")), _num(item.get("max")), _num(item.get("secs"))
        if low is None or high is None or secs is None:
            continue
        buckets.append({"min": low, "max": high, "secs": secs})
    buckets.sort(key=lambda b: b["min"])
    total = sum(b["secs"] for b in buckets)
    for bucket in buckets:
        bucket["percent"] = round(bucket["secs"] / total * 100, 1) if total else 0.0
        if speed:
            bucket["pace_slowest"] = format_pace(bucket["min"])
            bucket["pace_fastest"] = format_pace(bucket["max"])
    return buckets


@tool("read")
async def get_activity_histogram(  # pylint: disable=too-many-locals
    activity_id: ActivityId,
    metric: Annotated[
        Literal["power", "hr", "pace", "gap"],
        BeforeValidator(lower_choice),
        Field(description="power (W), hr (bpm), pace or gap (grade-adjusted pace); pace and gap buckets are m/s"),
    ] = "power",
    bucket_size: Annotated[
        int | None, Field(description="Bucket width in W (power, default 25) or bpm (hr, default 5); not for pace or gap")
    ] = None,
    output_format: OutputFormat = "text",
) -> str:
    """Use for the time distribution of one activity independent of zone definitions: how polarised a ride was, time above FTP, the pace spread of a run.

    Returns the seconds and % of recorded time in each power (W), heart rate (bpm), pace or
    grade-adjusted pace bucket, as computed by Intervals.icu; pace and GAP buckets are speeds
    in m/s shown with the pace range. bucket_size applies to power and hr only. Read-only,
    one API call. Method: intervals://methods/comparisons (get_guide).
    """
    key = metric.strip().lower()
    if key not in HISTOGRAMS:
        return f"Error: metric must be one of {', '.join(HISTOGRAMS)}."
    endpoint, units, default_bucket = HISTOGRAMS[key]
    if bucket_size is not None:
        if default_bucket is None:
            return "Error: bucket_size only applies to the power and hr histograms."
        if bucket_size <= 0:
            return "Error: bucket_size must be positive."
    width = bucket_size or default_bucket
    api = _Api()
    result = await api.get(f"/activity/{seg(activity_id)}/{endpoint}", {"bucketSize": width} if width else None)
    error = _error(result, f"{key} histogram")
    if error:
        return error
    speed = default_bucket is None
    rows = _histogram_rows(result, speed)
    total = sum(row["secs"] for row in rows)
    if output_format.strip().lower() == "json":
        payload = {
            "activity_id": activity_id, "metric": key, "units": units, "bucket_size": width,
            "total_secs": total, "buckets": rows, "api_calls": api.calls,
        }
        return json.dumps(payload, ensure_ascii=False)
    title = f"{key.upper() if key in ('hr', 'gap') else key.capitalize()} histogram for activity {activity_id}"
    lines = [f"{title} ({'bucket ' + str(width) + ' ' + units + ', ' if width else ''}total {hms(total)} in {len(rows)} buckets):"]
    if speed:
        lines.append("Buckets are speeds in m/s; the pace range is shown slowest to fastest.")
    for row in rows:
        if row["secs"] <= 0:
            continue
        if speed:
            span = f"{row['min']:.2f}-{row['max']:.2f} m/s ({row['pace_slowest']} to {row['pace_fastest']})"
        else:
            span = f"{row['min']:g}-{row['max']:g} {units}"
        lines.append(f"  {span}: {hms(row['secs'])} ({row['percent']:.1f} %)")
    if not rows:
        lines.append("  no histogram data")
    lines.append(f"API calls: {api.calls}")
    return "\n".join(lines)


# ------------------------------------------------------- workout comparison
def _power_source(activity: dict[str, Any], gear_map: dict[str, str]) -> str:
    """Which power meter the watts come from, as far as the activity tells (never assumed)."""
    meter = activity.get("power_meter")
    gear = _gear_label(activity, gear_map)
    parts = [f"power meter {meter}" if meter else "power meter unknown", f"gear {gear}"]
    if is_indoor(activity):
        parts.append("indoor/trainer")
    return ", ".join(parts)


def _parse_range(text: str | None, name: str) -> tuple[float, float] | str | None:
    """'220-240' -> (220.0, 240.0); None when not given; an error string when malformed."""
    if not text:
        return None
    try:
        low, high = (float(part) for part in text.split("-"))
    except ValueError:
        return f"Error: {name} must look like '220-240'."
    if low > high:
        return f"Error: {name} must have low <= high."
    return low, high


def _workout_row(  # pylint: disable=too-many-arguments
    activity: dict[str, Any], intervals: list[dict[str, Any]], gear_map: dict[str, str], filters: dict[str, Any]
) -> dict[str, Any]:
    """Main work set (time-weighted), surges and excluded intervals of one activity."""
    ftp = _num(activity.get("icu_ftp"))
    split = split_work(intervals, ftp, **filters)
    main = set_summary(split["main"], ftp)
    return {
        **_activity_json(activity, gear_map),
        "power_source": _power_source(activity, gear_map),
        "power_meter": activity.get("power_meter"),
        "indoor": is_indoor(activity),
        "main_set": main,
        "other_intervals": set_summary(split["other"], ftp) if split["other"] else None,
        "surges": [{"secs": interval_secs(i), "avg_watts": _num(i.get("average_watts")), "max_watts": _num(i.get("max_watts")),
                    "avg_hr": _num(i.get("average_heartrate")), "start_time": i.get("start_time")} for i in split["surges"]],
        "excluded": [{"secs": interval_secs(e["interval"]), "avg_watts": _num(e["interval"].get("average_watts")), "reason": e["reason"]}
                     for e in split["excluded"]],
        "rpe_whole_activity": _num(activity.get("icu_rpe")),
        "feel_whole_activity": _num(activity.get("feel")),
    }


def _trend(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    """First -> last change of a main-set value over comparable rows (oldest first)."""
    series = [(row["date"], value) for row in rows if (value := row["main_set"].get(key) if key in row["main_set"] else row.get(key)) is not None]
    if len(series) < 2:
        return None
    first, last = series[0][1], series[-1][1]
    values = [v for _, v in series]
    return {
        "key": key, "n": len(series), "first": first, "last": last, "first_date": series[0][0], "last_date": series[-1][0],
        "diff": round(last - first, 3), "pct": _round_pct(_pct_change(first, last)),
        "min": min(values), "max": max(values), "reliable": len(series) >= MIN_TREND_ACTIVITIES,
    }


def _round_pct(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


TREND_LABELS = (
    ("avg_watts", "power (time-weighted avg)", "W", 0),
    ("avg_watts_same_gear", "power on the reference gear only", "W", 0),
    ("w_per_bpm_same_gear", "W/bpm on the reference gear only", "", 2),
    ("avg_hr", "HR (time-weighted avg)", "bpm", 0),
    ("max_hr", "max HR", "bpm", 0),
    ("cadence", "cadence", "rpm", 0),
    ("w_per_bpm", "W/bpm", "", 2),
    ("rpe_whole_activity", "RPE (whole activity, not per interval)", "", 0),
)


def _set_text(main: dict[str, Any]) -> str:
    if not main.get("count"):
        return "no comparable work intervals"
    pct = f" @ {main['pct_ftp']:.0f}% FTP" if main.get("pct_ftp") is not None else ""
    return f"{main['count']} x {hms(main['secs_median'])}{pct}"


def _row_text(row: dict[str, Any], include_rpe: bool) -> list[str]:
    main = row["main_set"]
    cells = [
        f"{row['date']} {row['type']} '{row['name']}' ({row['id']})",
        _set_text(main),
        f"{_fmt(main.get('avg_watts'), 0, 'W')} (NP {_fmt(main.get('np_watts'), 0, 'W')})",
        f"HR {_fmt(main.get('avg_hr'), 0)}/max {_fmt(main.get('max_hr'), 0)} bpm",
        f"cad {cadence_text(main.get('cadence'), row.get('type'))}",
        f"{_fmt(main.get('w_per_bpm'), 2)} W/bpm",
        f"FTP {_fmt(_num(row['ftp']), 0, 'W')}",
        row["power_source"],
    ]
    if include_rpe:
        cells.append(f"RPE {_fmt(row['rpe_whole_activity'], 0)} (whole activity)")
    lines = [" | ".join(cells)]
    if main.get("intervals"):
        lines.append("    intervals: " + ", ".join(
            f"{hms(i['secs'])} {_fmt(i['avg_watts'], 0, 'W')} HR {_fmt(i['avg_hr'], 0)}/{_fmt(i['max_hr'], 0)}" for i in main["intervals"]
        ))
    if row["surges"]:
        lines.append("    not averaged (short surges/sprints): " + ", ".join(
            f"{hms(s['secs'])} @ {_fmt(s['avg_watts'], 0, 'W')}" for s in row["surges"][:6]
        ) + (" ..." if len(row["surges"]) > 6 else ""))
    other = row.get("other_intervals")
    if other and other.get("count"):
        lines.append(f"    not averaged (other WORK intervals): {other['count']} x ~{hms(other['secs_median'])} at {_fmt(other.get('avg_watts'), 0, 'W')}")
    if row["excluded"]:
        lines.append("    excluded: " + ", ".join(f"{hms(e['secs'])} @ {_fmt(e['avg_watts'], 0, 'W')} ({e['reason']})" for e in row["excluded"][:4]))
    return lines


def _comparability_notes(rows: list[dict[str, Any]]) -> list[str]:
    notes = []
    gears = sorted({str(row["gear_id"]) for row in rows if row.get("gear_id")})
    meters = sorted({str(row["power_meter"]) for row in rows if row.get("power_meter")})
    if len(gears) > 1 or len(meters) > 1:
        notes.append(
            f"Different gear ({', '.join(gears) or 'n/a'}) / power meters ({', '.join(meters) or 'unknown'}): watts come from sensors "
            "that are not calibrated against each other; compare power within one gear, use HR and RPE across gear."
        )
    if any(row.get("indoor") for row in rows) and not all(row.get("indoor") for row in rows):
        notes.append("Indoor and outdoor sessions are mixed (trainer power and outdoor power meter may differ).")
    if any(not row.get("power_meter") for row in rows):
        notes.append("Power meter identity unknown for some activities (no power meter name in the file).")
    ftps = sorted({row["ftp"] for row in rows if row.get("ftp")})
    if len(ftps) > 1:
        notes.append(f"FTP changed over the period ({', '.join(f'{f:.0f}' for f in ftps)} W): % of FTP values use the FTP at the time.")
    return notes


@tool("read")
async def compare_workouts(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements,too-many-branches,too-many-statements
    query: Annotated[str | None, Field(description='Text in activity names, or "#tag" for an exact tag')] = None,
    activity_ids: Annotated[str | None, Field(description="Comma-separated activity ids (query is then ignored)")] = None,
    start_date: Annotated[
        str | None,
        Field(description="First day YYYY-MM-DD; default 90 days before end_date (name search: none, or 365 days before the reference)"),
    ] = None,
    end_date: EndDate = None,
    sport_types: Annotated[
        str | None,
        Field(description='Comma-separated activity types or "all"; default the sport family of the reference or newest'),
    ] = None,
    limit: Annotated[int, Field(description="Maximum activities, newest first; capped to 1-12")] = 8,
    athlete_id: AthleteId = None,
    output_format: OutputFormat = "text",
    reference_activity_id: Annotated[
        OptionalActivityId, Field(description="Activity whose main set defines the pattern; default the newest")
    ] = None,
    gear_id: GearId = None,
    min_interval_secs: Annotated[
        int | None, Field(description="Only WORK intervals at least this long (s); shorter ones under 2 min are surges")
    ] = None,
    max_interval_secs: Annotated[int | None, Field(description="Only WORK intervals at most this long (s)")] = None,
    min_intensity: Annotated[
        float | None, Field(description="Only WORK intervals at or above this % of FTP (replaces the 70 % floor)")
    ] = None,
    max_intensity: Annotated[float | None, Field(description="Only WORK intervals at or below this % of FTP")] = None,
    min_reps: Annotated[int | None, Field(description="Only main sets with at least this many intervals")] = None,
    max_reps: Annotated[int | None, Field(description="Only main sets with at most this many intervals")] = None,
    ftp_range: Annotated[str | None, Field(description='Only activities with FTP at the time in "low-high" W, e.g. "225-240"')] = None,
    include_rpe: Annotated[bool, Field(description="Show whole-activity RPE and its trend")] = True,
    comparable_only: Annotated[
        bool, Field(description="Table only activities matching the pattern; false tables all, with the reason")
    ] = True,
) -> str:
    """Use to compare repeated executions of the same workout over time (e.g. every 3 x 10 min threshold session) on truly comparable work intervals.

    Activities by name search (query), activity_ids or a date range (default the last 90
    days); limit 8 (max 12). WORK intervals are split into main set (similar length and
    intensity), surges, other and excluded; the main set gets time-weighted power, NP, HR, max
    HR, cadence, W/bpm. Only activities matching the reference pattern (reference_activity_id,
    else the newest; length ±25 %, intensity ±8 points of FTP) are compared; trends first ->
    last with n (< 3 not reliable). RPE is whole-activity only; gear and power meter
    differences are flagged; no verdict. Read-only; one API call per activity plus 1-3 (one
    more per id with activity_ids). Method: intervals://methods/comparisons (get_guide).
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    date_error = _validate_optional_dates(start_date, end_date)
    if date_error:
        return date_error
    ftp_bounds = _parse_range(ftp_range, "ftp_range")
    if isinstance(ftp_bounds, str):
        return ftp_bounds
    reps = _reps_params(min_reps, max_reps)
    if isinstance(reps, str):
        return reps
    capped = min(max(limit, 1), MAX_WORKOUTS)

    api = _Api()
    fetch_limit = min(capped * 4, MAX_SEARCH_RESULTS)
    reference: dict[str, Any] | None = None
    window_note = None
    if reference_activity_id and not activity_ids:
        # The reference is fetched by id, so it is the pattern even when it is older than the newest
        # `limit` activities or outside the listed range.
        reference, error = await _reference_activity(api, reference_activity_id)
        if reference is None:
            return error or f"Reference activity {reference_activity_id} not found."
        if query:
            # A name search is anchored on the reference like find_similar_intervals: without start_date it
            # covers REFERENCE_LOOKBACK_DAYS before it, and that whole window is listed (not only 90 days).
            start_date, window_note = _reference_window(reference, start_date)
    name_search = bool(query) and not activity_ids and not (start_date or end_date)
    activities, error, source = await _collect_activities(
        api, athlete_id_to_use, activity_ids=activity_ids, query=query, start_date=start_date,
        end_date=end_date, fetch_limit=fetch_limit,
    )
    if error:
        return error
    if reference_activity_id and reference is None:  # activity_ids: the reference is one of them or fetched
        reference = next((a for a in activities if str(a.get("id")) == str(reference_activity_id)), None)
        if reference is None:
            reference, error = await _reference_activity(api, reference_activity_id)
            if reference is None:
                return error or f"Reference activity {reference_activity_id} not found."
    if reference is not None:
        # The fetched reference replaces its (shorter) list entry, or is added when the list does not have it.
        activities = [a for a in activities if str(a.get("id")) != str(reference.get("id"))] + [reference]
    newest = _select_activities(activities, start_date=start_date, end_date=end_date, limit=1)
    anchor = reference or (newest[0] if newest else None)
    if sport_types and sport_types.strip().lower() == "all":
        sport_filter = None
    elif sport_types:
        sport_filter = sport_types
    else:
        sport_filter = ",".join(family_types(anchor.get("type"))) if anchor else None
    selected = _select_activities(
        activities, sport_types=sport_filter, gear_id=gear_id, start_date=start_date, end_date=end_date
    )
    if ftp_bounds:
        selected = [a for a in selected if (f := _num(a.get("icu_ftp"))) is not None and ftp_bounds[0] <= f <= ftp_bounds[1]]
    if reference is not None:
        # The reference always takes one of the `limit` places (the oldest other activity makes room).
        others = [a for a in selected if str(a.get("id")) != str(reference.get("id"))]
        selected = sorted(others[: capped - 1] + [reference], key=lambda a: str(a.get("start_date_local") or ""), reverse=True)
    else:
        selected = selected[:capped]
    filters_text = _filter_text(sport_types if sport_types else None, gear_id, start_date, end_date)
    if sport_filter and not sport_types and anchor is not None:
        filters_text = ", ".join(p for p in (filters_text, f"sport family {sport_family(anchor.get('type'))} (default)") if p)
    if window_note:
        filters_text = ", ".join(p for p in (filters_text, window_note) if p)
    truncated = truncation_note(activities, fetch_limit, start_date, "name search") if name_search else None
    if not selected:
        return f"No activities found ({source}{'; ' + filters_text if filters_text else ''})." + (
            f" Note: {truncated} A name search with start_date/end_date lists that range instead." if truncated else ""
        )
    gear_map = await get_gear_map(athlete_id=athlete_id_to_use)
    split_filters = {"min_secs": min_interval_secs, "max_secs": max_interval_secs, "min_pct": min_intensity, "max_pct": max_intensity}
    rows: list[dict[str, Any]] = []
    for activity in reversed(selected):
        intervals, error = await _intervals_of(api, activity.get("id"))
        if error:
            return error
        rows.append(_workout_row(activity, intervals, gear_map, split_filters))
    if reference is not None:
        ref_row = next(r for r in rows if str(r["id"]) == str(reference.get("id")))
    else:
        ref_row = next((r for r in reversed(rows) if r["main_set"].get("count")), rows[-1])
    pattern = pattern_of(ref_row["main_set"])
    for row in rows:
        reason = matches_pattern(row["main_set"], pattern) if pattern else "no reference pattern"
        count = row["main_set"].get("count") or 0
        if reason is None and (min_reps is not None and count < min_reps or max_reps is not None and count > max_reps):
            reason = f"{count} repetitions outside the requested range"
        row["comparable"] = reason is None
        row["not_comparable_reason"] = reason
    compared = [r for r in rows if r["comparable"] or not comparable_only]
    trend_keys = [t for t in TREND_LABELS if include_rpe or t[0] != "rpe_whole_activity"]
    base_keys = [t for t in trend_keys if not t[0].endswith("_same_gear")]
    comparable_rows = [r for r in rows if r["comparable"]]
    trends = [t for key, *_ in base_keys if (t := _trend(comparable_rows, key))]
    notes = _comparability_notes(comparable_rows)
    gears = {row.get("gear_id") for row in comparable_rows}
    if len(gears) > 1 and ref_row.get("gear_id"):
        same_gear = [row for row in comparable_rows if row.get("gear_id") == ref_row.get("gear_id")]
        for key in ("avg_watts", "w_per_bpm"):
            trend = _trend(same_gear, key)
            if trend:
                trends.append({**trend, "key": f"{key}_same_gear", "gear_id": ref_row.get("gear_id")})

    if output_format.strip().lower() == "json":
        payload = {
            "athlete_id": athlete_id_to_use, "source": source, "filters": filters_text or None, "limit": capped,
            "window": {"start": start_date, "end": end_date, "default_lookback_days": REFERENCE_LOOKBACK_DAYS if window_note else None},
            "reference": {"activity_id": ref_row["id"], "pattern": pattern, "explicit": reference is not None},
            "activities": rows, "trends": trends, "notes": notes, "api_result_truncated": truncated, "api_calls": api.calls,
        }
        return json.dumps(payload, ensure_ascii=False)
    pattern_text = (
        f"{pattern['count']} x {hms(pattern['secs'])}" + (f" @ {pattern['pct_ftp']:.0f}% FTP" if pattern.get("pct_ftp") is not None else "")
        if pattern else "n/a (no main set of WORK intervals found)"
    )
    lines = [
        f"Workout comparison for athlete {athlete_id_to_use} (source {source}{'; ' + filters_text if filters_text else ''}; "
        f"{len(rows)} of {len(activities)} activities analysed, oldest first; limit {capped}"
        + (f", capped from {limit}" if limit > capped else "") + "):",
        f"Reference pattern: {pattern_text} from {'reference' if reference is not None else 'the newest'} activity {ref_row['id']} "
        f"({ref_row['date']}); comparable = interval length within 25 % and intensity within 8 % of FTP.",
        "Main set per activity, time-weighted means over the comparable WORK intervals; surges under 2 min and other "
        "WORK intervals are listed but never averaged in.",
        "Activity | set | power (NP) | HR avg/max | cadence | W/bpm | FTP | power source" + (" | RPE" if include_rpe else ""),
    ]
    for row in compared:
        lines.extend(_row_text(row, include_rpe))
        if not row["comparable"]:
            lines.append(f"    NOT comparable: {row['not_comparable_reason']}")
    skipped = [r for r in rows if not r["comparable"]] if comparable_only else []
    if skipped:
        lines.append("Not comparable (left out of the trends): " + "; ".join(
            f"{r['date']} '{r['name']}' ({r['id']}, {r['type']}): {r['not_comparable_reason']}" for r in skipped
        ))
    if trends:
        lines.append(f"Trends over the {len(comparable_rows)} comparable activities (first -> last):")
        foot = bool(comparable_rows) and all(is_foot_sport(row.get("type")) for row in comparable_rows)
        for trend in trends:
            label, units, digits = next((lab, u, d) for k, lab, u, d in trend_keys if k == trend["key"])
            if trend["key"] == "cadence" and foot:  # stored per leg: show steps per minute
                trend = {**trend, **{k: trend[k] * 2 for k in ("first", "last", "min", "max") if trend.get(k) is not None}}
                label, units = "cadence (steps/min = 2 x stored per-leg value)", "spm"
            pct = f" ({trend['pct']:+.1f} %)" if trend.get("pct") is not None else ""
            reliability = "" if trend["reliable"] else f"; only {trend['n']} activities, not reliable"
            lines.append(
                f"  {label}: {_fmt(trend['first'], digits, units)} -> {_fmt(trend['last'], digits, units)}{pct}, "
                f"range {_fmt(trend['min'], digits, units)}-{_fmt(trend['max'], digits, units)} (n {trend['n']}{reliability})"
            )
    else:
        lines.append("Trends: fewer than two comparable activities.")
    lines.extend(f"Note: {note}" for note in notes)
    if truncated:
        lines.append(f"Note: {truncated} A name search with start_date/end_date lists that range instead.")
    lines.append(f"API calls: {api.calls} {GEAR_CALL_NOTE}")
    return "\n".join(lines)


# ------------------------------------------------------- power:HR efficiency
MIN_ACTIVITIES_PER_GROUP = 3


def _bands(csv: str) -> list[tuple[float, float]] | str:
    bands: list[tuple[float, float]] = []
    for part in _split(csv):
        try:
            low, high = (float(bit) for bit in part.split("-"))
        except ValueError:
            return f"Error: power_bands must look like '150-200,200-250', got '{part}'."
        if low < 0 or high <= low:
            return f"Error: power band '{part}' must have 0 <= low < high."
        bands.append((low, high))
    if not bands:
        return "Error: at least one power band is required."
    ordered = sorted(bands)
    for (low_a, high_a), (low_b, _) in zip(ordered, ordered[1:], strict=False):
        if low_b < high_a:
            return f"Error: power bands {low_a:g}-{high_a:g} and {low_b:g}-... overlap; use non-overlapping bands such as '150-200,200-250'."
    return bands


def _band_label(band: tuple[float, float]) -> str:
    return f"{band[0]:g}-{band[1]:g} W"


def _band_stats(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    intervals: list[dict[str, Any]], bands: list[tuple[float, float]], min_secs: int,
    min_start_s: float = 0.0, max_start_s: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Per band: n, time-weighted mean watts and HR and W/bpm of the steady WORK intervals in it.

    Bands are half-open (low <= W < high); only intervals starting between min_start_s and
    max_start_s (seconds from the start) count.
    """
    stats: dict[str, dict[str, Any]] = {}
    for band in bands:
        hits: list[tuple[float, float, float]] = []
        for interval in intervals:
            secs = _num(interval.get("moving_time")) or _num(interval.get("elapsed_time"))
            watts, hr = _num(interval.get("average_watts")), _num(interval.get("average_heartrate"))
            start = _num(interval.get("start_time")) or 0.0
            if secs is None or secs < min_secs or watts is None or hr is None or hr <= 0:
                continue
            if start < min_start_s or (max_start_s is not None and start > max_start_s):
                continue
            if band[0] <= watts < band[1]:
                hits.append((watts, hr, secs))
        if hits:
            total = sum(s for _, _, s in hits)
            mean_w = sum(w * s for w, _, s in hits) / total
            mean_hr = sum(h * s for _, h, s in hits) / total
            stats[_band_label(band)] = {
                "n": len(hits), "secs": total, "watts": round(mean_w, 1), "hr": round(mean_hr, 1),
                "w_per_bpm": round(mean_w / mean_hr, 3),
            }
    return stats


def _band_trend(rows: list[dict[str, Any]], label: str, min_per_group: int, gear: str | None = None) -> dict[str, Any]:
    """Oldest vs newest group of independent activities (mean W/bpm per activity) in one band.

    Each activity counts once. A trend needs at least ``min_per_group`` activities in each group;
    the change is compared with the day-to-day standard deviation of the per-activity values.
    """
    series = [row["bands"][label]["w_per_bpm"] for row in rows if label in row["bands"]]
    base: dict[str, Any] = {"band": label, "gear_id": gear, "activities": len(series), "min_per_group": min_per_group}
    if len(series) < 2 * min_per_group:
        return {**base, "reliable": False,
                "note": f"not enough independent activities ({len(series)}; a trend needs at least {2 * min_per_group})"}
    group = max(min_per_group, len(series) // 3)
    oldest, newest = _mean(series[:group]), _mean(series[-group:])
    change = _pct_change(oldest, newest)
    spread = statistics.stdev(series)
    diff = (newest or 0) - (oldest or 0)
    return {
        **base, "group_size": group, "reliable": True,
        "oldest_mean": round(oldest, 3) if oldest is not None else None,
        "newest_mean": round(newest, 3) if newest is not None else None,
        "change_pct": round(change, 1) if change is not None else None,
        "sd_per_activity": round(spread, 3),
        "beyond_variation": abs(diff) > spread,
    }


def _efficiency_trends(
    rows: list[dict[str, Any]], labels: list[str], min_per_group: int
) -> list[dict[str, Any]]:
    """Trends per band, separately per gear when several bikes / power meters are involved."""
    gears = sorted({row.get("gear_id") or "" for row in rows})
    if len(gears) <= 1:
        return [_band_trend(rows, label, min_per_group) for label in labels]
    names = {row.get("gear_id") or "": row.get("gear_name") for row in rows}
    trends = []
    for label in labels:
        for gear in gears:
            subset = [r for r in rows if (r.get("gear_id") or "") == gear]
            if not any(label in r["bands"] for r in subset):
                continue
            name = f"{names[gear]} ({gear})" if names.get(gear) else (gear or "no gear")
            trends.append(_band_trend(subset, label, min_per_group, name))
    return trends


def _trend_text(trend: dict[str, Any]) -> str:
    where = f"{trend['band']}" + (f" on gear {trend['gear_id']}" if trend.get("gear_id") else "")
    if not trend["reliable"]:
        return f"  {where}: {trend['note']} - no trend, not reliable"
    change = f"{trend['change_pct']:+.1f} %" if trend["change_pct"] is not None else "n/a"
    verdict = (
        "larger than the day-to-day variation" if trend["beyond_variation"]
        else "within the day-to-day variation, no meaningful change"
    )
    return (
        f"  {where}: {trend['oldest_mean']:.2f} -> {trend['newest_mean']:.2f} W/bpm ({change}; {trend['group_size']} of "
        f"{trend['activities']} activities per group; SD per activity {trend['sd_per_activity']:.2f}: {verdict})"
    )


@tool("read")
async def get_power_hr_efficiency(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements,too-many-branches
    start_date: Annotated[StartDate, Field(description="First day YYYY-MM-DD; default 90 days before end_date")] = None,
    end_date: EndDate = None,
    sport_types: Annotated[str, Field(description='Comma-separated activity types, e.g. "Ride,VirtualRide"')] = "Ride,GravelRide,VirtualRide",
    power_bands: Annotated[
        str, Field(description='Comma-separated non-overlapping bands in W as "low-high"; low <= W < high')
    ] = "150-200,200-250,250-300",
    min_interval_secs: Annotated[int, Field(description="Shortest WORK interval counted, in seconds")] = 300,
    limit: Annotated[int, Field(description="Maximum activities, newest first; capped to 1-60")] = 30,
    athlete_id: AthleteId = None,
    output_format: OutputFormat = "text",
    gear_id: GearId = None,
    environment: Environment = None,
    min_start_minutes: Annotated[float, Field(description="Only intervals starting at least this many minutes into the activity")] = 0.0,
    max_start_minutes: Annotated[
        float | None, Field(description="Only intervals starting at most this many minutes into the activity")
    ] = None,
    min_activities_per_group: Annotated[
        int, Field(description="Independent activities needed in the oldest and in the newest group for a trend, >= 1")
    ] = MIN_ACTIVITIES_PER_GROUP,
) -> str:
    """Use to track power-to-heart-rate efficiency (W per bpm at the same power) over weeks.

    Puts every WORK interval of at least min_interval_secs with power and HR into the power
    band of its average power; returns per activity and band the count and time-weighted W,
    HR and W/bpm, and per band the oldest vs the newest group of activities (each counts once;
    at least 3 per group, else not reliable) compared with the day-to-day SD. Default the
    last 90 days of rides, limit 30 (max 60); trends per gear when several power meters are
    involved; filters for gear, indoor/outdoor and interval start time. Heat, fatigue,
    cadence and sensors move the ratio: statistics only, no fitness verdict. Read-only; one API
    call per activity plus 2. Method: intervals://methods/comparisons (get_guide).
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    bands = _bands(power_bands)
    if isinstance(bands, str):
        return bands
    if min_interval_secs <= 0:
        return "Error: min_interval_secs must be positive."
    if min_activities_per_group < 1:
        return "Error: min_activities_per_group must be at least 1."
    env = (environment or "").strip().lower() or None
    if env not in (None, "indoor", "outdoor"):
        return "Error: environment must be 'indoor' or 'outdoor'."
    span = _resolve_range(start_date, end_date, DEFAULT_RANGE_DAYS)
    if isinstance(span, str):
        return span
    capped = min(max(limit, 1), MAX_EFFICIENCY_ACTIVITIES)

    api = _Api()
    activities, error, _ = await _collect_activities(api, athlete_id_to_use, start_date=span[0], end_date=span[1])
    if error:
        return error
    selected = _select_activities(activities, sport_types=sport_types, gear_id=gear_id)
    if env:
        selected = [a for a in selected if is_indoor(a) == (env == "indoor")]
    selected = selected[:capped]
    if not selected:
        return f"No activities found between {span[0]} and {span[1]} for sports {sport_types}."
    gear_map = await get_gear_map(athlete_id=athlete_id_to_use)
    rows: list[dict[str, Any]] = []
    for activity in reversed(selected):
        intervals, error = await _work_intervals(api, activity.get("id"))
        if error:
            return error
        stats = _band_stats(intervals, bands, min_interval_secs, min_start_minutes * 60,
                            max_start_minutes * 60 if max_start_minutes is not None else None)
        rows.append({**_activity_json(activity, gear_map), "power_meter": activity.get("power_meter"),
                     "indoor": is_indoor(activity), "bands": stats})
    labels = [_band_label(band) for band in bands]
    trends = _efficiency_trends(rows, labels, min_activities_per_group)
    position = (
        f", intervals starting {min_start_minutes:g}-{max_start_minutes:g} min into the activity" if max_start_minutes is not None
        else (f", intervals starting after {min_start_minutes:g} min" if min_start_minutes else "")
    )

    if output_format.strip().lower() == "json":
        payload = {
            "athlete_id": athlete_id_to_use, "start": span[0], "end": span[1], "sport_types": sport_types,
            "bands": labels, "band_rule": "low <= W < high", "min_interval_secs": min_interval_secs, "limit": capped,
            "filters": {"gear_id": gear_id, "environment": env, "min_start_minutes": min_start_minutes,
                        "max_start_minutes": max_start_minutes, "min_activities_per_group": min_activities_per_group},
            "activities": rows, "trends": trends, "note": EFFICIENCY_NOTE, "api_calls": api.calls,
        }
        return json.dumps(payload, ensure_ascii=False)
    lines = [
        f"Power:HR efficiency for athlete {athlete_id_to_use}, {span[0]} to {span[1]}, sports {sport_types}: "
        f"{len(rows)} activities (oldest first; limit {capped}{', capped from ' + str(limit) if limit > capped else ''}), "
        f"WORK intervals of at least {hms(min_interval_secs)} with power and HR{position}, bands {', '.join(labels)} "
        "(low <= W < high; time-weighted means)."
        + (f" Filters: {', '.join(f for f in (f'gear {gear_id}' if gear_id else '', env or '') if f)}." if gear_id or env else ""),
    ]
    for row in rows:
        gear = f"{row['gear_name']} ({row['gear_id']})" if row["gear_name"] else (row["gear_id"] or "no gear")
        cells = [
            f"{label}: {s['n']} x {s['watts']:.0f} W / {s['hr']:.0f} bpm = {s['w_per_bpm']:.2f} W/bpm"
            for label in labels
            if (s := row["bands"].get(label))
        ]
        lines.append(
            f"{row['date']} {row['type']} '{row['name']}' ({row['id']}), gear {gear}{', indoor' if row['indoor'] else ''}, "
            f"FTP {_fmt(_num(row['ftp']), 0, 'W')}: " + ("; ".join(cells) if cells else "no qualifying intervals")
        )
    per_gear = any(t.get("gear_id") for t in trends)
    lines.append(
        f"Trend per band{' and gear (power meters are not mixed)' if per_gear else ''} (mean W/bpm per activity, oldest vs "
        f"newest group, at least {min_activities_per_group} independent activities per group):"
    )
    lines.extend(_trend_text(trend) for trend in trends)
    lines.append(EFFICIENCY_NOTE)
    lines.append(f"API calls: {api.calls} {GEAR_CALL_NOTE}")
    return "\n".join(lines)


# ---------------------------------------------------------- fatigue resistance
KJ_PER_KG_SUGGESTION = (15, 30)  # kJ per kg body mass suggested for kJ0 / kJ1 (literature uses ~10-40 kJ/kg)
SUGGESTION_ROUND_KJ = 250
SUGGESTION_LOOKBACK_DAYS = 90


def _curve_lookup(curve: dict[str, Any], duration: int) -> tuple[float | None, int | None]:
    """Watts at the requested duration (exact or nearest within 10%) and the duration used."""
    secs_raw, values_raw = curve.get("secs"), curve.get("values") or curve.get("watts")
    secs: list[Any] = secs_raw if isinstance(secs_raw, list) else []
    values: list[Any] = values_raw if isinstance(values_raw, list) else []
    best_diff: float | None = None
    best_secs: int | None = None
    best_watts: float | None = None
    for sec, value in zip(secs, values, strict=False):
        sec_num, watts = _num(sec), _num(value)
        if sec_num is None or watts is None:
            continue
        diff = abs(sec_num - duration)
        if diff <= duration * 0.1 and (best_diff is None or diff < best_diff):
            best_diff, best_secs, best_watts = diff, int(sec_num), watts
    return best_watts, best_secs


def _curve_status(block: dict[str, Any], key: str, durations: list[int]) -> str:
    """'ok', 'missing' (no data at the durations) or 'identical' (equals the fresh curve everywhere)."""
    pairs = [(_curve_lookup(block["fresh"], d)[0], _curve_lookup(block.get(key) or {}, d)[0]) for d in durations]
    values = [(fresh, fatigued) for fresh, fatigued in pairs if fatigued is not None]
    if not values:
        return "missing"
    if all(fresh is not None and fresh == fatigued for fresh, fatigued in values):
        return "identical"
    return "ok"


def _fatigue_rows(block: dict[str, Any], durations: list[int], keys: tuple[str, ...] = ("kj0", "kj1")) -> list[dict[str, Any]]:
    rows = []
    status = {key: _curve_status(block, key, durations) for key in keys}
    for duration in durations:
        fresh, secs_used = _curve_lookup(block["fresh"], duration)
        row: dict[str, Any] = {"duration": duration, "secs_used": secs_used, "fresh": fresh}
        for key in keys:
            value, _ = _curve_lookup(block.get(key) or {}, duration)
            usable = status[key] == "ok"
            row[key] = value if usable else None
            drop = _pct_change(fresh, value) if usable else None
            row[f"{key}_change_pct"] = round(drop, 1) if drop is not None else None
        rows.append(row)
    block["status"] = status
    return rows


async def _kj_thresholds(api: _Api, athlete_id: str, activity_type: str) -> tuple[dict[str, Any] | None, str]:
    """after_kj0 / after_kj1 / ftp of the sport setting covering activity_type and a note."""
    result = await api.get(f"/athlete/{seg(athlete_id)}/sport-settings")
    error = _error(result, "sport settings")
    if error:
        return None, f"{error}; kJ thresholds unknown."
    wanted = activity_type.strip().lower()
    for setting in _list_of_dicts(result):
        types = [str(t).lower() for t in (setting.get("types") or [])]
        if wanted in types:
            thresholds = {k: setting.get(k) for k in ("after_kj0", "after_kj1", "ftp")}
            kj0, kj1 = thresholds["after_kj0"], thresholds["after_kj1"]
            if not kj0 and not kj1:
                return thresholds, (
                    f"Fatigued power curves are not configured for {activity_type} in Intervals.icu (after_kj0 / after_kj1 "
                    "are empty): Intervals.icu then returns the fresh curve as kJ0/kJ1, so no fatigue values are shown."
                )
            parts = [
                f"after kJ0 = {kj0} kJ" if kj0 else "after kJ0 not configured",
                f"after kJ1 = {kj1} kJ" if kj1 else "after kJ1 not configured",
            ]
            return thresholds, f"Sport setting for {activity_type}: " + ", ".join(parts) + "."
    return None, f"No sport setting covers '{activity_type}'; kJ thresholds unknown."


def _round_kj(value: float, down: bool = False) -> int:
    steps = math.floor(value / SUGGESTION_ROUND_KJ) if down else round(value / SUGGESTION_ROUND_KJ)
    return int(max(SUGGESTION_ROUND_KJ, steps * SUGGESTION_ROUND_KJ))


async def _suggest_thresholds(  # pylint: disable=too-many-locals
    api: _Api, athlete_id: str, activity_type: str, ftp: float | None
) -> dict[str, Any]:
    """Plausible kJ0 / kJ1 from body mass (or FTP) and from the work of recent rides (suggestion only, never saved)."""
    athlete = await api.get(f"/athlete/{seg(athlete_id)}")
    weight = _num(athlete.get("icu_weight")) if isinstance(athlete, dict) else None
    end = get_default_end_date()
    start = (date.fromisoformat(end) - timedelta(days=SUGGESTION_LOOKBACK_DAYS - 1)).isoformat()
    result = await api.get(f"/athlete/{seg(athlete_id)}/activities", {"oldest": start, "newest": end, "fields": "id,type,icu_joules,moving_time"})
    family = set(family_types(activity_type))
    work = sorted(
        joules / 1000 for a in _list_of_dicts(result)
        if a.get("type") in family and (joules := _num(a.get("icu_joules"))) and (_num(a.get("moving_time")) or 0) >= 3600
    )
    suggestion: dict[str, Any] = {"weight_kg": weight, "ftp": ftp, "rides_60min_plus": len(work), "lookback_days": SUGGESTION_LOOKBACK_DAYS}
    if weight:
        kj0, kj1 = (weight * factor for factor in KJ_PER_KG_SUGGESTION)
        basis = f"{KJ_PER_KG_SUGGESTION[0]} and {KJ_PER_KG_SUGGESTION[1]} kJ/kg at {weight:g} kg"
    elif ftp:
        kj_per_hour = ftp * 0.55 * 3.6  # endurance riding at about 55 % of FTP
        kj0, kj1 = 1.5 * kj_per_hour, 3 * kj_per_hour
        basis = f"1.5 h and 3 h at about 55 % of FTP {ftp:g} W"
    else:
        return {**suggestion, "kj0": None, "kj1": None, "basis": "no weight or FTP available"}
    suggestion.update(kj0=_round_kj(kj0), kj1=_round_kj(kj1), basis=basis)
    if work:
        median, p75 = statistics.median(work), work[int(0.75 * (len(work) - 1))]
        suggestion.update(median_ride_kj=round(median), p75_ride_kj=round(p75))
        if p75 < suggestion["kj1"] or median < suggestion["kj0"]:
            ride_kj0 = _round_kj(min(kj0, median), down=True)
            ride_kj1 = max(_round_kj(min(kj1, p75), down=True), ride_kj0 + SUGGESTION_ROUND_KJ)
            suggestion["by_recent_rides"] = {"kj0": ride_kj0, "kj1": ride_kj1}
    return suggestion


def _fatigue_table(block: dict[str, Any], rows: list[dict[str, Any]], thresholds: dict[str, Any] | None, keys: tuple[str, ...]) -> list[str]:
    lines = [f"{block['label']}:"]
    header = "  Duration | fresh"
    for key in keys:
        threshold = (thresholds or {}).get(f"after_{key}")
        status = (block.get("status") or {}).get(key)
        label = f"after {threshold} kJ" if threshold else key
        if status == "missing":
            label += " (no data)"
        elif status == "identical":
            label += " (identical to fresh)"
        header += f" | {label}"
    lines.append(header)
    for row in rows:
        cells = [_fmt(row["fresh"], 0, "W")]
        for key in keys:
            cell = _fmt(row[key], 0, "W")
            if row[f"{key}_change_pct"] is not None:
                cell += f" ({row[f'{key}_change_pct']:+.1f} %)"
            cells.append(cell)
        used = f" (curve point {row['secs_used']} s)" if row["secs_used"] not in (None, row["duration"]) else ""
        lines.append(f"  {_duration_label(row['duration'])}{used} | " + " | ".join(cells))
    for key, state in (block.get("status") or {}).items():
        if state == "identical":
            lines.append(f"  {key}: identical to the fresh curve at every duration - either the best efforts all came after the "
                         "threshold or the threshold is too low; no fatigue change is computed.")
        elif state == "missing":
            lines.append(f"  {key}: no efforts after the threshold in this period (curve missing) - no values computed.")
    return lines


@tool("read")
async def get_fatigue_resistance(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements,too-many-branches,too-many-statements
    activity_id: Annotated[
        OptionalActivityId, Field(description="Activity whose curves are compared; omit for the athlete curves")
    ] = None,
    activity_type: Annotated[str, Field(description="Activity type of the curves and kJ thresholds, e.g. Ride, VirtualRide")] = "Ride",
    durations: Annotated[str, Field(description='Comma-separated seconds, e.g. "60,300,1200"')] = "60,300,1200",
    curves: Annotated[
        str, Field(description='Comma-separated athlete curve ids, e.g. "42d,90d,1y,s0"; ignored with activity_id')
    ] = "42d",
    athlete_id: AthleteId = None,
    output_format: OutputFormat = "text",
    suggest_thresholds: Annotated[
        bool, Field(description="Suggest kJ0/kJ1 when not both are configured; a suggestion only, nothing is saved")
    ] = True,
) -> str:
    """Use for fatigue resistance: best power fresh vs after the athlete's work thresholds kJ0 / kJ1 (Intervals.icu fatigued power curves).

    Reads the sport settings first. When they hold no kJ thresholds no fatigued values are
    shown: the tool explains the setting and (suggest_thresholds) proposes thresholds from
    body mass and recent rides, never saved. Otherwise returns watts per duration fresh and
    after kJ0 / kJ1 with the change in %, for one activity or for athlete curves (default
    42d); a fatigued curve without data is reported as missing, one equal to fresh as
    identical, without a change. Read-only, 2-5 API calls. Within one long ride:
    get_long_ride_fatigue_profile. Method: intervals://methods/comparisons (get_guide).
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    duration_list = _numbers(durations, "durations")
    if isinstance(duration_list, str):
        return duration_list
    if not duration_list:
        return "Error: at least one duration is required."
    curve_ids = _split(curves)
    if not activity_id and not curve_ids:
        return "Error: at least one curve id is required when no activity_id is given."

    api = _Api()
    thresholds, note = await _kj_thresholds(api, athlete_id_to_use, activity_type)
    if thresholds is None:
        keys: tuple[str, ...] = ("kj0", "kj1")  # thresholds unknown: show what Intervals.icu returns, with the caveat
    else:
        keys = tuple(key for key in ("kj0", "kj1") if thresholds.get(f"after_{key}"))
    blocks: list[dict[str, Any]] = []
    if activity_id:
        block: dict[str, Any] = {"id": activity_id, "label": f"Activity {activity_id} ({activity_type})"}
        for key, fatigue in (("fresh", None), *((k, k) for k in keys)):
            result = await api.get(
                f"/activity/{seg(activity_id)}/power-curve.json", {"fatigue": fatigue} if fatigue else None
            )
            error = _error(result, f"power curve ({key})")
            if error:
                return error
            block[key] = result if isinstance(result, dict) else {}
        blocks.append(block)
    else:
        wanted = [cid for curve_id in curve_ids for cid in (curve_id, *(f"{curve_id}-{k}" for k in keys))]
        result = await api.get(
            f"/athlete/{seg(athlete_id_to_use)}/power-curves.json",
            {"type": activity_type, "curves": ",".join(wanted)},
        )
        error = _error(result, "athlete power curves")
        if error:
            return error
        by_id = {str(c.get("id")): c for c in _list_of_dicts(result.get("list") if isinstance(result, dict) else None)}
        for curve_id in curve_ids:
            fresh = by_id.get(curve_id, {})
            blocks.append({
                "id": curve_id, "label": f"Curve {fresh.get('label') or curve_id} ({curve_id}, {activity_type})",
                "fresh": fresh, **{k: by_id.get(f"{curve_id}-{k}", {}) for k in keys},
            })
    tables = [(block, _fatigue_rows(block, duration_list, keys)) for block in blocks]
    configured = thresholds is None or bool(keys)
    suggestion = None
    if thresholds is not None and len(keys) < 2 and suggest_thresholds:
        suggestion = await _suggest_thresholds(api, athlete_id_to_use, activity_type, _num(thresholds.get("ftp")))

    if output_format.strip().lower() == "json":
        payload = {
            "athlete_id": athlete_id_to_use, "activity_id": activity_id, "activity_type": activity_type,
            "thresholds": thresholds, "configured": {k: bool((thresholds or {}).get(f"after_{k}")) for k in ("kj0", "kj1")},
            "note": note, "durations": duration_list,
            "curves": [{"id": block["id"], "label": block["label"], "status": block.get("status"), "rows": rows} for block, rows in tables],
            "suggestion": suggestion, "api_calls": api.calls,
        }
        return json.dumps(payload, ensure_ascii=False)
    lines = [f"Fatigue resistance for athlete {athlete_id_to_use}: power fresh vs after kJ thresholds ({activity_type}).", note]
    if not configured:
        lines.append(
            f"To evaluate fatigue resistance set the thresholds in Intervals.icu: Settings -> {activity_type} sport settings -> "
            "power -> fatigued curves 'after kJ' (kJ0, kJ1). This tool never changes settings."
        )
    for block, rows in tables:
        if configured:
            lines.extend(_fatigue_table(block, rows, thresholds, keys))
        else:
            fresh_text = ", ".join(f"{_duration_label(r['duration'])} {_fmt(r['fresh'], 0, 'W')}" for r in rows)
            lines.append(f"{block['label']} - fresh curve for reference: {fresh_text}")
    if suggestion:
        if suggestion.get("kj0"):
            text = f"Suggestion only (not saved): kJ0 ≈ {suggestion['kj0']} kJ, kJ1 ≈ {suggestion['kj1']} kJ ({suggestion['basis']})"
            if suggestion.get("median_ride_kj") is not None:
                text += (
                    f"; your rides of 1 h+ in the last {SUGGESTION_LOOKBACK_DAYS} days: median {suggestion['median_ride_kj']} kJ, "
                    f"75th percentile {suggestion['p75_ride_kj']} kJ (n {suggestion['rides_60min_plus']})"
                )
            rides = suggestion.get("by_recent_rides")
            if rides:
                text += (
                    f"; thresholds above most of your rides leave the fatigued curves almost empty, so kJ0 ≈ {rides['kj0']} kJ / "
                    f"kJ1 ≈ {rides['kj1']} kJ fit your current rides better"
                )
            lines.append(text + ".")
        else:
            lines.append(f"No threshold suggestion: {suggestion['basis']}.")
    if configured:
        lines.append("Change in % is relative to the fresh curve; 'n/a' means no usable value near that duration.")
    lines.append(f"API calls: {api.calls}")
    return "\n".join(lines)
