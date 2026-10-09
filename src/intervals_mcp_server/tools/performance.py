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
from datetime import date, timedelta
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.gear import get_gear_map
from intervals_mcp_server.utils.dates import get_default_end_date
from intervals_mcp_server.utils.sports import format_pace, hms
from intervals_mcp_server.utils.streams import find_stream
from intervals_mcp_server.utils.validation import resolve_athlete_id, validate_date

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

DEFAULT_RANGE_DAYS = 90
MAX_COMPARE_ACTIVITIES = 25
MAX_WORKOUTS = 12
MAX_EFFICIENCY_ACTIVITIES = 60
MAX_SEARCH_RESULTS = 100
MAX_EFFORT_COUNT = 20
MAX_INTENSITY = 300
TARGETS = ("POWER", "HR", "PACE")
ACTIVITY_FIELDS = (
    "id,name,start_date_local,type,gear,icu_ftp,icu_training_load,icu_intensity,moving_time,"
    "icu_rpe,feel,compliance,interval_summary"
)
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
# JSON key, column label, decimals, units of the compare_workouts columns
WORKOUT_COLUMNS: tuple[tuple[str, str, int, str], ...] = (
    ("work_intervals", "intervals", 0, ""),
    ("work_time", "work time", 0, "s"),
    ("avg_watts", "avg W", 0, "W"),
    ("np", "NP", 0, "W"),
    ("avg_hr", "HR", 0, "bpm"),
    ("cadence", "cadence", 0, "rpm"),
    ("pw_hr", "Pw:HR", 2, "W/bpm"),
    ("training_load", "load", 0, ""),
    ("intensity", "IF", 0, "%"),
    ("ftp", "FTP", 0, "W"),
    ("rpe", "RPE", 0, ""),
    ("feel", "feel", 0, ""),
)
POWER_METER_NOTE = (
    "Note: values are averages of the recorded stream as computed by Intervals.icu. Different bikes "
    "may carry different power meters that are not calibrated against each other, so compare "
    "across gear with care; 'n/a' means the activity is shorter than the duration or has no such stream."
)
EFFICIENCY_NOTE = (
    "Note: this is a statistical comparison of W per bpm in steady WORK intervals. Heat, fatigue, "
    "hydration, cadence, indoor vs outdoor, interval position in the ride and power meter "
    "differences between bikes all move the ratio; it is not a fitness verdict."
)
GEAR_CALL_NOTE = "(plus 1 for the gear catalog unless cached)"


class _Api:  # pylint: disable=too-few-public-methods
    """Issues GET requests for one tool call and counts them."""

    def __init__(self, api_key: str | None) -> None:
        self.api_key = api_key
        self.calls = 0

    async def get(
        self, url: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """GET an endpoint (path relative to the API base) and count the call."""
        self.calls += 1
        return await make_intervals_request(url=url, api_key=self.api_key, params=params)


# ------------------------------------------------------------------ helpers
def _error(result: Any, what: str) -> str | None:
    """'Error fetching <what>: <message>' when the client returned an error dict, else None."""
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching {what}: {result.get('message', 'Unknown error')}"
    return None


def _split(csv: str | None) -> list[str]:
    return [part.strip() for part in (csv or "").split(",") if part.strip()]


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
    """Float of a numeric value; None for null, bool or text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
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
        result = await api.get(f"/activity/{activity_id}")
        error = _error(result, f"activity {activity_id}")
        if error:
            return [], error
        activity = result[0] if isinstance(result, list) and result else result
        if isinstance(activity, dict) and activity:
            activities.append(activity)
    return activities, None


async def _collect_activities(
    api: _Api,
    athlete_id: str,
    *,
    activity_ids: str | None = None,
    query: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    fetch_limit: int = MAX_SEARCH_RESULTS,
) -> tuple[list[dict[str, Any]], str | None, str]:
    """Activities by id, by name search or by date range; returns (activities, error, source text)."""
    if activity_ids:
        ids = _split(activity_ids)
        activities, error = await _activities_by_ids(api, ids)
        return activities, error, f"{len(ids)} activity id(s)"
    if query:
        result = await api.get(
            f"/athlete/{athlete_id}/activities/search-full", {"q": query, "limit": fetch_limit}
        )
        error = _error(result, "activity search")
        return _list_of_dicts(result), error, f"name search '{query}'"
    span = _resolve_range(start_date, end_date, DEFAULT_RANGE_DAYS)
    if isinstance(span, str):
        return [], span, ""
    result = await api.get(
        f"/athlete/{athlete_id}/activities",
        {"oldest": span[0], "newest": span[1], "fields": ACTIVITY_FIELDS},
    )
    return _list_of_dicts(result), _error(result, "activities"), f"{span[0]} to {span[1]}"


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


async def _work_intervals(api: _Api, activity_id: Any) -> tuple[list[dict[str, Any]], str | None]:
    result = await api.get(f"/activity/{activity_id}/intervals")
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


def _position(time_data: list[Any], index: Any) -> str:
    secs = _time_at(time_data, index)
    return hms(secs) if secs is not None else f"index {index}"


def _request_label(requested: dict[str, int]) -> str:
    if "distance" in requested:
        return f"{requested['distance']} m"
    return _duration_label(requested["duration"])


def _effort_row(
    effort: dict[str, Any], stream: str, time_data: list[Any], rank: int, requested: dict[str, int]
) -> dict[str, Any]:
    average = _num(effort.get("average"))
    start, end = effort.get("start_index"), effort.get("end_index")
    return {
        "requested": requested,
        "available": True,
        "rank": rank,
        "average": round(average, 2) if average is not None else None,
        "units": STREAM_UNITS.get(stream),
        "pace": format_pace(average) if stream == "velocity_smooth" and average else None,
        "duration": effort.get("duration"),
        "distance": effort.get("distance"),
        "start_index": start,
        "end_index": end,
        "start_secs": _time_at(time_data, start),
        "end_secs": _time_at(time_data, end),
        "start": _position(time_data, start),
        "end": _position(time_data, end),
    }


def _effort_text(row: dict[str, Any]) -> str:
    units = row.get("units") or ""
    value = _fmt(row["average"], 2 if units == "m/s" else 1, units)
    if row.get("pace"):
        value += f" ({row['pace']})"
    text = f"#{row['rank']} {value}"
    if "distance" in row["requested"] and row.get("duration") is not None:
        text += f" in {hms(row['duration'])}"
    return f"{text} from {row['start']} to {row['end']} (samples {row['start_index']}-{row['end_index']})"


@tool("read")
async def get_best_efforts(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-return-statements
    activity_id: str,
    stream: str = "watts",
    durations: str | None = "5,30,60,300,1200,3600",
    distances: str | None = None,
    count: int = 1,
    exclude_intervals: bool = False,
    start_index: int | None = None,
    end_index: int | None = None,
    api_key: str | None = None,
    output_format: str = "text",
) -> str:
    """Best efforts of one activity for given durations or distances (peak power, HR, pace)

    Asks Intervals.icu for the best average of a stream over each requested duration (seconds)
    or distance (metres) and reports, per effort, the average (W, bpm or m/s with pace), where
    it happened as h:mm:ss from the activity's time stream (recording pauses excluded; sample
    indices when the time stream is missing), the sample indices (usable as start_index /
    end_index in other tools) and the duration/distance covered. Use it to find the peak 5 s /
    1 min / 5 min / 20 min power of a ride, the fastest kilometre of a run (stream
    velocity_smooth with distances) or the highest sustained heart rate. Durations longer than
    the activity are reported as not available. One API call per duration or distance plus one
    for the time stream.

    Args:
        activity_id: The Intervals.icu activity ID
        stream: Stream to evaluate: watts (default), heartrate, velocity_smooth, cadence ...
        durations: Comma-separated durations in seconds (optional, default "5,30,60,300,1200,3600")
        distances: Comma-separated distances in metres, e.g. "1000,5000" for runs (optional)
        count: Number of best (non-overlapping) efforts per duration/distance, 1-20 (optional, default 1)
        exclude_intervals: Ignore samples inside detected intervals (optional, default False)
        start_index: First sample index to search from (optional)
        end_index: Sample index to stop before (optional)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
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

    api = _Api(api_key)
    time_data = _time_stream(await api.get(f"/activity/{activity_id}/streams", {"types": "time"}))
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
        result = await api.get(f"/activity/{activity_id}/best-efforts", {**base, key: value})
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
            _effort_row(effort, stream, time_data, rank, {key: value})
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
        "Positions are h:mm:ss from the time stream (recording pauses excluded)."
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
    activity_ids: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sport_types: str | None = None,
    gear_id: str | None = None,
    stream: str = "watts",
    durations: str = "60,300,1200",
    limit: int = 10,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
) -> str:
    """Compare the best efforts (e.g. 1 / 5 / 20 min power) of several activities side by side

    Builds a matrix of activities (rows: date, name, id, sport, gear name, FTP at the time)
    by durations (columns: best average of the stream) and names the best activity per
    duration. Activities come from a comma-separated id list or from a date range (default
    the last 90 days) and can be narrowed by sport type and gear id. Use it to see how peak
    power developed over a block, to compare races, or to check whether a new bike / power
    meter reads differently. Different bikes may have different power meters; nothing is
    calibrated. One API call per activity and duration (at most 25 activities), plus one to
    list the activities (or one per id) and one for the gear catalog.

    Args:
        activity_ids: Comma-separated activity IDs (optional; otherwise the date range is used)
        start_date: Start date YYYY-MM-DD (optional, default 90 days before end_date)
        end_date: End date YYYY-MM-DD (optional, default today)
        sport_types: Comma-separated sport types to keep, e.g. "Ride,VirtualRide" (optional)
        gear_id: Keep only activities done on this gear id (optional)
        stream: Stream to evaluate: watts (default), heartrate, velocity_smooth ...
        durations: Comma-separated durations in seconds (optional, default "60,300,1200")
        limit: Maximum number of activities, newest first, 1-25 (optional, default 10)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    duration_list = _numbers(durations, "durations")
    if isinstance(duration_list, str):
        return duration_list
    if not duration_list:
        return "Error: at least one duration is required."
    date_error = _validate_optional_dates(start_date, end_date)
    if date_error:
        return date_error
    capped = min(max(limit, 1), MAX_COMPARE_ACTIVITIES)

    api = _Api(api_key)
    activities, error, source = await _collect_activities(
        api, athlete_id_to_use, activity_ids=activity_ids, start_date=start_date, end_date=end_date
    )
    if error:
        return error
    selected = _select_activities(
        activities, sport_types=sport_types, gear_id=gear_id, start_date=start_date, end_date=end_date, limit=capped
    )
    filters = _filter_text(sport_types, gear_id, start_date, end_date)
    if not selected:
        return f"No activities found ({source}{'; ' + filters if filters else ''})."
    gear_map = await get_gear_map(athlete_id=athlete_id_to_use, api_key=api_key)

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for activity in selected:
        efforts: list[dict[str, Any]] = []
        for duration in duration_list:
            result = await api.get(
                f"/activity/{activity.get('id')}/best-efforts",
                {"stream": stream, "duration": duration, "count": 1},
            )
            error = _error(result, "best efforts")
            if error:
                errors.append(error)
            efforts.append({"duration": duration, "average": _best_average(result)})
        rows.append({**_activity_json(activity, gear_map), "efforts": efforts})
    if errors and len(errors) == len(selected) * len(duration_list):
        return errors[0]

    units = STREAM_UNITS.get(stream, "")
    best: list[dict[str, Any]] = []
    for position, duration in enumerate(duration_list):
        candidates = [(row["efforts"][position]["average"], row) for row in rows if row["efforts"][position]["average"] is not None]
        if candidates:
            value, row = max(candidates, key=lambda item: item[0])
            best.append({"duration": duration, "average": value, "activity_id": row["id"], "name": row["name"], "date": row["date"]})
        else:
            best.append({"duration": duration, "average": None, "activity_id": None, "name": None, "date": None})

    if output_format.strip().lower() == "json":
        payload = {
            "athlete_id": athlete_id_to_use, "stream": stream, "units": units, "durations": duration_list,
            "source": source, "limit": capped, "activities": rows, "best": best, "api_calls": api.calls,
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
        cells = [_fmt(effort["average"], 0, units) for effort in row["efforts"]]
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
            for label, item in zip(labels, best, strict=True)
        )
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
    params: dict[str, Any] = {
        "minSecs": min_secs, "maxSecs": max_secs, "minIntensity": min_intensity, "maxIntensity": max_intensity,
        "limit": limit,
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


def _interval_search_block(activity: dict[str, Any], gear_map: dict[str, str]) -> str:
    summary = activity.get("interval_summary")
    summary_text = "; ".join(str(s) for s in summary) if isinstance(summary, list) and summary else "n/a"
    details = [
        f"intervals: {summary_text}",
        f"moving {hms(activity.get('moving_time'))}",
        f"load {_fmt(_num(activity.get('icu_training_load')))}",
        f"intensity {_fmt(_num(activity.get('icu_intensity')), 0, '%')}",
        f"FTP {_fmt(_num(activity.get('icu_ftp')), 0, 'W')}",
        f"gear {_gear_label(activity, gear_map)}",
        f"compliance {_fmt(_num(activity.get('compliance')), 0, '%')}",
    ]
    return f"{_activity_label(activity)}\n  " + ", ".join(details)


@tool("read")
async def find_similar_intervals(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    min_secs: int,
    max_secs: int,
    min_intensity: float,
    max_intensity: float,
    target: str | None = None,
    min_reps: int | None = None,
    max_reps: int | None = None,
    limit: int = 20,
    start_date: str | None = None,
    end_date: str | None = None,
    sport_types: str | None = None,
    gear_id: str | None = None,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
) -> str:
    """Find activities containing intervals of a given length and intensity (% of FTP)

    Wraps the Intervals.icu interval search: activities with WORK intervals between min_secs
    and max_secs long at min_intensity-max_intensity percent of FTP, optionally restricted to
    a workout target (POWER, HR or PACE; this is the target type, not the sport) and a repeat
    count. Use it to find comparable sessions ("all rides with 3 or more 8-12 min efforts at
    95-105%") before comparing them with compare_workouts or get_activity_intervals. The API
    has no date, sport or gear filter, so those are applied here on the returned list (more
    results are requested from the API when such a filter is set). Per activity: date, sport,
    name, id, the interval summary, moving time, load, intensity, FTP at the time, gear name
    and compliance. One API call (plus one for the gear catalog).

    Args:
        min_secs: Minimum interval length in seconds
        max_secs: Maximum interval length in seconds
        min_intensity: Minimum intensity in % of FTP (0-300)
        max_intensity: Maximum intensity in % of FTP (0-300)
        target: Workout target type POWER, HR or PACE (optional)
        min_reps: Minimum number of matching repetitions in the activity (optional)
        max_reps: Maximum number of matching repetitions in the activity (optional)
        limit: Maximum number of activities to return, newest first (optional, default 20)
        start_date: Keep only activities on or after this local date YYYY-MM-DD (optional)
        end_date: Keep only activities on or before this local date YYYY-MM-DD (optional)
        sport_types: Comma-separated sport types to keep, e.g. "Ride,VirtualRide" (optional)
        gear_id: Keep only activities done on this gear id (optional)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    date_error = _validate_optional_dates(start_date, end_date)
    if date_error:
        return date_error
    filters = _filter_text(sport_types, gear_id, start_date, end_date)
    api_limit = min(limit * 5, MAX_SEARCH_RESULTS) if filters else limit
    params = _interval_search_params(
        min_secs, max_secs, min_intensity, max_intensity, target, min_reps, max_reps, max(api_limit, limit)
    )
    if isinstance(params, str):
        return params

    api = _Api(api_key)
    result = await api.get(f"/athlete/{athlete_id_to_use}/activities/interval-search", params)
    error = _error(result, "interval search")
    if error:
        return error
    found = _list_of_dicts(result)
    selected = _select_activities(
        found, sport_types=sport_types, gear_id=gear_id, start_date=start_date, end_date=end_date, limit=limit
    )
    gear_map = await get_gear_map(athlete_id=athlete_id_to_use, api_key=api_key) if selected else {}

    criteria = (
        f"{min_secs}-{max_secs} s at {min_intensity:g}-{max_intensity:g}% of FTP"
        + (f", target {params['type']}" if "type" in params else "")
        + (f", reps {min_reps if min_reps is not None else 'any'}-{max_reps if max_reps is not None else 'any'}"
           if min_reps is not None or max_reps is not None else "")
    )
    if output_format.strip().lower() == "json":
        payload = {
            "athlete_id": athlete_id_to_use, "criteria": criteria, "api_params": params,
            "returned_by_api": len(found), "filters": filters or None,
            "activities": [
                {
                    **_activity_json(activity, gear_map),
                    "rpe": activity.get("icu_rpe"), "feel": activity.get("feel"),
                    "compliance": activity.get("compliance"), "interval_summary": activity.get("interval_summary"),
                }
                for activity in selected
            ],
            "api_calls": api.calls,
        }
        return json.dumps(payload, ensure_ascii=False)
    lines = [f"Interval search for athlete {athlete_id_to_use}: {criteria}, limit {limit}"]
    if filters:
        lines.append(f"API returned {len(found)} activities; client-side filters ({filters}) keep {len(selected)}.")
    else:
        lines.append(f"API returned {len(found)} activities, newest first.")
    lines.extend(_interval_search_block(activity, gear_map) for activity in selected)
    if not selected:
        lines.append("No matching activities.")
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
    activity_id: str,
    metric: str = "power",
    bucket_size: int | None = None,
    api_key: str | None = None,
    output_format: str = "text",
) -> str:
    """Time-in-bucket histogram of an activity for power, heart rate, pace or grade-adjusted pace

    Returns how many seconds (and what percentage of the recorded time) the activity spent in
    each power (W), heart rate (bpm), pace or GAP bucket, as computed by Intervals.icu. Use it
    to see how polarised a ride was, how much time was spent above FTP, or the pace
    distribution of a run independent of the zone definitions. Pace and GAP buckets are speeds
    in m/s and are shown with the matching pace range. The bucket size can be chosen for power
    and heart rate only. One API call.

    Args:
        activity_id: The Intervals.icu activity ID
        metric: "power" (default), "hr", "pace" or "gap"
        bucket_size: Bucket width in W (power, default 25) or bpm (hr, default 5) (optional)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
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
    api = _Api(api_key)
    result = await api.get(f"/activity/{activity_id}/{endpoint}", {"bucketSize": width} if width else None)
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
def _work_summary(intervals: list[dict[str, Any]]) -> dict[str, Any]:
    """Count, total time and simple means of the interval averages over the WORK intervals."""

    def values(key: str) -> list[float]:
        return [v for i in intervals if (v := _num(i.get(key))) is not None]

    elapsed = values("elapsed_time") or values("moving_time")
    watts, hrs = values("average_watts"), values("average_heartrate")
    mean_w, mean_hr = _mean(watts), _mean(hrs)
    return {
        "work_intervals": len(intervals),
        "work_time": sum(elapsed) if elapsed else None,
        "avg_watts": mean_w,
        "np": _mean(values("weighted_average_watts")),
        "avg_hr": mean_hr,
        "cadence": _mean(values("average_cadence")),
        "pw_hr": mean_w / mean_hr if mean_w is not None and mean_hr else None,
    }


def _workout_row(activity: dict[str, Any], intervals: list[dict[str, Any]]) -> dict[str, Any]:
    row = {
        "id": activity.get("id"), "name": activity.get("name"), "date": _day(activity), "type": activity.get("type"),
        **_work_summary(intervals),
        "training_load": _num(activity.get("icu_training_load")),
        "intensity": _num(activity.get("icu_intensity")),
        "ftp": _num(activity.get("icu_ftp")),
        "rpe": _num(activity.get("icu_rpe")),
        "feel": _num(activity.get("feel")),
    }
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in row.items()}


def _column_text(row: dict[str, Any], key: str, digits: int, units: str) -> str:
    if key == "work_time":
        return hms(row.get(key))
    return _fmt(row.get(key), digits, units)


def _changes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    first, last = rows[0], rows[-1]
    changes = []
    for key, label, digits, units in WORKOUT_COLUMNS:
        a, b = first.get(key), last.get(key)
        if a is None or b is None:
            continue
        pct = _pct_change(a, b)
        changes.append({
            "key": key, "label": label, "first": a, "last": b, "diff": round(b - a, 2),
            "pct": round(pct, 1) if pct is not None else None,
            "text": f"{label} {_column_text(first, key, digits, units)} -> {_column_text(last, key, digits, units)}"
            + (f" ({pct:+.1f} %)" if pct is not None else ""),
        })
    return changes


@tool("read")
async def compare_workouts(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    query: str | None = None,
    activity_ids: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sport_types: str | None = None,
    limit: int = 8,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
) -> str:
    """Compare repeated executions of the same workout over time (WORK intervals per session)

    Collects activities by name search (e.g. "Sweet Spot" or a tag such as "#threshold"), by
    id list or by date range (default the last 90 days), optionally narrowed by sport, and
    summarises the WORK intervals of each: count, total work time, mean average power, mean
    normalised power, mean heart rate, mean cadence and Pw:HR (mean W / mean HR), plus the
    activity's load, intensity, FTP at the time, RPE and feel. The table is chronological
    (oldest first) and a "change first -> last" line is given per column. Means are simple
    means of the per-interval averages, not time-weighted; use it to see whether the same
    session is being done at higher power, lower HR or lower RPE. One API call to collect
    the activities (or one per id) plus one per activity for its intervals (at most 12).

    Args:
        query: Text to match in activity names, tags with leading # (optional)
        activity_ids: Comma-separated activity IDs (optional)
        start_date: Start date YYYY-MM-DD (optional, default 90 days before end_date; also filters query results)
        end_date: End date YYYY-MM-DD (optional, default today)
        sport_types: Comma-separated sport types to keep, e.g. "Ride,VirtualRide" (optional)
        limit: Maximum number of activities, newest first, 1-12 (optional, default 8)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    date_error = _validate_optional_dates(start_date, end_date)
    if date_error:
        return date_error
    capped = min(max(limit, 1), MAX_WORKOUTS)

    api = _Api(api_key)
    activities, error, source = await _collect_activities(
        api, athlete_id_to_use, activity_ids=activity_ids, query=query, start_date=start_date,
        end_date=end_date, fetch_limit=min(capped * 4, MAX_SEARCH_RESULTS),
    )
    if error:
        return error
    selected = _select_activities(
        activities, sport_types=sport_types, start_date=start_date, end_date=end_date, limit=capped
    )
    filters = _filter_text(sport_types, None, start_date, end_date)
    if not selected:
        return f"No activities found ({source}{'; ' + filters if filters else ''})."
    rows: list[dict[str, Any]] = []
    for activity in reversed(selected):
        intervals, error = await _work_intervals(api, activity.get("id"))
        if error:
            return error
        rows.append(_workout_row(activity, intervals))
    changes = _changes(rows) if len(rows) > 1 else []

    if output_format.strip().lower() == "json":
        payload = {
            "athlete_id": athlete_id_to_use, "source": source, "filters": filters or None, "limit": capped,
            "columns": [key for key, _, _, _ in WORKOUT_COLUMNS], "activities": rows,
            "change_first_to_last": [{k: v for k, v in c.items() if k != "text"} for c in changes],
            "api_calls": api.calls,
        }
        return json.dumps(payload, ensure_ascii=False)
    lines = [
        f"Workout comparison for athlete {athlete_id_to_use} (source {source}{'; ' + filters if filters else ''}; "
        f"{len(rows)} of {len(activities)} activities, oldest first; limit {capped}"
        + (f", capped from {limit}" if limit > capped else "") + "):",
        "WORK intervals per activity: count, total time, simple means of the interval averages "
        "(power, NP, HR, cadence) and Pw:HR = mean W / mean HR; load/IF/FTP/RPE/feel from the activity.",
        "Date | Activity | " + " | ".join(label for _, label, _, _ in WORKOUT_COLUMNS),
    ]
    for row in rows:
        cells = [_column_text(row, key, digits, units) for key, _, digits, units in WORKOUT_COLUMNS]
        lines.append(f"{row['date']} | '{row['name']}' ({row['id']}, {row['type']}) | " + " | ".join(cells))
    if changes:
        lines.append(
            f"Change first -> last ({rows[0]['date']} -> {rows[-1]['date']}): "
            + "; ".join(change["text"] for change in changes)
        )
    lines.append(f"API calls: {api.calls}")
    return "\n".join(lines)


# ------------------------------------------------------- power:HR efficiency
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
    return bands or "Error: at least one power band is required."


def _band_label(band: tuple[float, float]) -> str:
    return f"{band[0]:g}-{band[1]:g} W"


def _band_stats(
    intervals: list[dict[str, Any]], bands: list[tuple[float, float]], min_secs: int
) -> dict[str, dict[str, Any]]:
    """Per band: n, mean watts, mean HR and W/bpm of the steady WORK intervals falling into it."""
    stats: dict[str, dict[str, Any]] = {}
    for band in bands:
        hits: list[tuple[float, float]] = []
        for interval in intervals:
            secs = _num(interval.get("elapsed_time"))
            watts, hr = _num(interval.get("average_watts")), _num(interval.get("average_heartrate"))
            if secs is None or secs < min_secs or watts is None or hr is None or hr <= 0:
                continue
            if band[0] <= watts < band[1]:
                hits.append((watts, hr))
        if hits:
            mean_w = sum(w for w, _ in hits) / len(hits)
            mean_hr = sum(h for _, h in hits) / len(hits)
            stats[_band_label(band)] = {
                "n": len(hits), "watts": round(mean_w, 1), "hr": round(mean_hr, 1),
                "w_per_bpm": round(mean_w / mean_hr, 3),
            }
    return stats


def _band_trend(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """Oldest third vs newest third (mean W/bpm) of the activities with data in the band."""
    series = [row["bands"][label]["w_per_bpm"] for row in rows if label in row["bands"]]
    if len(series) < 2:
        return {"band": label, "activities": len(series), "note": "not enough activities with data in this band"}
    third = max(1, len(series) // 3)
    oldest, newest = _mean(series[:third]), _mean(series[-third:])
    change = _pct_change(oldest, newest)
    return {
        "band": label, "activities": len(series), "group_size": third,
        "oldest_mean": round(oldest, 3) if oldest is not None else None,
        "newest_mean": round(newest, 3) if newest is not None else None,
        "change_pct": round(change, 1) if change is not None else None,
    }


@tool("read")
async def get_power_hr_efficiency(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    start_date: str | None = None,
    end_date: str | None = None,
    sport_types: str = "Ride,GravelRide,VirtualRide",
    power_bands: str = "150-200,200-250,250-300",
    min_interval_secs: int = 300,
    limit: int = 30,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
) -> str:
    """Power-to-heart-rate ratio (W per bpm) per power band across steady intervals over time

    Takes the activities of the date range (default the last 90 days) for the given sports,
    fetches their intervals and puts every WORK interval of at least min_interval_secs with
    both power and heart rate into the power band matching its average power. Per activity
    and band it reports the number of intervals, mean watts, mean HR and W/bpm; per band it
    then compares the mean W/bpm of the oldest third of activities with data against the
    newest third. A higher W/bpm at the same power usually means lower HR for the same output,
    but heat, fatigue, hydration, cadence, indoor/outdoor and power meter differences between
    bikes all move the ratio, so this is a statistical comparison, not a fitness verdict. One
    API call to list the activities plus one per activity (at most 60) and one for the gear
    catalog.

    Args:
        start_date: Start date YYYY-MM-DD (optional, default 90 days before end_date)
        end_date: End date YYYY-MM-DD (optional, default today)
        sport_types: Comma-separated sport types (optional, default "Ride,GravelRide,VirtualRide")
        power_bands: Comma-separated bands in W as "low-high" (optional, default "150-200,200-250,250-300")
        min_interval_secs: Minimum WORK interval length in seconds (optional, default 300)
        limit: Maximum number of activities, newest first, 1-60 (optional, default 30)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    bands = _bands(power_bands)
    if isinstance(bands, str):
        return bands
    if min_interval_secs <= 0:
        return "Error: min_interval_secs must be positive."
    span = _resolve_range(start_date, end_date, DEFAULT_RANGE_DAYS)
    if isinstance(span, str):
        return span
    capped = min(max(limit, 1), MAX_EFFICIENCY_ACTIVITIES)

    api = _Api(api_key)
    activities, error, _ = await _collect_activities(api, athlete_id_to_use, start_date=span[0], end_date=span[1])
    if error:
        return error
    selected = _select_activities(activities, sport_types=sport_types, limit=capped)
    if not selected:
        return f"No activities found between {span[0]} and {span[1]} for sports {sport_types}."
    gear_map = await get_gear_map(athlete_id=athlete_id_to_use, api_key=api_key)
    rows: list[dict[str, Any]] = []
    for activity in reversed(selected):
        intervals, error = await _work_intervals(api, activity.get("id"))
        if error:
            return error
        rows.append({**_activity_json(activity, gear_map), "bands": _band_stats(intervals, bands, min_interval_secs)})
    labels = [_band_label(band) for band in bands]
    trends = [_band_trend(rows, label) for label in labels]

    if output_format.strip().lower() == "json":
        payload = {
            "athlete_id": athlete_id_to_use, "start": span[0], "end": span[1], "sport_types": sport_types,
            "bands": labels, "min_interval_secs": min_interval_secs, "limit": capped,
            "activities": rows, "trends": trends, "note": EFFICIENCY_NOTE, "api_calls": api.calls,
        }
        return json.dumps(payload, ensure_ascii=False)
    lines = [
        f"Power:HR efficiency for athlete {athlete_id_to_use}, {span[0]} to {span[1]}, sports {sport_types}: "
        f"{len(rows)} activities (oldest first; limit {capped}{', capped from ' + str(limit) if limit > capped else ''}), "
        f"WORK intervals of at least {hms(min_interval_secs)} with power and HR, bands {', '.join(labels)}.",
    ]
    for row in rows:
        gear = f"{row['gear_name']} ({row['gear_id']})" if row["gear_name"] else (row["gear_id"] or "no gear")
        cells = [
            f"{label}: {s['n']} x {s['watts']:.0f} W / {s['hr']:.0f} bpm = {s['w_per_bpm']:.2f} W/bpm"
            for label in labels
            if (s := row["bands"].get(label))
        ]
        lines.append(
            f"{row['date']} {row['type']} '{row['name']}' ({row['id']}), gear {gear}, FTP {_fmt(_num(row['ftp']), 0, 'W')}: "
            + ("; ".join(cells) if cells else "no qualifying intervals")
        )
    lines.append("Trend per band (mean W/bpm of the oldest third vs the newest third of the activities with data):")
    for trend in trends:
        if "note" in trend:
            lines.append(f"  {trend['band']}: {trend['note']} ({trend['activities']})")
            continue
        change = f"{trend['change_pct']:+.1f} %" if trend["change_pct"] is not None else "n/a"
        lines.append(
            f"  {trend['band']}: {trend['oldest_mean']:.2f} -> {trend['newest_mean']:.2f} W/bpm ({change}; "
            f"{trend['group_size']} of {trend['activities']} activities per group)"
        )
    lines.append(EFFICIENCY_NOTE)
    lines.append(f"API calls: {api.calls} {GEAR_CALL_NOTE}")
    return "\n".join(lines)


# ---------------------------------------------------------- fatigue resistance
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


def _fatigue_rows(block: dict[str, Any], durations: list[int]) -> list[dict[str, Any]]:
    rows = []
    for duration in durations:
        fresh, secs_used = _curve_lookup(block["fresh"], duration)
        row: dict[str, Any] = {"duration": duration, "secs_used": secs_used, "fresh": fresh}
        for key in ("kj0", "kj1"):
            value, _ = _curve_lookup(block[key], duration)
            row[key] = value
            drop = _pct_change(fresh, value)
            row[f"{key}_change_pct"] = round(drop, 1) if drop is not None else None
        rows.append(row)
    return rows


async def _kj_thresholds(api: _Api, athlete_id: str, activity_type: str) -> tuple[dict[str, Any] | None, str]:
    """after_kj0 / after_kj1 / ftp of the sport setting covering activity_type and a note."""
    result = await api.get(f"/athlete/{athlete_id}/sport-settings")
    error = _error(result, "sport settings")
    if error:
        return None, f"{error}; kJ thresholds unknown."
    wanted = activity_type.strip().lower()
    for setting in _list_of_dicts(result):
        types = [str(t).lower() for t in (setting.get("types") or [])]
        if wanted in types:
            thresholds = {k: setting.get(k) for k in ("after_kj0", "after_kj1", "ftp")}
            kj0, kj1 = thresholds["after_kj0"], thresholds["after_kj1"]
            if kj0 is None and kj1 is None:
                return thresholds, (
                    f"Fatigued power curves are not configured for {activity_type} in Intervals.icu "
                    "(Settings -> Power -> 'after kJ' is empty), so the kJ0 and kJ1 curves equal the fresh curve."
                )
            parts = [
                f"after kJ0 = {kj0} kJ" if kj0 is not None else "after kJ0 not configured (equals the fresh curve)",
                f"after kJ1 = {kj1} kJ" if kj1 is not None else "after kJ1 not configured (equals the fresh curve)",
            ]
            return thresholds, f"Sport setting for {activity_type}: " + ", ".join(parts) + "."
    return None, f"No sport setting covers '{activity_type}'; kJ thresholds unknown."


def _fatigue_table(block: dict[str, Any], rows: list[dict[str, Any]], thresholds: dict[str, Any] | None) -> list[str]:
    kj0 = (thresholds or {}).get("after_kj0")
    kj1 = (thresholds or {}).get("after_kj1")
    lines = [
        f"{block['label']}:",
        "  Duration | fresh | " + (f"after {kj0} kJ" if kj0 is not None else "kJ0 (not configured)")
        + " | " + (f"after {kj1} kJ" if kj1 is not None else "kJ1 (not configured)"),
    ]
    for row in rows:
        cells = [_fmt(row["fresh"], 0, "W")]
        for key in ("kj0", "kj1"):
            cell = _fmt(row[key], 0, "W")
            if row[f"{key}_change_pct"] is not None:
                cell += f" ({row[f'{key}_change_pct']:+.1f} %)"
            cells.append(cell)
        used = f" (curve point {row['secs_used']} s)" if row["secs_used"] not in (None, row["duration"]) else ""
        lines.append(f"  {_duration_label(row['duration'])}{used} | " + " | ".join(cells))
    return lines


@tool("read")
async def get_fatigue_resistance(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements,too-many-branches
    activity_id: str | None = None,
    activity_type: str = "Ride",
    durations: str = "60,300,1200",
    curves: str = "42d",
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
) -> str:
    """Fatigue resistance: best power fresh vs after the athlete's kJ thresholds (kJ0, kJ1)

    Intervals.icu keeps, besides the normal power curve, two "fatigued" curves built only from
    efforts that started after a configurable amount of work (Settings -> Power -> "after kJ",
    stored as after_kj0 / after_kj1 in the sport settings). With an activity_id the tool
    compares that activity's fresh curve with its kJ0 and kJ1 curves; without it the athlete
    curves for each id in `curves` (e.g. 42d, 90d, s0 for this season, 1y) are compared. The
    table shows watts per duration for fresh / after kJ0 / after kJ1 and the change in %. When
    the thresholds are not configured the fatigued curves equal the fresh curve and the tool
    says so. Three API calls per activity (plus one for the sport settings) or one for the
    athlete curves plus one for the sport settings.

    Args:
        activity_id: The Intervals.icu activity ID (optional; without it the athlete curves are used)
        activity_type: Sport whose curves and kJ thresholds apply (optional, default "Ride")
        durations: Comma-separated durations in seconds (optional, default "60,300,1200")
        curves: Comma-separated athlete curve ids such as "42d,90d,s0" (optional, default "42d")
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
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

    api = _Api(api_key)
    blocks: list[dict[str, Any]] = []
    if activity_id:
        block: dict[str, Any] = {"id": activity_id, "label": f"Activity {activity_id} ({activity_type})"}
        for key, fatigue in (("fresh", None), ("kj0", "kj0"), ("kj1", "kj1")):
            result = await api.get(
                f"/activity/{activity_id}/power-curve.json", {"fatigue": fatigue} if fatigue else None
            )
            error = _error(result, f"power curve ({key})")
            if error:
                return error
            block[key] = result if isinstance(result, dict) else {}
        blocks.append(block)
    else:
        wanted = [cid for curve_id in curve_ids for cid in (curve_id, f"{curve_id}-kj0", f"{curve_id}-kj1")]
        result = await api.get(
            f"/athlete/{athlete_id_to_use}/power-curves.json",
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
                "fresh": fresh, "kj0": by_id.get(f"{curve_id}-kj0", {}), "kj1": by_id.get(f"{curve_id}-kj1", {}),
            })
    thresholds, note = await _kj_thresholds(api, athlete_id_to_use, activity_type)
    tables = [(block, _fatigue_rows(block, duration_list)) for block in blocks]

    if output_format.strip().lower() == "json":
        payload = {
            "athlete_id": athlete_id_to_use, "activity_id": activity_id, "activity_type": activity_type,
            "thresholds": thresholds, "note": note, "durations": duration_list,
            "curves": [{"id": block["id"], "label": block["label"], "rows": rows} for block, rows in tables],
            "api_calls": api.calls,
        }
        return json.dumps(payload, ensure_ascii=False)
    lines = [f"Fatigue resistance for athlete {athlete_id_to_use}: power fresh vs after kJ thresholds ({activity_type}).", note]
    for block, rows in tables:
        lines.extend(_fatigue_table(block, rows, thresholds))
    lines.append("Change in % is relative to the fresh curve; 'n/a' means the curve has no point near that duration.")
    lines.append(f"API calls: {api.calls}")
    return "\n".join(lines)
