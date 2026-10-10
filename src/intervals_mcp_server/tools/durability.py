"""
Aerobic durability MCP tool for Intervals.icu (read-only).

get_durability reports the aerobic decoupling (Pw:HR or Pace:HR drift between the first and
second half) of steady long sessions and the efficiency factor trend, from the values
Intervals.icu computes per activity. Sessions pass a quality filter; excluded sessions are
counted per reason. The filter and the recent-versus-window comparison follow the coach
metrics proposed by morritter in upstream pull request mvilanova/intervals-mcp-server#150
(``utils.durability``). It complements get_power_hr_efficiency, which compares watts per
heartbeat of single work intervals per power band. One API call (the activity list).
"""

import json
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field

from intervals_mcp_server.tools.training_load import (
    DURABILITY_FIELDS,
    LOAD_FIELDS,
    fetch_activities,
    filter_types,
    fmt,
    resolve_period,
    resolve_request,
    wanted_types,
)
from intervals_mcp_server.utils.durability import (
    DRIFT_THRESHOLD_PCT,
    EF_BAND_PCT,
    FAMILIES,
    MAX_TEMP_C,
    MAX_VI,
    REASONS,
    REFERENCES,
    SMALL_SAMPLE,
    TEMPERATURE_SOURCES,
    TREND_BAND_PP,
    decoupling_summary,
    efficiency_summary,
)
from intervals_mcp_server.utils.params import (
    AthleteId,
    DetailLevel,
    EndDate,
    Environment,
    OutputFormat,
    SportTypes,
    lower_choice,
)
from intervals_mcp_server.utils.sports import SPORT_FAMILIES

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool


DEFAULT_DAYS = 42
MAX_DAYS = 366


def sample_note(entry: dict[str, Any], short: bool = False) -> str:
    """'6 of 19 sessions qualify; small sample (< 8), not reliable; mixed sample: 2 different bikes/shoes'.

    ``short`` gives 'n 6 of 19, small sample, not reliable, mixed: 2 different bikes/shoes' for compact views.
    """
    considered = entry.get("considered", entry["n"])
    if short:
        parts = [f"n {entry['n']} of {considered}"]
        if entry["small_sample"]:
            parts.append("small sample, not reliable")
        if entry.get("heterogeneity"):
            parts.append("mixed: " + ", ".join(entry["heterogeneity"]))
        return ", ".join(parts)
    parts = [f"{entry['n']} of {considered} sessions qualify"]
    if entry["small_sample"]:
        parts.append(f"small sample (< {SMALL_SAMPLE}), not reliable")
    if entry.get("heterogeneity"):
        parts.append("mixed sample: " + ", ".join(entry["heterogeneity"]))
    return "; ".join(parts)


def _family_text(family: str, entry: dict[str, Any], ef: dict[str, Any], threshold: float) -> str:
    if not entry["n"]:
        text = f"  {family}: no qualifying sessions ({entry.get('considered', 0)} considered)"
    else:
        spread = f", IQR {fmt(entry['p25'], 1)} to {fmt(entry['p75'], 1)}" if "p25" in entry else ""
        recent = entry["recent"]
        trend = (
            f"; last {recent['days']} d median {fmt(recent['median'], 1)} % (n {recent['n']}), "
            f"{fmt(recent['delta_pp'], 1, ' pp', signed=True)} vs window: {recent['direction']}"
            if recent["direction"] else f"; last {recent['days']} d: n {recent['n']} (too few for a comparison)"
        )
        basis = ", ".join(f"{b} {n}" for b, n in entry["basis"].items())
        text = (
            f"  {family}: decoupling median {fmt(entry['median'], 1)} % (n {entry['n']}, range {fmt(entry['min'], 1)} "
            f"to {fmt(entry['max'], 1)}{spread}; {basis}; {entry['indoor_sessions']} indoor), {entry['above_threshold']} above "
            f"{threshold:g} %{trend}"
        )
        text += f"\n    sample: {sample_note(entry)}"
    if ef["n"]:
        change = (
            f", last 7 d mean {fmt(ef['recent_mean'], 2)} (n {ef['recent_n']}), {fmt(ef['change_pct'], 1, ' %', signed=True)}: "
            f"{ef['direction']}" if ef["direction"] else f", last 7 d n {ef['recent_n']} (too few for a comparison)"
        )
        gear = f"; {ef['gear_ids']} different bikes/shoes, compare per gear with get_power_hr_efficiency" if ef["gear_ids"] > 1 else ""
        text += (
            f"\n    efficiency factor mean {fmt(ef['mean'], 2)} (n {ef['n']}){change}{gear}"
            + (" - small sample, not reliable" if ef["small_sample"] else "")
        )
    return text


def _temp_text(session: dict[str, Any], source: str | None = None) -> str:
    """'device 24 °C, weather 19 °C' (what exists; plus feels-like when it drives the filter), 'n/a °C' without either."""
    keys = [("avg_temp_c", "device"), ("weather_temp_c", "weather")] + ([("feels_like_c", "feels-like")] if source == "feels_like" else [])
    parts = [f"{label} {fmt(session.get(key), 0, ' °C')}" for key, label in keys if session.get(key) is not None]
    return ", ".join(parts) if parts else "n/a °C"


def _text(payload: dict[str, Any], detail_level: str) -> str:
    result, filters = payload["decoupling"], payload["filters"]
    source = {"device": "device sensor", "weather": "weather", "feels_like": "weather feels-like"}[filters.get("temperature_source") or "device"]
    temp = (f"average temperature ({source}) <= {filters['max_temp_c']:g} °C (missing passes)" if filters["max_temp_c"] is not None
            else "no temperature limit")
    lines = [
        f"Durability for athlete {payload['athlete_id']}, {payload['start']} to {payload['end']}: aerobic decoupling "
        f"(Intervals.icu, drift of power:HR or pace:HR between the halves, %) of steady sessions; filter: at least "
        f"{filters['min_minutes']:g} min moving, moving >= 85 % of elapsed time, HR, variability index <= {filters['max_vi']:g} "
        f"(rides need power), {temp}" + (f", {filters['environment']} only" if filters["environment"] else "") + ".",
        f"Sessions considered {result['considered']}, qualifying {result['considered'] - result['excluded']}, excluded "
        f"{result['excluded']}" + (": " + ", ".join(f"{REASONS.get(r, r)} {n}" for r, n in result["excluded_by_reason"].items())
                                   if result["excluded_by_reason"] else ""),
    ]
    for family, entry in result["by_sport"].items():
        lines.append(_family_text(family, entry, payload["efficiency"].get(family, {"n": 0}), filters["drift_threshold_pct"]))
    if detail_level != "compact":
        for family, entry in result["by_sport"].items():
            for s in entry["sessions"]:
                lines.append(
                    f"    {s['date']} {family} {s['type']} '{s['name']}' ({s['id']}): {fmt(s['decoupling_pct'], 1, ' %', signed=True)}, "
                    f"{fmt(s['minutes'])} min, VI {fmt(s['variability_index'], 2)}, EF {fmt(s['efficiency_factor'], 2)}, "
                    f"{_temp_text(s, filters.get('temperature_source'))}{', indoor' if s['indoor'] else ''}"
                )
        lines.append(
            f"Reference: {REFERENCES['decoupling']['text']} ({REFERENCES['decoupling']['source']}). Recent vs window: "
            f"+/- {TREND_BAND_PP:g} pp band for decoupling, +/- {EF_BAND_PCT:g} % for the efficiency factor."
        )
    if detail_level == "full" and result["excluded_sessions"]:
        lines.append("Excluded:")
        lines.extend(
            f"    {s['date']} {s['type']} '{s['name']}' ({s['id']}): {REASONS.get(s['reason'], s['reason'])}"
            for s in result["excluded_sessions"]
        )
    lines.append("Heat, hydration, fatigue, terrain and pacing all affect decoupling; statistics only, no assessment is made.")
    return "\n".join(lines)


@tool("read")
async def get_durability(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    start_date: Annotated[str | None, Field(
        description="First day YYYY-MM-DD; default 41 days before end_date (6 weeks); max 366 days"
    )] = None,
    end_date: EndDate = None,
    sport_types: Annotated[SportTypes, Field(
        description='Comma-separated activity types, e.g. "Ride,VirtualRide"; default every cycling and running type'
    )] = None,
    min_minutes: Annotated[float, Field(description="Minimum moving time in minutes, above 0")] = 60,
    max_vi: Annotated[float, Field(description="Maximum variability index (steadiness), at least 1")] = MAX_VI,
    max_temp_c: Annotated[float | None, Field(
        description="Maximum average temperature in °C; null = no limit; a missing temperature passes"
    )] = MAX_TEMP_C,
    drift_threshold_pct: Annotated[float, Field(description="Decoupling in % above which sessions are counted")] = DRIFT_THRESHOLD_PCT,
    recent_days: Annotated[int, Field(description="Recent window in days compared with the whole period")] = 7,
    environment: Environment = None,
    temperature_source: Annotated[
        Literal["device", "weather", "feels_like"],
        BeforeValidator(lower_choice),
        Field(description="Temperature for max_temp_c: device sensor, Intervals.icu weather or its feels-like"),
    ] = "device",
    athlete_id: AthleteId = None,
    output_format: OutputFormat = "text",
    detail_level: Annotated[DetailLevel, Field(
        description="compact = per-sport summary; standard adds the qualifying sessions and the reference; full "
        "adds the excluded sessions"
    )] = "standard",
) -> str:
    """Use for aerobic durability: Intervals.icu's aerobic decoupling (power:HR, or pace:HR for runs without power; drift between the halves in %) of steady long cycling and running sessions and the efficiency factor trend (read-only, default the last 6 weeks).

    Sessions pass a quality filter (min_minutes moving, few stops, HR, rides with power,
    variability index up to max_vi, temperature up to max_temp_c, optionally indoor or outdoor
    only); excluded ones are counted per reason. Per sport family: median, range, quartiles,
    sessions above drift_threshold_pct (5 % commonly cited) and the last recent_days against the
    window. Fewer than 8 qualifying sessions are flagged as a small sample; mixed indoor/outdoor,
    bikes/shoes or power meters are noted. Statistics only, no verdict. Per interval and bike:
    get_power_hr_efficiency. Method: intervals://methods/durability (get_guide).
    """
    athlete_id_to_use, error_msg = resolve_request(athlete_id, detail_level)
    if error_msg:
        return error_msg
    env = (environment or "").strip().lower() or None
    if env not in (None, "indoor", "outdoor"):
        return "Error: environment must be 'indoor' or 'outdoor'."
    temp_source = (temperature_source or "device").strip().lower()
    if temp_source not in TEMPERATURE_SOURCES:
        return f"Error: temperature_source must be one of {', '.join(TEMPERATURE_SOURCES)}."
    if min_minutes <= 0 or max_vi < 1 or recent_days < 1 or drift_threshold_pct <= 0:
        return "Error: min_minutes, recent_days and drift_threshold_pct must be positive and max_vi at least 1."
    period = resolve_period(start_date, end_date, DEFAULT_DAYS, MAX_DAYS)
    if isinstance(period, str):
        return period
    start, end = period
    activities, error = await fetch_activities(athlete_id_to_use, start, end, f"{LOAD_FIELDS},{DURABILITY_FIELDS}")
    if error:
        return error
    wanted = wanted_types(sport_types)
    activities = filter_types(activities, wanted)
    families = FAMILIES if not wanted else tuple(
        family for family, types in SPORT_FAMILIES.items() if any(t.lower() in wanted for t in types)
    ) or FAMILIES
    filters = {"min_minutes": min_minutes, "max_vi": max_vi, "max_temp_c": max_temp_c, "temperature_source": temp_source, "environment": env,
               "drift_threshold_pct": drift_threshold_pct, "recent_days": recent_days, "sport_types": sport_types}
    decoupling = decoupling_summary(
        activities, start, end, recent_days=recent_days, threshold_pct=drift_threshold_pct, families=families,
        min_moving_s=min_minutes * 60, max_vi=max_vi, max_temp_c=max_temp_c, environment=env, temperature_source=temp_source,
    )
    efficiency = efficiency_summary(activities, start, end, recent_days=recent_days, families=families, max_vi=max_vi, environment=env)
    payload: dict[str, Any] = {
        "athlete_id": athlete_id_to_use, "start": start.isoformat(), "end": end.isoformat(), "filters": filters,
        "decoupling": decoupling, "efficiency": efficiency, "reasons": REASONS, "references": REFERENCES,
    }
    if detail_level == "compact":
        for entry in decoupling["by_sport"].values():
            entry.pop("sessions")
    if detail_level != "full":
        decoupling.pop("excluded_sessions")
    if output_format.strip().lower() == "json":
        return json.dumps(payload, ensure_ascii=False)
    return _text(payload, detail_level)
