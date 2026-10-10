"""
Training intensity distribution MCP tool for Intervals.icu (read-only).

get_intensity_distribution collapses the time in zones of every activity into the
three-zone model, computes the polarization index after Treff et al. 2019 and the
distribution class per period, sport family, ISO week and half of the period, and counts
hard sessions and days. The metrics follow the coach metrics proposed by morritter in
upstream pull request mvilanova/intervals-mcp-server#150 and are computed in
``utils.intensity``. One API call (the activity list with a field selection).
"""

import json
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field

from intervals_mcp_server.tools.training_load import (
    LOAD_FIELDS,
    ZONE_FIELDS,
    fetch_activities,
    filter_types,
    fmt,
    resolve_period,
    resolve_request,
    wanted_types,
)
from intervals_mcp_server.utils.intensity import (
    CLASS_RULES,
    HARD_HIGH_SECS,
    HARD_IF,
    HARD_IF_MIN_SECS,
    REFERENCES,
    THRESHOLD_MODES,
    ZONE_BASES,
    analyze_period,
    mapping_text,
)
from intervals_mcp_server.utils.params import (
    AthleteId,
    DetailLevel,
    EndDate,
    OutputFormat,
    SportTypes,
    ThresholdAs,
    lower_choice,
)

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool


DEFAULT_DAYS = 28
MAX_DAYS = 366


def dist_text(block: dict[str, Any], with_hard: bool = True) -> str:
    """'Z1 78.1 % | Z2 9.0 % | Z3 12.9 % (12.3 h, 9 sessions), PI 2.05, Polarized (Z1 > Z3 > Z2)'."""
    if block.get("pct") is None:
        text = "no zone data"
    else:
        z1, z2, z3 = block["pct"]
        pi = fmt(block["pi"], 2) if block["pi"] is not None else f"n/a ({block['pi_note']})"
        note = f" ({block['pi_note']})" if block["pi"] is not None and block.get("pi_note") else ""
        text = (
            f"Z1 {fmt(z1, 1)} % | Z2 {fmt(z2, 1)} % | Z3 {fmt(z3, 1)} % ({fmt(block['hours'], 1)} h, "
            f"{block['sessions']} session{'' if block['sessions'] == 1 else 's'}), PI {pi}{note}, {block['class']} ({block['order']})"
        )
    if with_hard and "hard_sessions" in block:
        text += f"; hard sessions {block['hard_sessions']} on {block['hard_days']} days"
    return text


def _basis_text(block: dict[str, Any]) -> str:
    shares = block.get("basis_pct") or {}
    return ", ".join(f"{basis} {fmt(pct)} %" for basis, pct in shares.items()) or "none"


def mixed_basis_note(block: dict[str, Any]) -> str | None:
    """Caveat when a total combines time in zones of different bases (power for rides, HR for others)."""
    shares = block.get("basis_pct") or {}
    if len(shares) < 2:
        return None
    return (
        f"the total combines zone bases ({_basis_text(block)} of the time: power zones for cycling, HR/pace zones for "
        "other sports), which are not the same thresholds; compare the per-sport split"
    )


def sport_split_text(by_sport: dict[str, Any], limit: int = 4) -> str:
    """'cycling (power) 52/36/12 %, walking (hr) 88/12/0 %' - Z1/Z2/Z3 shares per sport family and its basis."""
    parts = []
    for family, block in list(by_sport.items())[:limit]:
        if block.get("pct") is None:
            continue
        basis = "/".join(block.get("basis_pct") or {}) or "n/a"
        parts.append(f"{family} ({basis}) " + "/".join(fmt(v) for v in block["pct"]) + " %")
    return ", ".join(parts)


HARD_RULE_SHORT = f"hard session = >= {HARD_HIGH_SECS // 60} min in Z3 or IF >= {HARD_IF:.2f} on >= {HARD_IF_MIN_SECS // 60} min"


def _text(payload: dict[str, Any], detail_level: str) -> str:  # pylint: disable=too-many-locals
    result = payload["result"]
    total, coverage, drift = result["total"], result["coverage"], result["drift"]
    lines = [
        f"Intensity distribution for athlete {payload['athlete_id']}, {result['start']} to {result['end']} "
        f"({result['days']} days, three-zone model, zone basis {result['zone_basis']}, power Z4 counted as {result.get('threshold_as', 'moderate')}; time in zones of each activity):",
        f"Period: {dist_text(total)}",
        f"Zone basis of the time: {_basis_text(total)}. Coverage: {coverage['sessions_with_zones']} of {coverage['sessions']} "
        f"sessions with usable zones, {fmt(coverage['moving_time_with_zones_pct'])} % of {fmt(coverage['moving_hours'], 1)} h moving time"
        + ("; excluded: " + ", ".join(f"{reason} {count}" for reason, count in coverage["excluded"].items()) if coverage["excluded"] else ""),
    ]
    mixed = mixed_basis_note(total)
    if mixed:
        lines.append(f"Caveat: {mixed} below.")
    if detail_level == "compact":
        lines.append(f"Rule: {HARD_RULE_SHORT}.")
    if result["by_sport"]:
        lines.append("By sport family:")
        for family, block in result["by_sport"].items():
            lines.append(f"  {family}: {dist_text(block)} [basis {_basis_text(block)}]")
    if drift["available"]:
        delta = ", ".join(f"Z{i + 1} {fmt(v, 1, signed=True)} pp" for i, v in enumerate(drift["delta_pp"]))
        lines.append(
            f"Drift {drift['first']['start']}..{drift['first']['end']} -> {drift['second']['start']}..{drift['second']['end']}: "
            f"{delta}; PI {fmt(drift['first']['pi'], 2)} -> {fmt(drift['second']['pi'], 2)}; class {drift['first']['class']} -> "
            f"{drift['second']['class']}"
        )
    else:
        lines.append(f"Drift between the halves: n/a ({drift.get('note')})")
    if detail_level != "compact":
        lines.append("ISO weeks:")
        for week in result["weeks"]:
            partial = f", {week['days']} d" if week["days"] < 7 else ""
            lines.append(f"  {week['week']} ({week['start']} to {week['end']}{partial}): {dist_text(week)}")
        maps = mapping_text(result.get("threshold_as", "moderate"))
        lines.append(
            "Zone mapping to Z1 | Z2 | Z3 by zone count: "
            + "; ".join(f"{basis} " + ", ".join(f"{n} zones {m}" for n, m in counts.items()) for basis, counts in maps.items())
            + ". Auto basis: power for cycling, otherwise heart rate, then pace, then power."
        )
        lines.append(f"Classes: {CLASS_RULES}.")
        lines.append(
            f"Hard session: {REFERENCES['hard_session']['text']}. Polarization index: {REFERENCES['polarization_index']['source']}."
        )
    if detail_level == "full":
        lines.append("Sessions:")
        for row in result["sessions"]:
            if row["z"] is None:
                zones = f"no zones ({row['excluded']})"
            else:
                zones = " / ".join(f"{v / 60:.0f}" for v in row["z"]) + f" min Z1/Z2/Z3 ({row['basis']}, {row['zone_count']} zones)"
            hard = f", hard: {', '.join(row['hard_reasons'])}" if row["hard"] else ""
            lines.append(
                f"  {row['date']} {row['type']} '{row['name']}' ({row['id']}): {zones}, IF {fmt(row['intensity_factor'], 2)}{hard}"
            )
    lines.append("Statistics of the recorded zone times only; no assessment is made.")
    return "\n".join(lines)


@tool("read")
async def get_intensity_distribution(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    start_date: Annotated[str | None, Field(
        description="First day YYYY-MM-DD; default 27 days before end_date (4 weeks); max 366 days"
    )] = None,
    end_date: EndDate = None,
    zone_basis: Annotated[
        Literal["auto", "power", "hr", "pace"],
        BeforeValidator(lower_choice),
        Field(description="auto = power for cycling, else HR, then pace, then power; or one basis for all"),
    ] = "auto",
    sport_types: SportTypes = None,
    threshold_as: ThresholdAs = "moderate",
    athlete_id: AthleteId = None,
    output_format: OutputFormat = "text",
    detail_level: Annotated[DetailLevel, Field(
        description="compact = period, sports, drift; standard adds ISO weeks, zone mapping and rules; full adds "
        "every session"
    )] = "standard",
) -> str:
    """Use for the training intensity distribution of a period (default the last 4 weeks, read-only): Intervals.icu's time in zones of each activity mapped to the three-zone model (Z1 below the first threshold, Z2 between, Z3 above the second).

    Returns Z1/Z2/Z3 shares and hours, the polarization index (Treff et al. 2019), the class
    (Base, Polarized, Pyramidal, Threshold, HIT) and hard sessions and days for the period, per
    sport family and per ISO week, and the drift between the two halves. The mapping depends on
    the zone basis and the zone count of the athlete's model. Activities without zone data are
    never counted as easy; the coverage is reported. Statistics only, no verdict. Overview:
    get_coach_context. Method: intervals://methods/intensity (get_guide).
    """
    athlete_id_to_use, error_msg = resolve_request(athlete_id, detail_level)
    if error_msg:
        return error_msg
    basis = (zone_basis or "auto").strip().lower()
    if basis not in ZONE_BASES:
        return f"Error: zone_basis must be one of {', '.join(ZONE_BASES)}."
    threshold_mode = (threshold_as or "moderate").strip().lower()
    if threshold_mode not in THRESHOLD_MODES:
        return f"Error: threshold_as must be one of {', '.join(THRESHOLD_MODES)}."
    period = resolve_period(start_date, end_date, DEFAULT_DAYS, MAX_DAYS)
    if isinstance(period, str):
        return period
    start, end = period
    activities, error = await fetch_activities(
        athlete_id_to_use, start, end, f"{LOAD_FIELDS},{ZONE_FIELDS}"
    )
    if error:
        return error
    result = analyze_period(filter_types(activities, wanted_types(sport_types)), start, end, basis, threshold_mode)
    if detail_level != "full":
        result.pop("sessions")
    payload = {
        "athlete_id": athlete_id_to_use, "sport_types": sport_types, "result": result,
        "mapping": mapping_text(threshold_mode), "class_rules": CLASS_RULES, "references": REFERENCES,
    }
    if output_format.strip().lower() == "json":
        return json.dumps(payload, ensure_ascii=False)
    return _text(payload, detail_level)
