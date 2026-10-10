"""
Analysis MCP tools for Intervals.icu: dual power meter comparison (one ride or several) and
planned-vs-executed workout analysis. Climb/descent segmentation lives in tools/climbs.py.

All tools are read-only; they combine the activity, its intervals, its streams and (when
paired) the planned workout, and compute statistics from recorded samples only.
"""

import asyncio
import difflib
import json
from datetime import date, timedelta
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.athlete import assigned_field_ids
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.tools.gear import get_gear_raw
from intervals_mcp_server.tools.performance import _resolve_range, cap_ids, ids_note  # pylint: disable=protected-access
from intervals_mcp_server.utils.custom_fields import (
    ACTIVITY_FIELD,
    ACTIVITY_STREAM,
    assigned_codes,
    custom_fields_json,
    format_custom_field_lines,
)
from intervals_mcp_server.utils.execution import DETAIL_LEVELS, Tolerances, analyze, format_execution, plan_steps
from intervals_mcp_server.utils.power_compare import (
    compare_power_streams as compute_power_comparison,
    comparison_to_json,
    format_power_comparison,
    format_rides_summary,
    ride_summary,
    summarize_rides,
)
from intervals_mcp_server.utils.sports import format_start_times, hms, is_indoor, start_times
from intervals_mcp_server.utils.streams import find_stream
from intervals_mcp_server.utils.validation import resolve_athlete_id

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

PACE_SPORTS = ("Run", "TrailRun", "VirtualRun", "Walk", "Hike", "Swim", "OpenWaterSwim")
CORE_STREAMS = ("time", "watts", "heartrate", "cadence", "velocity_smooth", "distance", "altitude")


async def _get_activity(activity_id: str, api_key: str | None) -> tuple[dict[str, Any] | None, str | None]:
    result = await make_intervals_request(url=f"/activity/{seg(activity_id)}", api_key=api_key)
    if isinstance(result, dict) and "error" in result:
        return None, f"Error fetching activity details: {result.get('message', 'Unknown error')}"
    activity = result[0] if isinstance(result, list) and result else result
    if not isinstance(activity, dict) or not activity:
        return None, f"No details found for activity {activity_id}."
    return activity, None


async def _get_streams(
    activity_id: str, api_key: str | None, types: list[str] | None
) -> tuple[list[dict[str, Any]], str | None]:
    params = {"types": ",".join(types)} if types else None
    result = await make_intervals_request(
        url=f"/activity/{seg(activity_id)}/streams", api_key=api_key, params=params
    )
    if isinstance(result, dict) and "error" in result:
        return [], f"Error fetching activity streams: {result.get('message', 'Unknown error')}"
    streams = [s for s in result if isinstance(s, dict)] if isinstance(result, list) else []
    return streams, None if streams else f"No stream data found for activity {activity_id}."


async def _defs(item_type: str, api_key: str | None, athlete_id: Any) -> dict[str, dict[str, Any]]:
    athlete = str(athlete_id) if athlete_id else config.athlete_id
    if not athlete:
        return {}
    return (await get_custom_item_index(athlete_id=athlete, api_key=api_key)).get(item_type, {})


def _activity_header(activity: dict[str, Any]) -> str:
    return (
        f"{activity.get('name', 'Unnamed')} ({activity.get('id')}, {activity.get('type', '?')}, "
        f"{format_start_times(activity)})"
    )


# ------------------------------------------------------------------ power meters
POWER_RIDES_DEFAULT_DAYS = 180
MAX_POWER_RIDES = 20
POWER_LIST_FIELDS = (
    "id,name,type,start_date_local,gear,stream_types,power_field_names,device_name,power_meter,"
    "power_meter_serial,power_field,trainer"
)


def _device_text(activity: dict[str, Any]) -> str:
    device = ", ".join(
        f"{label} {activity[key]}"
        for key, label in (
            ("device_name", "device"),
            ("power_meter", "power meter"),
            ("power_meter_serial", "serial"),
            ("power_field", "primary power field"),
        )
        if activity.get(key)
    )
    fields = activity.get("power_field_names")
    if isinstance(fields, list) and fields:
        device += f"; power fields in file: {', '.join(str(f) for f in fields)}"
    return device


def _has_stream(activity: dict[str, Any], stream_type: str) -> bool:
    """True when the activity lists the stream (or, for secondary_power, a second power field)."""
    types = activity.get("stream_types")
    if isinstance(types, list) and types:
        return stream_type in types
    fields = activity.get("power_field_names")
    return stream_type == "secondary_power" and isinstance(fields, list) and len(fields) > 1


def _secondary_identity(activity: dict[str, Any], secondary: str) -> str:
    """Label of the second power source: its field name in the file when known; the device is never in the activity data."""
    if secondary != "secondary_power":
        return f"stream '{secondary}' (device not identified)"
    fields = [str(f) for f in activity.get("power_field_names") or [] if f]
    primary_field = str(activity.get("power_field") or (fields[0] if fields else ""))
    others = [f for f in fields if f != primary_field]
    if others:
        return f"file field '{others[0]}' (device not identified)"
    return "'secondary_power' (field and device not identified)"


def _meter_identity(activity: dict[str, Any], gear_items: list[dict[str, Any]]) -> tuple[str, str]:
    """(gear label, primary power meter label) from the file's device data or the bike's gear components."""
    gear = activity.get("gear")
    gear_id = str(gear.get("id")) if isinstance(gear, dict) and gear.get("id") else None
    by_id = {str(item.get("id")): item for item in gear_items if isinstance(item, dict)}
    bike = by_id.get(gear_id or "")
    gear_label = f"{bike.get('name')} ({gear_id})" if bike and bike.get("name") else (gear_id or "no gear")
    if activity.get("power_meter"):
        meter = str(activity["power_meter"]) + (f" #{activity['power_meter_serial']}" if activity.get("power_meter_serial") else "")
        return gear_label, f"{meter} (file)"
    components = [
        str(by_id[str(cid)].get("name")) for cid in (bike or {}).get("component_ids") or []
        if str(cid) in by_id and str(by_id[str(cid)].get("type")) == "PowerMeter" and not by_id[str(cid)].get("retired")
    ]
    if components:
        return gear_label, f"{', '.join(components)} (gear component)"
    return gear_label, "not identified"


async def _power_rides(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    athlete_id: str, api_key: str | None, activity_ids: list[str], start_date: str | None,
    end_date: str | None, secondary: str,
) -> tuple[list[dict[str, Any]], str | None, str]:
    """Activities for the multi-ride comparison (by id - already de-duplicated and capped - or by date range
    with the second stream, newest first)."""
    if activity_ids:
        rides = []
        for activity_id in activity_ids:
            activity, error = await _get_activity(activity_id, api_key)
            if error or activity is None:
                return [], error, ""
            rides.append(activity)
        return rides, None, f"{len(rides)} activity id(s)"
    span = _resolve_range(start_date, end_date, POWER_RIDES_DEFAULT_DAYS)
    if isinstance(span, str):
        return [], span, ""
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id}/activities", api_key=api_key,
        params={"oldest": span[0], "newest": span[1], "fields": POWER_LIST_FIELDS},
    )
    if isinstance(result, dict) and "error" in result:
        return [], f"Error fetching activities: {result.get('message', 'Unknown error')}", ""
    activities = [a for a in result if isinstance(a, dict)] if isinstance(result, list) else []
    rides = [a for a in activities if _has_stream(a, secondary)]
    rides.sort(key=lambda a: str(a.get("start_date_local") or ""), reverse=True)
    return rides, None, f"{span[0]} to {span[1]}, {len(rides)} of {len(activities)} activities with '{secondary}'"


def _ride_line(ride: dict[str, Any]) -> str:
    s = ride["summary"]
    lag = s["best_lag_s"]
    return (
        f"{ride['date']} '{ride['name']}' ({ride['id']}), {ride['gear']}, {ride['environment']}, primary {ride['meter']}, "
        f"secondary {ride['secondary_source']}: {s['used']} pairs, "
        f"mean primary {_fmt_num(s['mean_primary_w'])} W, diff {_fmt_num(s['mean_diff_pct'], 2)}% (median "
        f"{_fmt_num(s['median_diff_pct'], 2)}%, sd {_fmt_num(s['stdev_diff_pct'], 2)}%), stable windows "
        f"{_fmt_num(s['stable_diff_pct'], 2, '%')} (n {s['stable_windows'] or 0}), drift last-first quarter "
        f"{_fmt_num(s['drift_last_minus_first_pp'], 2, ' pp')}, lag {_fmt_num(lag, 0, ' s')}, "
        f"outliers {_fmt_num(s['outlier_pct'], 1, '%')}"
    )


def _fmt_num(value: Any, digits: int = 0, unit: str = "") -> str:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return "n/a"
    return f"{value:.{digits}f}{unit}"


async def _compare_rides(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    athlete_id: str, api_key: str | None, primary: str, secondary: str, activity_ids: str | None,
    start_date: str | None, end_date: str | None, limit: int, output_format: str, detail_level: str,
) -> str:
    """Multi-ride mode of compare_power_streams."""
    capped = min(max(limit, 1), MAX_POWER_RIDES)
    ids, dropped, duplicates = cap_ids(activity_ids, capped)
    rides, error, source = await _power_rides(athlete_id, api_key, ids, start_date, end_date, secondary)
    if error:
        return error
    skipped = len(rides) - capped if len(rides) > capped else 0
    rides = rides[:capped]
    source += ids_note(dropped, duplicates, capped)
    if not rides:
        return f"No activities with a '{secondary}' stream found ({source})."
    gear_items = await get_gear_raw(athlete_id=athlete_id, api_key=api_key)
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for activity in rides:
        activity_id = str(activity.get("id"))
        streams, _ = await _get_streams(activity_id, api_key, ["time", primary, secondary])
        first, second, time_stream = (find_stream(streams, t) for t in (primary, secondary, "time"))
        if first is None or second is None:
            missing.append({"id": activity_id, "reason": f"no '{primary if first is None else secondary}' stream returned"})
            continue
        result = compute_power_comparison(first.get("data") or [], second.get("data") or [], (time_stream or {}).get("data") or [])
        gear, meter = _meter_identity(activity, gear_items)
        source_label = _secondary_identity(activity, secondary)
        setting = "indoor" if is_indoor(activity) else "outdoor"
        rows.append({
            "id": activity_id, "name": activity.get("name"), "date": str(activity.get("start_date_local") or "")[:10],
            "type": activity.get("type"), "gear": gear, "environment": setting, "meter": meter, "secondary_source": source_label,
            "device": _device_text(activity), "group": f"{gear} | {setting} | primary {meter} | secondary {source_label}",
            "summary": ride_summary(result), "comparison": result,
        })
    summary = summarize_rides(rows)
    summary["excluded"].extend(missing)
    if any(row["meter"] == "not identified" for row in rows):
        summary["notes"].append(
            "Primary power meter not identified (no power meter in the file data and no PowerMeter component on the bike): "
            "the rides of such a group may come from different meters.")
    if any("device not identified" in row["secondary_source"] for row in rows):
        summary["notes"].append(
            "The second power source is known only by its field name in the file; the activity data does not say which "
            "device recorded it (pedals, trainer, another crank), so groups assume the same device per bike, setting and field.")
    if output_format.strip().lower() == "json":
        keep = ("id", "name", "date", "type", "gear", "environment", "meter", "secondary_source", "device", "group", "summary")
        payload = {
            "mode": "rides", "source": source, "primary": primary, "secondary": secondary, "limit": capped,
            "not_analysed_beyond_limit": skipped, "ids_beyond_limit": dropped, "duplicate_ids_ignored": duplicates,
            "rides": [
                {**{k: row[k] for k in keep}, **({"comparison": comparison_to_json(row["comparison"])} if detail_level == "full" else {})}
                for row in rows
            ] if detail_level != "compact" else [{"id": row["id"], "date": row["date"], "group": row["group"]} for row in rows],
            "between_rides": comparison_to_json(summary),
        }
        return json.dumps(payload, ensure_ascii=False)
    lines = [
        f"Power meter comparison over {len(rows)} ride{'' if len(rows) == 1 else 's'} ({source}; limit {capped}"
        + (f", {skipped} older not analysed" if skipped else "") + f"): '{secondary}' vs '{primary}', "
        "difference = secondary - primary in % of primary, each ride compared on its own (see compare_power_streams "
        "for one ride)."
    ]
    if detail_level != "compact":
        lines.append("Per ride:")
        for row in rows:
            lines.append("  " + _ride_line(row))
            if detail_level == "full":
                bins = ", ".join(f"{label} W {_fmt_num(b['mean_diff_pct'], 2)}% (n {b['n']})" for label, b in row["summary"]["bins"].items())
                lines.append(f"    bins: {bins or 'none'}; device: {row['device'] or 'not available'}")
    lines.append("Between rides (per bike and power meter identity):")
    lines.extend("  " + line for line in format_rides_summary(summary))
    lines.extend(f"Note: {note}" for note in summary["notes"])
    return "\n".join(lines)


@tool("read")
async def compare_power_streams(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    activity_id: str | None = None,
    api_key: str | None = None,
    primary: str = "watts",
    secondary: str = "secondary_power",
    start_index: int | None = None,
    end_index: int | None = None,
    output_format: str = "text",
    activity_ids: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 10,
    detail_level: str = "standard",
    athlete_id: str | None = None,
) -> str:
    """Compare two power streams of an activity sample by sample (e.g. head unit vs. second power meter)

    Compares the primary power stream with a second one recorded on the same activity
    (Intervals.icu stores a second power meter as stream type "secondary_power", shown as
    "Power2" in power_field_names). Reports the overall offset in W and %, the offset per
    power band, stable 30 s / 60 s windows only, drift over the ride quarters, a lag estimate,
    best efforts per duration from each stream and how many samples were excluded as
    coasting, missing or outliers. No calibration is performed and nothing is written; which
    physical sensor each stream belongs to must be taken from the device data shown in the
    header (power meter name/serial), not assumed.

    Without activity_id (or with activity_ids) several rides are compared: the rides of the
    date range (default 180 days) that carry the second stream, each analysed on its own and
    summarised per bike and power meter identity with n, median, between-ride SD and range of
    the offset (overall, per power band, stable windows), drift, lag and outliers. No
    correction factor is derived.

    Args:
        activity_id: The Intervals.icu activity ID (one ride)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        primary: Stream type of the primary power source (default "watts")
        secondary: Stream type of the second power source (default "secondary_power")
        start_index: First sample index to compare (one ride only)
        end_index: Sample index to stop before (one ride only)
        output_format: "text" (default) or "json"
        activity_ids: Comma-separated activity IDs to compare as several rides (optional)
        start_date: Several rides from YYYY-MM-DD (optional, default 180 days before end_date)
        end_date: Several rides until YYYY-MM-DD (optional, default today)
        limit: Several rides: at most this many, newest first, 1-20 (default 10)
        detail_level: Several rides: "compact" (summary only), "standard" (default, plus one line
            per ride) or "full" (plus bins and device data per ride)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
    """
    if not activity_id or activity_ids:
        if start_index is not None or end_index is not None:
            return "Error: start_index / end_index apply to one activity_id only."
        if detail_level not in DETAIL_LEVELS:
            return f"Error: detail_level must be one of {', '.join(DETAIL_LEVELS)}."
        athlete, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
        if error_msg:
            return error_msg
        return await _compare_rides(athlete, api_key, primary, secondary, activity_ids, start_date, end_date,
                                    limit, output_format, detail_level)
    activity, error = await _get_activity(activity_id, api_key)
    if error or activity is None:
        return error or "Error"
    streams, error = await _get_streams(activity_id, api_key, ["time", primary, secondary])
    if error:
        return error
    first, second, time_stream = (find_stream(streams, t) for t in (primary, secondary, "time"))
    if first is None or second is None:
        available = ", ".join(str(t) for t in (activity.get("stream_types") or []))
        return (
            f"Activity {activity_id} has no '{primary if first is None else secondary}' stream. "
            f"Available streams: {available or 'unknown (use list_activity_streams)'}."
        )
    time_data = (time_stream or {}).get("data") or []
    sl = slice(start_index or 0, end_index)
    result = compute_power_comparison(
        (first.get("data") or [])[sl], (second.get("data") or [])[sl], time_data[sl]
    )
    device = _device_text(activity)
    header = (
        f"Power stream comparison for {_activity_header(activity)}\n"
        f"Primary '{primary}' vs secondary '{secondary}'"
        + (f", samples {start_index or 0}-{(end_index or len(time_data)) - 1}" if start_index or end_index else "")
        + f"\nDevice data: {device or 'not available'}\n"
    )
    if output_format.strip().lower() == "json":
        payload = {
            "activity": {"id": activity.get("id"), "name": activity.get("name"), **start_times(activity)},
            "primary": primary,
            "secondary": secondary,
            "device": {k: activity.get(k) for k in ("device_name", "power_meter", "power_meter_serial", "power_field", "power_field_names")},
            "comparison": comparison_to_json(result),
        }
        return json.dumps(payload, ensure_ascii=False)
    return header + "\n" + format_power_comparison(result, primary, secondary)


# ------------------------------------------------------------- workout execution
def tolerances_from_args(
    duration_tolerance_pct: float | None,
    start_tolerance_s: float | None,
    pause_tolerance_s: float | None,
    detail_level: str = "standard",
) -> Tolerances | str:
    """Tolerances of the execution analysis from optional tool arguments, or an error string.

    Also validates the detail level shared by the execution and report tools.
    """
    if detail_level not in DETAIL_LEVELS:
        return f"Error: detail_level must be one of {', '.join(DETAIL_LEVELS)}."
    defaults = Tolerances()
    values = {
        "duration_pct": defaults.duration_pct if duration_tolerance_pct is None else float(duration_tolerance_pct),
        "start_shift_s": defaults.start_shift_s if start_tolerance_s is None else float(start_tolerance_s),
        "pause_s": defaults.pause_s if pause_tolerance_s is None else float(pause_tolerance_s),
    }
    if not 0 <= values["duration_pct"] <= 100:
        return "Error: duration_tolerance_pct must be between 0 and 100."
    if values["start_shift_s"] < 0 or values["pause_s"] < 0:
        return "Error: start_tolerance_s and pause_tolerance_s must not be negative."
    return Tolerances(duration_pct=values["duration_pct"], start_shift_s=values["start_shift_s"], pause_s=values["pause_s"])


def _threshold_context(activity: dict[str, Any]) -> dict[str, Any]:
    return {
        "ftp": activity.get("icu_ftp"),
        "lthr": activity.get("lthr"),
        "max_hr": activity.get("athlete_max_hr"),
        "threshold_pace": activity.get("threshold_pace"),
        "power_zones": activity.get("icu_power_zones"),
        "hr_zones": activity.get("icu_hr_zones"),
        "pace_zones": activity.get("pace_zones"),
    }


async def _get_event(athlete_id: str, event_id: Any, api_key: str | None) -> dict[str, Any] | None:
    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id)}/events/{seg(event_id)}", api_key=api_key, params={"resolve": "true"}
    )
    return result if isinstance(result, dict) and "error" not in result else None


async def _match_candidates(  # pylint: disable=too-many-locals
    athlete_id: str, activity: dict[str, Any], api_key: str | None
) -> list[dict[str, Any]]:
    """Read-only suggestion of planned workouts that could belong to an unpaired activity.

    Looks at WORKOUT events on the activity's day and the day before/after and scores them
    by sport, planned vs actual moving time and name similarity. Nothing is paired or changed.
    """
    day = str(activity.get("start_date_local", ""))[:10]
    try:
        start = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        end = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    except ValueError:
        return []
    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id)}/events", api_key=api_key,
        params={"oldest": start, "newest": end, "category": "WORKOUT"},
    )
    if not isinstance(result, list):
        return []
    actual = activity.get("moving_time") or activity.get("elapsed_time") or 0
    name = str(activity.get("name") or "").lower()
    candidates: list[dict[str, Any]] = []
    for event in result:
        if not isinstance(event, dict):
            continue
        score = 0.0
        reasons = []
        if str(event.get("type")) == str(activity.get("type")):
            score += 0.4
            reasons.append("same sport")
        planned = event.get("moving_time") or 0
        if planned and actual:
            ratio = min(planned, actual) / max(planned, actual)
            score += 0.4 * ratio
            reasons.append(f"duration {hms(planned)} planned vs {hms(actual)} actual")
        similarity = difflib.SequenceMatcher(None, name, str(event.get("name") or "").lower()).ratio()
        score += 0.2 * similarity
        if similarity > 0.5:
            reasons.append(f"name similarity {similarity:.0%}")
        if str(event.get("start_date_local", ""))[:10] != day:
            score -= 0.2
            reasons.append("different day")
        if event.get("paired_activity_id"):
            reasons.append(f"already paired with {event['paired_activity_id']}")
            score -= 0.3
        candidates.append({"event_id": event.get("id"), "name": event.get("name"), "date": str(event.get("start_date_local", ""))[:10],
                           "type": event.get("type"), "moving_time": planned, "score": round(score, 2), "reasons": reasons})
    candidates.sort(key=lambda c: float(c["score"]), reverse=True)
    return candidates[:3]


def _stream_types_for_execution(activity: dict[str, Any], stream_defs: dict[str, Any]) -> list[str]:
    available = [str(t) for t in (activity.get("stream_types") or [])]
    wanted = [t for t in CORE_STREAMS if t in available or t == "time"]
    wanted += [t for t in available if t in stream_defs]
    return wanted


@tool("read")
async def analyze_workout_execution(  # pylint: disable=too-many-locals,too-many-branches,too-many-arguments,too-many-positional-arguments,too-many-statements
    activity_id: str,
    api_key: str | None = None,
    event_id: str | None = None,
    planned_workout_doc: dict[str, Any] | None = None,
    suggest_matches: bool = True,
    output_format: str = "text",
    detail_level: str = "standard",
    duration_tolerance_pct: float | None = None,
    start_tolerance_s: float | None = None,
    pause_tolerance_s: float | None = None,
) -> str:
    """Compare a planned workout with how it was actually executed (or analyse the intervals alone)

    Uses, in this order, the given planned_workout_doc, the given event_id, or the event
    paired with the activity, together with the intervals Intervals.icu detected. Planned
    steps (repeats expanded) are aligned with the actual intervals by order, duration (or
    distance) and target intensity (never by interval names); a step may also match any
    number of consecutive intervals of about the same intensity (an effort split by laps,
    e.g. 1 km device auto-laps, or a stop). Lap presses are kept as step boundaries. With
    device auto-laps (most laps of one distance or duration) a step boundary inside a lap is
    placed where the intensity changes, a step without a lap of its own between two matched
    steps is found at its two intensity changes, and an overrun is reported as longer than
    planned; a boundary the samples cannot place is kept and the durations there are not
    judged. A caveat line and JSON alignment_confidence (high / medium / low) with notes say
    how far the per-step results can be trusted. Open-ended targets (top zone, a %/W range
    with a start only) are lower bounds. Steps in the recovery zone or between two clearly
    harder steps count as rest (no length limit), easy aerobic steps as work. Steps without
    duration (distance, lap button) restart the plan clock at their actual end. Planned
    steps are capped at their planned duration: when an interval is longer than
    its step (beyond the tolerance), it is split logically (analysis only, nothing on
    Intervals.icu changes); the planned part is evaluated against the plan from the samples
    and the remainder is reported separately. For each step: planned vs actual (moving)
    duration, the target range (resolved to W, bpm or pace), the actual average, below/in/
    above target with the offset from the exact range, time within the target range (±5%),
    HR start/end, the HR drop in the first minute after work steps, cadence, power/speed
    fade, Pw:HR drift for work steps of 10 min or more (Intervals.icu decoupling sign:
    positive = HR rose relative to power), the change of every custom stream
    (e.g. stamina; clock counters and other-sport streams are left out) and notes on clear
    deviations (short, too long, off target, paused, shifted). The Intervals.icu interval
    type is kept and shown next to the planned step type when they differ. Everything after
    the end of the last planned step (e.g. a cool-down continued for the ride home, extra
    sprints) is reported as additional training with its own metrics, kJ share, estimated
    load and the extra efforts it contains, and is not counted against the plan; riding
    before the first step is reported the same way. Without a plan the same metrics are
    reported per detected interval; for an unpaired activity up to three planned workouts
    of the same days are suggested (read-only, nothing is paired or changed). The content
    of a deleted event is never reconstructed; pass planned_workout_doc instead.

    Args:
        activity_id: The Intervals.icu activity ID
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        event_id: Planned workout (event) to compare against (optional; default: the event
            paired with the activity, if any)
        planned_workout_doc: Workout document with "steps" (same format as add_or_update_event)
            to compare against, e.g. when the calendar event no longer exists (optional)
        suggest_matches: For unpaired activities without a plan, list candidate events of the
            same days as a read-only suggestion (optional, default True)
        output_format: "text" (default) or "json"
        detail_level: "compact" (one line per step plus the summary), "standard" (default) or
            "full" (also counter and other-sport custom streams)
        duration_tolerance_pct: How much longer/shorter (in % of the step, at least 30 s) a step
            may be before it is split or flagged as short (optional, default 10)
        start_tolerance_s: Steps starting more than this many seconds away from the plan
            timeline are flagged (optional, default 120)
        pause_tolerance_s: Recording pauses inside a step up to this many seconds are not
            flagged (optional, default 60); durations are always compared on moving time
    """
    tolerances = tolerances_from_args(duration_tolerance_pct, start_tolerance_s, pause_tolerance_s, detail_level)
    if isinstance(tolerances, str):
        return tolerances
    activity, error = await _get_activity(activity_id, api_key)
    if error or activity is None:
        return error or "Error"
    athlete_id = str(activity.get("icu_athlete_id") or config.athlete_id or "")
    intervals_result = await make_intervals_request(url=f"/activity/{seg(activity_id)}/intervals", api_key=api_key)
    intervals: list[dict[str, Any]] = []
    load_errors: list[str] = []  # API errors are reported, never shown as missing data (API-7)
    if isinstance(intervals_result, dict) and "error" not in intervals_result:
        intervals = [i for i in intervals_result.get("icu_intervals") or [] if isinstance(i, dict)]
    elif isinstance(intervals_result, dict):
        load_errors.append(f"intervals could not be loaded: {intervals_result.get('message', 'Unknown error')}")

    stream_defs = await _defs(ACTIVITY_STREAM, api_key, athlete_id)
    field_defs = await _defs(ACTIVITY_FIELD, api_key, athlete_id)
    streams, streams_error = await _get_streams(activity_id, api_key, _stream_types_for_execution(activity, stream_defs))
    if streams_error and streams_error.startswith("Error"):
        load_errors.append(streams_error)

    event: dict[str, Any] | None = None
    steps: Any = None
    plan_source = ""
    if isinstance(planned_workout_doc, dict) and isinstance(planned_workout_doc.get("steps"), list):
        steps = planned_workout_doc["steps"]
        plan_source = "workout document provided by the caller"
    paired = event_id or activity.get("paired_event_id")
    if steps is None and paired and athlete_id:
        event = await _get_event(athlete_id, paired, api_key)
        steps = ((event or {}).get("workout_doc") or {}).get("steps") if event else None
        if event:
            plan_source = f"event {event.get('id')} ('{event.get('name')}', {str(event.get('start_date_local', ''))[:10]})"
    planned = plan_steps(steps, _threshold_context(activity)) if isinstance(steps, list) else []
    candidates: list[dict[str, Any]] = []
    if not planned and not paired and suggest_matches and athlete_id:
        candidates = await _match_candidates(athlete_id, activity, api_key)

    doc = planned_workout_doc if isinstance(planned_workout_doc, dict) else ((event or {}).get("workout_doc") or {})
    context = {**_threshold_context(activity), "activity_type": activity.get("type"), "stream_defs": stream_defs,
               "include_all_streams": detail_level == "full", "pace_units": doc.get("pace_units")}
    # CPU-bound (many laps): run off the event loop so other requests are not blocked
    result = await asyncio.to_thread(analyze, planned, intervals, streams, tolerances=tolerances, context=context)
    pace_based = str(activity.get("type")) in PACE_SPORTS or (event or {}).get("target") == "PACE"

    assigned = assigned_codes(field_defs, await assigned_field_ids(athlete_id, api_key, activity.get("type")))
    device_lines = format_custom_field_lines(activity, field_defs, prefix="", only=assigned)
    header = f"Workout execution for {_activity_header(activity)}"
    if planned:
        header += f"\nPlan source: {plan_source}"
    elif paired and event:
        header += f"\nEvent {event.get('id')} ('{event.get('name')}', {event.get('category')}) has no workout steps; analysing intervals only."
    elif paired:
        header += f"\nPlanned workout {paired} could not be loaded; analysing intervals only."
    else:
        header += "\nNo planned workout paired with this activity (paired_event_id is empty); analysing intervals only."
        if candidates:
            header += "\nPossible planned workouts (read-only suggestion, nothing was paired):"
            for cand in candidates:
                header += f"\n  - event {cand['event_id']} '{cand['name']}' {cand['date']} {cand['type']} {hms(cand['moving_time'])}, score {cand['score']} ({'; '.join(cand['reasons'])})"
        elif suggest_matches:
            header += "\nNo planned workouts found on the activity's day or the days around it."
    extras = []
    if activity.get("compliance") is not None:
        extras.append(f"Intervals.icu compliance {activity['compliance']:.0f}%")
    if activity.get("icu_rpe") is not None:
        extras.append(f"RPE {activity['icu_rpe']}/10")
    if activity.get("feel") is not None:
        extras.append(f"feel {activity['feel']}/5")
    if activity.get("icu_training_load") is not None:
        extras.append(f"load {activity['icu_training_load']} (Intervals.icu)")
    if extras:
        header += "\n" + ", ".join(extras)
    if device_lines and detail_level != "compact":
        header += "\nDevice/custom fields" + (" (assigned to the sport)" if assigned is not None else "") + ": " + "; ".join(device_lines)
    if load_errors:
        header += "\nINCOMPLETE DATA (API error, not missing data; retry later): " + "; ".join(load_errors)

    if output_format.strip().lower() == "json":
        payload = {
            "activity": {
                "id": activity.get("id"), "name": activity.get("name"), "type": activity.get("type"),
                **start_times(activity), "compliance": activity.get("compliance"),
                "rpe": activity.get("icu_rpe"), "feel": activity.get("feel"),
                "training_load": activity.get("icu_training_load"),
            },
            "event": {"id": event.get("id"), "name": event.get("name"), "start_date_local": event.get("start_date_local")} if event else None,
            "plan_source": plan_source or None,
            "match_candidates": candidates,
            "thresholds": _threshold_context(activity),
            "custom_fields": [row for row in custom_fields_json(activity, field_defs, assigned) if row["status"] in ("value", "zero")],
            "summary": result["summary"],
            "rows": result["rows"],
            "extension": result.get("extension"),
            "pre_plan": result.get("pre_plan"),
            "hidden_streams": result.get("hidden_streams"),
            "load_errors": load_errors,
        }
        return json.dumps(payload, ensure_ascii=False)
    return format_execution(result, header, pace_based, detail_level)
