"""
Training load metrics (pure functions, no API access).

Daily Intervals.icu load, acute and chronic load, the acute:chronic workload ratio,
Foster's monotony and strain, deload-like weeks, CTL/ATL/form/ramp from the wellness
records, the exponential fitness model used to project them over planned workouts, and
the most notable sessions of a period.

The metric set follows the coach metrics proposed by morritter in upstream pull request
mvilanova/intervals-mcp-server#150; it is re-implemented here as separate functions with
explicit sample sizes and without verdicts.

Conventions:

- Only the Intervals.icu training load (``icu_training_load``) is used; device loads
  (custom fields) are handled by the tools and never added to it.
- A day without activities is a real rest day with load 0 (it counts in means, SDs and
  monotony). An activity without a load does not add to its day, as in Intervals.icu's own
  fitness model, and is counted separately so the caller can report it.
- A missing wellness value is never replaced by 0.
"""

import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from intervals_mcp_server.utils.sports import sport_family

Activity = dict[str, Any]

CTL_DAYS = 42
ATL_DAYS = 7
MONOTONY_DAYS = 7
DELOAD_RATIO = 0.8
# Monotony of a single sport or a calendar week needs this many days with load.
MIN_ACTIVE_DAYS_MONOTONY = 3
# A partial calendar week needs this many days for a weekly monotony.
MIN_WEEK_DAYS_MONOTONY = 5
# Fewer days with load in the chronic window: the ratio is flagged as a small sample.
MIN_CHRONIC_ACTIVE_DAYS = 8
TOP_SESSIONS = 5

REFERENCES: dict[str, dict[str, Any]] = {
    "acwr": {
        "range": [0.8, 1.3],
        "text": "0.8-1.3 is the range commonly cited for the acute:chronic workload ratio",
        "source": "Gabbett 2016, Br J Sports Med 50:273-280 (team sports; debated, e.g. Impellizzeri et al. 2020, "
        "Int J Sports Physiol Perform 15:907-913)",
    },
    "monotony": {
        "high": 2.0,
        "text": "monotony above 2.0 is commonly cited as high",
        "source": "Foster 1998, Med Sci Sports Exerc 30:1164-1168",
    },
    "deload": {
        "ratio": DELOAD_RATIO,
        "text": "a 7-day load at or below 80 % of the chronic weekly average is reported as deload-like",
        "source": "rule of thumb adopted from mvilanova/intervals-mcp-server#150",
    },
}

DEFINITIONS: dict[str, str] = {
    "acute_load": "sum of the daily Intervals.icu load in the acute window (rest days count as 0)",
    "chronic_load": "sum of the daily Intervals.icu load in the chronic window (includes the acute window)",
    "acwr": "acute daily mean / chronic daily mean (coupled rolling averages); undefined when the chronic load is 0",
    "monotony": "Foster: mean / sample SD of the daily load over the last 7 days, rest days included; "
    "undefined when the SD is 0 (no load or identical days)",
    "strain": "Foster: 7-day load x monotony",
    "deload_like": "7-day load <= 80 % of the chronic weekly average (chronic load / chronic days x 7)",
    "form": "CTL - ATL (Intervals.icu form / TSB)",
    "ramp": "CTL change over 7 days (Intervals.icu rampRate)",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def num(value: Any) -> float | None:
    """A finite number as float; None for null, NaN, booleans, text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def rnd(value: float | None, digits: int = 1) -> float | None:
    """Round for output; None stays None and -0.0 becomes 0.0."""
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits) + 0.0


def parse_day(value: Any) -> date | None:
    """Date part of an ISO date or datetime string."""
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def activity_day(activity: Activity) -> date | None:
    """Local calendar day of an activity or event."""
    return parse_day(activity.get("start_date_local"))


def activity_load(activity: Activity) -> float | None:
    """Intervals.icu training load of an activity or planned event; None when missing."""
    load = num(activity.get("icu_training_load"))
    return max(load, 0.0) if load is not None else None


def day_range(start: date, end: date) -> list[date]:
    """Every day from start to end inclusive (empty when start > end)."""
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def iso_week(day: date) -> str:
    """ISO week label such as 2026-W41."""
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def between(activities: list[Activity], start: date, end: date, family: str | None = None) -> list[Activity]:
    """Activities whose local day lies in [start, end], optionally of one sport family."""
    selected = []
    for activity in activities:
        day = activity_day(activity)
        if day is None or not start <= day <= end:
            continue
        if family is not None and sport_family(activity.get("type")) != family:
            continue
        selected.append(activity)
    return selected


def daily_loads(activities: list[Activity], start: date, end: date, family: str | None = None) -> dict[date, float]:
    """Daily Intervals.icu load from start to end, zero-filled (rest days are real zeros)."""
    per_day: dict[date, float] = dict.fromkeys(day_range(start, end), 0.0)
    for activity in between(activities, start, end, family):
        load = activity_load(activity)
        day = activity_day(activity)
        if load is not None and day is not None:
            per_day[day] += load
    return per_day


def monotony(values: list[float]) -> float | None:
    """Foster monotony: mean / sample SD of daily loads; None when undefined."""
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    if sd == 0:
        return None
    return statistics.fmean(values) / sd


def _monotony_note(values: list[float]) -> str | None:
    if len(values) < 2:
        return "too few days"
    if not any(values):
        return "no load"
    if statistics.stdev(values) == 0:
        return "identical daily loads"
    return None


def _position(value: float | None, low: float, high: float) -> str | None:
    if value is None:
        return None
    if value < low:
        return "below"
    if value > high:
        return "above"
    return "inside"


# ---------------------------------------------------------------------------
# Acute / chronic load, monotony, strain, deload
# ---------------------------------------------------------------------------


def _session_counts(activities: list[Activity]) -> dict[str, int]:
    days = {activity_day(a) for a in activities}
    return {
        "sessions": len(activities),
        "sessions_without_load": sum(1 for a in activities if activity_load(a) is None),
        "days_with_activity": len({d for d in days if d is not None}),
    }


def load_metrics(  # pylint: disable=too-many-locals
    activities: list[Activity], end: date, acute_days: int = 7, chronic_days: int = 28, family: str | None = None
) -> dict[str, Any]:
    """Acute and chronic load, ACWR, monotony, strain and the deload ratio at ``end``.

    Monotony and strain always use the last 7 days (Foster's weekly definition), the
    acute window may differ. ``family`` restricts everything to one sport family.
    """
    acute_start = end - timedelta(days=acute_days - 1)
    chronic_start = end - timedelta(days=chronic_days - 1)
    week_start = end - timedelta(days=MONOTONY_DAYS - 1)
    daily = daily_loads(activities, min(acute_start, chronic_start, week_start), end, family)
    acute = [daily[d] for d in day_range(acute_start, end)]
    chronic = [daily[d] for d in day_range(chronic_start, end)]
    week = [daily[d] for d in day_range(week_start, end)]

    acute_load, chronic_load, week_load = sum(acute), sum(chronic), sum(week)
    acute_mean = acute_load / acute_days
    chronic_mean = chronic_load / chronic_days
    acwr = acute_mean / chronic_mean if chronic_mean > 0 else None
    chronic_week = chronic_mean * 7
    week_monotony = monotony(week)
    strain = week_load * week_monotony if week_monotony is not None else None
    week_vs_chronic = week_load / chronic_week if chronic_week > 0 else None
    chronic_active = sum(1 for v in chronic if v > 0)
    week_active = sum(1 for v in week if v > 0)
    acute_acts = between(activities, acute_start, end, family)
    chronic_acts = between(activities, chronic_start, end, family)
    return {
        "acute": {
            "start": acute_start.isoformat(), "end": end.isoformat(), "days": acute_days,
            "load": rnd(acute_load, 0), "daily_mean": rnd(acute_mean, 1),
            "days_with_load": sum(1 for v in acute if v > 0), "zero_load_days": sum(1 for v in acute if v == 0),
            **_session_counts(acute_acts),
        },
        "chronic": {
            "start": chronic_start.isoformat(), "end": end.isoformat(), "days": chronic_days,
            "load": rnd(chronic_load, 0), "daily_mean": rnd(chronic_mean, 1), "weekly_mean": rnd(chronic_week, 0),
            "days_with_load": chronic_active, "zero_load_days": sum(1 for v in chronic if v == 0),
            **_session_counts(chronic_acts),
        },
        "acwr": rnd(acwr, 2),
        "acwr_position": _position(rnd(acwr, 2), *REFERENCES["acwr"]["range"]),
        "acwr_small_sample": chronic_active < MIN_CHRONIC_ACTIVE_DAYS,
        "week": {
            "start": week_start.isoformat(), "end": end.isoformat(), "load": rnd(week_load, 0),
            "days_with_load": week_active,
            "monotony": rnd(week_monotony, 2), "monotony_note": _monotony_note(week),
            "strain": rnd(strain, 0),
            "vs_chronic_weekly_mean_pct": rnd(week_vs_chronic * 100, 0) if week_vs_chronic is not None else None,
            "deload_like": week_vs_chronic <= DELOAD_RATIO if week_vs_chronic is not None else None,
        },
    }


def sport_breakdown(
    activities: list[Activity], end: date, acute_days: int = 7, chronic_days: int = 28
) -> dict[str, Any]:
    """Load metrics per sport family plus the primary sport (highest 7-day load).

    A family's monotony is only kept with at least ``MIN_ACTIVE_DAYS_MONOTONY`` days with
    load in the last 7 days. With several families, a steady low load from cross-training
    raises the total monotony (higher mean, same SD), so the primary sport's own value is
    reported next to it (approach of #150).
    """
    chronic_start = end - timedelta(days=max(chronic_days, acute_days, MONOTONY_DAYS) - 1)
    families = sorted({sport_family(a.get("type")) for a in between(activities, chronic_start, end)})
    by_family: dict[str, Any] = {}
    for family in families:
        metrics = load_metrics(activities, end, acute_days, chronic_days, family)
        week = metrics["week"]
        if week["days_with_load"] < MIN_ACTIVE_DAYS_MONOTONY and week["monotony"] is not None:
            week["monotony"] = None
            week["strain"] = None
            week["monotony_note"] = f"fewer than {MIN_ACTIVE_DAYS_MONOTONY} days with load"
        by_family[family] = metrics
    ordered = dict(sorted(by_family.items(), key=lambda item: -(item[1]["chronic"]["load"] or 0)))
    with_week_load = [f for f, m in ordered.items() if (m["week"]["load"] or 0) > 0]
    primary = max(with_week_load, key=lambda f: ordered[f]["week"]["load"] or 0) if with_week_load else None
    return {
        "by_sport": ordered,
        "primary_sport": primary,
        "primary_monotony": ordered[primary]["week"]["monotony"] if primary else None,
        "multi_sport": len(with_week_load) > 1,
    }


def weekly_rows(  # pylint: disable=too-many-locals
    activities: list[Activity], end: date, weeks: int, chronic_days: int = 28
) -> list[dict[str, Any]]:
    """ISO weeks (Monday-Sunday) up to ``end``, oldest first; the last week may be partial.

    Per week: load, sessions, hours, days with activity, rest days (days without any
    activity), load per sport family, monotony (at least 5 days and 3 days with load),
    strain (complete weeks only) and, for complete weeks, the load as a percentage of the
    weekly mean of the ``chronic_days`` ending on the week's Sunday (coupled, like the ACWR).
    """
    last_monday = end - timedelta(days=end.weekday())
    first_monday = last_monday - timedelta(days=7 * (weeks - 1))
    daily = daily_loads(activities, first_monday - timedelta(days=chronic_days), end)
    rows: list[dict[str, Any]] = []
    for index in range(weeks):
        monday = first_monday + timedelta(days=7 * index)
        week_end = min(monday + timedelta(days=6), end)
        days = day_range(monday, week_end)
        loads = [daily[d] for d in days]
        acts = between(activities, monday, week_end)
        active_days = {activity_day(a) for a in acts}
        families: dict[str, list[float]] = defaultdict(list)
        for activity in acts:
            load = activity_load(activity)
            families[sport_family(activity.get("type"))] += [] if load is None else [load]
        complete = len(days) == 7
        week_monotony = (
            monotony(loads)
            if len(days) >= MIN_WEEK_DAYS_MONOTONY and sum(1 for v in loads if v > 0) >= MIN_ACTIVE_DAYS_MONOTONY
            else None
        )
        row: dict[str, Any] = {
            "week": iso_week(monday), "start": monday.isoformat(), "end": week_end.isoformat(),
            "days": len(days), "complete": complete,
            "load": rnd(sum(loads), 0), "sessions": len(acts),
            "sessions_without_load": sum(1 for a in acts if activity_load(a) is None),
            "hours": rnd(sum(num(a.get("moving_time")) or 0.0 for a in acts) / 3600, 1),
            "days_with_activity": len(active_days), "rest_days": len(days) - len(active_days),
            # A sport whose sessions all lack a load gets None, not 0.
            "by_sport": {
                f: rnd(sum(v), 0) if v else None for f, v in sorted(families.items(), key=lambda kv: -sum(kv[1]))
            },
            "monotony": rnd(week_monotony, 2),
            "strain": rnd(sum(loads) * week_monotony, 0) if complete and week_monotony is not None else None,
            "vs_chronic_weekly_mean_pct": None, "deload_like": None,
        }
        if complete:
            trailing = [daily[d] for d in day_range(week_end - timedelta(days=chronic_days - 1), week_end)]
            weekly_mean = sum(trailing) / chronic_days * 7
            if weekly_mean > 0:
                ratio = sum(loads) / weekly_mean
                row["vs_chronic_weekly_mean_pct"] = rnd(ratio * 100, 0)
                row["deload_like"] = ratio <= DELOAD_RATIO
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Fitness (CTL / ATL / form) and the exponential model
# ---------------------------------------------------------------------------


def wellness_by_day(entries: Any) -> dict[date, dict[str, Any]]:
    """Index wellness records (a list or a date-keyed dict) by day."""
    items: list[tuple[Any, Any]] = []
    if isinstance(entries, dict):
        items = list(entries.items())
    elif isinstance(entries, list):
        items = [(e.get("id") if isinstance(e, dict) else None, e) for e in entries]
    indexed: dict[date, dict[str, Any]] = {}
    for key, record in items:
        if isinstance(record, dict):
            day = parse_day(record.get("id")) or parse_day(key)
            if day is not None:
                indexed[day] = record
    return indexed


def model_step(value: float, load: float, days: float) -> float:
    """One day of the exponentially weighted average used for CTL (42 d) and ATL (7 d)."""
    return value + (load - value) * (1 - math.exp(-1 / days))


def fitness_status(  # pylint: disable=too-many-locals
    wellness: dict[date, dict[str, Any]],
    end: date,
    completed_load: float | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """CTL, ATL, form and ramp at ``end`` from the Intervals.icu wellness records.

    Uses the last day up to 7 days before ``end`` that has both CTL and ATL (``date``).
    If that day is today and its ``ctlLoad`` exceeds the load completed today
    (``completed_load``), Intervals.icu has already counted planned workouts that are not
    done yet; CTL and ATL are then recomputed from the previous day with the completed load
    only (``source`` = recomputed_without_planned, approach of #150).
    """
    record_day = next(
        (
            day for day in (end - timedelta(days=offset) for offset in range(8))
            if num(wellness.get(day, {}).get("ctl")) is not None and num(wellness.get(day, {}).get("atl")) is not None
        ),
        None,
    )
    if record_day is None:
        return {"available": False, "note": "no CTL/ATL in the wellness records of the 7 days up to the end date"}
    record = wellness[record_day]
    ctl, atl = num(record.get("ctl")), num(record.get("atl"))
    ramp = num(record.get("rampRate"))
    source = "intervals.icu"
    note = None
    if today is not None and record_day == today == end and completed_load is not None:
        counted = num(record.get("ctlLoad"))
        previous = wellness.get(end - timedelta(days=1), {})
        prev_ctl, prev_atl = num(previous.get("ctl")), num(previous.get("atl"))
        if counted is not None and counted > completed_load + 1 and prev_ctl is not None and prev_atl is not None:
            ctl = model_step(prev_ctl, completed_load, CTL_DAYS)
            atl = model_step(prev_atl, completed_load, ATL_DAYS)
            week_ago = num(wellness.get(end - timedelta(days=7), {}).get("ctl"))
            ramp = ctl - week_ago if week_ago is not None else None
            source = "recomputed_without_planned"
            note = (
                f"Intervals.icu counted a load of {counted:.0f} for today, {completed_load:.0f} is completed: "
                "values recomputed without the planned rest of today"
            )
    if ramp is None and ctl is not None:
        week_ago = num(wellness.get(record_day - timedelta(days=7), {}).get("ctl"))
        ramp = ctl - week_ago if week_ago is not None else None
    if ctl is None or atl is None:  # pragma: no cover - guarded by the search above
        return {"available": False, "note": "no CTL/ATL"}
    return {
        "available": True, "date": record_day.isoformat(), "ctl": rnd(ctl, 1), "atl": rnd(atl, 1),
        "form": rnd(ctl - atl, 1), "form_pct_of_ctl": rnd((ctl - atl) / ctl * 100, 0) if ctl else None,
        "ramp": rnd(ramp, 1), "source": source, "note": note,
    }


def model_check(  # pylint: disable=too-many-locals
    wellness: dict[date, dict[str, Any]], end: date, days: int = 14,
    ctl_days: float = CTL_DAYS, atl_days: float = ATL_DAYS,
) -> dict[str, Any] | None:
    """Replay the last ``days`` days of Intervals.icu loads (``ctlLoad``/``atlLoad``) through the model.

    Returns the largest absolute CTL and ATL difference to the stored values, or None when
    the records needed are missing. A small difference shows that the time constants match
    the athlete's Intervals.icu fitness settings.
    """
    start = end - timedelta(days=days)
    base = wellness.get(start, {})
    ctl, atl = num(base.get("ctl")), num(base.get("atl"))
    if ctl is None or atl is None:
        return None
    max_ctl = max_atl = 0.0
    for day in day_range(start + timedelta(days=1), end):
        record = wellness.get(day, {})
        ctl_load, atl_load = num(record.get("ctlLoad")), num(record.get("atlLoad"))
        stored_ctl, stored_atl = num(record.get("ctl")), num(record.get("atl"))
        if ctl_load is None or stored_ctl is None or stored_atl is None:
            return None
        ctl = model_step(ctl, ctl_load, ctl_days)
        atl = model_step(atl, atl_load if atl_load is not None else ctl_load, atl_days)
        max_ctl = max(max_ctl, abs(ctl - stored_ctl))
        max_atl = max(max_atl, abs(atl - stored_atl))
    return {"days": days, "max_ctl_diff": rnd(max_ctl, 2), "max_atl_diff": rnd(max_atl, 2)}


def project_fitness(  # pylint: disable=too-many-arguments
    start_ctl: float, start_atl: float, start_day: date, loads: dict[date, float], end: date,
    ctl_history: dict[date, float] | None = None, ctl_days: float = CTL_DAYS, atl_days: float = ATL_DAYS,
) -> list[dict[str, Any]]:
    """Daily CTL/ATL/form from the day after ``start_day`` to ``end`` with the given daily loads.

    ``start_ctl``/``start_atl`` are the values at the end of ``start_day``; days without an
    entry in ``loads`` have load 0. ``ctl_history`` (day -> CTL) lets the ramp of the first
    days reach back before the projection.
    """
    history = dict(ctl_history or {})
    history[start_day] = start_ctl
    ctl, atl = start_ctl, start_atl
    rows = []
    for day in day_range(start_day + timedelta(days=1), end):
        load = loads.get(day, 0.0)
        ctl = model_step(ctl, load, ctl_days)
        atl = model_step(atl, load, atl_days)
        history[day] = ctl
        week_ago = history.get(day - timedelta(days=7))
        rows.append({
            "date": day.isoformat(), "load": rnd(load, 0), "ctl": rnd(ctl, 1), "atl": rnd(atl, 1),
            "form": rnd(ctl - atl, 1), "ramp": rnd(ctl - week_ago, 1) if week_ago is not None else None,
        })
    return rows


# ---------------------------------------------------------------------------
# Notable sessions
# ---------------------------------------------------------------------------


def intensity_factor(activity: Activity) -> float | None:
    """Intensity factor as a fraction; Intervals.icu always stores ``icu_intensity`` in percent.

    A small value (e.g. 2.6 % for a ride with a mostly-zero power meter) stays small and is
    never read as a fraction (IF 2.6).
    """
    value = num(activity.get("icu_intensity"))
    if value is None or value <= 0:
        return None
    return value / 100


def top_sessions(activities: list[Activity], count: int = TOP_SESSIONS) -> list[dict[str, Any]]:
    """The ``count`` sessions with the highest load (ties: higher IF, then newer).

    If the session with the highest IF is not among them it replaces the last entry, so a
    short hard session is not hidden behind long easy ones (approach of #150).
    """
    if count <= 0 or not activities:
        return []

    def key(activity: Activity) -> tuple[float, float, int]:
        day = activity_day(activity)
        return (-(activity_load(activity) or 0.0), -(intensity_factor(activity) or 0.0), -(day.toordinal() if day else 0))

    top = sorted(activities, key=key)[:count]
    with_if = [a for a in activities if intensity_factor(a) is not None]
    if with_if and len(top) == count:
        hardest = max(with_if, key=lambda a: (intensity_factor(a) or 0.0, activity_load(a) or 0.0))
        if not any(entry is hardest for entry in top):
            top[-1] = hardest
    return [
        {
            "date": (activity_day(a) or date.min).isoformat(), "id": a.get("id"), "name": a.get("name"),
            "type": a.get("type"), "minutes": rnd((num(a.get("moving_time")) or 0.0) / 60, 0),
            "load": rnd(activity_load(a), 0), "intensity_factor": rnd(intensity_factor(a), 2),
        }
        for a in top
    ]
