"""
One-call activity report: overview, plan-vs-execution (or intervals), power meter check and
climb segmentation from a single set of API requests (activity, intervals, streams, event).

Designed for token- and API-efficient coaching conversations: four requests at most, every
section compact, and data-quality notes instead of guesses. ``detail_level`` controls the
size: "compact" (core numbers, up to five key findings, sport-relevant custom fields, data
quality), "standard" (the full analysis without raw stream dumps; default) or "full".
"""

import asyncio
import json
from typing import Annotated, Any

from pydantic import Field

from intervals_mcp_server.tenancy import default_athlete
from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.activities import _compact_details, _compact_intervals  # pylint: disable=protected-access
from intervals_mcp_server.tools.analysis import PACE_SPORTS, _get_event, _threshold_context, tolerances_from_args  # pylint: disable=protected-access
from intervals_mcp_server.tools.athlete import assigned_field_ids
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.tools.data_audit import expected_field_codes
from intervals_mcp_server.tools.gear import resolve_gear_for_activity
from intervals_mcp_server.utils.activity_context import (
    MAX_ROUTE_HISTORY,
    ROUTE_FIELDS,
    route_history,
    route_history_lines,
    weather_summary,
    wprime_summary,
)
from intervals_mcp_server.utils.custom_fields import ACTIVITY_FIELD, ACTIVITY_STREAM, INTERVAL_FIELD, assigned_codes, is_missing
from intervals_mcp_server.utils.execution import analyze, format_execution, plan_steps, target_text
from intervals_mcp_server.utils.field_policy import aggregation_policy, start_end_pairs
from intervals_mcp_server.utils.fueling import activity_fueling
from intervals_mcp_server.utils.params import ActivityId, DetailLevel, OutputFormat
from intervals_mcp_server.utils.power_compare import compare_power_streams as compute_power_comparison
from intervals_mcp_server.utils.provenance import STRAVA_STUB_NOTE, freshness, is_strava_stub, provenance_notes, source_summary
from intervals_mcp_server.utils.segments import detect_segments
from intervals_mcp_server.utils.sports import format_pace, hms, utc_offset
from intervals_mcp_server.utils.streams import find_stream, gear_units_note, numeric_values
from intervals_mcp_server.utils.work_sets import set_summary, split_work

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

CORE_STREAMS = ("time", "watts", "heartrate", "cadence", "velocity_smooth", "distance", "altitude", "secondary_power")
MAX_CLIMBS = 6
MAX_FINDINGS = 5
STANDARD_MAX_INTERVALS = 30
MIN_POWER_PAIRS = 300  # five minutes of valid paired samples before two power meters are compared
IDENTICAL_SHARE = 0.99
INTERVAL_JSON_KEYS = (
    "type", "label", "start_index", "end_index", "start_time", "end_time", "elapsed_time", "moving_time",
    "average_watts", "weighted_average_watts", "max_watts", "average_heartrate", "max_heartrate",
    "average_cadence", "average_speed", "intensity", "group_id",
)


def _power_check(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Second power meter check; None without two power streams, a status when not comparable."""
    first, second, time_stream = (find_stream(streams, t) for t in ("watts", "secondary_power", "time"))
    if first is None or second is None or time_stream is None:
        return None
    primary, secondary = first.get("data") or [], second.get("data") or []
    usable = [(a, b) for a, b in zip(primary, secondary, strict=False)
              if numeric_values([a]) and numeric_values([b]) and a > 0 and b > 0]
    if len(usable) < MIN_POWER_PAIRS:
        return {"status": "insufficient", "paired_valid": len(usable),
                "note": f"second power stream has only {len(usable)} usable samples paired with the primary; no comparison"}
    if sum(1 for a, b in usable if a == b) / len(usable) >= IDENTICAL_SHARE:
        return {"status": "identical", "paired_valid": len(usable),
                "note": "both power streams are identical (the same sensor recorded twice); no comparison"}
    result = compute_power_comparison(primary, secondary, time_stream.get("data") or [])
    overall = result.get("overall") or {}
    stable = (result.get("stable_windows") or {}).get(60) or {}
    if (result.get("paired_valid") or 0) < MIN_POWER_PAIRS or overall.get("mean_diff_pct") is None:
        return {"status": "insufficient", "paired_valid": result.get("paired_valid"),
                "note": "not enough valid paired samples after excluding coasting and outliers"}
    return {
        "status": "ok",
        "paired_valid": result.get("paired_valid"),
        "mean_diff_pct": overall.get("mean_diff_pct"),
        "ratio": overall.get("ratio_secondary_to_primary"),
        "stable_60s_diff_pct": stable.get("mean_diff_pct"),
        "stable_60s_windows": stable.get("windows"),
        "lag_s": (result.get("lag") or {}).get("best_lag_s"),
    }


def _climb_summary(streams: list[dict[str, Any]], activity_type: Any, limit: int | None) -> dict[str, Any] | None:
    result = detect_segments(streams, sport=activity_type)
    if result.get("error"):
        return None
    climbs = [s for s in result.get("segments", []) if s.get("type") == "climb"]
    climbs.sort(key=lambda s: s.get("elevation_gain_m") or 0, reverse=True)
    return {"summary": result.get("summary"), "climbs": climbs[:limit] if limit else climbs, "pauses": len(result.get("pauses", [])),
            "quality": result.get("quality")}


def _format_climbs(data: dict[str, Any]) -> list[str]:
    summary = data.get("summary") or {}
    lines = [
        f"Climbs: {summary.get('climbs', 0)} (+{summary.get('total_climb_gain_m', 0):.0f} m inside climbs), descents {summary.get('descents', 0)}, "
        f"pauses {hms(summary.get('pause_time_s'))} ({data.get('pauses', 0)}), slow movement kept as moving {hms(summary.get('slow_movement_s'))}"
    ]
    for climb in data.get("climbs", []):
        grade = climb.get("avg_grade_pct")
        lines.append(
            f"  {hms(climb.get('start_time'))}-{hms(climb.get('end_time'))}: +{climb.get('elevation_gain_m') or 0:.0f} m over "
            f"{(climb.get('distance_m') or 0) / 1000:.1f} km, {f'{grade:.1f}%' if grade is not None else 'grade n/a'}, VAM {climb.get('vam_m_per_h') or 'n/a'} m/h, "
            f"avg {climb.get('avg_watts') or 'n/a'} W, NP {climb.get('normalized_power') or 'n/a'} W, HR {climb.get('avg_hr') or 'n/a'}"
            + (f" [{'; '.join(climb['quality_flags'])}]" if climb.get("quality_flags") else "")
        )
    return lines


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or is_missing(value):
        return None
    return float(value)


ACTUAL_KEYS = {"power": "avg_watts", "hr": "avg_hr", "pace": "avg_speed_m_s"}


TARGET_GROUP_PCT = 10.0  # work targets whose midpoints differ less are summarised as one range


def _target_groups(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Work rows grouped by similar targets (midpoints within 10 %), in order of first appearance."""
    groups: list[list[dict[str, Any]]] = []
    for row in rows:
        target = row["planned"]["target"]
        mid = target["low"] if target["high"] is None else (target["low"] + target["high"]) / 2
        for group in groups:
            first = group[0]["planned"]["target"]
            ref = first["low"] if first["high"] is None else (first["low"] + first["high"]) / 2
            if ref and abs(mid - ref) / ref * 100 < TARGET_GROUP_PCT and (target["high"] is None) == (first["high"] is None):
                group.append(row)
                break
        else:
            groups.append([row])
    return groups


def _work_vs_plan(work: list[dict[str, Any]], pace_units: str | None) -> str:
    """'work steps 249/249/253 W vs 238-246 W planned' in the unit of the planned targets.

    Power targets are compared with the average power, pace targets with the average pace
    and HR targets with the average HR; nothing when the work steps mix target kinds.
    Clearly different targets (e.g. an easy block and strides) are listed separately.
    """
    rows = [r for r in work if r["planned"].get("target")]
    kinds = {r["planned"]["target"]["kind"] for r in rows}
    if len(kinds) != 1:
        return ""
    kind = kinds.pop()
    parts = []
    for group in _target_groups(rows):
        values = [r["metrics"][ACTUAL_KEYS[kind]] for r in group if r["metrics"].get(ACTUAL_KEYS[kind]) is not None]
        if not values:
            continue
        targets = [r["planned"]["target"] for r in group]
        highs = [t["high"] for t in targets]
        span = {"kind": kind, "low": min(t["low"] for t in targets), "high": None if None in highs else max(highs),
                "units": "bpm" if kind == "hr" else "W"}
        if kind == "pace":
            actual = ", ".join(format_pace(v, pace_units) for v in values)
        else:
            actual = "/".join(f"{v:.0f}" for v in values) + f" {span['units']}"
        parts.append(f"{actual} vs {target_text(span, pace_units)} planned")
    return "; work steps " + "; ".join(parts) if parts else ""


def _execution_findings(execution: dict[str, Any]) -> list[str]:
    summary = execution["summary"]
    rows = [r for r in execution["rows"] if r.get("planned") and r.get("metrics")]
    work = [r for r in rows if r["planned"]["kind"] == "work"]
    findings = []
    text = f"Plan: {summary['matched']}/{summary['planned_steps']} steps executed"
    if work:
        text += _work_vs_plan(work, execution.get("pace_units"))
        text += f" ({summary['work_steps_in_range']} within ±5%, {summary.get('work_steps_inside_exact_range')} inside the exact range"
        if summary.get("work_time_in_target_pct_mean") is not None:
            text += f", mean time in target {summary['work_time_in_target_pct_mean']:.0f}%"
        text += ")"
    text += f"; steps with clear deviations: {summary.get('steps_with_deviations', 0)}"
    findings.append(text)
    extension = execution.get("extension")
    if extension:
        efforts = ", ".join(
            f"{hms(e.get('elapsed_time'))} @ {e['average_watts']} W" if e.get("average_watts") is not None
            else f"{hms(e.get('elapsed_time'))} @ {format_pace(e.get('average_speed'), execution.get('pace_units'))}"
            for e in extension.get("efforts") or []
        )
        share = f", {summary['extension_work_share_pct']:.0f}% of the work" if summary.get("extension_work_share_pct") is not None else ""
        findings.append(
            f"Additional training after the plan: {hms(extension['duration_s'])} from {hms(extension.get('start_time'))}{share}"
            + (f"; extra efforts {efforts}" if efforts else "")
        )
    return findings


def _main_set_finding(intervals: list[dict[str, Any]], ftp: float | None) -> str | None:
    split = split_work(intervals, ftp)
    main = set_summary(split["main"], ftp)
    if not main.get("count"):
        return None
    text = f"Main work set: {main['count']} x {hms(main['secs_median'])}"
    if main.get("avg_watts") is not None:
        text += f" at {main['avg_watts']:.0f} W" + (f" ({main['pct_ftp']:.0f}% FTP)" if main.get("pct_ftp") is not None else "")
    if main.get("avg_hr") is not None:
        text += f", HR {main['avg_hr']:.0f} (max {main.get('max_hr') or 'n/a'})"
    if split["surges"]:
        peak = max((s.get("average_watts") or 0) for s in split["surges"])
        text += f"; {len(split['surges'])} short surge(s) not averaged in (up to {peak:.0f} W)"
    return text


def _field_findings(activity: dict[str, Any], field_defs: dict[str, Any], assigned: set[str] | None) -> list[str]:
    findings = []
    defs = {c: d for c, d in field_defs.items() if assigned is None or c in assigned}
    for start_code, end_code, stem in start_end_pairs(defs):
        first, last = _num(activity.get(start_code)), _num(activity.get(end_code))
        if first is not None and last is not None:
            units = defs[start_code].get("units") or ""
            findings.append(f"{stem.capitalize()} {first:g} → {last:g} {units}".rstrip())
    loads = [
        f"{d['name']} {activity[c]:g}" for c, d in defs.items()
        if aggregation_policy(d)["policy"] == "device_load_sum" and _num(activity.get(c)) not in (None, 0.0) and "load" in str(d["name"]).lower()
    ]
    if activity.get("icu_training_load") is not None:
        text = f"Load {activity['icu_training_load']} (Intervals.icu)"
        if loads:
            text += f"; device {', '.join(loads)} (separate scale)"
        findings.append(text)
    return findings


def _key_findings(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    activity: dict[str, Any], execution: dict[str, Any] | None, planned: bool, intervals: list[dict[str, Any]],
    power: dict[str, Any] | None, climbs: dict[str, Any] | None, field_defs: dict[str, Any], assigned: set[str] | None,
) -> list[str]:
    """Up to five short findings for the compact report."""
    findings: list[str] = []
    skipped = (execution or {}).get("summary", {}).get("plan_skipped")
    if skipped:  # the plan comparison fell back to the intervals: say so instead of "0/0 steps"
        findings.append(f"Plan: {skipped}")
    if execution and planned and not skipped:
        findings.extend(_execution_findings(execution))
    elif intervals:
        main = _main_set_finding(intervals, _num(activity.get("icu_ftp")))
        if main:
            findings.append(main)
    findings.extend(_field_findings(activity, field_defs, assigned))
    if power and power.get("status") == "ok":
        findings.append(
            f"Second power meter reads {power['mean_diff_pct']:+.1f}% vs the primary"
            + (f" ({power['stable_60s_diff_pct']:+.1f}% in stable 60 s windows)" if power.get("stable_60s_diff_pct") is not None else "")
        )
    if climbs:
        summary = climbs.get("summary") or {}
        findings.append(f"Climbs {summary.get('climbs', 0)} (+{summary.get('total_climb_gain_m', 0):.0f} m inside climbs), descents {summary.get('descents', 0)}")
    return findings[:MAX_FINDINGS]


def _load_error_note(note: str, intervals_result: Any, streams_result: Any) -> str:
    """Replace a "no data" note by the API error that caused it."""
    for marker, result, what in (
        ("no intervals detected", intervals_result, "intervals"),
        ("no streams returned", streams_result, "streams"),
    ):
        if note.startswith(marker) and isinstance(result, dict) and "error" in result:
            return (
                f"{what} could not be loaded from Intervals.icu ({result.get('message', 'Unknown error')}); "
                f"the report is without {what} - retry later"
            )
    return note


def _quality_notes(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    activity: dict[str, Any], available: list[str], streams: list[dict[str, Any]], intervals: list[dict[str, Any]],
    power: dict[str, Any] | None, stream_defs: dict[str, Any], execution: dict[str, Any] | None,
    provenance: list[str] | None = None,
) -> list[str]:
    notes: list[str] = list(provenance or [])
    if not activity.get("device_name"):
        notes.append("device unknown (no device data in the file)")
    if "watts" in available and not activity.get("power_meter"):
        notes.append("power meter identity unknown: the file carries no power meter name/serial")
    if "watts" in available and "secondary_power" not in available:
        notes.append("only one power stream recorded; no power meter comparison possible")
    if power and power.get("status") != "ok":
        notes.append(power["note"])
    if not streams:
        notes.append("no streams returned by Intervals.icu (file not retained?)")
    if not intervals:
        notes.append("no intervals detected by Intervals.icu")
    if activity.get("compliance") == 0 and not activity.get("paired_event_id"):
        notes.append("compliance 0 only means the activity is not paired with a planned workout")
    for stream in streams:
        stream_type = str(stream.get("type"))
        definition = stream_defs.get(stream_type) or {}
        note = gear_units_note(stream_type, definition.get("name"), definition.get("units"), stream.get("data") or [])
        if note:
            notes.append(f"{stream_type}: {note}")
    hidden = (execution or {}).get("hidden_streams") or {}
    if hidden:
        notes.append("custom streams left out of the per-step / per-interval statistics: "
                     + ", ".join(f"{code} ({why})" for code, why in sorted(hidden.items())))
    return notes


async def _route_history(activity: dict[str, Any], athlete_id: str, field_defs: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    """Earlier activities on the activity's Intervals.icu route (one list request, one for the route name)."""
    route_id = activity.get("route_id")
    if not route_id or not athlete_id:
        return None, 0
    pairs = start_end_pairs(field_defs)
    codes = sorted({code for start, end, _ in pairs for code in (start, end)})
    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id)}/activities",
        params={"oldest": "2000-01-01", "newest": str(activity.get("start_date_local") or "")[:10], "route_id": route_id,
                "limit": MAX_ROUTE_HISTORY + 1, "fields": ",".join([ROUTE_FIELDS, *codes])},
    )
    candidates = [a for a in result if isinstance(a, dict)] if isinstance(result, list) else []
    history = route_history(activity, candidates, pairs, truncated=len(candidates) >= MAX_ROUTE_HISTORY + 1)
    route = await make_intervals_request(url=f"/athlete/{seg(athlete_id)}/routes/{seg(route_id)}")
    history["route_name"] = route.get("name") if isinstance(route, dict) and "error" not in route else None
    if isinstance(result, dict) and "error" in result:
        history["error"] = result.get("message")
    return history, 2


@tool("read")
async def get_activity_report(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-arguments,too-many-positional-arguments,too-many-return-statements
    activity_id: ActivityId,
    planned_workout_doc: Annotated[dict[str, Any] | None, Field(
        description='Workout document {"steps": [...]} to compare against, e.g. when the event was deleted'
    )] = None,
    include_climbs: Annotated[bool | None, Field(
        description="Force (true) or suppress (false) the climbs; default only without intervals or above 500 m gain"
    )] = None,
    output_format: OutputFormat = "text",
    detail_level: Annotated[DetailLevel, Field(
        description="compact = core numbers, up to 5 key findings, data-quality flags; standard = full analysis, "
        "at most 30 interval lines; full = everything, all custom fields and streams"
    )] = "standard",
    duration_tolerance_pct: Annotated[float | None, Field(
        description="Step duration tolerance in % before a step is split or flagged short; default 10"
    )] = None,
    start_tolerance_s: Annotated[float | None, Field(
        description="Plan timeline shift before a step is flagged, seconds; default 120"
    )] = None,
    pause_tolerance_s: Annotated[float | None, Field(
        description="Recording pause per step before it is flagged, seconds; default 60"
    )] = None,
    include_route_history: Annotated[bool, Field(
        description="Add earlier activities on the same Intervals.icu route (2 extra requests)"
    )] = False,
) -> str:
    """Use first for any question about one activity: overview, plan vs execution, power meter check, climbs and data quality in one call (read-only, 3-4 API requests).

    The overview has thresholds, device, sport-assigned custom fields, fueling, weather and W′
    balance. With a paired event or planned_workout_doc the plan-vs-execution analysis follows
    (as analyze_workout_execution), otherwise the intervals; a second-power-meter check only with
    enough valid paired samples; climbs without intervals or above 500 m gain; then data-quality
    notes. compact = core numbers and up to 5 key findings. Full detail: get_activity_details,
    analyze_workout_execution, compare_power_streams, analyze_climbs, get_activity_data_audit.
    Methods: intervals://methods/activity-data, intervals://methods/execution (get_guide).
    """
    tolerances = tolerances_from_args(duration_tolerance_pct, start_tolerance_s, pause_tolerance_s, detail_level)
    if isinstance(tolerances, str):
        return tolerances
    result = await make_intervals_request(url=f"/activity/{seg(activity_id)}")
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching activity details: {result.get('message', 'Unknown error')}"
    activity = result[0] if isinstance(result, list) and result else result
    if not isinstance(activity, dict) or not activity:
        return f"No details found for activity {activity_id}."
    if is_strava_stub(activity):
        if output_format.strip().lower() == "json":
            return json.dumps({"activity": {k: activity.get(k) for k in ("id", "name", "type", "start_date_local", "source")},
                               "strava_stub": True, "notes": [STRAVA_STUB_NOTE], "api_calls": 1}, ensure_ascii=False, default=str)
        return f"== Overview\n{activity.get('name', 'Unnamed')} ({activity.get('id')}, {activity.get('type', '?')})\n== Data quality\n- {STRAVA_STUB_NOTE}"
    athlete_id = str(activity.get("icu_athlete_id") or default_athlete(config.athlete_id) or "")
    await resolve_gear_for_activity(activity, athlete_id=athlete_id or None)
    index = await get_custom_item_index(athlete_id=athlete_id) if athlete_id else {}
    field_defs, stream_defs, interval_defs = (index.get(t, {}) for t in (ACTIVITY_FIELD, ACTIVITY_STREAM, INTERVAL_FIELD))
    assigned = assigned_codes(field_defs, await assigned_field_ids(athlete_id, activity.get("type")))
    sport = str(activity.get("type") or "")
    expected = (await expected_field_codes(athlete_id, field_defs, [sport])).get(sport) if athlete_id and sport else assigned

    intervals_result = await make_intervals_request(url=f"/activity/{seg(activity_id)}/intervals")
    intervals_payload = intervals_result if isinstance(intervals_result, dict) and "error" not in intervals_result else {}
    intervals = [i for i in intervals_payload.get("icu_intervals") or [] if isinstance(i, dict)]

    available = [str(t) for t in (activity.get("stream_types") or [])]
    wanted = [t for t in CORE_STREAMS if t in available or t == "time"] + [t for t in available if t in stream_defs]
    if "watts" in available:
        wanted.append("w_bal")  # computed by Intervals.icu on request; silently omitted without W′ data
    streams_result = await make_intervals_request(
        url=f"/activity/{seg(activity_id)}/streams", params={"types": ",".join(wanted)}
    )
    streams = [s for s in streams_result if isinstance(s, dict)] if isinstance(streams_result, list) else []
    w_bal = find_stream(streams, "w_bal")
    streams = [s for s in streams if s.get("type") != "w_bal"]
    time_stream = find_stream(streams, "time")
    wprime = wprime_summary(activity, intervals, (w_bal or {}).get("data"), (time_stream or {}).get("data"))

    steps: Any = None
    plan_source = ""
    doc: dict[str, Any] = {}
    if isinstance(planned_workout_doc, dict) and isinstance(planned_workout_doc.get("steps"), list):
        doc, plan_source = planned_workout_doc, "workout document provided by the caller"
    elif activity.get("paired_event_id") and athlete_id:
        event = await _get_event(athlete_id, activity["paired_event_id"])
        if event:
            doc = event.get("workout_doc") or {}
            plan_source = f"event {event.get('id')} ('{event.get('name')}')"
    steps = doc.get("steps")
    planned = plan_steps(steps, _threshold_context(activity)) if isinstance(steps, list) else []
    context = {**_threshold_context(activity), "activity_type": activity.get("type"), "stream_defs": stream_defs,
               "include_all_streams": detail_level == "full", "pace_units": doc.get("pace_units")}
    execution = (  # CPU-bound (many laps): run off the event loop
        await asyncio.to_thread(analyze, planned, intervals, streams, tolerances=tolerances, context=context) if intervals else None
    )
    pace_based = str(activity.get("type")) in PACE_SPORTS
    power = _power_check(streams)
    want_climbs = include_climbs if include_climbs is not None else (not intervals or (activity.get("total_elevation_gain") or 0) > 500)
    climbs = _climb_summary(streams, activity.get("type"), None if detail_level == "full" else MAX_CLIMBS) if want_climbs and streams else None
    notes = _quality_notes(activity, available, streams, intervals, power, stream_defs, execution,
                           provenance_notes(activity, field_defs, expected, intervals, compact=detail_level == "compact"))
    route, route_calls = await _route_history(activity, athlete_id, field_defs) if include_route_history else (None, 0)
    # An API error is not "no intervals" / "file not retained": say what failed (API-7).
    notes = [_load_error_note(note, intervals_result, streams_result) for note in notes]
    findings = _key_findings(activity, execution, bool(planned), intervals, power, climbs, field_defs, assigned)

    if output_format.strip().lower() == "json":
        execution_json = None
        if execution:
            execution_json = {"summary": execution["summary"], "extension": execution.get("extension"), "pre_plan": execution.get("pre_plan"),
                              "hidden_streams": execution.get("hidden_streams")}
            if detail_level != "compact":
                execution_json["rows"] = execution["rows"]
        interval_rows: list[dict[str, Any]] | None = None
        if not planned:
            interval_rows = intervals if detail_level != "compact" else [{k: i.get(k) for k in INTERVAL_JSON_KEYS} for i in intervals]
        payload = {
            "activity": {
                **{k: activity.get(k) for k in ("id", "name", "type", "start_date_local", "start_date", "moving_time", "elapsed_time", "distance", "total_elevation_gain", "icu_training_load", "icu_intensity", "icu_ftp", "device_name", "power_meter", "power_field_names", "compliance", "paired_event_id", "_resolved_gear_name")},
                "utc_offset": utc_offset(activity),
            },
            "detail_level": detail_level,
            "plan_source": plan_source or None,
            "key_findings": findings,
            "execution": execution_json,
            "intervals": interval_rows,
            "power_check": power,
            "climbs": climbs,
            "fueling": activity_fueling(activity, field_defs),
            "weather": weather_summary(activity),
            "w_prime": wprime,
            "provenance": {**source_summary(activity), "freshness": freshness(activity, field_defs, expected)},
            "route_history": route,
            "notes": notes,
            "api_calls": 3 + (1 if plan_source.startswith("event") else 0) + route_calls,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    overview = _compact_details(activity, field_defs, None if detail_level == "full" else assigned, wprime,
                                full_context=detail_level != "compact")
    sections = ["== Overview", overview]
    route_lines = route_history_lines(route, detail_level) if route else (
        ["No Intervals.icu route for this activity (routes need GPS)"] if include_route_history else [])
    if detail_level == "compact":
        if findings:
            sections.append("== Key findings")
            sections.extend(f"- {finding}" for finding in findings)
        if route_lines:
            sections.append("== Same route")
            sections.extend(route_lines[:3])
        if notes:
            sections.append("== Data quality")
            sections.extend(f"- {n}" for n in notes)
        return "\n".join(sections)
    if execution and planned:
        sections.append("== Plan vs execution (" + plan_source + ")")
        sections.append(format_execution(execution, "", pace_based, detail_level).strip())
    elif intervals:
        sections.append("== Intervals")
        shown_streams = streams if detail_level == "full" else [
            s for s in streams if s.get("custom") and str(s.get("type")) not in ((execution or {}).get("hidden_streams") or {})
        ]
        listing = _compact_intervals(intervals_payload, interval_defs, shown_streams, stream_defs, activity_type=activity.get("type"))
        lines = listing.split("\n")
        if detail_level == "standard" and len(lines) > STANDARD_MAX_INTERVALS + 1:
            lines = lines[: STANDARD_MAX_INTERVALS + 1] + [f"... {len(lines) - STANDARD_MAX_INTERVALS - 1} more lines (detail_level=full)"]
        sections.append("\n".join(lines))
    if power and power.get("status") == "ok":
        sections.append("== Second power meter check (watts vs secondary_power, no calibration)")
        sections.append(
            f"valid pairs {power['paired_valid']}, mean diff {power['mean_diff_pct']:+.1f}% (ratio {power['ratio']:.3f}), "
            f"stable 60 s windows {power['stable_60s_windows']}: "
            + (f"{power['stable_60s_diff_pct']:+.1f}%" if power.get("stable_60s_diff_pct") is not None else "n/a")
            + f", lag {power['lag_s']} s"
        )
    if climbs:
        sections.append("== Climbs")
        sections.extend(_format_climbs(climbs))
    if route_lines:
        sections.append("== Same route")
        sections.extend(route_lines)
    if notes:
        sections.append("== Data quality")
        sections.extend(f"- {n}" for n in notes)
    return "\n".join(sections)
