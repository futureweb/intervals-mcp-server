"""
Training summary tool for Intervals.icu: activity-based totals per week, month, sport or
gear with separate load sources, time in zones, intensity, frequency, long sessions,
subjective feel/RPE distribution and custom field aggregates.

Complements get_weekly_summary (which uses the athlete-summary endpoint and is cheaper)
with grouping by month/sport/gear, elevation, separate power/HR/pace loads, custom field
totals (e.g. a device training load, kept apart from the Intervals.icu load) and gear splits.
"""

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.athlete import assigned_field_ids
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.tools.gear import get_gear_map
from intervals_mcp_server.utils.custom_fields import ACTIVITY_FIELD, CustomFieldDefs, assigned_codes, is_missing
from intervals_mcp_server.utils.field_policy import aggregate_custom_fields, format_aggregate, format_pair, pair_changes
from intervals_mcp_server.utils.dates import get_default_end_date
from intervals_mcp_server.utils.sports import hms
from intervals_mcp_server.utils.validation import resolve_athlete_id, validate_date

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

GROUPINGS = ("week", "month", "sport", "gear", "total")
LONG_SESSION_SECS = 3 * 3600
BASE_FIELDS = (
    "id,start_date_local,type,name,moving_time,elapsed_time,distance,total_elevation_gain,"
    "icu_training_load,hr_load,power_load,pace_load,icu_intensity,icu_zone_times,icu_hr_zone_times,"
    "pace_zone_times,feel,icu_rpe,gear,power_meter,trainer,icu_joules,icu_average_watts,"
    "icu_weighted_avg_watts,average_heartrate,device_name"
)


def _group_key(activity: dict[str, Any], group_by: str, gear_map: dict[str, str]) -> str:
    day = str(activity.get("start_date_local", ""))[:10]
    if group_by == "week":
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            return "unknown"
        monday = parsed - timedelta(days=parsed.weekday())
        return f"{monday.isoformat()} (ISO {parsed.isocalendar().year}-W{parsed.isocalendar().week:02d})"
    if group_by == "month":
        return day[:7] or "unknown"
    if group_by == "sport":
        return str(activity.get("type") or "unknown")
    if group_by == "gear":
        gear = activity.get("gear")
        gear_id = str(gear.get("id")) if isinstance(gear, dict) and gear.get("id") else str(activity.get("gear_id") or "")
        return f"{gear_map.get(gear_id, gear_id or 'no gear')} ({gear_id})" if gear_id else "no gear"
    return "total"


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and not is_missing(value) else 0.0


def _zone_secs(activity: dict[str, Any], key: str) -> dict[str, float]:
    zones = activity.get(key)
    if not isinstance(zones, list):
        return {}
    out: dict[str, float] = {}
    for zone in zones:
        if isinstance(zone, dict) and zone.get("id") and isinstance(zone.get("secs"), (int, float)):
            out[str(zone["id"])] = float(zone["secs"])
    return out


def _aggregate_custom(
    activities: list[dict[str, Any]], defs: CustomFieldDefs, assigned_by_type: dict[str, set[str] | None] | None = None
) -> dict[str, dict[str, Any]]:
    """Aggregate numeric custom fields by their policy (units / semantics / definition, see utils.field_policy)."""
    return aggregate_custom_fields(activities, defs, get_config().custom_aggregate_overrides, assigned_by_type)


def _summarize(  # pylint: disable=too-many-locals
    activities: list[dict[str, Any]], defs: CustomFieldDefs, gear_map: dict[str, str],
    assigned_by_type: dict[str, set[str] | None] | None = None,
) -> dict[str, Any]:
    moving = sum(_num(a.get("moving_time")) for a in activities)
    power_tiz: dict[str, float] = defaultdict(float)
    hr_tiz: dict[str, float] = defaultdict(float)
    sports: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    gears: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    feel: dict[str, int] = defaultdict(int)
    rpes: list[float] = []
    longest: dict[str, Any] | None = None
    for a in activities:
        for key, secs in _zone_secs(a, "icu_zone_times").items():
            power_tiz[key] += secs
        for key, secs in _zone_secs(a, "icu_hr_zone_times").items():
            hr_tiz[key] += secs
        sport = sports[str(a.get("type") or "unknown")]
        sport["sessions"] += 1
        sport["moving_time"] += _num(a.get("moving_time"))
        sport["distance"] += _num(a.get("distance"))
        sport["load"] += _num(a.get("icu_training_load"))
        gear = gears[_group_key(a, "gear", gear_map)]
        gear["sessions"] += 1
        gear["moving_time"] += _num(a.get("moving_time"))
        gear["distance"] += _num(a.get("distance"))
        gear["load"] += _num(a.get("icu_training_load"))
        if a.get("feel") is not None:
            feel[str(a["feel"])] += 1
        if isinstance(a.get("icu_rpe"), (int, float)):
            rpes.append(float(a["icu_rpe"]))
        if longest is None or _num(a.get("moving_time")) > _num(longest.get("moving_time")):
            longest = a
    intensities = [(_num(a.get("icu_intensity")), _num(a.get("moving_time"))) for a in activities if a.get("icu_intensity") is not None]
    weighted = sum(i * t for i, t in intensities) / sum(t for _, t in intensities) if intensities and sum(t for _, t in intensities) else None
    return {
        "sessions": len(activities),
        "moving_time_s": moving,
        "elapsed_time_s": sum(_num(a.get("elapsed_time")) for a in activities),
        "distance_m": sum(_num(a.get("distance")) for a in activities),
        "elevation_gain_m": sum(_num(a.get("total_elevation_gain")) for a in activities),
        "training_load": sum(_num(a.get("icu_training_load")) for a in activities),
        "power_load": sum(_num(a.get("power_load")) for a in activities),
        "hr_load": sum(_num(a.get("hr_load")) for a in activities),
        "pace_load": sum(_num(a.get("pace_load")) for a in activities),
        "intensity_time_weighted_pct": round(weighted, 1) if weighted is not None else None,
        "time_in_power_zones_s": dict(power_tiz),
        "time_in_hr_zones_s": dict(hr_tiz),
        "by_sport": {k: dict(v) for k, v in sports.items()},
        "by_gear": {k: dict(v) for k, v in gears.items()},
        "feel_distribution": dict(feel),
        "rpe_mean": round(sum(rpes) / len(rpes), 1) if rpes else None,
        "long_sessions_3h_plus": sum(1 for a in activities if _num(a.get("moving_time")) >= LONG_SESSION_SECS),
        "longest_session": {"id": longest.get("id"), "name": longest.get("name"), "moving_time_s": longest.get("moving_time")} if longest else None,
        "trainer_sessions": sum(1 for a in activities if a.get("trainer")),
        "custom_fields": _aggregate_custom(activities, defs, assigned_by_type),
        "custom_field_changes": pair_changes(activities, defs),
    }


def _fitness_at(wellness: list[dict[str, Any]], last_day: str) -> dict[str, Any] | None:
    candidates = [w for w in wellness if str(w.get("id", "")) <= last_day and w.get("ctl") is not None]
    if not candidates:
        return None
    entry = max(candidates, key=lambda w: str(w.get("id")))
    ctl, atl = entry.get("ctl"), entry.get("atl")
    return {
        "date": entry.get("id"),
        "ctl": ctl,
        "atl": atl,
        "form": round(ctl - atl, 1) if isinstance(ctl, (int, float)) and isinstance(atl, (int, float)) else None,
        "ramp_rate": entry.get("rampRate"),
    }


def _format_group(  # pylint: disable=too-many-locals,too-many-branches
    name: str, s: dict[str, Any], fitness: dict[str, Any] | None, include_gear: bool, detail_level: str = "standard"
) -> str:
    lines = [
        f"{name}: {s['sessions']} sessions | {hms(s['moving_time_s'])} moving ({hms(s['elapsed_time_s'])} elapsed) | "
        f"{s['distance_m'] / 1000:.1f} km | +{s['elevation_gain_m']:.0f} m",
        f"  Load (Intervals.icu): total {s['training_load']:.0f} = power {s['power_load']:.0f} / HR {s['hr_load']:.0f} / pace {s['pace_load']:.0f}"
        + (f" | time-weighted intensity {s['intensity_time_weighted_pct']}%" if s["intensity_time_weighted_pct"] is not None else ""),
    ]
    if fitness:
        lines.append(f"  End of period ({fitness['date']}): CTL {fitness['ctl']:.1f}, ATL {fitness['atl']:.1f}, form {fitness['form']}, ramp {fitness['ramp_rate']}")
    if detail_level == "compact":
        lines.append("  " + ", ".join(f"{sport} {int(v['sessions'])}x {hms(v['moving_time'])}" for sport, v in s["by_sport"].items()))
        loads = [format_aggregate(code, agg) for code, agg in s["custom_fields"].items() if agg["policy"] == "device_load_sum"]
        if loads:
            lines.append("  Device loads (separate scale): " + "; ".join(loads))
        return "\n".join(lines)
    if s["time_in_power_zones_s"]:
        lines.append("  Time in power zones: " + ", ".join(f"{z} {hms(v)}" for z, v in s["time_in_power_zones_s"].items() if v))
    if s["time_in_hr_zones_s"]:
        lines.append("  Time in HR zones: " + ", ".join(f"{z} {hms(v)}" for z, v in s["time_in_hr_zones_s"].items() if v))
    for sport, v in s["by_sport"].items():
        lines.append(f"  - {sport}: {int(v['sessions'])} sessions, {hms(v['moving_time'])}, {v['distance'] / 1000:.1f} km, load {v['load']:.0f}")
    if include_gear and len(s["by_gear"]) > 1 or (include_gear and "no gear" not in s["by_gear"]):
        for gear, v in s["by_gear"].items():
            lines.append(f"  - gear {gear}: {int(v['sessions'])} sessions, {hms(v['moving_time'])}, {v['distance'] / 1000:.1f} km, load {v['load']:.0f}")
    extras = []
    if s["feel_distribution"]:
        extras.append("feel " + ", ".join(f"{k}: {v}" for k, v in sorted(s["feel_distribution"].items())))
    if s["rpe_mean"] is not None:
        extras.append(f"RPE mean {s['rpe_mean']}")
    extras.append(f"sessions >= 3 h: {s['long_sessions_3h_plus']}")
    if s["longest_session"]:
        extras.append(f"longest {hms(s['longest_session']['moving_time_s'])} ('{s['longest_session']['name']}')")
    if s["trainer_sessions"]:
        extras.append(f"trainer sessions {s['trainer_sessions']}")
    lines.append("  " + " | ".join(extras))
    if s["custom_fields"]:
        shown = {code: agg for code, agg in s["custom_fields"].items() if detail_level == "full" or agg["policy"] != "none"}
        parts = [format_aggregate(code, agg, show_reason=detail_level == "full") for code, agg in shown.items()]
        skipped = [code for code in s["custom_fields"] if code not in shown]
        if parts:
            lines.append(
                "  Custom fields (aggregated by units and meaning: sums only for additive values, device loads kept "
                "separate from the Intervals.icu load): " + "; ".join(parts)
            )
        if skipped:
            lines.append(f"  Custom fields without a meaningful aggregate (not summed): {', '.join(skipped)}")
    for pair in s.get("custom_field_changes") or []:
        lines.append("  " + format_pair(pair))
    return "\n".join(lines)


@tool("read")
async def get_training_summary(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    start_date: str,
    end_date: str | None = None,
    group_by: str = "week",
    sport_types: str | None = None,
    include_gear: bool = True,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
    detail_level: str = "standard",
) -> str:
    """Training totals for a period grouped by week, month, sport, gear or in total (read-only)

    Per group: sessions, moving and elapsed time, distance, elevation gain, the
    Intervals.icu training load split into its power, HR and pace components, the
    time-weighted intensity, CTL/ATL/form/ramp at the end of the period, time in power
    and HR zones, per-sport and per-gear splits, feel distribution and mean RPE,
    number of sessions of 3 h or more, the longest session, trainer sessions, and the
    numeric custom activity fields aggregated by a generic policy derived from their
    units, meaning and definition: additive values (kcal, ml, distance, time) are summed,
    device training loads / EPOC are summed but labelled as a device scale separate from
    the Intervals.icu load, estimates and states (VO2max, performance condition, recovery
    time, detected thresholds) get latest value, change and range, and per-activity values
    (percentages such as stamina, scores, training effects, running dynamics, temperatures,
    heart rate) get mean, median, min and max - they are never summed, even when the field
    definition says SUM. Fields without units or a recognisable meaning get no aggregate.
    Values of a field on activities whose sport does not have it assigned (sport settings,
    e.g. running dynamics stored as 0 on rides) are ignored; for estimates a stored 0 means
    "no value" and is left out. Paired "... at start" / "... at end" fields (e.g. stamina)
    also get the typical start-to-end change. CUSTOM_AGGREGATE_OVERRIDES ("Code=sum|device_load_sum|trend|mean|none")
    overrides the policy per field. For a quick per-week view see also get_weekly_summary.

    Args:
        start_date: Start date YYYY-MM-DD
        end_date: End date YYYY-MM-DD (optional, default today)
        group_by: "week" (default, ISO weeks starting Monday), "month", "sport", "gear" or "total"
        sport_types: Comma-separated activity types to include, e.g. "Ride,GravelRide" (optional)
        include_gear: Show the per-gear split inside each group (optional, default True)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
        detail_level: "compact" (totals, loads, fitness, sessions per sport and device loads per
            group), "standard" (default, everything above plus zones, gear, feel/RPE and custom
            field aggregates) or "full" (standard plus the aggregation reason per custom field and
            the fields without a meaningful aggregate)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if detail_level not in ("compact", "standard", "full"):
        return "Error: detail_level must be one of compact, standard, full."
    if group_by not in GROUPINGS:
        return f"Error: group_by must be one of {', '.join(GROUPINGS)}."
    end = end_date or get_default_end_date()
    try:
        validate_date(start_date)
        validate_date(end)
    except ValueError as exc:
        return f"Error: {exc}"
    if start_date > end:
        return "Error: start_date must not be after end_date."

    defs = (await get_custom_item_index(athlete_id=athlete_id_to_use, api_key=api_key)).get(ACTIVITY_FIELD, {})
    fields = BASE_FIELDS + "".join(f",{code}" for code in defs)
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id_to_use}/activities", api_key=api_key,
        params={"oldest": start_date, "newest": end, "fields": fields},
    )
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching activities: {result.get('message', 'Unknown error')}"
    activities = [a for a in result if isinstance(a, dict)] if isinstance(result, list) else []
    wanted = {t.strip().lower() for t in (sport_types or "").split(",") if t.strip()}
    if wanted:
        activities = [a for a in activities if str(a.get("type", "")).lower() in wanted]
    if not activities:
        return f"No activities found for athlete {athlete_id_to_use} between {start_date} and {end}."
    gear_map = await get_gear_map(athlete_id=athlete_id_to_use, api_key=api_key)
    wellness_result = await make_intervals_request(
        url=f"/athlete/{athlete_id_to_use}/wellness", api_key=api_key,
        params={"oldest": start_date, "newest": end, "fields": "id,ctl,atl,rampRate"},
    )
    wellness = [w for w in wellness_result if isinstance(w, dict)] if isinstance(wellness_result, list) else []

    assigned_by_type = {
        sport: assigned_codes(defs, await assigned_field_ids(athlete_id_to_use, api_key, sport))
        for sport in {str(a.get("type") or "") for a in activities} if sport
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for activity in sorted(activities, key=lambda a: str(a.get("start_date_local"))):
        groups[_group_key(activity, group_by, gear_map)].append(activity)
    rows: list[dict[str, Any]] = []
    for name, items in groups.items():
        last_day = max(str(a.get("start_date_local", ""))[:10] for a in items)
        rows.append({"group": name, "summary": _summarize(items, defs, gear_map, assigned_by_type), "fitness_at_end": _fitness_at(wellness, last_day)})
    overall = _summarize(activities, defs, gear_map, assigned_by_type)

    if output_format.strip().lower() == "json":
        return json.dumps(
            {"start": start_date, "end": end, "group_by": group_by, "groups": rows, "overall": overall,
             "fitness_at_end": _fitness_at(wellness, end), "generated": datetime.now().isoformat(timespec="minutes")},
            ensure_ascii=False,
        )
    text = f"Training summary for athlete {athlete_id_to_use}, {start_date} to {end}, grouped by {group_by}:\n\n"
    text += "\n\n".join(_format_group(r["group"], r["summary"], r["fitness_at_end"], include_gear, detail_level) for r in rows)
    if len(rows) > 1:
        text += "\n\n" + _format_group("TOTAL", overall, _fitness_at(wellness, end), include_gear, detail_level)
    return text
