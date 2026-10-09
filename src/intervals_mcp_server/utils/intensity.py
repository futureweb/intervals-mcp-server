"""
Training intensity distribution (pure functions, no API access).

Collapses the per-activity time in zones that Intervals.icu stores (power, heart rate or
pace zones) into the three-zone model (Z1 below the first threshold, Z2 between the
thresholds, Z3 above the second threshold), computes the polarization index after Treff et
al. 2019 (Front Physiol 10:707), classifies the distribution (Base, Polarized, Pyramidal,
Threshold, HIT), counts hard sessions and days and compares the two halves of a period.

The metric set and the edge cases of the index follow the coach metrics proposed by
morritter in upstream pull request mvilanova/intervals-mcp-server#150; the zone mapping
here depends on the number of zones of the athlete's zone model.

Missing zone data is never counted as easy time: activities without zone times (or with a
zone model that has no documented mapping) are left out and counted in the coverage.
"""

import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from intervals_mcp_server.utils.load_metrics import activity_day, day_range, intensity_factor, iso_week, num, rnd
from intervals_mcp_server.utils.sports import sport_family

Activity = dict[str, Any]

BASES = ("power", "hr", "pace")
ZONE_BASES = ("auto",) + BASES

# Zone index (Z1 = first entry) -> three-zone model, keyed by the number of zones.
# power, % of FTP (Coggan 7 zones: Z1-Z2 <= 75 % low, Z3 76-90 % and Z4 91-105 % moderate,
#   Z5-Z7 > 105 % high). Z4 is threshold work around the second threshold, which Seiler's
#   model places in the middle zone; it is counted as moderate by default, consistent with the
#   LTHR-based HR mapping. threshold_as="high" counts it as high instead (the choice of #150);
# hr, % of LTHR (Intervals.icu / Friel 7 zones: Z1-Z2 < ~90 % low, Z3-Z4 90-99 % moderate,
#   Z5a-Z5c >= LTHR high);
# pace, % of threshold pace (Intervals.icu 7 zones: Z1-Z2 low, Z3-Z4 moderate, Z5a-Z5c high);
# 5 zones (e.g. %HRmax or Seiler's five zones): Z1-Z2 low, Z3 moderate, Z4-Z5 high;
# 3 zones: taken as they are.
ZONE_MAPS: dict[str, dict[int, tuple[int, ...]]] = {
    "power": {3: (1, 2, 3), 5: (1, 1, 2, 3, 3), 6: (1, 1, 2, 2, 3, 3), 7: (1, 1, 2, 2, 3, 3, 3)},
    "hr": {3: (1, 2, 3), 5: (1, 1, 2, 3, 3), 7: (1, 1, 2, 2, 3, 3, 3)},
    "pace": {3: (1, 2, 3), 5: (1, 1, 2, 3, 3), 7: (1, 1, 2, 2, 3, 3, 3)},
}
# Power mappings with the threshold zone (Coggan Z4) counted as high (threshold_as="high").
POWER_THRESHOLD_HIGH: dict[int, tuple[int, ...]] = {6: (1, 1, 2, 3, 3, 3), 7: (1, 1, 2, 3, 3, 3, 3)}
THRESHOLD_MODES = ("moderate", "high")
# Preferred zone basis per sport family in "auto" mode (first one with data wins).
AUTO_ORDER: dict[str, tuple[str, ...]] = {"cycling": ("power", "hr", "pace")}
DEFAULT_ORDER: tuple[str, ...] = ("hr", "pace", "power")

PI_Z3_MIN = 0.01
PI_Z2_SUBSTITUTE = 0.01
PI_POLARIZED = 2.0
HARD_HIGH_SECS = 600
HARD_IF = 0.85
HARD_IF_MIN_SECS = 1200

REFERENCES: dict[str, dict[str, Any]] = {
    "polarization_index": {
        "polarized_above": PI_POLARIZED,
        "text": "PI = log10(Z1/Z2 x Z3 x 100) with zone fractions; a distribution with Z1 > Z3 > Z2 and PI > 2.0 "
        "is classed as polarized",
        "source": "Treff et al. 2019, Front Physiol 10:707",
    },
    "three_zone_model": {
        "text": "Z1 below the first lactate/ventilatory threshold, Z2 between the thresholds, Z3 above the second; "
        "threshold work (power Z4, 91-105 % FTP) is middle-zone work by default",
        "source": "Seiler & Kjerland 2006, Scand J Med Sci Sports 16:49-56",
    },
    "hard_session": {
        "text": f"at least {HARD_HIGH_SECS // 60} min in three-zone Z3, or an intensity factor of at least "
        f"{HARD_IF:.2f} on a session of at least {HARD_IF_MIN_SECS // 60} min",
        "source": "IF bands after Allen & Coggan (Training and Racing with a Power Meter): above 0.85 is beyond "
        "endurance-paced training",
    },
}

CLASS_RULES = (
    "Base: Z3 < 1 % and Z1 >= Z2; Polarized: Z1 > Z3 > Z2 and PI > 2.0; Pyramidal: Z1 > Z2 > Z3; "
    "Threshold: Z2 largest; HIT: Z3 largest; any other order (e.g. Z1 > Z3 > Z2 with PI <= 2.0) is reported "
    "as Pyramidal (rules of #150)"
)


def _mapping(basis: str, count: int, threshold_as: str = "moderate") -> tuple[int, ...] | None:
    if basis == "power" and threshold_as == "high" and count in POWER_THRESHOLD_HIGH:
        return POWER_THRESHOLD_HIGH[count]
    return ZONE_MAPS.get(basis, {}).get(count)


def mapping_text(threshold_as: str = "moderate") -> dict[str, dict[str, str]]:
    """Readable zone mapping per basis and zone count, e.g. {'power': {'7': 'Z1-Z2 | Z3-Z4 | Z5-Z7'}}."""
    out: dict[str, dict[str, str]] = {}
    for basis, maps in ZONE_MAPS.items():
        out[basis] = {}
        for count in maps:
            mapping = _mapping(basis, count, threshold_as) or maps[count]
            groups = []
            for target in (1, 2, 3):
                zones = [i + 1 for i, value in enumerate(mapping) if value == target]
                groups.append(f"Z{zones[0]}" if len(zones) == 1 else f"Z{zones[0]}-Z{zones[-1]}")
            out[basis][str(count)] = " | ".join(groups)
    return out


# ---------------------------------------------------------------------------
# Zone times of one activity
# ---------------------------------------------------------------------------


def zone_seconds(activity: Activity, basis: str) -> list[float] | None:
    """Seconds per zone (Z1 first) of one basis; None without data or with zero total time.

    Power zones come from ``icu_zone_times`` ({id: Z1.., secs}); the sweet-spot bucket (id SS)
    overlaps Z3/Z4 and is ignored. HR and pace zones are plain lists (index = zone).
    """
    if basis == "power":
        raw = activity.get("icu_zone_times")
        if not isinstance(raw, list):
            return None
        by_zone: dict[int, float] = {}
        for zone in raw:
            if not isinstance(zone, dict) or not isinstance(zone.get("id"), str):
                continue
            zone_id = zone["id"].strip().upper()
            if len(zone_id) >= 2 and zone_id[0] == "Z" and zone_id[1:].isdigit():
                by_zone[int(zone_id[1:])] = max(num(zone.get("secs")) or 0.0, 0.0)
        if not by_zone or min(by_zone) < 1:
            return None
        secs = [by_zone.get(index, 0.0) for index in range(1, max(by_zone) + 1)]
    else:
        raw = activity.get("icu_hr_zone_times" if basis == "hr" else "pace_zone_times")
        if not isinstance(raw, list) or not raw:
            return None
        secs = [max(num(value) or 0.0, 0.0) for value in raw]
    return secs if sum(secs) > 0 else None


def three_zones(secs: list[float], basis: str, threshold_as: str = "moderate") -> list[float] | None:
    """Collapse zone seconds into [Z1, Z2, Z3]; None when the zone count has no mapping."""
    mapping = _mapping(basis, len(secs), threshold_as)
    if mapping is None:
        return None
    result = [0.0, 0.0, 0.0]
    for index, value in enumerate(secs):
        result[mapping[index] - 1] += value
    return result


def activity_zones(activity: Activity, zone_basis: str = "auto", threshold_as: str = "moderate") -> dict[str, Any]:
    """Three-zone seconds of one activity: {"z": [..] or None, "basis", "zone_count", "excluded"}.

    ``zone_basis`` "auto" uses power for cycling and heart rate (then pace, then power) for
    every other sport, falling back to the next basis with usable data.
    """
    order = (zone_basis,) if zone_basis in BASES else AUTO_ORDER.get(sport_family(activity.get("type")), DEFAULT_ORDER)
    unmapped = None
    for basis in order:
        secs = zone_seconds(activity, basis)
        if secs is None:
            continue
        collapsed = three_zones(secs, basis, threshold_as)
        if collapsed is not None:
            return {"z": collapsed, "basis": basis, "zone_count": len(secs), "excluded": None}
        unmapped = unmapped or f"{basis} zone model with {len(secs)} zones has no three-zone mapping"
    return {"z": None, "basis": None, "zone_count": None, "excluded": unmapped or "no zone times"}


# ---------------------------------------------------------------------------
# Polarization index and classification
# ---------------------------------------------------------------------------


def polarization_index(z1: float, z2: float, z3: float) -> tuple[float | None, str | None]:
    """Polarization index after Treff et al. 2019 with zone fractions, and a note for edge cases.

    No zone time -> (None, "no zone data"); Z3 < 1 % -> (None, "Z3 below 1 %": the log is
    undefined for Z3 = 0 and such a distribution is not polarized); Z1 = 0 -> (None, "Z1 is 0");
    Z2 = 0 -> Z2 is replaced by 0.01 as proposed by Treff et al. ("Z2 = 0 replaced by 0.01").
    """
    if z1 + z2 + z3 <= 0:
        return None, "no zone data"
    if z3 < PI_Z3_MIN:
        return None, "Z3 below 1 %"
    if z1 <= 0:
        return None, "Z1 is 0"
    note = None
    if z2 <= 0:
        z2 = PI_Z2_SUBSTITUTE
        note = "Z2 = 0 replaced by 0.01"
    return math.log10(z1 / z2 * z3 * 100), note


def classify(z1: float, z2: float, z3: float, pi: float | None) -> str:
    """Distribution class from zone fractions (rules in CLASS_RULES)."""
    if z3 < PI_Z3_MIN and z1 >= z2:
        return "Base"
    if z1 > z3 > z2 and pi is not None and pi > PI_POLARIZED:
        return "Polarized"
    if z1 > z2 > z3:
        return "Pyramidal"
    if z2 >= z1 and z2 >= z3:
        return "Threshold"
    if z3 >= z1 and z3 >= z2:
        return "HIT"
    return "Pyramidal"


def zone_order(z1: float, z2: float, z3: float) -> str:
    """Order of the three zones by time, e.g. 'Z1 > Z3 > Z2' (ties shown with '=')."""
    ranked = sorted((("Z1", z1), ("Z2", z2), ("Z3", z3)), key=lambda item: -item[1])
    text = ranked[0][0]
    for (_, previous), (name, value) in zip(ranked, ranked[1:], strict=False):
        text += (" = " if math.isclose(previous, value) else " > ") + name
    return text


def distribution(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Period distribution of activity zone entries (``activity_zones`` results with "z")."""
    totals = [0.0, 0.0, 0.0]
    by_basis: dict[str, float] = defaultdict(float)
    sessions = 0
    for entry in entries:
        if entry.get("z") is None:
            continue
        sessions += 1
        for index in range(3):
            totals[index] += entry["z"][index]
        by_basis[entry["basis"]] += sum(entry["z"])
    total = sum(totals)
    if total <= 0:
        return {"sessions": sessions, "hours": 0.0, "pct": None, "pi": None, "pi_note": "no zone data",
                "class": None, "order": None, "basis_pct": {}}
    z1, z2, z3 = (value / total for value in totals)
    pi, note = polarization_index(z1, z2, z3)
    return {
        "sessions": sessions,
        "hours": rnd(total / 3600, 1),
        "hours_by_zone": [rnd(value / 3600, 1) for value in totals],
        "pct": [rnd(z1 * 100, 1), rnd(z2 * 100, 1), rnd(z3 * 100, 1)],
        "pi": rnd(pi, 2),
        "pi_note": note,
        "class": classify(z1, z2, z3, pi),
        "order": zone_order(z1, z2, z3),
        "basis_pct": {basis: rnd(secs / total * 100, 0) for basis, secs in sorted(by_basis.items())},
    }


def hard_session(activity: Activity, entry: dict[str, Any]) -> tuple[bool | None, list[str]]:
    """Whether a session counts as hard and why; None when neither zone data nor IF exist."""
    reasons = []
    z = entry.get("z")
    if z is not None and z[2] >= HARD_HIGH_SECS:
        reasons.append(f"{z[2] / 60:.0f} min in Z3")
    factor = intensity_factor(activity)
    moving = num(activity.get("moving_time")) or 0.0
    if factor is not None and factor >= HARD_IF and moving >= HARD_IF_MIN_SECS:
        reasons.append(f"IF {factor:.2f}")
    if reasons:
        return True, reasons
    if z is None and factor is None:
        return None, []
    return False, []


# ---------------------------------------------------------------------------
# Period analysis
# ---------------------------------------------------------------------------


def session_rows(activities: list[Activity], zone_basis: str = "auto", threshold_as: str = "moderate") -> list[dict[str, Any]]:
    """One row per activity with its three-zone seconds, basis, hard flag and exclusion reason."""
    rows = []
    for activity in sorted(activities, key=lambda a: str(a.get("start_date_local") or "")):
        day = activity_day(activity)
        if day is None:
            continue
        entry = activity_zones(activity, zone_basis, threshold_as)
        hard, reasons = hard_session(activity, entry)
        rows.append({
            "date": day.isoformat(), "day": day, "id": activity.get("id"), "name": activity.get("name"),
            "type": activity.get("type"), "family": sport_family(activity.get("type")),
            "moving_time": num(activity.get("moving_time")), "z": entry["z"], "basis": entry["basis"],
            "zone_count": entry["zone_count"], "excluded": entry["excluded"],
            "intensity_factor": rnd(intensity_factor(activity), 2), "hard": hard, "hard_reasons": reasons,
        })
    return rows


def _hard_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    hard = [row for row in rows if row["hard"]]
    return {
        "hard_sessions": len(hard),
        "hard_days": len({row["day"] for row in hard}),
        "sessions_unclassified": sum(1 for row in rows if row["hard"] is None),
    }


def _block(rows: list[dict[str, Any]], start: date, end: date) -> dict[str, Any]:
    selected = [row for row in rows if start <= row["day"] <= end]
    return {"start": start.isoformat(), "end": end.isoformat(), **distribution(selected), **_hard_counts(selected)}


def _coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    with_zones = [row for row in rows if row["z"] is not None]
    excluded: dict[str, int] = defaultdict(int)
    for row in rows:
        if row["excluded"]:
            excluded[row["excluded"]] += 1
    moving_total = sum(row["moving_time"] or 0.0 for row in rows)
    moving_zones = sum(row["moving_time"] or 0.0 for row in with_zones)
    return {
        "sessions": len(rows),
        "sessions_with_zones": len(with_zones),
        "moving_hours": rnd(moving_total / 3600, 1),
        "moving_hours_with_zones": rnd(moving_zones / 3600, 1),
        "moving_time_with_zones_pct": rnd(moving_zones / moving_total * 100, 0) if moving_total else None,
        "excluded": dict(sorted(excluded.items())),
    }


def drift(rows: list[dict[str, Any]], start: date, end: date) -> dict[str, Any]:
    """Distribution of the first and second half of the period and the change between them."""
    days = (end - start).days + 1
    if days < 2:
        return {"available": False, "note": "period shorter than 2 days"}
    middle = start + timedelta(days=days // 2 - 1)
    first = _block(rows, start, middle)
    second = _block(rows, middle + timedelta(days=1), end)
    result: dict[str, Any] = {"first": first, "second": second, "available": False}
    if first["pct"] is None or second["pct"] is None:
        result["note"] = "one half has no zone data"
        return result
    result["available"] = True
    result["delta_pp"] = [rnd(b - a, 1) for a, b in zip(first["pct"], second["pct"], strict=True)]
    result["pi_delta"] = rnd(second["pi"] - first["pi"], 2) if first["pi"] is not None and second["pi"] is not None else None
    result["class_changed"] = first["class"] != second["class"]
    return result


def analyze_period(
    activities: list[Activity], start: date, end: date, zone_basis: str = "auto", threshold_as: str = "moderate"
) -> dict[str, Any]:
    """Distribution for the period, per sport family, per ISO week and per half, plus coverage."""
    rows = [row for row in session_rows(activities, zone_basis, threshold_as) if start <= row["day"] <= end]
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        families[row["family"]].append(row)
    by_family = {
        family: {**distribution(items), **_hard_counts(items), "coverage": _coverage(items)}
        for family, items in sorted(families.items(), key=lambda kv: -sum(r["moving_time"] or 0 for r in kv[1]))
    }
    weeks = []
    monday = start - timedelta(days=start.weekday())
    while monday <= end:
        week_start, week_end = max(monday, start), min(monday + timedelta(days=6), end)
        block = _block(rows, week_start, week_end)
        weeks.append({"week": iso_week(monday), "days": len(day_range(week_start, week_end)), **block})
        monday += timedelta(days=7)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": (end - start).days + 1,
        "zone_basis": zone_basis,
        "threshold_as": threshold_as,
        "total": {**distribution(rows), **_hard_counts(rows)},
        "by_sport": by_family,
        "weeks": weeks,
        "drift": drift(rows, start, end),
        "coverage": _coverage(rows),
        "sessions": [{key: value for key, value in row.items() if key != "day"} for row in rows],
    }
