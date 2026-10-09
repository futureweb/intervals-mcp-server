"""
Athlete profile, sport settings and training zone tools for Intervals.icu.

The athlete object returned by ``/athlete/{id}`` contains account internals (e-mail,
API key, OAuth scopes, notification switches). Only coaching-relevant fields are
exposed here. Sport settings (FTP, LTHR, thresholds, zones, default gear) come from
``/athlete/{id}/sport-settings``; both are cached per process and can be refreshed.
"""

import json
from typing import Any

from intervals_mcp_server.api import client as api_client
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.gear import get_gear_map
from intervals_mcp_server.utils.dates import get_default_end_date, get_default_start_date
from intervals_mcp_server.utils.sports import format_pace, zone_ranges
from intervals_mcp_server.utils.validation import resolve_athlete_id

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

_ATHLETE_CACHE: dict[str, dict[str, Any]] = {}
_SPORT_SETTINGS_CACHE: dict[str, list[dict[str, Any]]] = {}

# Fields of the athlete object that are safe and useful for coaching.
PROFILE_FIELDS = (
    "id",
    "name",
    "firstname",
    "lastname",
    "sex",
    "icu_date_of_birth",
    "city",
    "state",
    "country",
    "timezone",
    "locale",
    "measurement_preference",
    "icu_weight",
    "weight",
    "height",
    "icu_resting_hr",
    "icu_mmp_days",
    "icu_effort_secs",
    "training_plan_id",
    "training_plan_start_date",
    "icu_coach",
    "plan",
    "icu_last_seen",
)

SPORT_SETTING_FIELDS = (
    "id",
    "types",
    "ftp",
    "indoor_ftp",
    "w_prime",
    "p_max",
    "lthr",
    "max_hr",
    "threshold_pace",
    "pace_units",
    "power_zones",
    "power_zone_names",
    "hr_zones",
    "hr_zone_names",
    "pace_zones",
    "pace_zone_names",
    "sweet_spot_min",
    "sweet_spot_max",
    "hr_load_type",
    "pace_load_type",
    "load_order",
    "default_gear_id",
    "default_indoor_gear_id",
    "warmup_time",
    "cooldown_time",
    "power_field",
    "ftp_est_min_secs",
    "gap_model",
    "use_gap_zone_times",
    "eFTPSupported",
    "best_effort_distances",
    "updated",
)


async def get_athlete_raw(
    athlete_id: str, api_key: str | None = None, *, refresh: bool = False
) -> dict[str, Any]:
    """Return (and cache) the raw athlete object; {} on error."""
    if not refresh and athlete_id in _ATHLETE_CACHE:
        return _ATHLETE_CACHE[athlete_id]
    result = await api_client.make_intervals_request(url=f"/athlete/{athlete_id}", api_key=api_key)
    if not isinstance(result, dict) or "error" in result:
        return {}
    _ATHLETE_CACHE[athlete_id] = result
    return result


async def get_sport_settings_raw(
    athlete_id: str, api_key: str | None = None, *, refresh: bool = False
) -> list[dict[str, Any]]:
    """Return (and cache) the sport settings list; [] on error."""
    if not refresh and athlete_id in _SPORT_SETTINGS_CACHE:
        return _SPORT_SETTINGS_CACHE[athlete_id]
    result = await api_client.make_intervals_request(
        url=f"/athlete/{athlete_id}/sport-settings", api_key=api_key
    )
    if not isinstance(result, list):
        return []
    settings = [item for item in result if isinstance(item, dict)]
    _SPORT_SETTINGS_CACHE[athlete_id] = settings
    return settings


async def get_latest_eftp(athlete_id: str, api_key: str | None = None) -> dict[str, Any]:
    """Latest eFTP/W'/Pmax estimates per sport from the wellness sportInfo (last 14 days).

    Returns {"date": ..., "by_type": {"Ride": {"eftp", "wPrime", "pMax"}, ...}} or {}.
    """
    params = {
        "oldest": get_default_start_date(14),
        "newest": get_default_end_date(),
        "fields": "id,sportInfo",
    }
    result = await api_client.make_intervals_request(
        url=f"/athlete/{athlete_id}/wellness", api_key=api_key, params=params
    )
    if not isinstance(result, list):
        return {}
    for entry in sorted(
        (e for e in result if isinstance(e, dict)), key=lambda e: str(e.get("id")), reverse=True
    ):
        info = entry.get("sportInfo")
        if isinstance(info, list) and info:
            by_type = {
                str(s.get("type")): {k: s.get(k) for k in ("eftp", "wPrime", "pMax")}
                for s in info
                if isinstance(s, dict) and s.get("type")
            }
            return {"date": entry.get("id"), "by_type": by_type}
    return {}


def _setting_for_sport(
    settings: list[dict[str, Any]], sport_type: str | None
) -> list[dict[str, Any]]:
    """Settings whose types include the sport (case-insensitive); all when sport_type is None."""
    if not sport_type:
        return settings
    wanted = sport_type.strip().lower()
    return [
        s for s in settings if any(str(t).lower() == wanted for t in (s.get("types") or []))
    ]


def _zones_for_setting(setting: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Power, HR and pace zones of a sport setting with absolute ranges."""
    zones: dict[str, list[dict[str, Any]]] = {}
    power = zone_ranges(
        "power", setting.get("power_zones"), setting.get("power_zone_names"), ftp=setting.get("ftp")
    )
    if power:
        zones["power"] = power
    heart = zone_ranges("hr", setting.get("hr_zones"), setting.get("hr_zone_names"))
    if heart:
        zones["hr"] = heart
    pace = zone_ranges(
        "pace",
        setting.get("pace_zones"),
        setting.get("pace_zone_names"),
        threshold_pace=setting.get("threshold_pace"),
        pace_units=setting.get("pace_units"),
    )
    if pace:
        zones["pace"] = pace
    return zones


def _format_zone_rows(kind: str, rows: list[dict[str, Any]], setting: dict[str, Any]) -> list[str]:
    """Readable lines for one zone table."""
    lines: list[str] = []
    for row in rows:
        upper, lower = row["upper_bound"], row["lower_bound"]
        if kind == "hr":
            span = f"≤{upper:g} bpm" if not lower else f"{lower:g}-{upper:g} bpm"
        elif upper >= 999:
            span = f">{lower:g}%"
        else:
            span = f"≤{upper:g}%" if not lower else f"{lower:g}-{upper:g}%"
        extra = ""
        if kind == "power" and "min_watts" in row:
            extra = f" = {row['min_watts']}-{row['max_watts']} W" if row["max_watts"] else f" = >{row['min_watts']} W"
        elif kind == "pace" and row.get("min_speed_m_s") is not None:
            slow = row.get("slowest_pace") or "slower"
            fast = row.get("fastest_pace") or "faster"
            extra = f" = {slow} to {fast} ({row['min_speed_m_s']}-{row.get('max_speed_m_s') or '...'} m/s)"
        lines.append(f"    Z{row['zone']} {row['name']}: {span}{extra}")
    if kind == "power" and setting.get("sweet_spot_min"):
        ftp = setting.get("ftp") or 0
        watts = (
            f" = {int(round(setting['sweet_spot_min'] / 100 * ftp))}-{int(round(setting['sweet_spot_max'] / 100 * ftp))} W"
            if ftp
            else ""
        )
        lines.append(f"    Sweet spot: {setting['sweet_spot_min']}-{setting['sweet_spot_max']}%{watts}")
    return lines


def _format_setting(
    setting: dict[str, Any], gear_map: dict[str, str], eftp: dict[str, Any], include_zones: bool
) -> str:
    """Readable block for one sport setting."""
    types = ", ".join(str(t) for t in (setting.get("types") or []))
    lines = [f"Sport setting {setting.get('id')} for {types}:"]
    power = []
    if setting.get("ftp") is not None:
        power.append(f"FTP {setting['ftp']} W")
    if setting.get("indoor_ftp") is not None:
        power.append(f"indoor FTP {setting['indoor_ftp']} W")
    if setting.get("w_prime") is not None:
        power.append(f"W' {setting['w_prime']} J")
    if setting.get("p_max") is not None:
        power.append(f"Pmax {setting['p_max']} W")
    if power:
        lines.append("  Power: " + ", ".join(power))
    for sport in setting.get("types") or []:
        estimate = (eftp.get("by_type") or {}).get(str(sport))
        if estimate and estimate.get("eftp") is not None:
            lines.append(
                f"  eFTP ({sport}, Intervals.icu estimate as of {eftp.get('date')}): "
                f"{estimate['eftp']:.0f} W, W' {estimate.get('wPrime') or 0:.0f} J, Pmax {estimate.get('pMax') or 0:.0f} W"
            )
    heart = []
    if setting.get("lthr") is not None:
        heart.append(f"LTHR {setting['lthr']} bpm")
    if setting.get("max_hr") is not None:
        heart.append(f"max HR {setting['max_hr']} bpm")
    if setting.get("hr_load_type"):
        heart.append(f"HR load model {setting['hr_load_type']}")
    if heart:
        lines.append("  Heart rate: " + ", ".join(heart))
    if setting.get("threshold_pace") is not None:
        lines.append(
            f"  Threshold pace: {format_pace(setting['threshold_pace'], setting.get('pace_units'))} "
            f"({setting['threshold_pace']:.4f} m/s, units {setting.get('pace_units')}, pace load {setting.get('pace_load_type')})"
        )
    gear = []
    for key, label in (("default_gear_id", "default gear"), ("default_indoor_gear_id", "default indoor gear")):
        gear_id = setting.get(key)
        if gear_id:
            gear.append(f"{label} {gear_map.get(str(gear_id), '?')} ({gear_id})")
    if gear:
        lines.append("  Gear: " + ", ".join(gear))
    misc = [
        f"load order {setting.get('load_order')}",
        f"warmup/cooldown {setting.get('warmup_time')}/{setting.get('cooldown_time')} s",
    ]
    if setting.get("gap_model") and setting["gap_model"] != "NONE":
        misc.append(f"GAP model {setting['gap_model']}")
    if setting.get("power_field"):
        misc.append(f"power field {setting['power_field']}")
    lines.append("  Other: " + ", ".join(misc) + f"; updated {setting.get('updated')}")
    if include_zones:
        for kind, rows in _zones_for_setting(setting).items():
            label = {"power": "Power zones (% of FTP)", "hr": "HR zones", "pace": "Pace zones (% of threshold speed)"}[kind]
            lines.append(f"  {label}:")
            lines.extend(_format_zone_rows(kind, rows, setting))
    return "\n".join(lines)


def _setting_json(setting: dict[str, Any], gear_map: dict[str, str], eftp: dict[str, Any]) -> dict[str, Any]:
    """Machine-readable sport setting with derived zones and gear names."""
    row = {key: setting.get(key) for key in SPORT_SETTING_FIELDS if key in setting}
    row["zones"] = _zones_for_setting(setting)
    row["default_gear_name"] = gear_map.get(str(setting.get("default_gear_id")))
    row["default_indoor_gear_name"] = gear_map.get(str(setting.get("default_indoor_gear_id")))
    row["eftp_estimates"] = {
        str(t): (eftp.get("by_type") or {}).get(str(t)) for t in (setting.get("types") or [])
    }
    row["eftp_estimate_date"] = eftp.get("date")
    if setting.get("threshold_pace") is not None:
        row["threshold_pace_formatted"] = format_pace(setting["threshold_pace"], setting.get("pace_units"))
    return row


@tool("read")
async def get_athlete_profile(  # pylint: disable=too-many-locals,too-many-branches
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
    refresh: bool = False,
) -> str:
    """Get the athlete profile from Intervals.icu (coaching-relevant fields only)

    Returns name, sex, date of birth, timezone, units, weight, height, resting HR,
    bikes and shoes, training plan and a one-line summary per sport setting (FTP,
    LTHR, threshold pace). Account internals such as e-mail, API keys or OAuth
    scopes are never returned. Use get_sport_settings / get_training_zones for details.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
        refresh: Re-fetch instead of using the per-process cache (optional, default False)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    athlete = await get_athlete_raw(athlete_id_to_use, api_key, refresh=refresh)
    if not athlete:
        return f"Error fetching athlete profile for {athlete_id_to_use}."

    profile = {key: athlete.get(key) for key in PROFILE_FIELDS if athlete.get(key) not in (None, "")}
    gear_rows = []
    for kind in ("bikes", "shoes"):
        for item in athlete.get(kind) or []:
            if isinstance(item, dict):
                gear_rows.append(
                    {
                        "kind": kind[:-1],
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "primary": item.get("primary"),
                        "retired": item.get("retired"),
                        "distance_km": round((item.get("distance") or 0) / 1000, 1),
                    }
                )
    settings = athlete.get("sportSettings")
    if not isinstance(settings, list) or not settings:
        settings = await get_sport_settings_raw(athlete_id_to_use, api_key, refresh=refresh)
    sport_rows = [
        {
            "id": s.get("id"),
            "types": s.get("types"),
            "ftp": s.get("ftp"),
            "indoor_ftp": s.get("indoor_ftp"),
            "lthr": s.get("lthr"),
            "max_hr": s.get("max_hr"),
            "threshold_pace": s.get("threshold_pace"),
            "pace_units": s.get("pace_units"),
            "default_gear_id": s.get("default_gear_id"),
        }
        for s in settings
        if isinstance(s, dict)
    ]

    if output_format.strip().lower() == "json":
        return json.dumps(
            {"profile": profile, "gear": gear_rows, "sport_settings": sport_rows}, ensure_ascii=False
        )

    lines = [f"Athlete profile {athlete_id_to_use}:"]
    for key in PROFILE_FIELDS:
        if key in profile:
            lines.append(f"- {key}: {profile[key]}")
    if gear_rows:
        lines.append("Gear:")
        for row in gear_rows:
            flags = ", ".join(f for f, on in (("primary", row["primary"]), ("retired", row["retired"])) if on)
            lines.append(
                f"- {row['kind']} {row['name']} ({row['id']}), {row['distance_km']} km"
                + (f" [{flags}]" if flags else "")
            )
    if sport_rows:
        lines.append("Sport settings (details: get_sport_settings, zones: get_training_zones):")
        for row in sport_rows:
            bits = [f"FTP {row['ftp']} W" if row["ftp"] is not None else None]
            bits.append(f"indoor FTP {row['indoor_ftp']} W" if row["indoor_ftp"] is not None else None)
            bits.append(f"LTHR {row['lthr']}" if row["lthr"] is not None else None)
            bits.append(f"max HR {row['max_hr']}" if row["max_hr"] is not None else None)
            if row["threshold_pace"] is not None:
                bits.append(f"threshold pace {format_pace(row['threshold_pace'], row['pace_units'])}")
            lines.append(
                f"- {', '.join(str(t) for t in (row['types'] or []))} (id {row['id']}): "
                + ", ".join(b for b in bits if b)
            )
    return "\n".join(lines)


@tool("read")
async def get_sport_settings(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches
    athlete_id: str | None = None,
    api_key: str | None = None,
    sport_type: str | None = None,
    output_format: str = "text",
    refresh: bool = False,
) -> str:
    """Get the per-sport settings of an athlete from Intervals.icu

    For every sport setting (a group of activity types such as Ride, GravelRide/MountainBikeRide,
    Run/TrailRun ...): FTP and indoor FTP, W', Pmax, the current eFTP estimate, LTHR and max HR,
    threshold pace with pace units, load models, default outdoor/indoor gear, warm-up/cool-down
    times and the power, HR and pace zones with absolute ranges. Thresholds that were in force
    for a past activity are shown by get_activity_details (snapshot stored with the activity).

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        sport_type: Only the setting that covers this activity type, e.g. "GravelRide" (optional)
        output_format: "text" (default) or "json"
        refresh: Re-fetch instead of using the per-process cache (optional, default False)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    settings = await get_sport_settings_raw(athlete_id_to_use, api_key, refresh=refresh)
    if not settings:
        return f"No sport settings found for athlete {athlete_id_to_use}."
    selected = _setting_for_sport(settings, sport_type)
    if not selected:
        known = sorted({str(t) for s in settings for t in (s.get("types") or [])})
        return f"No sport setting covers '{sport_type}'. Known activity types: {', '.join(known)}."

    gear_map = await get_gear_map(athlete_id=athlete_id_to_use, api_key=api_key)
    eftp = await get_latest_eftp(athlete_id_to_use, api_key)

    if output_format.strip().lower() == "json":
        return json.dumps(
            {"sport_settings": [_setting_json(s, gear_map, eftp) for s in selected]},
            ensure_ascii=False,
        )
    blocks = [_format_setting(s, gear_map, eftp, include_zones=True) for s in selected]
    return "\n\n".join(blocks)


@tool("read")
async def get_training_zones(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    athlete_id: str | None = None,
    api_key: str | None = None,
    sport_type: str | None = None,
    zone_type: str = "all",
    output_format: str = "text",
) -> str:
    """Get the training zones (power, heart rate, pace) of an athlete from Intervals.icu

    Zones come from the sport settings; power zones are % of FTP (watts derived from the
    current FTP), HR zones are bpm, pace zones are % of threshold speed (pace derived from
    the threshold pace). Each zone lists its name, bounds and absolute range.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        sport_type: Only the setting that covers this activity type, e.g. "Run" (optional, default all)
        zone_type: "all" (default), "power", "hr" or "pace"
        output_format: "text" (default) or "json"
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    kind = zone_type.strip().lower()
    if kind not in ("all", "power", "hr", "pace"):
        return "Error: zone_type must be one of all, power, hr, pace."
    settings = await get_sport_settings_raw(athlete_id_to_use, api_key)
    selected = _setting_for_sport(settings, sport_type)
    if not selected:
        return f"No sport settings found for athlete {athlete_id_to_use} (sport_type={sport_type})."

    payload = []
    lines = []
    for setting in selected:
        zones = _zones_for_setting(setting)
        if kind != "all":
            zones = {k: v for k, v in zones.items() if k == kind}
        types = ", ".join(str(t) for t in (setting.get("types") or []))
        payload.append(
            {
                "sport_setting_id": setting.get("id"),
                "types": setting.get("types"),
                "ftp": setting.get("ftp"),
                "lthr": setting.get("lthr"),
                "max_hr": setting.get("max_hr"),
                "threshold_pace": setting.get("threshold_pace"),
                "pace_units": setting.get("pace_units"),
                "sweet_spot": [setting.get("sweet_spot_min"), setting.get("sweet_spot_max")],
                "zones": zones,
            }
        )
        lines.append(f"Zones for {types} (setting {setting.get('id')}):")
        if not zones:
            lines.append("  (no zones of the requested type)")
        for zone_kind, rows in zones.items():
            label = {"power": f"Power zones (% of FTP {setting.get('ftp')} W)", "hr": "HR zones", "pace": "Pace zones (% of threshold speed)"}[zone_kind]
            lines.append(f"  {label}:")
            lines.extend(_format_zone_rows(zone_kind, rows, setting))
    if output_format.strip().lower() == "json":
        return json.dumps({"training_zones": payload}, ensure_ascii=False)
    return "\n".join(lines)
