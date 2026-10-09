"""
Aerobic durability and efficiency (pure functions, no API access).

Uses the per-activity values Intervals.icu computes: aerobic decoupling (``decoupling``,
the drift of power:HR or pace:HR between the first and second half, in %) and the
efficiency factor (``icu_efficiency_factor``, normalized power / average HR). Only steady,
long enough sessions are meaningful, so every session passes a quality filter and excluded
sessions are counted per reason.

The filter, the 5 % drift threshold and the recent-versus-window comparison with a
stability band follow the coach metrics proposed by morritter in upstream pull request
mvilanova/intervals-mcp-server#150. Interval-level watts per heartbeat per power band is a
different view and lives in ``get_power_hr_efficiency``.
"""

import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from intervals_mcp_server.utils.load_metrics import activity_day, num, rnd
from intervals_mcp_server.utils.sports import is_indoor, sport_family

Activity = dict[str, Any]

FAMILIES = ("cycling", "running")
MIN_MOVING_S = 3600
MIN_MOVING_RATIO = 0.85
MAX_VI = 1.20
MAX_TEMP_C = 25.0
DRIFT_THRESHOLD_PCT = 5.0
TREND_BAND_PP = 1.0
MIN_RECENT = 2
MIN_WINDOW = 3
SMALL_SAMPLE = 8  # fewer qualifying sessions per sport are flagged "small sample, not reliable"
EF_MIN_MOVING_S = 1200
EF_BAND_PCT = 2.0

REASONS: dict[str, str] = {
    "short": "moving time below the minimum",
    "pauses": "moving time below 85 % of elapsed time (long stops)",
    "environment": "indoor/outdoor filter",
    "heat": "average temperature above the limit",
    "no_hr": "no heart rate",
    "no_power": "ride without power (no variability index)",
    "variable": "variability index above the limit (not steady)",
    "no_decoupling": "no decoupling value from Intervals.icu",
    "no_efficiency_factor": "no efficiency factor from Intervals.icu",
}

REFERENCES: dict[str, dict[str, Any]] = {
    "decoupling": {
        "threshold_pct": DRIFT_THRESHOLD_PCT,
        "text": "aerobic decoupling below 5 % over a steady long session is commonly read as good aerobic endurance",
        "source": "Friel (The Cyclist's Training Bible); Allen & Coggan (Training and Racing with a Power Meter)",
    },
}


def exclusion_reason(  # pylint: disable=too-many-arguments,too-many-return-statements
    activity: Activity,
    *,
    min_moving_s: float = MIN_MOVING_S,
    max_vi: float = MAX_VI,
    max_temp_c: float | None = MAX_TEMP_C,
    environment: str | None = None,
    need: str = "decoupling",
) -> str | None:
    """First failed quality criterion of a session (keys of REASONS), None when it qualifies.

    A missing temperature (typical indoors) passes the heat check; runs without power skip
    the variability check (pace:HR). ``need`` = "efficiency" requires power and checks the
    efficiency factor instead of the decoupling, without the pause check.
    """
    moving = num(activity.get("moving_time")) or 0.0
    if moving < min_moving_s:
        return "short"
    elapsed = num(activity.get("elapsed_time"))
    if need == "decoupling" and elapsed and moving / elapsed < MIN_MOVING_RATIO:
        return "pauses"
    if environment and is_indoor(activity) != (environment == "indoor"):
        return "environment"
    temp = num(activity.get("average_temp"))
    if max_temp_c is not None and temp is not None and temp > max_temp_c:
        return "heat"
    heart_rate = num(activity.get("average_heartrate"))
    if heart_rate is None or heart_rate <= 0:
        return "no_hr"
    vi = num(activity.get("icu_variability_index"))
    if vi is None or vi <= 0:
        if need == "efficiency" or sport_family(activity.get("type")) == "cycling":
            return "no_power"
    elif vi > max_vi:
        return "variable"
    if need == "decoupling" and num(activity.get("decoupling")) is None:
        return "no_decoupling"
    if need == "efficiency" and num(activity.get("icu_efficiency_factor")) is None:
        return "no_efficiency_factor"
    return None


def _gear_id(activity: Activity) -> Any:
    gear = activity.get("gear")
    return gear.get("id") if isinstance(gear, dict) else activity.get("gear_id")


def _session(activity: Activity) -> dict[str, Any]:
    day = activity_day(activity)
    vi = num(activity.get("icu_variability_index"))
    return {
        "date": day.isoformat() if day else None, "id": activity.get("id"), "name": activity.get("name"),
        "type": activity.get("type"), "minutes": rnd((num(activity.get("moving_time")) or 0) / 60, 0),
        "decoupling_pct": rnd(num(activity.get("decoupling")), 1),
        "efficiency_factor": rnd(num(activity.get("icu_efficiency_factor")), 3),
        "variability_index": rnd(vi, 2), "avg_temp_c": rnd(num(activity.get("average_temp")), 1),
        "indoor": is_indoor(activity), "basis": "power:HR" if vi else "pace:HR",
        "gear_id": _gear_id(activity), "power_meter": activity.get("power_meter") or None,
    }


def heterogeneity(sessions: list[dict[str, Any]]) -> list[str]:
    """Notes when the qualifying sessions mix indoor/outdoor, several bikes/shoes or power meters."""
    notes = []
    indoor = sum(1 for s in sessions if s["indoor"])
    if 0 < indoor < len(sessions):
        notes.append(f"indoor and outdoor mixed ({indoor} indoor, {len(sessions) - indoor} outdoor)")
    gear = {s["gear_id"] for s in sessions if s.get("gear_id")}
    if len(gear) > 1:
        notes.append(f"{len(gear)} different bikes/shoes")
    meters = {s["power_meter"] for s in sessions if s.get("power_meter")}
    if len(meters) > 1:
        notes.append(f"{len(meters)} different power meters ({', '.join(sorted(meters))})")
    return notes


def _describe(values: list[float]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(values), "median": rnd(statistics.median(values), 1) if values else None}
    if values:
        out.update({"mean": rnd(statistics.fmean(values), 1), "min": rnd(min(values), 1), "max": rnd(max(values), 1)})
    if len(values) >= 4:
        p25, _, p75 = statistics.quantiles(values, n=4, method="inclusive")
        out.update({"p25": rnd(p25, 1), "p75": rnd(p75, 1)})
    return out


def _direction(delta: float, band: float) -> str:
    if delta < -band:
        return "lower"
    if delta > band:
        return "higher"
    return "within band"


def decoupling_summary(  # pylint: disable=too-many-arguments,too-many-locals
    activities: list[Activity],
    start: date,
    end: date,
    *,
    recent_days: int = 7,
    threshold_pct: float = DRIFT_THRESHOLD_PCT,
    band_pp: float = TREND_BAND_PP,
    families: tuple[str, ...] = FAMILIES,
    **filters: Any,
) -> dict[str, Any]:
    """Decoupling of qualifying sessions per sport family over [start, end].

    Per family: median (plus mean, range and quartiles from 4 values), count and share above
    ``threshold_pct``, and the median of the last ``recent_days`` against the window median
    (direction "lower"/"higher" outside +/- ``band_pp`` percentage points, else "within band";
    needs 2 recent and 3 window values). Fewer than 8 values are flagged as a small sample;
    the number of sessions considered per family (qualifying share) and the mix of the
    qualifying sessions (indoor/outdoor, bikes/shoes, power meters) are reported.
    Negative values (HR drifting down) are kept.
    """
    recent_start = end - timedelta(days=recent_days - 1)
    reasons: dict[str, int] = defaultdict(int)
    by_family: dict[str, list[dict[str, Any]]] = {family: [] for family in families}
    considered_by_family: dict[str, int] = defaultdict(int)
    excluded_rows: list[dict[str, Any]] = []
    considered = 0
    for activity in sorted(activities, key=lambda a: str(a.get("start_date_local") or "")):
        day = activity_day(activity)
        family = sport_family(activity.get("type"))
        if day is None or not start <= day <= end or family not in by_family:
            continue
        considered += 1
        considered_by_family[family] += 1
        reason = exclusion_reason(activity, **filters)
        if reason is not None:
            reasons[reason] += 1
            excluded_rows.append({**_session(activity), "reason": reason})
            continue
        by_family[family].append(_session(activity))

    result: dict[str, Any] = {}
    for family, sessions in by_family.items():
        values = [s["decoupling_pct"] for s in sessions]
        recent = [s["decoupling_pct"] for s in sessions if s["date"] and s["date"] >= recent_start.isoformat()]
        stats = _describe(values)
        above = sum(1 for v in values if v > threshold_pct)
        entry: dict[str, Any] = {
            **stats,
            "above_threshold": above,
            "above_threshold_pct": rnd(above / len(values) * 100, 0) if values else None,
            "small_sample": len(values) < SMALL_SAMPLE,
            "considered": considered_by_family[family],
            "qualifying_share_pct": rnd(len(values) / considered_by_family[family] * 100, 0) if considered_by_family[family] else None,
            "heterogeneity": heterogeneity(sessions),
            "recent": {"days": recent_days, "n": len(recent),
                       "median": rnd(statistics.median(recent), 1) if recent else None,
                       "above_threshold": sum(1 for v in recent if v > threshold_pct)},
            "basis": {basis: sum(1 for s in sessions if s["basis"] == basis) for basis in sorted({s["basis"] for s in sessions})},
            "indoor_sessions": sum(1 for s in sessions if s["indoor"]),
            "sessions": sessions,
        }
        if stats["median"] is not None and len(recent) >= MIN_RECENT and len(values) >= MIN_WINDOW and recent_days < (end - start).days + 1:
            delta = statistics.median(recent) - statistics.median(values)
            entry["recent"].update({"delta_pp": rnd(delta, 1), "direction": _direction(delta, band_pp)})
        else:
            entry["recent"].update({"delta_pp": None, "direction": None})
        result[family] = entry
    return {
        "by_sport": result,
        "considered": considered,
        "excluded": sum(reasons.values()),
        "excluded_by_reason": dict(sorted(reasons.items())),
        "excluded_sessions": excluded_rows,
    }


def efficiency_summary(  # pylint: disable=too-many-locals
    activities: list[Activity],
    start: date,
    end: date,
    *,
    recent_days: int = 7,
    families: tuple[str, ...] = FAMILIES,
    max_vi: float = MAX_VI,
    environment: str | None = None,
) -> dict[str, Any]:
    """Efficiency factor of steady sessions with power, per sport family: window vs recent mean.

    Qualifying: at least 20 min moving, HR, power with a variability index up to ``max_vi``.
    The change of the recent mean against the window mean is reported in percent with a
    +/- 2 % band; it needs 2 recent and 3 window values. EF depends on the power meter, so
    the number of different gear ids is reported.
    """
    recent_start = end - timedelta(days=recent_days - 1)
    result: dict[str, Any] = {}
    for family in families:
        values: list[tuple[str, float, Any]] = []
        for activity in activities:
            day = activity_day(activity)
            if day is None or not start <= day <= end or sport_family(activity.get("type")) != family:
                continue
            if exclusion_reason(activity, min_moving_s=EF_MIN_MOVING_S, max_vi=max_vi, max_temp_c=None,
                                environment=environment, need="efficiency") is not None:
                continue
            values.append((day.isoformat(), num(activity.get("icu_efficiency_factor")) or 0.0, _gear_id(activity)))
        window = [v for _, v, _ in values]
        recent = [v for d, v, _ in values if d >= recent_start.isoformat()]
        window_mean = statistics.fmean(window) if window else None
        recent_mean = statistics.fmean(recent) if recent else None
        entry: dict[str, Any] = {
            "n": len(window), "mean": rnd(window_mean, 3), "recent_n": len(recent), "recent_mean": rnd(recent_mean, 3),
            "change_pct": None, "direction": None, "gear_ids": len({g for _, _, g in values if g}),
            "small_sample": len(window) < SMALL_SAMPLE,
        }
        if window_mean and recent_mean is not None and len(recent) >= MIN_RECENT and len(window) >= MIN_WINDOW:
            change = (recent_mean / window_mean - 1) * 100
            entry["change_pct"] = rnd(change, 1)
            entry["direction"] = _direction(change, EF_BAND_PCT)
        result[family] = entry
    return result
