"""
Athlete profile, sport settings and training zone tools for Intervals.icu.

The athlete object returned by ``/athlete/{id}`` contains account internals (e-mail,
API key, OAuth scopes, notification switches). Only coaching-relevant fields are
exposed here. Sport settings (FTP, LTHR, thresholds, zones, default gear) come from
``/athlete/{id}/sport-settings``; both are cached per process for ATHLETE_CACHE_TTL_S
(changes made in the web app show up after that) and can be refreshed. The athlete
profile's ``timezone`` defines "today" for all tools (utils.dates).
"""

import json
import re
from typing import Any

from intervals_mcp_server.api import client as api_client
from intervals_mcp_server.api.client import seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.gear import get_gear_map
from intervals_mcp_server.utils.cache import TTLCache, cache_key
from intervals_mcp_server.utils.dates import get_default_end_date, get_default_start_date, set_timezone_resolver
from intervals_mcp_server.utils.custom_fields import assigned_codes
from intervals_mcp_server.utils.sports import family_types, format_pace, zone_ranges
from intervals_mcp_server.utils.validation import resolve_athlete_id

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

ATHLETE_CACHE_TTL_S = 600
_ATHLETE_CACHE: TTLCache[dict[str, Any]] = TTLCache(ATHLETE_CACHE_TTL_S)
_SPORT_SETTINGS_CACHE: TTLCache[list[dict[str, Any]]] = TTLCache(ATHLETE_CACHE_TTL_S)
# Athletes whose time zone lookup failed recently (not retried on every tool call).
_TIMEZONE_FAILURES: TTLCache[bool] = TTLCache(300)

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


async def fetch_athlete(
    athlete_id: str, api_key: str | None = None, *, refresh: bool = False
) -> tuple[dict[str, Any], str | None]:
    """The raw athlete object (cached) and the error message when it could not be loaded."""
    key = cache_key(athlete_id, api_key)
    cached = None if refresh else _ATHLETE_CACHE.get(key)
    if cached is not None:
        return cached, None
    result = await api_client.make_intervals_request(url=f"/athlete/{seg(athlete_id)}", api_key=api_key)
    if isinstance(result, dict) and "error" in result:
        return {}, str(result.get("message", "Unknown error"))
    if not isinstance(result, dict) or not result:
        return {}, "unexpected empty response"
    _ATHLETE_CACHE.set(key, result)
    return result, None


async def get_athlete_raw(
    athlete_id: str, api_key: str | None = None, *, refresh: bool = False
) -> dict[str, Any]:
    """Return (and cache) the raw athlete object; {} on error."""
    athlete, _ = await fetch_athlete(athlete_id, api_key, refresh=refresh)
    return athlete


async def athlete_timezone(athlete_id: str, api_key: str | None = None) -> str | None:
    """IANA time zone of the athlete profile (cached with the profile); None when unknown."""
    key = cache_key(athlete_id, api_key)
    if _TIMEZONE_FAILURES.get(key):
        return None
    athlete, error = await fetch_athlete(athlete_id, api_key)
    zone = athlete.get("timezone") if isinstance(athlete, dict) else None
    if error or not zone:
        _TIMEZONE_FAILURES.set(key, True)
        return None
    return str(zone)


set_timezone_resolver(athlete_timezone)


async def canonical_athlete_id(athlete_id: str, api_key: str | None = None) -> str:
    """The athlete's own id ("i123") for the alias "0" (the key's athlete); other ids unchanged."""
    if str(athlete_id) != "0":
        return str(athlete_id)
    athlete = await get_athlete_raw(athlete_id, api_key)
    return str(athlete.get("id") or athlete_id)


async def fetch_sport_settings(
    athlete_id: str, api_key: str | None = None, *, refresh: bool = False
) -> tuple[list[dict[str, Any]], str | None]:
    """The sport settings list (cached) and the error message when it could not be loaded."""
    key = cache_key(athlete_id, api_key)
    cached = None if refresh else _SPORT_SETTINGS_CACHE.get(key)
    if cached is not None:
        return cached, None
    result = await api_client.make_intervals_request(
        url=f"/athlete/{seg(athlete_id)}/sport-settings", api_key=api_key
    )
    if isinstance(result, dict) and "error" in result:
        return [], str(result.get("message", "Unknown error"))
    if not isinstance(result, list):
        return [], "unexpected response"
    settings = [item for item in result if isinstance(item, dict)]
    _SPORT_SETTINGS_CACHE.set(key, settings)
    return settings, None


async def get_sport_settings_raw(
    athlete_id: str, api_key: str | None = None, *, refresh: bool = False
) -> list[dict[str, Any]]:
    """Return (and cache) the sport settings list; [] on error."""
    settings, _ = await fetch_sport_settings(athlete_id, api_key, refresh=refresh)
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
        url=f"/athlete/{seg(athlete_id)}/wellness", api_key=api_key, params=params
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


async def assigned_field_ids(athlete_id: str, api_key: str | None, activity_type: Any) -> list[Any] | None:
    """Custom activity field ids configured for the sport setting that covers activity_type.

    None when there is no such setting or it lists no fields (= no information).
    """
    if not athlete_id or not activity_type:
        return None
    settings = await get_sport_settings_raw(athlete_id, api_key)
    for setting in _setting_for_sport(settings, str(activity_type)):
        ids = setting.get("activity_field_ids")
        if isinstance(ids, list) and ids:
            return ids
    return None


async def field_assignments(
    athlete_id: str, api_key: str | None, defs: dict[str, dict[str, Any]], activity_types: Any
) -> dict[str, set[str] | None]:
    """Assigned custom field codes per activity type (None = the sport lists no fields).

    Covers the given types and the other types of their sport families, so a sport without its
    own field list can follow its family (GravelRide -> Ride) in ``utils.field_policy``.
    """
    types: set[str] = set()
    for activity_type in activity_types or []:
        if activity_type:
            types.add(str(activity_type))
            types.update(family_types(activity_type))
    return {sport: assigned_codes(defs, await assigned_field_ids(athlete_id, api_key, sport)) for sport in sorted(types)}


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


def _format_setting(  # pylint: disable=too-many-locals,too-many-branches
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
        refresh: Re-fetch instead of using the cache (optional, default False; the cache expires
            after 10 minutes)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    athlete, error = await fetch_athlete(athlete_id_to_use, api_key, refresh=refresh)
    if not athlete:
        return f"Error fetching athlete profile for {athlete_id_to_use}: {error}"

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
        refresh: Re-fetch instead of using the cache (optional, default False; the cache expires
            after 10 minutes)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    settings, error = await fetch_sport_settings(athlete_id_to_use, api_key, refresh=refresh)
    if error:
        return f"Error fetching sport settings for athlete {athlete_id_to_use}: {error}"
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
    refresh: bool = False,
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
        refresh: Re-fetch the sport settings instead of using the cache (optional, default False;
            the cache expires after 10 minutes)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    kind = zone_type.strip().lower()
    if kind not in ("all", "power", "hr", "pace"):
        return "Error: zone_type must be one of all, power, hr, pace."
    settings, error = await fetch_sport_settings(athlete_id_to_use, api_key, refresh=refresh)
    if error:
        return f"Error fetching sport settings for athlete {athlete_id_to_use}: {error}"
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


_SETTING_LIMITS: dict[str, tuple[float, float, str]] = {
    "ftp": (50, 600, "W"),
    "indoor_ftp": (50, 600, "W"),
    "lthr": (80, 220, "bpm"),
    "max_hr": (100, 230, "bpm"),
    "w_prime": (1000, 60000, "J"),
    "p_max": (300, 2500, "W"),
    "threshold_pace": (0.5, 10.0, "m/s"),
}

# Distance in metres per pace unit ("4:30/km" = 1000 m in 270 s).
_PACE_DISTANCES = {"/km": 1000.0, "/mi": 1609.344, "/100m": 100.0, "/100y": 91.44, "/500m": 500.0}
_PACE_UNIT_SUFFIX = {"MINS_KM": "/km", "MINS_MILE": "/mi", "SECS_100M": "/100m", "SECS_100Y": "/100y", "SECS_500M": "/500m"}
_PACE_RE = re.compile(r"(\d{1,2}):([0-5]\d)\s*(/km|/mi|/100m|/100y|/500m)?")
_SPEED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*m/s")


def parse_threshold_pace(value: Any, pace_units: Any) -> float | str:
    """Threshold pace in m/s from "4:30/km", "7:15/mi", "1:45/100m", "4:30" (in the setting's
    pace units) or "4.17 m/s"; an error string for a bare number, which is ambiguous (4.5 could
    be 4:30/km or 4.5 m/s = 3:42/km)."""
    text = str(value).strip().lower().replace(" ", "")
    bare_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if bare_number or re.fullmatch(r"\d+(\.\d+)?", text):
        return (
            f"Error: threshold_pace {value!r} is ambiguous as a bare number; pass it with its unit, "
            'e.g. "4:30/km", "7:15/mi", "1:45/100m" or "4.17 m/s".'
        )
    speed = _SPEED_RE.fullmatch(text)
    if speed:
        return float(speed.group(1))
    pace = _PACE_RE.fullmatch(text)
    if pace:
        seconds = int(pace.group(1)) * 60 + int(pace.group(2))
        suffix = pace.group(3) or _PACE_UNIT_SUFFIX.get(str(pace_units or "").upper())
        if not suffix:
            return 'Error: threshold_pace needs a unit, e.g. "4:30/km" (the sport setting has no pace units).'
        if seconds <= 0:
            return "Error: threshold_pace must be longer than 0:00."
        return round(_PACE_DISTANCES[suffix] / seconds, 4)
    return (
        f"Error: threshold_pace {value!r} is not a pace; use m:ss with a unit, e.g. \"4:30/km\", "
        '"7:15/mi", "1:45/100m", or a speed such as "4.17 m/s".'
    )


def _setting_value_text(name: str, value: Any, pace_units: Any) -> str:
    if name == "threshold_pace" and isinstance(value, (int, float)):
        return f"{value:.2f} m/s ({format_pace(value, pace_units)})"
    return str(value)


@tool("admin")
async def update_sport_settings(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-return-statements
    sport_type: str,
    ftp: int | None = None,
    indoor_ftp: int | None = None,
    lthr: int | None = None,
    max_hr: int | None = None,
    w_prime: int | None = None,
    p_max: int | None = None,
    threshold_pace: str | float | None = None,
    athlete_id: str | None = None,
    api_key: str | None = None,
) -> str:
    """ADMIN WRITE: change thresholds of the sport setting that covers a sport (FTP, LTHR, max HR ...)

    Only the values passed are sent; zones are NOT recalculated (the request asks Intervals.icu
    explicitly not to recalculate HR zones; the configured zone percentages/bpm stay, so check
    get_training_zones afterwards). Values are sanity-checked (FTP 50-600 W, LTHR 80-220 and
    below max HR 100-230 bpm, W' 1000-60000 J, Pmax 300-2500 W, threshold pace 0.5-10 m/s).
    Affects future analysis of every activity of the sport group (e.g. Ride) and the training
    load of new activities. Use only on explicit request of the athlete.

    Args:
        sport_type: Activity type whose sport setting is changed, e.g. "Ride" or "Run"
        ftp: New FTP in watts (optional)
        indoor_ftp: New indoor FTP in watts (optional)
        lthr: New lactate threshold heart rate in bpm (optional)
        max_hr: New maximum heart rate in bpm (optional)
        w_prime: New W' in joules (optional)
        p_max: New Pmax in watts (optional)
        threshold_pace: New threshold pace WITH its unit (optional): "4:30/km", "7:15/mi",
            "1:45/100m", "1:45/100y", "1:50/500m", "4:30" (in the setting's pace units) or a
            speed "4.17 m/s". A bare number is refused because 4.5 could mean 4:30/km or 4.5 m/s.
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    changes: dict[str, float] = {}
    for name, value in (("ftp", ftp), ("indoor_ftp", indoor_ftp), ("lthr", lthr), ("max_hr", max_hr),
                        ("w_prime", w_prime), ("p_max", p_max)):
        if value is None:
            continue
        low, high, units = _SETTING_LIMITS[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            return f"Error: {name} must be between {low:g} and {high:g} {units}."
        changes[name] = value
    if threshold_pace is not None and str(threshold_pace).strip() == "":
        threshold_pace = None
    if not changes and threshold_pace is None:
        return "Error: pass at least one value to change (ftp, indoor_ftp, lthr, max_hr, w_prime, p_max, threshold_pace)."
    settings, error = await fetch_sport_settings(athlete_id_to_use, api_key, refresh=True)
    if error:
        return f"Error fetching sport settings: {error}"
    matching = _setting_for_sport(settings, sport_type)
    if len(matching) != 1:
        known = sorted({str(t) for s in settings for t in (s.get("types") or [])})
        return f"Error: expected exactly one sport setting for '{sport_type}', found {len(matching)}. Known types: {', '.join(known)}."
    setting = matching[0]
    pace_units = setting.get("pace_units")
    if threshold_pace is not None:
        speed = parse_threshold_pace(threshold_pace, pace_units)
        if isinstance(speed, str):
            return speed
        low, high, units = _SETTING_LIMITS["threshold_pace"]
        if not low <= speed <= high:
            return f"Error: threshold_pace {threshold_pace!r} = {speed:.2f} m/s is outside {low:g}-{high:g} {units}."
        changes["threshold_pace"] = speed
    new_lthr = changes.get("lthr", setting.get("lthr"))
    new_max = changes.get("max_hr", setting.get("max_hr"))
    if new_lthr is not None and new_max is not None and new_lthr >= new_max:
        return f"Error: LTHR ({new_lthr}) must be below max HR ({new_max})."
    result = await api_client.make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/sport-settings/{seg(setting.get('id'))}",
        api_key=api_key,
        method="PUT",
        params={"recalcHrZones": "false"},
        data=changes,
    )
    if isinstance(result, dict) and "error" in result:
        return f"Error updating sport settings: {result.get('message', 'Unknown error')}"
    # The athlete object embeds the sport settings too: drop both (all aliases and keys).
    _SPORT_SETTINGS_CACHE.clear()
    _ATHLETE_CACHE.clear()
    echoed = {k: (result.get(k) if isinstance(result, dict) else None) for k in changes}
    before = {k: setting.get(k) for k in changes}
    return (
        f"Updated sport setting {setting.get('id')} ({', '.join(str(t) for t in setting.get('types') or [])}): "
        + "; ".join(
            f"{k} {_setting_value_text(k, before[k], pace_units)} -> "
            f"{_setting_value_text(k, echoed[k] if echoed[k] is not None else v, pace_units)}"
            for k, v in changes.items()
        )
        + ". Zones were not recalculated; verify them with get_training_zones."
    )
