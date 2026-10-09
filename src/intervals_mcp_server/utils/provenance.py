"""
Data provenance and data quality of activities (pure functions, no API access).

Where did an activity come from (Garmin Connect sync, file upload, Strava, ...), how fresh is
its analysis, is it a filtered duplicate, what did the device record (laps, recording stops,
sensors, streams) and which custom fields and streams carry values. Everything is derived from
the activity payload, the athlete's custom item definitions, optionally the activity's streams
and a list of recent activities; nothing is tied to one vendor.

Facts used:

- ``source`` is one of STRAVA, UPLOAD, MANUAL, GARMIN_CONNECT, OAUTH_CLIENT, DROPBOX, POLAR,
  SUUNTO, COROS, WAHOO, ZWIFT, ZEPP, CONCEPT2, HUAWEI. The API returns an empty stub for
  activities imported from Strava (Strava's terms), so they carry no metrics or streams.
- ``external_id`` of the Garmin Connect sync is the Garmin activity ID; a Garmin export uploaded
  by hand is "<id>_ACTIVITY.fit", the Garmin Intervals Bridge's upload mode uses "garmin:<id>".
- ``recording_stops`` are the time offsets (s) where the recording paused; ``elapsed_time`` minus
  ``icu_recording_time`` is the time not recorded.
- ``icu_lap_count`` counts the FIT laps; ``icu_intervals_edited`` marks intervals edited by hand,
  which Intervals.icu keeps instead of detecting them again.
- Intervals.icu stores 0 in a custom field filled from the device file when the file lacks the
  source field, so a 0 in such a field is ambiguous ("zero placeholder").
- Custom fields are computed when an activity is analysed: a field defined after the analysis
  has no value until the activity is reprocessed (or the value is written, e.g. by the bridge).
- The activity list leaves filtered duplicates out, so an activity that is not listed on its
  day while a listed one starts within two minutes is a filtered duplicate.
"""

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from intervals_mcp_server.utils.custom_fields import CustomFieldDefs, is_device_file_field, value_status
from intervals_mcp_server.utils.load_metrics import num, rnd
from intervals_mcp_server.utils.sports import hms, sport_family

SOURCE_LABELS = {
    "GARMIN_CONNECT": "Garmin Connect sync", "UPLOAD": "file upload", "STRAVA": "Strava import", "MANUAL": "manual entry",
    "OAUTH_CLIENT": "upload by an API client", "DROPBOX": "Dropbox sync", "POLAR": "Polar sync", "SUUNTO": "Suunto sync",
    "COROS": "COROS sync", "WAHOO": "Wahoo sync", "ZWIFT": "Zwift sync", "ZEPP": "Zepp sync", "CONCEPT2": "Concept2 sync",
    "HUAWEI": "Huawei sync",
}
STUB_KEYS = ("moving_time", "elapsed_time", "distance", "stream_types", "icu_training_load", "average_heartrate")
DUPLICATE_START_S = 120
DUPLICATE_DURATION_PCT = 5.0
RECORDING_GAP_S = 5
REANALYSED_AFTER_S = 600
BASELINE_DAYS = 28
EXPECTED_SHARE = 0.5
MIN_BASELINE = 3
LOW_COVERAGE_PCT = 98.0
AUDIT_LIST_FIELDS = (
    "id,name,type,start_date_local,start_date,elapsed_time,moving_time,source,external_id,strava_id,stream_types,"
    "device_name,power_meter,has_heartrate,device_watts,icu_intervals_edited,trainer,average_weather_temp,"
    "carbs_used,carbs_ingested"
)
_GARMIN_FILE = re.compile(r"^(\d+)_ACTIVITY\.fit$", re.IGNORECASE)
_GARMIN_BRIDGE = re.compile(r"^garmin:(\d+)$", re.IGNORECASE)


def parse_ts(value: Any) -> datetime | None:
    """ISO timestamp as an aware UTC datetime; None when missing or not parseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def span(seconds: float | None) -> str:
    """Human duration: '45 s', '16 min', '1:12 h', '3.5 days'; 'n/a' for None."""
    if seconds is None:
        return "n/a"
    value = abs(seconds)
    if value < 90:
        return f"{value:.0f} s"
    if value < 3600:
        return f"{value / 60:.0f} min"
    if value < 48 * 3600:
        hours, rest = divmod(int(round(value)), 3600)
        return f"{hours}:{rest // 60:02d} h"
    return f"{value / 86400:.1f} days"


# ---------------------------------------------------------------------------
# Source and freshness
# ---------------------------------------------------------------------------


def is_strava_stub(activity: dict[str, Any]) -> bool:
    """True for the empty stub the API returns for an activity imported from Strava."""
    if "strava" in str(activity.get("_note") or "").lower():
        return True
    return str(activity.get("source") or "").upper() == "STRAVA" and not any(
        activity.get(key) not in (None, [], "") for key in STUB_KEYS
    )


def garmin_activity_id(activity: dict[str, Any]) -> tuple[str | None, str | None]:
    """(Garmin activity id, how the file arrived) from source and external_id; (None, None) when unknown."""
    external = str(activity.get("external_id") or "").strip()
    source = str(activity.get("source") or "").upper()
    if source == "GARMIN_CONNECT" and external.isdigit():
        return external, "Garmin Connect sync"
    match = _GARMIN_FILE.match(external)
    if match:
        return match.group(1), "upload of the Garmin export (original FIT)"
    match = _GARMIN_BRIDGE.match(external)
    if match:
        return match.group(1), "Garmin Intervals Bridge upload (original FIT)"
    return None, None


def source_summary(activity: dict[str, Any]) -> dict[str, Any]:
    """Source, file, origin and cross references of an activity."""
    source = str(activity.get("source") or "").upper() or None
    garmin_id, origin = garmin_activity_id(activity)
    label = SOURCE_LABELS.get(source or "", source.lower() if source else "unknown source")
    parts = [origin or label]
    if activity.get("oauth_client_name"):
        parts.append(f"via {activity['oauth_client_name']}")
    if activity.get("file_type") and "FIT" not in (origin or ""):
        parts.append(f"{str(activity['file_type']).upper()} file")
    if garmin_id:
        parts.append(f"Garmin activity {garmin_id}")
    elif activity.get("external_id"):
        parts.append(f"external id {activity['external_id']}")
    if activity.get("strava_id"):
        parts.append(f"also on Strava ({activity['strava_id']})")
    return {
        "source": source, "label": label, "origin": origin, "file_type": activity.get("file_type"),
        "external_id": activity.get("external_id"), "garmin_activity_id": garmin_id, "strava_id": activity.get("strava_id"),
        "oauth_client": activity.get("oauth_client_name"), "device": activity.get("device_name"),
        "strava_stub": is_strava_stub(activity), "text": ", ".join(parts),
    }


def freshness(activity: dict[str, Any], defs: CustomFieldDefs | None = None, scope: set[str] | None = None) -> dict[str, Any]:
    """Upload and analysis times relative to the end of the activity, and fields defined after the analysis."""
    start = parse_ts(activity.get("start_date"))
    elapsed = num(activity.get("elapsed_time"))
    end = start + timedelta(seconds=elapsed) if start and elapsed is not None else None
    created, analysed, synced = (parse_ts(activity.get(k)) for k in ("created", "analyzed", "icu_sync_date"))
    upload_delay = (created - end).total_seconds() if created and end else None
    after_upload = (analysed - created).total_seconds() if analysed and created else None
    later: list[str] = []
    if analysed and defs:
        for code, definition in defs.items():
            if scope is not None and code not in scope:
                continue
            updated = parse_ts(definition.get("updated"))
            if updated and updated > analysed and value_status(definition, activity, code) != "value":
                later.append(code)
    return {
        "end_utc": end.isoformat() if end else None, "uploaded_utc": created.isoformat() if created else None,
        "synced_utc": synced.isoformat() if synced else None, "analysed_utc": analysed.isoformat() if analysed else None,
        "upload_delay_s": rnd(upload_delay, 0), "analysed_after_upload_s": rnd(after_upload, 0),
        "reanalysed": after_upload is not None and after_upload > REANALYSED_AFTER_S,
        "fields_defined_after_analysis": sorted(later),
    }


def freshness_text(info: dict[str, Any]) -> str:
    """'uploaded 1:00 after the end; last analysed 16:49 after the upload (edits or a reprocess trigger a new analysis)'."""
    parts = []
    if info["upload_delay_s"] is not None:
        parts.append(f"uploaded {span(info['upload_delay_s'])} after the end" if info["upload_delay_s"] >= 0
                     else "uploaded before the recorded end (clock or time zone mismatch)")
    if info["analysed_after_upload_s"] is not None:
        text = f"last analysed {span(info['analysed_after_upload_s'])} after the upload"
        if info["reanalysed"]:
            text += " (re-analysed later, e.g. after interval edits, a settings change or a reprocess)"
        parts.append(text)
    elif info["analysed_utc"] is None:
        parts.append("no analysis timestamp")
    return "; ".join(parts) if parts else "no upload or analysis timestamps"


# ---------------------------------------------------------------------------
# Recording, laps and intervals, sensors
# ---------------------------------------------------------------------------


def recording_summary(activity: dict[str, Any], time_data: list[Any] | None = None) -> dict[str, Any]:
    """Elapsed, recorded and moving time, recording stops and (with the time stream) every gap."""
    elapsed, recorded, moving = (num(activity.get(k)) for k in ("elapsed_time", "icu_recording_time", "moving_time"))
    stops = [s for s in activity.get("recording_stops") or [] if num(s) is not None]
    out: dict[str, Any] = {
        "elapsed_s": elapsed, "recorded_s": recorded, "moving_s": moving, "coasting_s": num(activity.get("coasting_time")),
        "not_recorded_s": rnd(max(0.0, elapsed - recorded), 0) if elapsed is not None and recorded is not None else None,
        "stationary_while_recording_s": rnd(max(0.0, recorded - moving), 0) if recorded is not None and moving is not None else None,
        "recording_stops": len(stops), "stop_offsets_s": stops[:50],
        "median_sample_s": num(activity.get("icu_median_time_delta")), "gaps": None,
    }
    if time_data:
        times = [num(t) for t in time_data]
        gaps = [
            {"at_s": rnd(a, 0), "duration_s": rnd(b - a, 0)}
            for a, b in zip(times, times[1:], strict=False) if a is not None and b is not None and b - a > RECORDING_GAP_S
        ]
        out["gaps"] = {"count": len(gaps), "total_s": rnd(sum(g["duration_s"] or 0 for g in gaps), 0),
                       "longest": max(gaps, key=lambda g: g["duration_s"] or 0) if gaps else None, "list": gaps[:20]}
    return out


def recording_text(info: dict[str, Any]) -> str:
    """'elapsed 1:27:23, recorded 1:27:22, moving 1:27:06; 2 recording stops, 0:01 not recorded; sampling 1 s'."""
    text = f"elapsed {hms(info['elapsed_s'])}, recorded {hms(info['recorded_s'])}, moving {hms(info['moving_s'])}"
    if info["recording_stops"] or info["not_recorded_s"]:
        text += f"; {info['recording_stops']} recording stop(s), {hms(info['not_recorded_s'])} not recorded"
    gaps = info.get("gaps")
    if gaps and gaps["count"]:
        longest = gaps["longest"]
        text += f" (gaps > {RECORDING_GAP_S} s in the time stream: {gaps['count']}, longest {hms(longest['duration_s'])} at {hms(longest['at_s'])})"
    if info["median_sample_s"] is not None:
        text += f"; sampling {info['median_sample_s']:g} s" + (" (smart recording: short events can be missed)" if info["median_sample_s"] > 1 else "")
    return text


def laps_and_intervals(activity: dict[str, Any], intervals: list[dict[str, Any]] | None) -> dict[str, Any]:
    """FIT laps against Intervals.icu intervals and what manual interval edits mean."""
    laps = num(activity.get("icu_lap_count"))
    count = len(intervals) if intervals is not None else None
    work = sum(1 for i in intervals or [] if str(i.get("type") or "").upper() == "WORK")
    edited = activity.get("icu_intervals_edited") is True
    notes = []
    if edited:
        notes.append(
            "intervals were edited by hand: Intervals.icu keeps them instead of detecting them again on reanalysis, so "
            "interval statistics, the interval summary and interval searches follow the edited boundaries"
        )
    if count == 0:
        notes.append("no intervals (Intervals.icu detected none, or the file was not processed)")
    elif laps is not None and count is not None and laps != count:
        notes.append(
            f"{laps:.0f} FIT lap(s) vs {count} interval(s): intervals come from Intervals.icu's detection (or the edits), "
            "not 1:1 from the device laps, so lap-based device summaries do not line up with them"
        )
    if activity.get("lock_intervals"):
        notes.append("intervals are locked")
    return {"laps": laps, "intervals": count, "work_intervals": work if intervals is not None else None,
            "edited": edited, "locked": bool(activity.get("lock_intervals")), "notes": notes}


def sensor_summary(activity: dict[str, Any]) -> dict[str, Any]:
    """Device and sensor identity: what the file names and what is missing."""
    types = {str(t) for t in activity.get("stream_types") or []}
    has_power = "watts" in types or activity.get("device_watts") is True
    fields = [str(p) for p in activity.get("power_field_names") or []]
    second = "secondary_power" in types or len(fields) > 1
    has_hr = "heartrate" in types or activity.get("has_heartrate") is True
    notes = []
    if not activity.get("device_name"):
        notes.append("recording device unknown (no device data in the file)")
    if has_power and not activity.get("power_meter"):
        notes.append("power meter name/serial not in the file (identity unknown)")
    if has_power and activity.get("device_watts") is False:
        notes.append("power is not from a power meter (estimated)")
    battery = str(activity.get("power_meter_battery") or "").upper()
    if battery and battery not in ("OK", "GOOD", "NEW"):
        notes.append(f"power meter battery status {activity['power_meter_battery']}")
    if has_hr:
        notes.append("heart rate sensor identity (chest strap or optical) is not exposed by the Intervals.icu API")
    return {
        "device": activity.get("device_name"), "power": has_power, "power_meter": activity.get("power_meter"),
        "power_meter_serial": activity.get("power_meter_serial"), "power_meter_battery": activity.get("power_meter_battery"),
        "power_fields": fields, "second_power_stream": second, "heart_rate": has_hr, "gps": "latlng" in types,
        "trainer": activity.get("trainer"), "notes": notes,
    }


# ---------------------------------------------------------------------------
# Streams and custom fields
# ---------------------------------------------------------------------------


def stream_coverage(stream: dict[str, Any]) -> dict[str, Any]:
    """Samples, share of valid values and of zeros of one stream (arrays count as values)."""
    data = stream.get("data") or []
    samples = len(data)
    numbers = [num(v) for v in data if not isinstance(v, list)]
    valid = sum(1 for v in numbers if v is not None) + sum(1 for v in data if isinstance(v, list) and v)
    zeros = sum(1 for v in numbers if v == 0)
    return {
        "type": stream.get("type"), "custom": bool(stream.get("custom")), "samples": samples,
        "valid_pct": rnd(valid / samples * 100, 1) if samples else 0.0,
        "zero_pct": rnd(zeros / samples * 100, 1) if samples else 0.0,
        "all_null": bool(stream.get("allNull")) or valid == 0,
    }


def baseline_presence(activities: list[dict[str, Any]], field_codes: set[str] | None = None) -> dict[str, Any]:
    """How many of the activities carry each stream and a non-zero value of each field."""
    streams: Counter[str] = Counter()
    fields: Counter[str] = Counter()
    for activity in activities:
        streams.update({str(t) for t in activity.get("stream_types") or []})
        for code in field_codes or set():
            value = num(activity.get(code))
            if value is not None and value != 0:
                fields[code] += 1
    return {"n": len(activities), "streams": dict(streams), "fields": dict(fields),
            "ids": [a.get("id") for a in activities]}


def _usual(counts: dict[str, int], n: int) -> set[str]:
    return {code for code, count in counts.items() if n >= MIN_BASELINE and count / n >= EXPECTED_SHARE}


def stream_inventory(
    activity: dict[str, Any], stream_defs: CustomFieldDefs, streams: list[dict[str, Any]] | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Standard and custom streams listed on the activity, custom streams defined but absent, streams
    usually present on recent activities of the sport but missing here, and (with the fetched streams)
    the coverage of each stream and listed streams the API did not return."""
    listed = [str(t) for t in activity.get("stream_types") or []]
    custom = [t for t in listed if t in stream_defs]
    out: dict[str, Any] = {
        "listed": len(listed), "standard": [t for t in listed if t not in stream_defs], "custom": custom,
        "custom_defined_absent": sorted(code for code in stream_defs if code not in listed),
        "usually_present_missing": [], "baseline_n": None, "coverage": None, "listed_not_returned": None, "issues": [],
    }
    if baseline:
        usual = _usual(baseline["streams"], baseline["n"])
        out["baseline_n"] = baseline["n"]
        out["usually_present_missing"] = [{"type": t, "present_on": baseline["streams"][t]} for t in sorted(usual) if t not in listed]
    if streams is not None:
        coverage = [stream_coverage(s) for s in streams]
        returned = {str(c["type"]) for c in coverage}
        out["coverage"] = coverage
        out["listed_not_returned"] = [t for t in listed if t not in returned]
        out["issues"] = _stream_issues(coverage, out["listed_not_returned"], listed)
    return out


def _stream_issues(coverage: list[dict[str, Any]], not_returned: list[str], listed: list[str]) -> list[str]:
    issues = []
    if listed and len(not_returned) == len(listed):
        issues.append("the streams endpoint returned none of the listed streams (file not retained, e.g. a filtered duplicate)")
    elif not_returned:
        issues.append("listed but not returned: " + ", ".join(not_returned))
    for item in coverage:
        kind = str(item["type"])
        if item["all_null"]:
            issues.append(f"{kind}: listed but empty (no values)")
        elif kind in ("heartrate", "watts", "cadence", "latlng", "velocity_smooth", "distance") and item["valid_pct"] < LOW_COVERAGE_PCT:
            issues.append(f"{kind}: {100 - item['valid_pct']:.1f} % of the samples without a value (dropouts or pauses)")
        if kind == "heartrate" and item["zero_pct"] >= 1:
            issues.append(f"heartrate: {item['zero_pct']:.1f} % of the samples are 0 (sensor contact lost?)")
    return issues


def field_inventory(
    activity: dict[str, Any], defs: CustomFieldDefs, expected: set[str] | None, baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Status of the expected custom activity fields (assigned to the sport, or its family's).

    ``expected`` None means the sport lists no fields: then only fields present on the activity
    are reported. Device-file fields (FIT source or script) with a stored 0 are zero placeholders.
    """
    scope = expected if expected is not None else {code for code in defs if code in activity}
    groups: dict[str, list[str]] = {"value": [], "zero_placeholder": [], "zero": [], "missing": [], "absent": []}
    device_file: list[str] = []
    for code in [c for c in defs if c in scope]:
        definition = defs[code]
        status = value_status(definition, activity, code)
        if is_device_file_field(definition):
            device_file.append(code)
            if status == "zero":
                status = "zero_placeholder"
        groups[status].append(code)
    other = [c for c in defs if c not in scope and value_status(defs[c], activity, c) == "value"]
    usual_missing = []
    if baseline:
        usual = _usual(baseline["fields"], baseline["n"])
        usual_missing = [{"code": c, "present_on": baseline["fields"][c]} for c in sorted(usual)
                         if value_status(defs.get(c), activity, c) != "value" and c in defs]
    return {
        "expected_known": expected is not None, "expected": len([c for c in defs if c in scope]), **groups,
        "device_file": device_file,
        "device_file_without_value": [c for c in device_file if c not in groups["value"]],
        "values_outside_scope": other, "usually_present_missing": usual_missing,
    }


def names(codes: list[str], defs: CustomFieldDefs, limit: int = 12) -> str:
    """'Performance Condition [PerformanceCondition], ...' (capped)."""
    shown = [f"{defs[c].get('name') or c} [{c}]" if c in defs else c for c in codes[:limit]]
    return ", ".join(shown) + (f" (+{len(codes) - limit} more)" if len(codes) > limit else "")


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def _overlaps(activity: dict[str, Any], other: dict[str, Any]) -> bool:
    start, other_start = parse_ts(activity.get("start_date")), parse_ts(other.get("start_date"))
    if not start or not other_start or abs((start - other_start).total_seconds()) > DUPLICATE_START_S:
        return False
    duration, other_duration = num(activity.get("elapsed_time")), num(other.get("elapsed_time"))
    if duration and other_duration:
        return abs(duration - other_duration) / max(duration, other_duration) * 100 <= DUPLICATE_DURATION_PCT
    return True


def listing_status(activity: dict[str, Any], listed: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether the activity appears in the activity list of its day and which listed activities overlap it."""
    is_listed = any(a.get("id") == activity.get("id") for a in listed)
    garmin_id, _ = garmin_activity_id(activity)
    overlaps = []
    for other in listed:
        if other.get("id") == activity.get("id") or not _overlaps(activity, other):
            continue
        other_garmin, _ = garmin_activity_id(other)
        overlaps.append({"id": other.get("id"), "name": other.get("name"), "type": other.get("type"),
                         "source": other.get("source"), "same_garmin_activity": bool(garmin_id and garmin_id == other_garmin)})
    if is_listed and not overlaps:
        status, text = "listed", "listed in the activity list; no other listed activity starts within 2 min"
    elif is_listed:
        status = "listed_with_overlap"
        text = "listed, and " + ", ".join(str(o["id"]) for o in overlaps) + " starts within 2 min with a similar duration (possible duplicate counted twice)"
    elif overlaps:
        first = overlaps[0]
        status = "filtered_duplicate"
        text = (f"NOT in the activity list: filtered duplicate of {first['id']} ('{first['name']}', {first['source'] or 'unknown source'}"
                + (", same Garmin activity" if first["same_garmin_activity"] else "") + "); Intervals.icu leaves it out of lists and the training load")
    else:
        status, text = "not_listed", "NOT in the activity list of its day and no listed activity overlaps it (hidden, deleted or a stub)"
    return {"status": status, "listed": is_listed, "overlapping": overlaps, "text": text}


# ---------------------------------------------------------------------------
# Notes for the activity report (payload only, no extra API call)
# ---------------------------------------------------------------------------


def provenance_notes(
    activity: dict[str, Any], defs: CustomFieldDefs, expected: set[str] | None, intervals: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Short provenance and data-quality notes from the payload and the field definitions."""
    if is_strava_stub(activity):
        return [STRAVA_STUB_NOTE]
    notes = []
    if activity.get("source"):
        info = source_summary(activity)
        notes.append(f"source: {info['text']}; {freshness_text(freshness(activity))}")
    recording = recording_summary(activity)
    if (recording["not_recorded_s"] or 0) > 60:
        notes.append(f"{recording['recording_stops']} recording stop(s), {hms(recording['not_recorded_s'])} not recorded (elapsed vs recorded time)")
    notes.extend(laps_and_intervals(activity, intervals)["notes"][:1])
    fields = field_inventory(activity, defs, expected)
    if fields["zero_placeholder"]:
        notes.append(
            "device-file fields stored as 0 (a real 0 or a source missing from the file, Intervals.icu stores 0 for both): "
            + names(fields["zero_placeholder"], defs, 6)
        )
    later = freshness(activity, defs, expected)["fields_defined_after_analysis"]
    if later:
        notes.append("fields defined after this activity was analysed (no value until a reprocess or a write): " + names(later, defs, 6))
    return notes


STRAVA_STUB_NOTE = (
    "Strava import: the Intervals.icu API returns only an empty stub for activities imported from Strava (no metrics, "
    "streams or intervals); connect the device platform (e.g. Garmin Connect) or upload the original file instead"
)


# ---------------------------------------------------------------------------
# Period coverage
# ---------------------------------------------------------------------------


def _pct(count: int, total: int) -> float | None:
    return rnd(count / total * 100, 0) if total else None


def coverage_summary(  # pylint: disable=too-many-locals
    activities: list[dict[str, Any]], defs: CustomFieldDefs, expected_by_type: dict[str, set[str] | None], stream_defs: CustomFieldDefs,
) -> dict[str, Any]:
    """Per activity type: sources, Strava stubs, power/HR/GPS/weather coverage, custom streams and the
    status of the expected custom fields (value, zero placeholder, no value)."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for activity in activities:
        by_type.setdefault(str(activity.get("type") or "unknown"), []).append(activity)
    out: dict[str, Any] = {}
    for sport, group in sorted(by_type.items(), key=lambda item: -len(item[1])):
        total = len(group)
        types = [{str(t) for t in a.get("stream_types") or []} for a in group]
        power = [a for a, t in zip(group, types, strict=False) if "watts" in t]
        counts = {
            "power": len(power), "power_meter_named": sum(1 for a in power if a.get("power_meter")),
            "second_power": sum(1 for t in types if "secondary_power" in t),
            "heart_rate": sum(1 for a, t in zip(group, types, strict=False) if "heartrate" in t or a.get("has_heartrate") is True),
            "gps": sum(1 for t in types if "latlng" in t), "weather": sum(1 for a in group if num(a.get("average_weather_temp")) is not None),
            "intervals_edited": sum(1 for a in group if a.get("icu_intervals_edited") is True),
            "carbs_used": sum(1 for a in group if num(a.get("carbs_used")) is not None),
            "carbs_ingested_logged": sum(1 for a in group if (num(a.get("carbs_ingested")) or 0) > 0),
            "strava_stubs": sum(1 for a in group if is_strava_stub(a)),
        }
        custom_streams = Counter(t for kinds in types for t in kinds if t in stream_defs)
        expected = expected_by_type.get(sport)
        fields = {}
        for code in sorted(expected or []):
            if code not in defs:
                continue
            statuses = Counter(
                "zero_placeholder" if value_status(defs[code], a, code) == "zero" and is_device_file_field(defs[code])
                else value_status(defs[code], a, code) for a in group
            )
            fields[code] = {"name": defs[code].get("name"), "value": statuses.get("value", 0),
                            "zero_placeholder": statuses.get("zero_placeholder", 0) + statuses.get("zero", 0),
                            "no_value": statuses.get("missing", 0) + statuses.get("absent", 0)}
        out[sport] = {
            "family": sport_family(sport), "n": total, "sources": dict(Counter(str(a.get("source") or "unknown") for a in group)),
            "counts": counts, "pct": {k: _pct(v, total) for k, v in counts.items()},
            "custom_streams": dict(custom_streams.most_common()), "expected_fields": fields,
            "expected_fields_known": expected is not None,
        }
    return out
