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
from typing import Any

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


def _temp_text(session: dict[str, Any]) -> str:
    """'device 24 °C, weather 19 °C' (what exists), 'n/a °C' without either."""
    parts = [f"{label} {fmt(session.get(key), 0, ' °C')}" for key, label in (("avg_temp_c", "device"), ("weather_temp_c", "weather"))
             if session.get(key) is not None]
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
                    f"{_temp_text(s)}{', indoor' if s['indoor'] else ''}"
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
    start_date: str | None = None,
    end_date: str | None = None,
    sport_types: str | None = None,
    min_minutes: float = 60,
    max_vi: float = MAX_VI,
    max_temp_c: float | None = MAX_TEMP_C,
    drift_threshold_pct: float = DRIFT_THRESHOLD_PCT,
    recent_days: int = 7,
    environment: str | None = None,
    temperature_source: str = "device",
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
    detail_level: str = "standard",
) -> str:
    """Aerobic durability: decoupling of steady long sessions and efficiency factor trend (read-only)

    Uses the aerobic decoupling Intervals.icu computes per activity (power:HR, or pace:HR for
    runs without power: the drift between the first and second half, in %) for cycling and
    running sessions that pass a quality filter: at least min_minutes moving, moving time at
    least 85 % of elapsed time (no long stops), heart rate present, rides with power and a
    variability index up to max_vi (steady), average temperature up to max_temp_c (device
    sensor, or the activity's weather / feels-like with temperature_source; missing, e.g.
    indoors, passes; sessions list device and weather temperature) and optionally only indoor or outdoor sessions. Excluded sessions
    are counted per reason. Per sport family: median, range and quartiles, how many sessions
    are above drift_threshold_pct (5 % is commonly cited, Friel / Allen & Coggan), and the
    median of the last recent_days against the window median with a +/- 1 percentage point
    stability band (needs 2 recent and 3 window sessions). Per sport the qualifying share
    (e.g. 6 of 19 sessions) is shown; fewer than 8 qualifying sessions are flagged as a small
    sample, not reliable, and a mix of indoor/outdoor sessions, several bikes/shoes or power
    meters among them is pointed out. Also the efficiency factor (normalized power / average HR) of steady
    sessions with power of at least 20 min: window mean versus recent mean (+/- 2 % band);
    EF depends on the power meter, so several bikes are pointed out. For interval-level
    watts per heartbeat per power band and per bike use get_power_hr_efficiency. Filter and
    trend after morritter's upstream PR #150; statistics only, no verdict.

    Args:
        start_date: Start date YYYY-MM-DD (optional, default 41 days before end_date = 6 weeks)
        end_date: End date YYYY-MM-DD (optional, default today)
        sport_types: Comma-separated activity types, e.g. "Ride,VirtualRide" (optional, default every cycling
            and running type)
        min_minutes: Minimum moving time in minutes (optional, default 60)
        max_vi: Maximum variability index (optional, default 1.20)
        max_temp_c: Maximum average temperature in °C, null for no limit (optional, default 25)
        drift_threshold_pct: Decoupling threshold in % for the count above it (optional, default 5)
        recent_days: Recent window compared with the whole period (optional, default 7)
        environment: "indoor" or "outdoor" (optional, default both)
        temperature_source: Temperature for max_temp_c: "device" (sensor, default; reads body and sun heat as well),
            "weather" (Intervals.icu weather along the track) or "feels_like" (optional)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
        detail_level: "compact" (per sport summary), "standard" (default, plus the qualifying sessions and the
            reference) or "full" (plus the excluded sessions; JSON includes session lists only at standard and full)
    """
    athlete_id_to_use, error_msg = resolve_request(athlete_id, detail_level)
    if error_msg:
        return error_msg
    env = (environment or "").strip().lower() or None
    if env not in (None, "indoor", "outdoor"):
        return "Error: environment must be 'indoor' or 'outdoor'."
    temperature_source = (temperature_source or "device").strip().lower()
    if temperature_source not in TEMPERATURE_SOURCES:
        return f"Error: temperature_source must be one of {', '.join(TEMPERATURE_SOURCES)}."
    if min_minutes <= 0 or max_vi < 1 or recent_days < 1 or drift_threshold_pct <= 0:
        return "Error: min_minutes, recent_days and drift_threshold_pct must be positive and max_vi at least 1."
    period = resolve_period(start_date, end_date, DEFAULT_DAYS, MAX_DAYS)
    if isinstance(period, str):
        return period
    start, end = period
    activities, error = await fetch_activities(athlete_id_to_use, api_key, start, end, f"{LOAD_FIELDS},{DURABILITY_FIELDS}")
    if error:
        return error
    wanted = wanted_types(sport_types)
    activities = filter_types(activities, wanted)
    families = FAMILIES if not wanted else tuple(
        family for family, types in SPORT_FAMILIES.items() if any(t.lower() in wanted for t in types)
    ) or FAMILIES
    filters = {"min_minutes": min_minutes, "max_vi": max_vi, "max_temp_c": max_temp_c, "temperature_source": temperature_source, "environment": env,
               "drift_threshold_pct": drift_threshold_pct, "recent_days": recent_days, "sport_types": sport_types}
    decoupling = decoupling_summary(
        activities, start, end, recent_days=recent_days, threshold_pct=drift_threshold_pct, families=families,
        min_moving_s=min_minutes * 60, max_vi=max_vi, max_temp_c=max_temp_c, environment=env, temperature_source=temperature_source,
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
