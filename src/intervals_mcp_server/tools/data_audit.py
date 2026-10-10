"""
Data audit MCP tool for Intervals.icu (read-only): provenance and data quality of one activity,
or the data coverage of a period.

One activity: the activity with its intervals (one request), the activity list of the 28 days up
to its day (one request, field selection: duplicates and what recent activities of the sport
usually carry) and, from detail_level standard on, its streams (one request). Custom item
definitions and sport settings come from the per-process caches. A period: one activity list
request with a field selection. The pure logic lives in ``utils.provenance``.
"""

import json
from datetime import date, timedelta
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.athlete import field_assignments
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.tools.training_load import filter_types, resolve_period, wanted_types
from intervals_mcp_server.utils.custom_fields import ACTIVITY_FIELD, ACTIVITY_STREAM, CustomFieldDefs
from intervals_mcp_server.utils.load_metrics import num, parse_day
from intervals_mcp_server.utils.provenance import (
    AUDIT_LIST_FIELDS,
    BASELINE_DAYS,
    MIN_BASELINE,
    STRAVA_STUB_NOTE,
    baseline_presence,
    field_inventory,
    freshness,
    freshness_text,
    is_strava_stub,
    laps_and_intervals,
    listing_status,
    names,
    recording_summary,
    recording_text,
    sensor_summary,
    source_summary,
    stream_inventory,
)
from intervals_mcp_server.utils.provenance import coverage_summary
from intervals_mcp_server.utils.sports import format_local_start, sport_family
from intervals_mcp_server.utils.validation import resolve_athlete_id

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

DETAIL_LEVELS = ("compact", "standard", "full")
DEFAULT_PERIOD_DAYS = 28
MAX_PERIOD_DAYS = 366
BRIDGE_NOTE = (
    "the Garmin Intervals Bridge (enrich) writes such fields from the original FIT when Garmin left the source out of the "
    "file it sent; the API does not say whether a value came from Intervals.icu's own file processing or the bridge"
)


async def expected_field_codes(
    athlete_id: str, defs: CustomFieldDefs, activity_types: list[str]
) -> dict[str, set[str] | None]:
    """Custom activity field codes each sport expects: its own field list, else the union of its family's
    lists (GravelRide follows Ride), else None (nothing assigned anywhere in the family)."""
    by_type = await field_assignments(athlete_id, defs, activity_types)
    out: dict[str, set[str] | None] = {}
    for sport in activity_types:
        codes = by_type.get(sport)
        if codes is None:
            family = [c for other, c in by_type.items() if other != sport and c is not None and sport_family(other) == sport_family(sport)]
            codes = set().union(*family) if family else None
        out[sport] = codes
    return out


def _error(result: Any, what: str) -> str | None:
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching {what}: {result.get('message', 'Unknown error')}"
    return None


async def _recent(athlete_id: str, day: date, extra_codes: set[str]) -> tuple[list[dict[str, Any]], str | None]:
    fields = ",".join([AUDIT_LIST_FIELDS, *sorted(extra_codes)])
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id}/activities",
        params={"oldest": (day - timedelta(days=BASELINE_DAYS)).isoformat(), "newest": day.isoformat(), "fields": fields},
    )
    error = _error(result, "the activity list")
    if error:
        return [], error
    return [a for a in result if isinstance(a, dict)] if isinstance(result, list) else [], None


def _baseline(activity: dict[str, Any], recent: list[dict[str, Any]], codes: set[str]) -> dict[str, Any] | None:
    """Presence of streams and fields on recent activities of the same type (or family when too few)."""
    start = str(activity.get("start_date_local") or "")
    earlier = [a for a in recent if a.get("id") != activity.get("id") and str(a.get("start_date_local") or "") < start and not is_strava_stub(a)]
    same = [a for a in earlier if a.get("type") == activity.get("type")]
    scope, label = same, str(activity.get("type"))
    if len(same) < MIN_BASELINE:
        scope = [a for a in earlier if sport_family(a.get("type")) == sport_family(activity.get("type"))]
        label = f"{sport_family(activity.get('type'))} activities"
    if not scope:
        return None
    presence = baseline_presence(scope, codes)
    presence["label"] = label
    return presence


async def _audit_activity(  # pylint: disable=too-many-locals
    activity_id: str, athlete_id: str | None, detail_level: str
) -> dict[str, Any] | str:
    result = await make_intervals_request(url=f"/activity/{activity_id}", params={"intervals": "true"})
    error = _error(result, "the activity")
    if error:
        return error
    activity = result[0] if isinstance(result, list) and result else result
    if not isinstance(activity, dict) or not activity:
        return f"No activity {activity_id} found."
    calls = 1
    head = {"id": activity.get("id"), "name": activity.get("name"), "type": activity.get("type"),
            "start": format_local_start(activity), "source": source_summary(activity)}
    if is_strava_stub(activity):
        return {**head, "strava_stub": True, "note": STRAVA_STUB_NOTE, "api_calls": calls}
    owner = str(activity.get("icu_athlete_id") or athlete_id or config.athlete_id or "")
    index = await get_custom_item_index(athlete_id=owner) if owner else {}
    field_defs, stream_defs = index.get(ACTIVITY_FIELD, {}), index.get(ACTIVITY_STREAM, {})
    sport = str(activity.get("type") or "")
    expected = (await expected_field_codes(owner, field_defs, [sport])).get(sport) if owner and sport else None
    day = parse_day(activity.get("start_date_local"))
    recent: list[dict[str, Any]] = []
    listing = None
    if owner and day:
        recent, error = await _recent(owner, day, set(expected or []))
        calls += 1
        if error is None:
            listing = listing_status(activity, [a for a in recent if str(a.get("start_date_local") or "")[:10] == day.isoformat()])
    baseline = _baseline(activity, recent, set(expected or field_defs))
    intervals = [i for i in activity.get("icu_intervals") or [] if isinstance(i, dict)] if "icu_intervals" in activity else None
    streams = None
    if detail_level != "compact" and activity.get("stream_types"):
        streams_result = await make_intervals_request(url=f"/activity/{activity_id}/streams")
        calls += 1
        streams = [s for s in streams_result if isinstance(s, dict)] if isinstance(streams_result, list) else []
    time_data = next((s.get("data") for s in streams or [] if s.get("type") == "time"), None)
    fields = field_inventory(activity, field_defs, expected, baseline)
    return {
        **head, "strava_stub": False,
        "freshness": freshness(activity, field_defs, expected),
        "listing": listing,
        "recording": recording_summary(activity, time_data),
        "laps_and_intervals": laps_and_intervals(activity, intervals),
        "sensors": sensor_summary(activity),
        "streams": stream_inventory(activity, stream_defs, streams, baseline),
        "fields": fields,
        "baseline": {k: v for k, v in (baseline or {}).items() if k != "ids"} or None,
        "context_data": {
            "weather": num(activity.get("average_weather_temp")) is not None, "route_id": activity.get("route_id"),
            "carbs_used": activity.get("carbs_used"), "carbs_ingested": activity.get("carbs_ingested"),
            "w_prime_depletion": activity.get("icu_max_wbal_depletion"), "analysis_issues": activity.get("analysis_issues"),
            "icu_sync_error": activity.get("icu_sync_error"),
        },
        "_defs": field_defs, "_stream_defs": stream_defs, "api_calls": calls,
    }


def _stream_text(info: dict[str, Any], baseline: dict[str, Any] | None, detail_level: str) -> list[str]:
    lines = [f"Streams: {info['listed']} listed ({len(info['standard'])} standard, {len(info['custom'])} custom)"]
    if info["usually_present_missing"] and baseline:
        lines.append(
            f"  usually present on recent {baseline['label']} (n {baseline['n']}) but missing here: "
            + ", ".join(f"{m['type']} ({m['present_on']}/{baseline['n']})" for m in info["usually_present_missing"])
        )
    if detail_level != "compact" and info["custom_defined_absent"]:
        lines.append(f"  custom streams defined but not on this activity: {', '.join(info['custom_defined_absent'][:15])}"
                     + (" ..." if len(info["custom_defined_absent"]) > 15 else ""))
    if info["coverage"] is not None:
        shown = info["coverage"] if detail_level == "full" else [c for c in info["coverage"] if c["valid_pct"] < 100 or c["all_null"]]
        if shown:
            lines.append("  coverage (valid samples): " + ", ".join(
                f"{c['type']} {c['valid_pct']:g} %" + (f" (0 in {c['zero_pct']:g} %)" if c["zero_pct"] and detail_level == "full" else "")
                for c in shown))
        elif info["coverage"]:
            lines.append(f"  coverage: every returned stream has a value in every sample ({len(info['coverage'])} streams)")
    lines.extend(f"  - {issue}" for issue in info["issues"])
    return lines


def _field_text(fields: dict[str, Any], defs: CustomFieldDefs, sport: Any, detail_level: str) -> list[str]:
    scope = f"{fields['expected']} expected for {sport}" if fields["expected_known"] else f"{sport} lists no fields; {fields['expected']} present"
    lines = [
        f"Custom fields ({scope}): {len(fields['value'])} with a value, {len(fields['zero_placeholder'])} zero placeholder(s), "
        f"{len(fields['missing'])} without value (null/NaN), {len(fields['absent'])} absent"
    ]
    for key, label in (("zero_placeholder", "zero placeholders (a real 0 or a source missing from the file)"),
                       ("missing", "without value"), ("absent", "not stored on this activity")):
        if fields[key]:
            lines.append(f"  {label}: {names(fields[key], defs, 20 if detail_level == 'full' else 8)}")
    if fields["usually_present_missing"]:
        lines.append("  usually with a value on recent activities of the sport but not here: "
                     + ", ".join(f"{defs[m['code']].get('name')} [{m['code']}] ({m['present_on']})" for m in fields["usually_present_missing"]))
    if fields["values_outside_scope"] and detail_level != "compact":
        lines.append(f"  values in fields not expected for {sport} (treat with care): {names(fields['values_outside_scope'], defs, 8)}")
    if detail_level == "full" and fields["value"]:
        lines.append(f"  with a value: {names(fields['value'], defs, 40)}")
    return lines


def _activity_text(audit: dict[str, Any], detail_level: str) -> str:  # pylint: disable=too-many-branches
    lines = [f"Data audit of {audit['id']} '{audit.get('name')}' ({audit.get('type')}) {audit['start']}",
             f"Source: {audit['source']['text']}" + (f"; device {audit['source']['device']}" if audit["source"].get("device") else "")]
    if audit["strava_stub"]:
        lines.append(f"- {audit['note']}")
        return "\n".join(lines)
    lines.append(f"Freshness: {freshness_text(audit['freshness'])}")
    later = audit["freshness"]["fields_changed_after_analysis"]
    if later:
        lines.append("  fields without a value whose definition was changed or created after the last analysis (the API gives only "
                     f"the last change time; no value until a reprocess or a write): {names(later, audit['_defs'])}")
    if audit["listing"]:
        lines.append(f"Listing: {audit['listing']['text']}")
    lines.append(f"Recording: {recording_text(audit['recording'])}")
    gaps = audit["recording"].get("gaps")
    if detail_level == "full" and gaps and gaps["list"]:
        lines.append("  gaps: " + ", ".join(f"at {g['at_s']:.0f} s for {g['duration_s']:.0f} s" for g in gaps["list"]))
    laps = audit["laps_and_intervals"]
    lines.append(
        f"Laps and intervals: {laps['laps']:.0f} FIT lap(s)" if laps["laps"] is not None else "Laps and intervals: FIT lap count unknown"
    )
    if laps["intervals"] is not None:
        lines[-1] += f", {laps['intervals']} Intervals.icu interval(s) ({laps['work_intervals']} work)" + (", edited by hand" if laps["edited"] else "")
    lines.extend(f"  - {note}" for note in laps["notes"])
    sensors = audit["sensors"]
    kinds = [k for k, flag in (("power", sensors["power"]), ("heart rate", sensors["heart_rate"]), ("GPS", sensors["gps"])) if flag]
    power = ""
    if sensors["power"]:
        power = f"; power meter {sensors['power_meter'] or 'not named'}" + (f" (serial {sensors['power_meter_serial']})" if sensors["power_meter_serial"] else "")
        power += f", power fields {', '.join(sensors['power_fields'])}" if sensors["power_fields"] else ""
        power += ", second power stream recorded" if sensors["second_power_stream"] else ""
    lines.append(f"Sensors: device {sensors['device'] or 'unknown'}; recorded {', '.join(kinds) or 'no sensor streams'}{power}")
    lines.extend(f"  - {note}" for note in sensors["notes"])
    lines.extend(_stream_text(audit["streams"], audit["baseline"], detail_level))
    defs = audit["_defs"]
    lines.extend(_field_text(audit["fields"], defs, audit.get("type"), detail_level))
    candidates = audit["fields"]["device_file_without_value"]
    if candidates:
        lines.append(f"Device-file fields with a 0, no value or no key: {names(candidates, defs, 10)}; {BRIDGE_NOTE}.")
    context = audit["context_data"]
    present = [label for key, label in (("weather", "weather"), ("route_id", "route"), ("carbs_used", "carbs used (estimate)"),
                                        ("carbs_ingested", "carbs ingested"), ("w_prime_depletion", "W′ depletion")) if context.get(key) not in (None, False)]
    lines.append("Context data present: " + (", ".join(present) if present else "none"))
    if context.get("analysis_issues") or context.get("icu_sync_error"):
        lines.append(f"  analysis issues: {context.get('analysis_issues') or ''} {context.get('icu_sync_error') or ''}".rstrip())
    lines.append(f"API calls: {audit['api_calls']} (custom item definitions and sport settings cached). Facts and counts, no assessment.")
    return "\n".join(lines)


async def _coverage(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    athlete_id: str, start_date: str | None, end_date: str | None, sport_types: str | None, detail_level: str,
) -> dict[str, Any] | str:
    period = resolve_period(start_date, end_date, DEFAULT_PERIOD_DAYS, MAX_PERIOD_DAYS)
    if isinstance(period, str):
        return period
    start, end = period
    index = await get_custom_item_index(athlete_id=athlete_id)
    field_defs, stream_defs = index.get(ACTIVITY_FIELD, {}), index.get(ACTIVITY_STREAM, {})
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id}/activities",
        params={"oldest": start.isoformat(), "newest": end.isoformat(), "fields": ",".join([AUDIT_LIST_FIELDS, *sorted(field_defs)])},
    )
    error = _error(result, "activities")
    if error:
        return error
    activities = filter_types([a for a in result if isinstance(a, dict)] if isinstance(result, list) else [], wanted_types(sport_types))
    types = sorted({str(a.get("type") or "unknown") for a in activities})
    expected = await expected_field_codes(athlete_id, field_defs, types)
    summary = coverage_summary(activities, field_defs, expected, stream_defs)
    if detail_level == "compact":
        for entry in summary.values():
            entry["expected_fields"] = {c: f for c, f in entry["expected_fields"].items() if f["zero_placeholder"] or f["no_value"]}
    return {"athlete_id": athlete_id, "start": start.isoformat(), "end": end.isoformat(), "sport_types": sport_types,
            "activities": len(activities), "by_type": summary, "api_calls": 1}


def _coverage_text(payload: dict[str, Any], detail_level: str) -> str:
    lines = [f"Data coverage for athlete {payload['athlete_id']}, {payload['start']} to {payload['end']}: {payload['activities']} activities"
             + (f" ({payload['sport_types']})" if payload["sport_types"] else "")]
    if not payload["activities"]:
        return lines[0] + "."
    for sport, entry in payload["by_type"].items():
        c, n = entry["counts"], entry["n"]
        sources = ", ".join(f"{k} {v}" for k, v in entry["sources"].items())
        text = (f"{sport} (n {n}; {sources}): power {c['power']} (meter named {c['power_meter_named']}, second power {c['second_power']}), "
                f"HR {c['heart_rate']}, GPS {c['gps']}, weather {c['weather']}, intervals edited {c['intervals_edited']}, "
                f"carbs used {c['carbs_used']}, carbs ingested logged {c['carbs_ingested_logged']}")
        if c["strava_stubs"]:
            text += f", Strava stubs {c['strava_stubs']} (no data via the API)"
        lines.append(text)
        if entry["custom_streams"] and detail_level != "compact":
            lines.append("  custom streams: " + ", ".join(f"{k} {v}/{n}" for k, v in entry["custom_streams"].items()))
        if not entry["expected_fields_known"]:
            lines.append("  no custom fields assigned to this sport or its family")
            continue
        fields = entry["expected_fields"] if detail_level == "full" else {
            code: f for code, f in entry["expected_fields"].items() if f["zero_placeholder"] or f["no_value"]}
        if fields:
            lines.append("  expected fields (value / 0 in a device-file field, real or placeholder / no value): " + "; ".join(
                f"{f['name']} [{code}] {f['value']}/{f['zero_placeholder']}/{f['no_value']}" for code, f in fields.items()))
        elif entry["expected_fields"]:
            lines.append(f"  all {len(entry['expected_fields'])} expected fields have a value on every activity")
    lines.append("Counts per activity type. Intervals.icu stores 0 in a device-file field when the file lacks the source, so such a 0 "
                 "can be real or a placeholder. Facts only, no assessment.")
    return "\n".join(lines)


@tool("read")
async def get_activity_data_audit(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements
    activity_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sport_types: str | None = None,
    athlete_id: str | None = None,
    output_format: str = "text",
    detail_level: str = "standard",
) -> str:
    """Data audit of one activity or data coverage of a period (read-only)

    With activity_id: source (Garmin sync, upload, Strava stub), upload/analysis times, filtered
    duplicates, recording stops, FIT laps vs intervals and edits, sensor identity, streams vs
    those usual for the sport with coverage, custom fields with value / zero placeholder / none,
    fields the Garmin Intervals Bridge could fill. Without activity_id: per sport how many
    activities have power, HR, GPS, weather, custom streams and fields. Facts, no verdict.

    Args:
        activity_id: Activity; omit for the period coverage
        start_date: YYYY-MM-DD (default end_date - 27 days)
        end_date: YYYY-MM-DD (default today)
        sport_types: e.g. "Ride,GravelRide"
        output_format: "text" or "json"
        detail_level: "compact" (no streams), "standard" or "full"
    """
    if detail_level not in DETAIL_LEVELS:
        return f"Error: detail_level must be one of {', '.join(DETAIL_LEVELS)}."
    as_json = output_format.strip().lower() == "json"
    if activity_id:
        audit = await _audit_activity(activity_id, athlete_id, detail_level)
        if isinstance(audit, str):
            return audit
        if as_json:
            return json.dumps({k: v for k, v in audit.items() if not k.startswith("_")}, ensure_ascii=False, default=str)
        return _activity_text(audit, detail_level)
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    payload = await _coverage(athlete_id_to_use, start_date, end_date, sport_types, detail_level)
    if isinstance(payload, str):
        return payload
    if as_json:
        return json.dumps(payload, ensure_ascii=False, default=str)
    return _coverage_text(payload, detail_level)
