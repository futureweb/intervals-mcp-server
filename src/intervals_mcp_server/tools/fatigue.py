"""
Fatigue MCP tools for Intervals.icu: the long-ride fatigue profile (HR, W/bpm, cadence and
stamina at matched power before and after work thresholds, with the prior work behind each
point) and the trend of the submaximal fatigue tests Intervals.icu detects.

Both tools are read-only and report statistics with sample sizes; nothing is calibrated or
judged. Methods: ``utils.fatigue_profile`` and ``utils.submax``.
"""

# pylint: disable=too-many-lines

import json
from typing import Any

from intervals_mcp_server.api.client import seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.tools.gear import get_gear_map
from intervals_mcp_server.tools.performance import (  # pylint: disable=protected-access
    _Api,
    _activities_by_ids,
    _bands,
    _error,
    _num,
    _resolve_range,
    _split,
    cap_ids,
    ids_note,
)
from intervals_mcp_server.utils.custom_fields import ACTIVITY_STREAM
from intervals_mcp_server.utils.fatigue_profile import (
    METHOD,
    across_rides,
    default_band,
    phase_labels,
    ride_profile,
    stamina_codes,
)
from intervals_mcp_server.utils.sports import hms, is_indoor
from intervals_mcp_server.utils.submax import REASONS, context_checks, normalise_test, trends, validity
from intervals_mcp_server.utils.validation import resolve_athlete_id

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

DETAIL_LEVELS = ("compact", "standard", "full")
PROFILE_DEFAULT_DAYS = 180
MAX_PROFILE_RIDES = 12
SUBMAX_DEFAULT_DAYS = 180
MAX_SUBMAX_TESTS = 60
PROFILE_FIELDS = (
    "id,name,type,start_date_local,gear,icu_ftp,icu_weight,icu_joules,moving_time,elapsed_time,stream_types,"
    "trainer,device_name,power_meter"
)
SUBMAX_FIELDS = "id,name,type,start_date_local,gear,trainer,icu_ftp,submax_fatigue_test"
CORE_STREAMS = ("time", "watts", "heartrate", "cadence", "temp", "altitude", "fixed_altitude", "distance", "velocity_smooth")
PROFILE_NOTE = (
    "Heart rate at a given power also follows heat, hydration, fuelling, sleep and the day's form, and outdoor "
    "steady segments differ in terrain and cadence; the changes are statistics of this ride / these rides, not a "
    "fitness verdict. Work above FTP before a point is context (riders often go harder on good days), not a predictor."
)
PROFILE_BACKGROUND = (
    "Background: Intervals.icu fatigued power curves after kJ0/kJ1 (forum 'Fatigue resistance'), kJ per kg thresholds "
    "('Power curve after kj/kg') and work above FTP vs the 'good-day' effect ('Three ways field data fooled me about "
    "durability') on forum.intervals.icu."
)
SUBMAX_NOTE = (
    "Only valid tests feed the trend; a detection inside a regular workout is listed, not used as a benchmark. HR at "
    "the end of the test depends on the target (FTP changes), heat, fatigue and the power meter; the trend is a "
    "statistic with n, not a fitness verdict. HRRc 0 without a recovery window means 'not measured'."
)


def _fmt(value: Any, digits: int = 0, unit: str = "", signed: bool = False) -> str:
    number = _num(value)
    if number is None:
        return "n/a"
    return f"{number:{'+' if signed else ''}.{digits}f}{unit}"


def _parse_thresholds(text: str) -> list[float] | str:
    try:
        values = sorted({float(part) for part in _split(text)})
    except ValueError:
        return "Error: work_thresholds must be a comma-separated list of numbers, e.g. '750,1500'."
    if not values or values[0] <= 0 or len(values) > 3:
        return "Error: work_thresholds needs 1 to 3 positive values, e.g. '750,1500'."
    return values


def _gear_id(activity: dict[str, Any]) -> str | None:
    gear = activity.get("gear")
    if isinstance(gear, dict) and gear.get("id"):
        return str(gear["id"])
    return str(activity["gear_id"]) if activity.get("gear_id") else None


async def _ride_list(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    api: _Api, athlete_id: str, activity_ids: list[str], span: tuple[str, str], fields: str, sport_types: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Activities by id (already de-duplicated and capped) or of the date range (filtered by sport, newest first)."""
    if activity_ids:
        return await _activities_by_ids(api, activity_ids)
    result = await api.get(f"/athlete/{seg(athlete_id)}/activities", {"oldest": span[0], "newest": span[1], "fields": fields})
    error = _error(result, "activities")
    if error:
        return [], error
    wanted = {sport.lower() for sport in _split(sport_types)}
    activities = [
        a for a in (result if isinstance(result, list) else [])
        if isinstance(a, dict) and (not wanted or str(a.get("type") or "").lower() in wanted)
    ]
    activities.sort(key=lambda a: str(a.get("start_date_local") or ""), reverse=True)
    return activities, None


# ------------------------------------------------------------ long-ride profile
def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _prior_text(prior: dict[str, Any] | None, with_kj: bool = True) -> str:
    if not prior:
        return "n/a"
    share = f" ({prior['above_ftp_share_pct']:.1f} % of the work)" if prior.get("above_ftp_share_pct") is not None else ""
    per_kg = f" ({prior['kj_per_kg']:.1f} kJ/kg)" if prior.get("kj_per_kg") is not None else ""
    above = prior["kj_above_ftp"]
    text = (
        f"{above:.{1 if above < 10 else 0}f} kJ above FTP{share}, {hms(prior['secs_above_ftp'])} above FTP, "
        f"{_plural(prior['efforts_above_ftp'], 'effort')} >= 60 s above FTP"
    )
    return f"{prior['kj']} kJ{per_kg}, {text}" if with_kj else text


def _stamina_text(first: Any, last: Any, potential_first: Any = None, potential_last: Any = None) -> str:
    if first is None and potential_first is None:
        return ""
    text = f", stamina {_fmt(first)}->{_fmt(last)}"
    if potential_first is not None:
        text += f" (potential {_fmt(potential_first)}->{_fmt(potential_last)})"
    return text


def _phase_text(phase: dict[str, Any]) -> str:
    if not phase["segments"]:
        return f"{phase['label']}: no steady segments"
    small = " [small sample]" if phase["small_sample"] else ""
    temp = f", {phase['temp_c']:.0f} °C" if phase.get("temp_c") is not None else ""
    return (
        f"{phase['label']}: {phase['segments']} seg / {phase['minutes']:.1f} min, {_fmt(phase['watts'])} W, {_fmt(phase['hr'])} bpm, "
        f"{_fmt(phase['w_per_bpm'], 2)} W/bpm, {_fmt(phase['cadence'])} rpm{temp}"
        f"{_stamina_text(phase['stamina_first'], phase['stamina_last'])}{small}"
    )


def _change_text(change: dict[str, Any]) -> str:
    if not change["comparable"]:
        return f"{change['label']}: not comparable (no segments in one of the phases)"
    small = " [small sample]" if change["small_sample"] else ""
    if change.get("cadence_shift"):
        small += " [cadence differs by more than 15 rpm]"
    temp = f", temp {_fmt(change['temp_c'], 1, ' °C', True)}" if change.get("temp_c") is not None else ""
    return (
        f"{change['label']}: HR {_fmt(change['hr_bpm'], 1, ' bpm', True)}, W/bpm {_fmt(change['w_per_bpm_pct'], 1, ' %', True)}, "
        f"cadence {_fmt(change['cadence_rpm'], 1, ' rpm', True)}, power {_fmt(change['watts_w'], 1, ' W', True)}{temp}{small}"
    )


def _climb_text(climb: dict[str, Any], after_h: float) -> str:
    when = f"after {after_h:g} h" if climb["late"] else f"before {after_h:g} h"
    return (
        f"at {hms(climb['start_s'])} [{when}]: {hms(climb['duration_s'])}, {_fmt(climb['gain_m'])} m, {_fmt(climb['avg_grade_pct'], 1)} %, "
        f"{_fmt(climb['avg_watts'])} W (NP {_fmt(climb['normalized_power'])}), {_fmt(climb['avg_hr'])} bpm, "
        f"{_fmt(climb['w_per_bpm'], 2)} W/bpm, {_fmt(climb['cadence'])} rpm"
        f"{_stamina_text(climb['stamina_start'], climb['stamina_end'], climb['potential_start'], climb['potential_end'])}; "
        f"before: {_prior_text(climb['prior_work'])}"
    )


def _ride_lines(ride: dict[str, Any], detail_level: str, after_h: float) -> list[str]:  # pylint: disable=too-many-locals
    profile = ride["profile"]
    weight = f", {ride['weight_kg']:.1f} kg ({ride['weight_source']})" if ride.get("weight_kg") else ", body mass unknown"
    above = (
        f"; above FTP {_fmt(profile['kj_above_ftp'])} kJ, {hms(profile['secs_above_ftp'])}, "
        f"{_plural(profile['efforts_above_ftp'], 'effort')} >= 60 s"
        if profile.get("kj_above_ftp") is not None else "; no FTP, work above FTP n/a"
    )
    per_kg = f" ({profile['total_kj_per_kg']:.1f} kJ/kg)" if profile.get("total_kj_per_kg") is not None else ""
    lines = [
        f"{ride['date']} {ride['type']} '{ride['name']}' ({ride['id']}), {ride['gear'] or 'no gear'}, FTP {_fmt(ride['ftp'], 0, ' W')}"
        f"{weight}: {profile['total_kj']} kJ{per_kg}, {hms(profile['elapsed_s'])} elapsed{above}"
    ]
    if detail_level != "compact":
        crossings = []
        for row in profile["thresholds"]:
            label = f"{row['kj']:.0f} kJ" + (f" ({row['kj_per_kg']:.1f} kJ/kg)" if row.get("kj_per_kg") is not None else "")
            if not row["reached"]:
                crossings.append(f"{label} not reached")
                continue
            prior = row["prior_work"]
            stamina = ""
            if row.get("stamina") is not None:
                stamina = f"; stamina {_fmt(row['stamina'])}" + (
                    f", potential {_fmt(row['potential_stamina'])}" if row.get("potential_stamina") is not None else "")
            crossings.append(f"{label} at {hms(prior['elapsed_s'])}: {_prior_text(prior, with_kj=False)}{stamina}")
        lines.append("  Thresholds: " + " | ".join(crossings))
    for band in profile["bands"]:
        if detail_level != "compact":
            lines.append(f"  {band['band']}: " + " | ".join(_phase_text(phase) for phase in band["phases"]))
        reference = band["phases"][0]["label"]
        lines.append(f"  {band['band']} change vs {reference}: " + "; ".join(_change_text(c) for c in band["changes"]))
    climbs = profile["climbs"]
    if detail_level == "compact":
        late = sum(1 for c in climbs if c["late"])
        lines.append(f"  Climbs: {len(climbs)} ({late} after {after_h:g} h)")
    else:
        for climb in climbs:
            lines.append("  Climb " + _climb_text(climb, after_h))
        if not climbs:
            lines.append(f"  Climbs: none detected{' (' + profile['climb_note'] + ')' if profile.get('climb_note') else ''}")
    if detail_level == "full":
        for segment in profile["segments"]:
            lines.append(
                f"    segment {segment['band']} phase {segment['phase']} at {hms(segment['start_s'])} for {hms(segment['duration_s'])}: "
                f"{_fmt(segment['watts'])} W, {_fmt(segment['hr'])} bpm, {_fmt(segment['w_per_bpm'], 2)} W/bpm, {_fmt(segment['cadence'])} rpm"
                f"{_stamina_text(segment['stamina_start'], segment['stamina_end'])}; before: {_prior_text(segment['prior_work'])}"
            )
    return lines


def _describe_text(stats: dict[str, Any], unit: str) -> str:
    if not stats["n"]:
        return "n/a"
    return f"{stats['median']:+.1f}{unit} [{stats['min']:+.1f}..{stats['max']:+.1f}] n {stats['n']}"


def _across_lines(rows: list[dict[str, Any]], reference: str) -> list[str]:
    lines = [f"Across rides (change vs {reference}; each ride once; median [min..max] n):"]
    for row in rows:
        small = " - small sample, not reliable" if row["small_sample"] else ""
        lines.append(
            f"  {row['band']}, {row['label']}: {row['rides']} rides; HR {_describe_text(row['hr_bpm'], ' bpm')}, "
            f"W/bpm {_describe_text(row['w_per_bpm_pct'], ' %')}, cadence {_describe_text(row['cadence_rpm'], ' rpm')}, "
            f"power {_describe_text(row['watts_w'], ' W')}{small}"
            + (f" ({row['rides_with_small_phase_samples']} with small phase samples)" if row["rides_with_small_phase_samples"] else "")
            + (f" ({row['rides_with_cadence_shift']} with cadence differing by > 15 rpm)" if row["rides_with_cadence_shift"] else "")
        )
        split = row.get("by_prior_intensity")
        if split:
            parts = []
            for name, label in (("lower_share", "lower"), ("higher_share", "higher")):
                group = split[name]
                parts.append(
                    f"{label} share above FTP (median {group['above_ftp_share_pct']['median']:.1f} %): "
                    f"HR {_describe_text(group['hr_bpm'], ' bpm')}, W/bpm {_describe_text(group['w_per_bpm_pct'], ' %')}"
                    + (" - small sample" if group["rides"] < 3 else "")
                )
            lines.append("    by work above FTP before the threshold: " + "; ".join(parts))
    return lines


def _heterogeneity(rides: list[dict[str, Any]]) -> list[str]:
    notes = []
    gears = {ride["gear_id"] for ride in rides if ride.get("gear_id")}
    if len(gears) > 1:
        notes.append(f"{len(gears)} bikes / power meters mixed: absolute power bands are not calibrated against each other")
    indoor = sum(1 for ride in rides if ride["indoor"])
    if 0 < indoor < len(rides):
        notes.append(f"indoor and outdoor rides mixed ({indoor} indoor)")
    return notes


async def _stamina_names(athlete_id: str) -> dict[str, str]:
    try:
        index = await get_custom_item_index(athlete_id=athlete_id)
    except Exception:  # pylint: disable=broad-exception-caught  # names only help to recognise streams
        return {}
    return {str(code): str(item.get("name") or "") for code, item in (index.get(ACTIVITY_STREAM) or {}).items()}


@tool("read")
async def get_long_ride_fatigue_profile(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements,too-many-branches,too-many-statements
    activity_ids: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sport_types: str = "Ride,GravelRide",
    power_bands: str | None = None,
    work_thresholds: str = "750,1500",
    threshold_unit: str = "kj",
    min_segment_secs: int = 120,
    climb_after_hours: float = 2.0,
    min_climb_gain_m: float = 100.0,
    limit: int = 6,
    output_format: str = "text",
    detail_level: str = "standard",
    athlete_id: str | None = None,
) -> str:
    """Long-ride fatigue profile: HR, W/bpm, cadence and stamina at matched power before vs after work thresholds

    For given rides (activity_ids) or the long rides of a period (default 180 days, rides
    reaching the highest threshold), steady segments in a power band (default 75-85 % FTP)
    are split into phases by work (default 750 / 1,500 kJ, or kJ per kg body mass) and
    compared with the phase before the first threshold. Each threshold and climb carries the
    prior work (kJ, kJ/kg, kJ and time above FTP, efforts above FTP) and Garmin stamina /
    potential stamina at its start and end. Across rides: median and range of the changes
    with n, small samples flagged, rides split by prior work above FTP. Method in the output;
    statistics only, no verdict. API calls: activity list (or one per id) plus one stream
    request per ride.

    Args:
        activity_ids: Comma-separated activity IDs (optional; default the long rides of the period)
        start_date: Start date YYYY-MM-DD (optional, default 180 days before end_date)
        end_date: End date YYYY-MM-DD (optional, default today)
        sport_types: Sports of the period search (default "Ride,GravelRide")
        power_bands: Bands in W as "low-high", e.g. "180-200" (optional, default 75-85 % FTP)
        work_thresholds: 1-3 work thresholds (default "750,1500")
        threshold_unit: "kj" (default) or "kj_per_kg"
        min_segment_secs: Minimum steady segment length in s, >= 90 (default 120)
        climb_after_hours: Climbs starting after this many hours are marked late (default 2)
        min_climb_gain_m: Minimum climb gain in m (default 100)
        limit: Rides of the period, newest first, 1-12 (default 6)
        output_format: "text" (default) or "json"
        detail_level: "compact", "standard" (default) or "full" (adds every segment)
        athlete_id: The Intervals.icu athlete ID (optional, default ATHLETE_ID)
    """
    athlete, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if detail_level not in DETAIL_LEVELS:
        return f"Error: detail_level must be one of {', '.join(DETAIL_LEVELS)}."
    unit = threshold_unit.strip().lower()
    if unit not in ("kj", "kj_per_kg"):
        return "Error: threshold_unit must be 'kj' or 'kj_per_kg'."
    thresholds = _parse_thresholds(work_thresholds)
    if isinstance(thresholds, str):
        return thresholds
    bands = _bands(power_bands) if power_bands else None
    if isinstance(bands, str):
        return bands
    if min_segment_secs < 90:
        return "Error: min_segment_secs must be at least 90 (the first 60 s of a segment are HR settling time)."
    if climb_after_hours < 0 or min_climb_gain_m <= 0:
        return "Error: climb_after_hours must not be negative and min_climb_gain_m must be positive."
    span = _resolve_range(start_date, end_date, PROFILE_DEFAULT_DAYS)
    if isinstance(span, str):
        return span
    capped = min(max(limit, 1), MAX_PROFILE_RIDES)
    ids, dropped_ids, duplicate_ids = cap_ids(activity_ids, capped)

    api = _Api()
    activities, error = await _ride_list(api, athlete, ids, span, PROFILE_FIELDS, None if ids else sport_types)
    if error:
        return error
    profile_weight: float | None = None
    if any(not _num(a.get("icu_weight")) for a in activities):
        result = await api.get(f"/athlete/{seg(athlete)}")
        profile_weight = _num(result.get("icu_weight")) if isinstance(result, dict) else None

    def weight_of(activity: dict[str, Any]) -> tuple[float | None, str]:
        if _num(activity.get("icu_weight")):
            return _num(activity.get("icu_weight")), "activity"
        return profile_weight, "athlete profile" if profile_weight else "unknown"

    skipped: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    for activity in activities:
        weight, _ = weight_of(activity)
        needed = thresholds[-1] * (weight or 0) if unit == "kj_per_kg" else thresholds[-1]
        joules = _num(activity.get("icu_joules"))
        if unit == "kj_per_kg" and not weight:
            skipped.append({"id": activity.get("id"), "reason": "no body mass for kJ/kg thresholds"})
        elif not ids and (joules is None or joules / 1000 < needed):
            continue  # not a long ride for these thresholds
        else:
            selected.append(activity)
    beyond_limit = max(0, len(selected) - capped)
    selected = selected[:capped]
    if not selected:
        what = f"activities {', '.join(ids)}" if ids else (
            f"{sport_types} rides between {span[0]} and {span[1]} reaching {thresholds[-1]:g} {'kJ/kg' if unit == 'kj_per_kg' else 'kJ'}")
        return f"No {what} found." + (f" Skipped: {skipped}" if skipped else "")
    if bands is None:
        band = default_band(_num(selected[0].get("icu_ftp")))
        if band is None:
            return "Error: the newest ride has no FTP; pass power_bands, e.g. '180-200'."
        bands = [band]
        band_source = f"default 75-85 % of FTP {_num(selected[0].get('icu_ftp')):g} W of the newest ride"
    else:
        band_source = "given"
    names = await _stamina_names(athlete)
    gear_map = await get_gear_map(athlete_id=athlete)
    unit_label = "kJ/kg" if unit == "kj_per_kg" else "kJ"
    labels = phase_labels(thresholds, unit_label)
    rides: list[dict[str, Any]] = []
    for activity in selected:
        weight, weight_source = weight_of(activity)
        listed = [{"type": t} for t in activity.get("stream_types") or []]
        codes = stamina_codes(listed, names)
        result = await api.get(f"/activity/{seg(activity.get('id'))}/streams", {"types": ",".join([*CORE_STREAMS, *codes.values()])})
        streams = [s for s in result if isinstance(s, dict)] if isinstance(result, list) else []
        if not streams:
            skipped.append({"id": activity.get("id"), "reason": _error(result, "streams") or "no streams returned"})
            continue
        codes = codes or stamina_codes(streams, names)
        thresholds_kj = [t * weight for t in thresholds] if unit == "kj_per_kg" and weight else list(thresholds)
        profile = ride_profile(
            streams, ftp=_num(activity.get("icu_ftp")), weight=weight, thresholds_kj=thresholds_kj, bands=bands,
            stamina=codes, sport=activity.get("type"), min_segment_secs=min_segment_secs,
            min_climb_gain_m=min_climb_gain_m, climb_after_s=climb_after_hours * 3600, labels=labels,
        )
        if "error" in profile:
            skipped.append({"id": activity.get("id"), "reason": profile["error"]})
            continue
        gear_id = _gear_id(activity)
        rides.append({
            "id": activity.get("id"), "name": activity.get("name"), "type": activity.get("type"),
            "date": str(activity.get("start_date_local") or "")[:10], "gear_id": gear_id,
            "gear": f"{gear_map[gear_id]} ({gear_id})" if gear_id and gear_map.get(gear_id) else gear_id,
            "indoor": is_indoor(activity), "ftp": _num(activity.get("icu_ftp")), "weight_kg": weight,
            "weight_source": weight_source, "thresholds_kj": [round(t) for t in thresholds_kj], "profile": profile,
        })
    across = across_rides(rides, labels) if len(rides) > 1 else []
    notes = _heterogeneity(rides)
    band_labels = [f"{low:g}-{high:g} W" for low, high in bands]
    source = f"{len(ids)} activity id(s)" if ids else f"{span[0]} to {span[1]}, {sport_types}"
    source += ids_note(dropped_ids, duplicate_ids, capped)

    if output_format.strip().lower() == "json":
        for ride in rides:
            if detail_level != "full":
                ride["profile"] = {k: v for k, v in ride["profile"].items() if k != "segments"}
            if detail_level == "compact":
                ride["profile"]["climbs"] = [c for c in ride["profile"]["climbs"] if c["late"]]
        payload = {
            "athlete_id": athlete, "source": source, "bands": band_labels, "band_source": band_source,
            "thresholds": {"values": thresholds, "unit": unit_label, "phases": labels},
            "settings": {"min_segment_secs": min_segment_secs, "climb_after_hours": climb_after_hours,
                         "min_climb_gain_m": min_climb_gain_m, "limit": capped, "rides_beyond_limit": beyond_limit,
                         "ids_beyond_limit": dropped_ids, "duplicate_ids_ignored": duplicate_ids},
            "method": METHOD, "rides": rides, "across_rides": across, "skipped": skipped,
            "notes": [*notes, PROFILE_NOTE, PROFILE_BACKGROUND], "api_calls": api.calls,
        }
        return json.dumps(payload, ensure_ascii=False)
    lines = [
        f"Long-ride fatigue profile for athlete {athlete}: {len(rides)} ride(s) ({source}"
        + (f"; {beyond_limit} older beyond limit {capped}" if beyond_limit else "")
        + f"), band {', '.join(band_labels)} ({band_source}), thresholds {' / '.join(f'{t:g}' for t in thresholds)} {unit_label}, "
        f"segments >= {min_segment_secs} s, climbs >= {min_climb_gain_m:g} m.",
    ]
    if detail_level != "compact":
        lines.append(f"Method: {METHOD}")
    for ride in rides:
        lines.extend(_ride_lines(ride, detail_level, climb_after_hours))
    if across:
        lines.extend(_across_lines(across, labels[0]))
    elif len(rides) == 1:
        lines.append("One ride: no across-ride statistics (pass a period or several activity_ids).")
    for item in skipped:
        lines.append(f"Skipped {item['id']}: {item['reason']}")
    phases = [p for ride in rides for band in ride["profile"]["bands"] for p in band["phases"]]
    if phases and sum(1 for p in phases if p["small_sample"]) > len(phases) / 2:
        lines.append("Most phases have few steady segments: a wider power band (power_bands), a shorter min_segment_secs or "
                     "more rides give larger samples.")
    lines.extend(f"Note: {note}" for note in notes)
    lines.append(PROFILE_NOTE)
    if detail_level != "compact":
        lines.append(PROFILE_BACKGROUND)
    lines.append(f"API calls: {api.calls} (plus gear catalog and stream definitions unless cached)")
    return "\n".join(lines)


# ------------------------------------------------------------ submax test trends
def _sft_settings(result: Any) -> dict[str, dict[str, Any]]:
    """Submax test configuration per activity type from the sport settings."""
    out: dict[str, dict[str, Any]] = {}
    for setting in result if isinstance(result, list) else []:
        if not isinstance(setting, dict):
            continue
        config_row = {k: setting.get(k) for k in setting if str(k).startswith("sft_")}
        for sport in setting.get("types") or []:
            out[str(sport)] = config_row
    return out


def _settings_text(settings: dict[str, dict[str, Any]], sports: set[str]) -> str:
    parts = []
    for sport in sorted(sports):
        row = settings.get(sport)
        if not row or str(row.get("sft_type") or "NONE") == "NONE":
            continue
        start = _num(row.get("sft_max_start_secs"))
        base = "threshold pace" if row.get("sft_type") == "PACE" else "FTP"
        if row.get("sft_ftp") or row.get("sft_threshold_pace"):
            base = f"{row.get('sft_ftp') or row.get('sft_threshold_pace')}"
        parts.append(
            f"{sport}: {row.get('sft_type')} {row.get('sft_duration')} s at {row.get('sft_target_percent')} % of {base} "
            f"(±{row.get('sft_tolerance_percent')} %, CV <= {row.get('sft_max_cv_percent')} %, start within "
            f"{f'{start / 60:g} min' if start else 'n/a'})"
        )
    return "; ".join(parts) or "not available"


def _test_line(test: dict[str, Any], detail_level: str) -> str:
    unit = "W" if test["test_type"] == "POWER" else "m/s"
    digits = 0 if unit == "W" else 2
    context = test.get("context") or {}
    recovery = f"HRRc {_fmt(test['hrrc_bpm'])} bpm" if test["recovery_measured"] else "HRRc not measured"
    line = (
        f"{test['date']} {test['type']} '{test['name']}' ({test['activity_id']}): {test['test_type']} {_fmt(test['duration_s'])} s, "
        f"{_fmt(test['average'], digits, ' ' + unit)} vs target {_fmt(test['target'], digits, ' ' + unit)} "
        f"({_fmt(test['deviation_pct'], 1, ' %', True)}), CV {_fmt(test['cv_pct'], 1, ' %')}, HR end {_fmt(test['final_bpm'])} bpm, "
        f"EF {_fmt(test['efficiency_factor'], 2)}, {recovery}"
    )
    if context.get("checked"):
        line += (
            f", HR {_fmt(context.get('hr_start'))}->{_fmt(context.get('hr_end'))} bpm, power 5 min before "
            f"{_fmt(context.get('power_before_w'), 0, ' W')}, 60 s after {_fmt(context.get('power_after_w'), 0, ' W')}"
        )
        if context.get("hr_drop_60s") is not None:
            line += f", HR drop 60 s after {_fmt(context['hr_drop_60s'], 0, ' bpm')} (stream)"
    elif context.get("note") and detail_level == "full":
        line += f" [{context['note']}]"
    status = "VALID" if not test["reasons"] else "EXCLUDED: " + "; ".join(r["text"] for r in test["reasons"])
    return f"{line} -> {status}"


def _trend_text(trend: dict[str, Any], label: str, unit: str, digits: int) -> str:
    if not trend["n"]:
        return f"  {label}: no valid values"
    text = f"  {label}: n {trend['n']}, {_fmt(trend['first'], digits)} -> {_fmt(trend['last'], digits)}{unit} " \
           f"(change {_fmt(trend['change'], digits, unit, True)})"
    if trend.get("sd") is not None:
        text += f", mean {_fmt(trend['mean'], digits)}{unit}, sd {_fmt(trend['sd'], digits)}{unit}"
    if trend.get("slope_per_week") is not None:
        text += f", slope {_fmt(trend['slope_per_week'], digits + 1, unit + '/week', True)} over {trend['weeks']:g} weeks"
    if trend["small_sample"]:
        text += " - small sample, not reliable"
    return text


TREND_LABELS = (
    ("final_bpm", "HR at the end of the test (Intervals.icu)", " bpm", 0),
    ("efficiency_factor", "Efficiency factor (average / final HR)", None, 3),
    ("hrrc_bpm", "HR recovery HRRc (Intervals.icu)", " bpm", 0),
    ("hr_rise", "HR rise during the test (stream)", " bpm", 0),
    ("hr_drop_60s", "HR drop 60 s after the test (stream, easy minute only)", " bpm", 0),
)


def _group_lines(group: dict[str, Any], detail_level: str) -> list[str]:
    """Trend block of one sport family and test type (its own units, never pooled with others)."""
    units = group["units"]
    pace = group["test_type"] == "PACE"
    lines = [
        f"Trend for {group['sport_family']} {group['test_type']} tests ({', '.join(group['sports'])}; average in "
        f"{units['average']}, efficiency factor in {units['efficiency_factor']}) over {group['tests']} valid test(s):"
    ]
    for key, label, unit, digits in TREND_LABELS:
        if detail_level == "compact" and key in ("hr_rise", "hr_drop_60s"):
            continue
        if unit is None:
            unit, digits = f" {units['efficiency_factor']}", 4 if pace else digits
        lines.append(_trend_text(group["metrics"][key], label, unit, digits))
    if group["weeks"] and detail_level != "compact":
        lines.append("  Per ISO week (means of valid tests):")
        for week in group["weeks"]:
            lines.append(
                f"    {week['week']}: {week['tests']} test(s), HR end {_fmt(week['final_bpm'])} bpm, "
                f"EF {_fmt(week['efficiency_factor'], 4 if pace else 3)} {units['efficiency_factor']}, HRRc {_fmt(week['hrrc_bpm'])} bpm, "
                f"average {_fmt(week['average'], 2 if pace else 0, ' ' + units['average'])} vs target "
                f"{_fmt(week['target'], 2 if pace else 0, ' ' + units['average'])}"
            )
    lines.extend(f"  Note: {note}" for note in group["notes"])
    return lines


@tool("read")
async def get_submax_test_trends(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements,too-many-branches,too-many-statements
    start_date: str | None = None,
    end_date: str | None = None,
    sport_types: str | None = None,
    tolerance_pct: float | None = None,
    require_recovery: bool = False,
    check_context: bool = True,
    limit: int = 30,
    output_format: str = "text",
    detail_level: str = "standard",
    athlete_id: str | None = None,
) -> str:
    """Submaximal fatigue tests detected by Intervals.icu (#SFT): validity and trend over the weeks

    Lists the tests of the period (default 180 days) with target, average, CV, HR at the end,
    efficiency factor and HR recovery (HRRc; missing is never 0) and trends the valid ones per
    sport family and test type (power and pace never pooled) and ISO week: n, change, slope,
    SD. Valid: average within the tolerance (default the test's own), CV within its limit, not
    ignored and, with check_context, not part of a longer work interval, not continued after
    the test and not after hard riding. Detections inside a regular workout are excluded with
    the reason. HRRc trends use tests with a recovery part (require_recovery excludes the
    others). Statistics only, no verdict. API calls: sport settings, activity list, plus
    intervals and streams per test with check_context.

    Args:
        start_date: Start date YYYY-MM-DD (optional, default 180 days before end_date)
        end_date: End date YYYY-MM-DD (optional, default today)
        sport_types: Comma-separated sports (optional, default all)
        tolerance_pct: Allowed deviation from the target in % (optional, default the test's own)
        require_recovery: Exclude tests without HR recovery (default False)
        check_context: Check intervals and streams around each power test (default True)
        limit: Tests, newest first, 1-60 (default 30)
        output_format: "text" (default) or "json"
        detail_level: "compact", "standard" (default) or "full"
        athlete_id: The Intervals.icu athlete ID (optional, default ATHLETE_ID)
    """
    athlete, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if detail_level not in DETAIL_LEVELS:
        return f"Error: detail_level must be one of {', '.join(DETAIL_LEVELS)}."
    if tolerance_pct is not None and not 0 < tolerance_pct <= 50:
        return "Error: tolerance_pct must be between 0 and 50."
    span = _resolve_range(start_date, end_date, SUBMAX_DEFAULT_DAYS)
    if isinstance(span, str):
        return span
    capped = min(max(limit, 1), MAX_SUBMAX_TESTS)

    api = _Api()
    settings_result = await api.get(f"/athlete/{seg(athlete)}/sport-settings")
    settings = _sft_settings(settings_result) if not _error(settings_result, "sport settings") else {}
    activities, error = await _ride_list(api, athlete, [], span, SUBMAX_FIELDS, sport_types)
    if error:
        return error
    tests = [test for activity in activities if (test := normalise_test(activity)) is not None]
    beyond_limit = max(0, len(tests) - capped)
    tests = tests[:capped]
    for test in tests:
        if check_context and test["test_type"] == "POWER":
            intervals_result = await api.get(f"/activity/{seg(test['activity_id'])}/intervals")
            items = intervals_result.get("icu_intervals") if isinstance(intervals_result, dict) else None
            intervals = [i for i in items or [] if isinstance(i, dict)]
            stream_result = await api.get(f"/activity/{seg(test['activity_id'])}/streams", {"types": "time,watts,heartrate"})
            streams = {
                str(s.get("type")): s.get("data") or []
                for s in (stream_result if isinstance(stream_result, list) else []) if isinstance(s, dict)
            }
            test["context"] = context_checks(test, streams, intervals)
        else:
            test["context"] = {"checked": False, "inside_workout": [], "hard_before": None,
                               "note": "context not checked" + ("" if check_context else " (check_context=False)")}
        test["reasons"] = validity(test, tolerance_pct, require_recovery)
    valid = [t for t in tests if not t["reasons"]]
    excluded = [t for t in tests if t["reasons"]]
    trend = trends(valid)
    by_reason: dict[str, int] = {}
    for test in excluded:
        for reason in test["reasons"]:
            by_reason[reason["code"]] = by_reason.get(reason["code"], 0) + 1
    sports = {str(t["type"]) for t in tests} or set(_split(sport_types)) or {"Ride"}

    if output_format.strip().lower() == "json":
        rows = tests if detail_level != "compact" else [
            {k: t[k] for k in ("activity_id", "date", "average", "target", "final_bpm", "efficiency_factor", "hrrc_bpm")}
            | {"valid": not t["reasons"], "reasons": [r["code"] for r in t["reasons"]]} for t in tests
        ]
        payload = {
            "athlete_id": athlete, "start": span[0], "end": span[1], "sport_types": sport_types,
            "settings": {sport: settings.get(sport) for sport in sorted(sports)},
            "filters": {"tolerance_pct": tolerance_pct, "require_recovery": require_recovery, "check_context": check_context,
                        "limit": capped, "tests_beyond_limit": beyond_limit},
            "tests": rows, "valid": len(valid), "excluded": len(excluded), "excluded_by_reason": by_reason,
            "reason_texts": REASONS, "trend": trend, "note": SUBMAX_NOTE, "api_calls": api.calls,
        }
        return json.dumps(payload, ensure_ascii=False)
    lines = [
        f"Submaximal fatigue tests for athlete {athlete}, {span[0]} to {span[1]}"
        + (f", sports {sport_types}" if sport_types else "")
        + f": {len(tests)} detected ({len(valid)} valid, {len(excluded)} excluded"
        + (f"; {beyond_limit} older beyond limit {capped}" if beyond_limit else "") + ").",
        f"Test settings: {_settings_text(settings, sports)}.",
    ]
    if not tests:
        lines.append("No submax tests detected in this period (Intervals.icu tags detected tests #SFT; the test is configured "
                     "per sport in the settings).")
    elif not valid:
        lines.append("No valid test yet: a benchmark test is a stand-alone steady effort at the target as configured above, "
                     "early in the activity and not part of a longer interval, followed by about a minute of easy riding for "
                     "the HR recovery.")
    if detail_level == "compact":
        if by_reason:
            lines.append("Excluded by reason: " + ", ".join(f"{REASONS[code]} {count}" for code, count in by_reason.items()))
    else:
        lines.extend(_test_line(test, detail_level) for test in tests)
    if not trend["groups"]:
        lines.append("Trend: no valid tests.")
    for group in trend["groups"]:
        lines.extend(_group_lines(group, detail_level))
    lines.append(SUBMAX_NOTE)
    lines.append(f"API calls: {api.calls}")
    return "\n".join(lines)
