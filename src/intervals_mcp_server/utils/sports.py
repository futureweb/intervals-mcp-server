"""
Sport-related formatting helpers: pace, durations, zones and start times.

Kept free of API access so they can be used by formatters and tests alike.
"""

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


def start_times(activity: dict[str, Any]) -> dict[str, Any]:
    """Local start, UTC start and timezone of an activity payload as explicit fields."""
    utc_raw = activity.get("start_date") or activity.get("startTime")
    return {
        "start_time_local": activity.get("start_date_local"),
        "start_time_utc": to_utc_iso(utc_raw) or utc_raw,
        "timezone": activity.get("timezone"),
    }


def format_start_times(activity: dict[str, Any]) -> str:
    """'2026-10-06T17:36:22 local (Europe/Vienna) / 2026-10-06T15:36:22Z UTC' from what is present."""
    times = start_times(activity)
    parts: list[str] = []
    if times["start_time_local"]:
        tz_note = f" ({times['timezone']})" if times["timezone"] else ""
        parts.append(f"{times['start_time_local']} local{tz_note}")
    if times["start_time_utc"]:
        parts.append(f"{times['start_time_utc']} UTC")
    return " / ".join(parts) if parts else "Unknown"


def _numeric(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []
    return [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


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
            row["min_watts"] = int(round(lower / 100 * ftp)) + (1 if lower else 0)
            row["max_watts"] = int(round(upper / 100 * ftp)) if upper < 999 else None
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
    """One-line zone table, e.g. 'Z1 ≤55% (≤129 W), Z2 56-75% (130-176 W), ...'."""
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


def family_types(activity_type: Any) -> list[str]:
    """All activity types of the family of ``activity_type`` (just the type when unknown)."""
    family = sport_family(activity_type)
    return list(SPORT_FAMILIES.get(family, (str(activity_type),)))


def is_indoor(activity: dict[str, Any]) -> bool:
    """True for trainer rides and virtual activities."""
    return bool(activity.get("trainer")) or str(activity.get("type") or "").startswith("Virtual")
