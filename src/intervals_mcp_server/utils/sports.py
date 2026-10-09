"""
Sport-related formatting helpers: pace, durations, zones and start times.

Kept free of API access so they can be used by formatters and tests alike.
"""

import math
from datetime import datetime, timezone
from typing import Any

PACE_UNIT_LABELS = {
    "MINS_KM": "/km",
    "MINS_MILE": "/mi",
    "SECS_100M": "/100 m",
    "SECS_100Y": "/100 yd",
    "SECS_500M": "/500 m",
    "SECS_400M": "/400 m",
}


def hms(seconds: Any) -> str:
    """Seconds as h:mm:ss (or m:ss below one hour); 'n/a' when not numeric."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "n/a"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_pace(speed_m_s: Any, pace_units: str | None = "MINS_KM") -> str:
    """Speed in m/s as pace text for the given pace units (default min/km)."""
    if isinstance(speed_m_s, bool) or not isinstance(speed_m_s, (int, float)) or speed_m_s <= 0:
        return "n/a"
    units = pace_units or "MINS_KM"
    distance = {
        "MINS_KM": 1000.0,
        "MINS_MILE": 1609.344,
        "SECS_100M": 100.0,
        "SECS_100Y": 91.44,
        "SECS_500M": 500.0,
        "SECS_400M": 400.0,
    }.get(units, 1000.0)
    secs = distance / speed_m_s
    label = PACE_UNIT_LABELS.get(units, "/km")
    if units.startswith("SECS_"):
        return f"{secs:.1f} s{label}"
    minutes, rem = divmod(int(round(secs)), 60)
    return f"{minutes}:{rem:02d}{label}"


def to_utc_iso(value: Any) -> str | None:
    """Normalise an ISO timestamp to 'YYYY-MM-DDTHH:MM:SSZ'; None when not parseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_offset(activity: dict[str, Any]) -> str | None:
    """UTC offset of the local start ('+02:00') derived from the local and the UTC start; None when unknown."""
    local_raw = activity.get("start_date_local")
    utc_raw = activity.get("start_date") or activity.get("startTime")
    if not isinstance(local_raw, str) or not isinstance(utc_raw, str):
        return None
    try:
        local = datetime.fromisoformat(local_raw.replace("Z", "+00:00"))
        utc = datetime.fromisoformat(utc_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if local.tzinfo is not None:  # an explicit offset wins
        offset = local.utcoffset()
        minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    else:
        if utc.tzinfo is None:
            utc = utc.replace(tzinfo=timezone.utc)
        delta = local - utc.astimezone(timezone.utc).replace(tzinfo=None)
        minutes = int(round(delta.total_seconds() / 900)) * 15  # offsets are multiples of 15 minutes
    if abs(minutes) > 14 * 60:
        return None
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"


def start_times(activity: dict[str, Any]) -> dict[str, Any]:
    """Local start, UTC start, timezone and UTC offset of an activity payload as explicit fields."""
    utc_raw = activity.get("start_date") or activity.get("startTime")
    return {
        "start_time_local": activity.get("start_date_local"),
        "start_time_utc": to_utc_iso(utc_raw) or utc_raw,
        "timezone": activity.get("timezone"),
        "utc_offset": utc_offset(activity),
    }


def local_zone_label(activity: dict[str, Any]) -> str:
    """'Europe/Vienna, UTC+02:00', 'UTC+02:00' or '' (timezone name when stored, offset from local vs UTC)."""
    parts = [str(activity["timezone"])] if activity.get("timezone") else []
    offset = utc_offset(activity)
    if offset:
        parts.append(f"UTC{offset}")
    return ", ".join(parts)


def format_start_times(activity: dict[str, Any]) -> str:
    """'2026-10-06T17:36:22 local (Europe/Vienna, UTC+02:00) / 2026-10-06T15:36:22Z UTC' from what is present."""
    times = start_times(activity)
    parts: list[str] = []
    if times["start_time_local"]:
        zone = local_zone_label(activity)
        parts.append(f"{times['start_time_local']} local" + (f" ({zone})" if zone else ""))
    if times["start_time_utc"]:
        parts.append(f"{times['start_time_utc']} UTC")
    return " / ".join(parts) if parts else "Unknown"


def format_local_start(activity: dict[str, Any]) -> str:
    """'2026-10-06 17:36 local (UTC+02:00)' for compact views; 'date unknown' without a local start."""
    local = str(activity.get("start_date_local") or "")[:16].replace("T", " ")
    if not local:
        return "date unknown"
    zone = local_zone_label(activity)
    return f"{local} local" + (f" ({zone})" if zone else "")


# Activity types whose cadence Intervals.icu stores per leg (strides per minute, like rpm);
# devices such as Garmin report steps per minute (spm) = 2 x the stored value.
FOOT_SPORTS = ("run", "trailrun", "virtualrun", "walk", "hike", "snowshoe")


def is_foot_sport(activity_type: Any) -> bool:
    """True for running, walking and hiking types (cadence stored per leg)."""
    return str(activity_type or "").strip().lower() in FOOT_SPORTS


def cadence_spm(value: Any, activity_type: Any) -> float | None:
    """Steps per minute (2 x the stored value) for foot sports; None otherwise or when missing."""
    if not is_foot_sport(activity_type) or isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        return None
    return float(value) * 2


def cadence_text(value: Any, activity_type: Any, digits: int = 0) -> str:
    """'88 rpm' (bike), '147 spm (74 rpm as stored)' (foot sports, steps = 2 x stored) or 'n/a'."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        return "n/a"
    spm = cadence_spm(value, activity_type)
    if spm is not None:
        return f"{spm:.0f} spm ({value:.{digits}f} rpm as stored)"
    return f"{value:.{digits}f} rpm"


def temperature_text(value: Any, digits: int = 1) -> str:
    """'16.5 °C' or 'n/a' (a missing temperature never becomes 0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        return "n/a"
    rounded = round(float(value), digits)
    return f"{int(rounded) if rounded.is_integer() else rounded} °C"


def _numeric(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []
    return [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


def _floor_watts(watts: float) -> int:
    """Whole watts rounded down, robust against float noise (70 % of 300 W is 210, not 209)."""
    return math.floor(watts + 1e-9)


def zone_ranges(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    kind: str,
    bounds: Any,
    names: Any = None,
    ftp: Any = None,
    threshold_pace: Any = None,
    pace_units: str | None = "MINS_KM",
) -> list[dict[str, Any]]:
    """Expand Intervals.icu zone upper bounds into rows with absolute ranges.

    kind "power": bounds are % of FTP (watts derived when ftp is known);
    kind "hr": bounds are bpm; kind "pace": bounds are % of threshold speed
    (speed and pace derived when threshold_pace in m/s is known).
    """
    uppers = _numeric(bounds)
    rows: list[dict[str, Any]] = []
    name_list = names if isinstance(names, list) else []
    lower = 0.0
    for index, upper in enumerate(uppers, 1):
        row: dict[str, Any] = {
            "zone": index,
            "name": name_list[index - 1] if index - 1 < len(name_list) else f"Z{index}",
            "upper_bound": upper,
            "lower_bound": lower,
        }
        if kind == "power" and isinstance(ftp, (int, float)) and ftp:
            # Intervals.icu floors the watt bounds: Z1 <= floor(55 % FTP), Z2 from that + 1 ...
            row["min_watts"] = _floor_watts(lower / 100 * ftp) + (1 if lower else 0)
            row["max_watts"] = _floor_watts(upper / 100 * ftp) if upper < 999 else None
        if kind == "pace" and isinstance(threshold_pace, (int, float)) and threshold_pace:
            low_speed = lower / 100 * threshold_pace
            high_speed = upper / 100 * threshold_pace if upper < 999 else None
            row["min_speed_m_s"] = round(low_speed, 3)
            row["max_speed_m_s"] = round(high_speed, 3) if high_speed else None
            row["slowest_pace"] = format_pace(low_speed, pace_units) if low_speed else None
            row["fastest_pace"] = format_pace(high_speed, pace_units) if high_speed else None
        rows.append(row)
        lower = upper
    return rows


def format_zone_table(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    kind: str,
    bounds: Any,
    names: Any = None,
    ftp: Any = None,
    threshold_pace: Any = None,
    pace_units: str | None = "MINS_KM",
) -> str:
    """One-line zone table, e.g. 'Z1 ≤55% (0-128 W), Z2 55-75% (129-175 W), ...'."""
    rows = zone_ranges(kind, bounds, names, ftp, threshold_pace, pace_units)
    if not rows:
        return ""
    parts: list[str] = []
    for row in rows:
        upper = row["upper_bound"]
        lower = row["lower_bound"]
        if kind == "hr":
            span = f"≤{upper:g} bpm" if not lower else f"{lower:g}-{upper:g} bpm"
        else:
            span = f"≤{upper:g}%" if not lower else f"{lower:g}-{upper:g}%"
            if upper >= 999:
                span = f">{lower:g}%"
        extra = ""
        if "min_watts" in row:
            extra = f" ({row['min_watts']}-{row['max_watts']} W)" if row["max_watts"] else f" (>{row['min_watts']} W)"
        elif "slowest_pace" in row and (row.get("slowest_pace") or row.get("fastest_pace")):
            extra = f" ({row.get('slowest_pace') or 'slower'} to {row.get('fastest_pace') or 'faster'})"
        parts.append(f"{row['name']} {span}{extra}")
    return ", ".join(parts)


# Sport families: activity types whose intensities (% of FTP, threshold pace) are comparable.
SPORT_FAMILIES: dict[str, tuple[str, ...]] = {
    "cycling": ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide", "EMountainBikeRide", "TrackRide", "Velomobile", "Handcycle"),
    "running": ("Run", "TrailRun", "VirtualRun"),
    "walking": ("Walk", "Hike", "Snowshoe"),
    "swimming": ("Swim", "OpenWaterSwim"),
    "skiing": ("NordicSki", "BackcountrySki", "VirtualSki", "RollerSki", "AlpineSki"),
    "rowing": ("Rowing", "VirtualRow", "Canoeing", "Kayaking"),
}


def sport_family(activity_type: Any) -> str:
    """Family name of an activity type ('cycling', 'running' ...); the type itself when unknown."""
    wanted = str(activity_type or "").strip().lower()
    for family, types in SPORT_FAMILIES.items():
        if wanted in (t.lower() for t in types):
            return family
    return wanted or "unknown"


DEFAULT_PACE_UNITS = {"swimming": "SECS_100M", "rowing": "SECS_500M"}


def default_pace_units(activity_type: Any) -> str:
    """Pace units for a sport when none are configured: per 100 m for swims, per 500 m for rowing, else min/km."""
    return DEFAULT_PACE_UNITS.get(sport_family(activity_type), "MINS_KM")


def family_types(activity_type: Any) -> list[str]:
    """All activity types of the family of ``activity_type`` (just the type when unknown)."""
    family = sport_family(activity_type)
    return list(SPORT_FAMILIES.get(family, (str(activity_type),)))


def is_indoor(activity: dict[str, Any]) -> bool:
    """True for trainer rides and virtual activities."""
    return bool(activity.get("trainer")) or str(activity.get("type") or "").startswith("Virtual")
