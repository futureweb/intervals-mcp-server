"""
What-if training plan simulation (pure functions, no API access).

Sessions that are not in the calendar (single sessions with a load, or with a duration and an
intensity factor from which the load is estimated, and weekly templates) are added to or
replace the planned workouts and run through the exponential fitness model of
``load_metrics`` (CTL 42 d / ATL 7 d unless the caller changes them). Also:

- CTL/ATL/form at the start of a target day (the end of the day before, so the target day's
  own load, e.g. the race, does not count);
- a one-dimensional grid search for the load in the last days before the target that puts
  that form into a caller-given range: as a percentage of the load planned in those days, or
  as a constant weekly load spread evenly over them;
- per-week plan statistics (CTL ramp, Foster monotony, longest session, rest days) next to
  commonly cited ranges with their sources.

Everything is arithmetic on the inputs: no verdicts, nothing is written anywhere.
"""

import json
import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from intervals_mcp_server.utils.load_metrics import (
    MIN_ACTIVE_DAYS_MONOTONY,
    MIN_WEEK_DAYS_MONOTONY,
    REFERENCES,
    activity_day,
    activity_load,
    day_range,
    iso_week,
    monotony,
    num,
    rnd,
)
from intervals_mcp_server.utils.sports import sport_family

Session = dict[str, Any]

LOAD_FORMULA = "load = hours x IF^2 x 100"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Default session days (Monday = 0) for a template with n sessions per week.
DEFAULT_DAYS: dict[int, tuple[int, ...]] = {
    1: (5,), 2: (2, 5), 3: (1, 3, 5), 4: (1, 3, 5, 6), 5: (1, 2, 3, 5, 6), 6: (1, 2, 3, 4, 5, 6), 7: (0, 1, 2, 3, 4, 5, 6),
}
CALENDAR_MODES = ("add", "replace", "none")
SCENARIO_KEYS = frozenset({"calendar", "sessions", "weekly"})
SESSION_KEYS = frozenset({"date", "load", "duration_min", "intensity_factor", "sport", "name"})
TEMPLATE_KEYS = frozenset({
    "start", "weeks", "load", "hours", "intensity_factor", "sessions", "days", "long_session_share", "long_day", "sport", "name",
})
MAX_SCENARIO_SESSIONS = 500
MAX_SESSION_LOAD = 1500.0
MAX_WEEK_LOAD = 5000.0
MAX_TEMPLATE_WEEKS = 26
# Grid of the target search: percent of the planned load and constant weekly load.
MAX_PCT = 200
WEEKLY_STEP = 5
MIN_WEEKLY_MAX = 1000

PLAN_REFERENCES: dict[str, dict[str, Any]] = {
    "ramp": {
        "range": [5.0, 8.0],
        "text": "a CTL increase of about 5-8 points per week is commonly cited for build weeks; only weeks above it "
        "are listed, because recovery, taper and transition weeks lie below it by design",
        "source": "Friel J. 2015, 'Why Ramp Rate Is an Important Training Metric', TrainingPeaks "
        "(\"an increase in CTL of about 5 to 8 points per week is about right for most\")",
    },
    "monotony": REFERENCES["monotony"],
    "rest_days": {
        "min": 1,
        "text": "at least one passive rest day per week is generally recommended (rest day here: no session at all)",
        "source": "Meeusen et al. 2013, Med Sci Sports Exerc 45:186-205 (ECSS/ACSM consensus statement on overtraining)",
    },
    "longest_session": {
        "text": "no commonly cited range; reported as a statistic (and as a share of the target event's planned "
        "duration when the calendar event has one)",
        "source": None,
    },
}


class ScenarioError(ValueError):
    """Invalid scenario, target or range input (the message is shown to the caller)."""


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def estimate_load(minutes: float, intensity_factor: float) -> float:
    """Load estimate from a duration and an intensity factor: hours x IF^2 x 100.

    This is the definition of TSS for a session with intensity factor IF (NP / FTP); for
    a planned IF, or HR/pace-based sports, the result is an estimate.
    """
    return minutes / 60 * intensity_factor**2 * 100


def next_monday(day: date) -> date:
    """The first Monday after ``day``."""
    return day + timedelta(days=7 - day.weekday())


def _number(value: Any, what: str, low: float, high: float) -> float:
    number = num(value)
    if number is None or not low <= number <= high:
        raise ScenarioError(f"{what} must be a number from {low:g} to {high:g}.")
    return number


def _intensity(value: Any, what: str) -> float:
    number = num(value)
    if number is not None and number > 3:
        number /= 100  # given in percent
    if number is None or not 0.3 <= number <= 1.5:
        raise ScenarioError(f"{what} must be an intensity factor from 0.3 to 1.5 (or 30 to 150 %).")
    return number


def _day(value: Any, what: str, first: date, last: date) -> date:
    try:
        day = date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        day = None
    if day is None:
        raise ScenarioError(f"{what} must be a date YYYY-MM-DD.")
    if not first <= day <= last:
        raise ScenarioError(f"{what} {day.isoformat()} must lie from {first.isoformat()} to {last.isoformat()}.")
    return day


def _text(value: Any) -> str | None:
    return (value.strip()[:80] or None) if isinstance(value, str) else None


def _check_keys(entry: Any, allowed: frozenset[str], where: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ScenarioError(f"{where} must be an object.")
    unknown = set(entry) - allowed
    if unknown:
        raise ScenarioError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(allowed))}.")
    return entry


def _parse_session(entry: Any, where: str, first: date, last: date) -> Session:
    entry = _check_keys(entry, SESSION_KEYS, where)
    day = _day(entry.get("date"), f"{where}.date", first, last)
    minutes = _number(entry["duration_min"], f"{where}.duration_min", 1, 1440) if entry.get("duration_min") is not None else None
    intensity = _intensity(entry["intensity_factor"], f"{where}.intensity_factor") if entry.get("intensity_factor") is not None else None
    if entry.get("load") is not None:
        load, estimated = _number(entry["load"], f"{where}.load", 0, MAX_SESSION_LOAD), False
    elif minutes is not None and intensity is not None:
        load, estimated = estimate_load(minutes, intensity), True
    else:
        raise ScenarioError(f"{where}: give 'load', or 'duration_min' and 'intensity_factor' to estimate it ({LOAD_FORMULA}).")
    return {
        "date": day, "sport": _text(entry.get("sport")), "load": load, "minutes": minutes, "intensity_factor": intensity,
        "estimated": estimated, "source": "proposed", "name": _text(entry.get("name")),
    }


def _per_week(value: Any, what: str, weeks: int, high: float) -> list[float] | None:
    if value is None:
        return None
    values = value if isinstance(value, list) else [value] * weeks
    if len(values) != weeks:
        raise ScenarioError(f"{what} lists one value per week ({weeks} weeks).")
    return [_number(v, what, 0, high) for v in values]


def _weekdays(value: Any, what: str) -> list[int]:
    names = [w.lower() for w in WEEKDAYS]
    if not isinstance(value, list) or not value:
        raise ScenarioError(f"{what} must be a list of weekdays such as [\"Tue\", \"Thu\", \"Sat\"].")
    days = []
    for item in value:
        key = item.strip()[:3].lower() if isinstance(item, str) else ""
        if key not in names:
            raise ScenarioError(f"{what}: unknown weekday {item!r} (use Mon, Tue, Wed, Thu, Fri, Sat, Sun).")
        days.append(names.index(key))
    if len(set(days)) != len(days):
        raise ScenarioError(f"{what} lists a weekday twice (one session per day; use 'sessions' entries for doubles).")
    return sorted(days)


def _template_weeks(entry: dict[str, Any], where: str) -> int:
    lengths = {len(entry[k]) for k in ("load", "hours") if isinstance(entry.get(k), list)}
    if len(lengths) > 1:
        raise ScenarioError(f"{where}: 'load' and 'hours' lists must have the same length.")
    if entry.get("weeks") is not None:
        weeks = int(_number(entry["weeks"], f"{where}.weeks", 1, MAX_TEMPLATE_WEEKS))
        if lengths and lengths != {weeks}:
            raise ScenarioError(f"{where}: 'weeks' differs from the length of the per-week list.")
        return weeks
    weeks = lengths.pop() if lengths else 1
    if not 1 <= weeks <= MAX_TEMPLATE_WEEKS:
        raise ScenarioError(f"{where}: 1 to {MAX_TEMPLATE_WEEKS} weeks.")
    return weeks


def _template_days(entry: dict[str, Any], where: str) -> tuple[list[int], int]:
    days = _weekdays(entry["days"], f"{where}.days") if entry.get("days") is not None else None
    count = int(_number(entry["sessions"], f"{where}.sessions", 1, 7)) if entry.get("sessions") is not None else None
    if days is not None and count is not None and count != len(days):
        raise ScenarioError(f"{where}: 'sessions' ({count}) differs from the number of 'days' ({len(days)}).")
    if days is None:
        days = list(DEFAULT_DAYS[count or 5])
    long_day = 5 if 5 in days else 6 if 6 in days else days[-1]
    if entry.get("long_day") is not None:
        long_day = _weekdays([entry["long_day"]], f"{where}.long_day")[0]
        if long_day not in days:
            raise ScenarioError(f"{where}.long_day must be one of the session days.")
    return days, long_day


def _parse_template(  # pylint: disable=too-many-locals
    entry: Any, where: str, today: date, last: date
) -> tuple[list[Session], dict[str, Any]]:
    entry = _check_keys(entry, TEMPLATE_KEYS, where)
    start = _day(entry["start"], f"{where}.start", today, last) if entry.get("start") is not None else next_monday(today)
    weeks = _template_weeks(entry, where)
    end = start + timedelta(days=7 * weeks - 1)
    if end > last:
        raise ScenarioError(f"{where} ends {end.isoformat()}, after the last possible day {last.isoformat()}.")
    loads = _per_week(entry.get("load"), f"{where}.load", weeks, MAX_WEEK_LOAD)
    hours = _per_week(entry.get("hours"), f"{where}.hours", weeks, 100)
    intensity = _intensity(entry["intensity_factor"], f"{where}.intensity_factor") if entry.get("intensity_factor") is not None else None
    estimated = loads is None
    if loads is None:
        if hours is None or intensity is None:
            raise ScenarioError(f"{where}: give the weekly 'load', or 'hours' and 'intensity_factor' to estimate it ({LOAD_FORMULA}).")
        loads = [estimate_load(h * 60, intensity) for h in hours]
    days, long_day = _template_days(entry, where)
    count = len(days)
    share = None
    if entry.get("long_session_share") is not None and count > 1:
        share = _number(entry["long_session_share"], f"{where}.long_session_share", 1 / count, 0.95)
    sport = _text(entry.get("sport")) or "Ride"
    sessions: list[Session] = []
    for week in range(weeks):
        for day in day_range(start + timedelta(days=7 * week), start + timedelta(days=7 * week + 6)):
            if day.weekday() not in days:
                continue
            part = (share if day.weekday() == long_day else (1 - share) / (count - 1)) if share is not None else 1 / count
            sessions.append({
                "date": day, "sport": sport, "load": loads[week] * part,
                "minutes": hours[week] * 60 * part if hours is not None else None, "intensity_factor": intensity,
                "estimated": estimated, "source": "template", "name": _text(entry.get("name")),
            })
    summary = {
        "name": _text(entry.get("name")), "start": start.isoformat(), "end": end.isoformat(), "weeks": weeks, "sport": sport,
        "sessions_per_week": count, "days": [WEEKDAYS[d] for d in days],
        "long_day": WEEKDAYS[long_day] if share is not None else None, "long_session_share": share,
        "weekly_load": [rnd(v, 0) for v in loads], "weekly_hours": [rnd(v, 1) for v in hours] if hours is not None else None,
        "load_estimated": estimated, "intensity_factor": intensity,
    }
    return sessions, summary


def parse_scenario(raw: Any, today: date, last: date) -> dict[str, Any]:
    """Validate a scenario (object, JSON text or a plain list of sessions) into sessions and a summary.

    Dates must lie from ``today`` to ``last``. Returns ``calendar`` (add/replace/none),
    ``sessions`` (proposed and template sessions sorted by date), ``templates`` (summaries)
    and ``span`` (first and last day covered; template weeks count completely).
    """
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ScenarioError(f"scenario is not valid JSON: {exc}") from exc
    if isinstance(data, list):
        data = {"sessions": data}
    data = _check_keys(data, SCENARIO_KEYS, "scenario")
    calendar = str(data.get("calendar") or "add").strip().lower()
    if calendar not in CALENDAR_MODES:
        raise ScenarioError(f"scenario.calendar must be one of {', '.join(CALENDAR_MODES)}.")
    sessions_raw = data.get("sessions") or []
    weekly_raw = data.get("weekly") or []
    if isinstance(weekly_raw, dict):
        weekly_raw = [weekly_raw]
    if not isinstance(sessions_raw, list) or not isinstance(weekly_raw, list):
        raise ScenarioError("scenario.sessions and scenario.weekly must be lists.")
    if not sessions_raw and not weekly_raw:
        raise ScenarioError("scenario needs 'sessions' and/or 'weekly'.")
    if len(sessions_raw) > MAX_SCENARIO_SESSIONS:
        raise ScenarioError(f"scenario: at most {MAX_SCENARIO_SESSIONS} sessions.")
    sessions = [_parse_session(entry, f"sessions[{i}]", today, last) for i, entry in enumerate(sessions_raw)]
    templates = []
    bounds = [s["date"] for s in sessions]
    for index, entry in enumerate(weekly_raw):
        template_sessions, summary = _parse_template(entry, f"weekly[{index}]", today, last)
        sessions += template_sessions
        templates.append(summary)
        bounds += [date.fromisoformat(summary["start"]), date.fromisoformat(summary["end"])]
    if len(sessions) > MAX_SCENARIO_SESSIONS:
        raise ScenarioError(f"scenario: at most {MAX_SCENARIO_SESSIONS} sessions (templates included).")
    return {
        "calendar": calendar, "sessions": sorted(sessions, key=lambda s: s["date"]), "templates": templates,
        "span": (min(bounds), max(bounds)),
    }


def parse_form_range(value: Any) -> tuple[float, float, str]:
    """A target form range: "5,15" / "5..15" (CTL-ATL points), "5%,20%" (% of CTL) or [5, 15]."""
    parts: list[str]
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    elif isinstance(value, str):
        text = value.replace("..", ",").replace(" to ", ",").replace(";", ",")
        parts = [p for p in (p.strip() for p in text.split(",")) if p]
    else:
        parts = []
    if len(parts) != 2:
        raise ScenarioError("target_form must be a range such as '5,15' (form points) or '5%,20%' (percent of CTL).")
    units = {p.endswith("%") for p in parts}
    if len(units) != 1:
        raise ScenarioError("target_form: give both ends in points or both in percent.")
    unit = "pct" if units.pop() else "points"
    try:
        low, high = (float(p.rstrip("%").strip()) for p in parts)
    except ValueError as exc:
        raise ScenarioError("target_form: both ends must be numbers.") from exc
    limit = 100.0 if unit == "pct" else 200.0
    if not (math.isfinite(low) and math.isfinite(high)) or low > high or min(low, high) < -limit or max(low, high) > limit:
        raise ScenarioError(f"target_form: low <= high, each from {-limit:g} to {limit:g}.")
    return low, high, unit


# ---------------------------------------------------------------------------
# Sessions from the calendar, the completed activities and the scenario
# ---------------------------------------------------------------------------


def calendar_sessions(workouts: dict[date, list[dict[str, Any]]]) -> list[Session]:
    """Sessions from unpaired WORKOUT events (planned load may be missing)."""
    sessions = []
    for day, events in sorted(workouts.items()):
        for event in events:
            secs = num(event.get("moving_time"))
            sessions.append({
                "date": day, "sport": event.get("type"), "load": activity_load(event),
                "minutes": secs / 60 if secs else None, "intensity_factor": None, "estimated": False,
                "source": "calendar", "name": event.get("name"),
            })
    return sessions


def completed_sessions(activities: list[dict[str, Any]], day: date) -> list[Session]:
    """Sessions from the activities completed on ``day``."""
    sessions = []
    for activity in activities:
        if activity_day(activity) != day:
            continue
        secs = num(activity.get("moving_time"))
        sessions.append({
            "date": day, "sport": activity.get("type"), "load": activity_load(activity),
            "minutes": secs / 60 if secs else None, "intensity_factor": None, "estimated": False,
            "source": "completed", "name": activity.get("name"),
        })
    return sessions


def apply_calendar(calendar: list[Session], scenario: dict[str, Any]) -> tuple[list[Session], list[Session]]:
    """(kept, dropped) calendar sessions for the scenario's calendar mode."""
    mode = scenario["calendar"]
    if mode == "add":
        return list(calendar), []
    if mode == "none":
        return [], list(calendar)
    first, last = scenario["span"]
    kept = [s for s in calendar if not first <= s["date"] <= last]
    return kept, [s for s in calendar if first <= s["date"] <= last]


def add_session_loads(history: dict[date, float], sessions: list[Session]) -> dict[date, float]:
    """Daily loads: the history plus every session's load (sessions without load add 0)."""
    loads = dict(history)
    for session in sessions:
        loads[session["date"]] = loads.get(session["date"], 0.0) + (session["load"] or 0.0)
    return loads


def session_view(session: Session) -> dict[str, Any]:
    """A session for the output."""
    return {
        "date": session["date"].isoformat(), "source": session["source"], "sport": session["sport"],
        "name": session["name"], "load": rnd(session["load"], 0), "minutes": rnd(session["minutes"], 0),
        "intensity_factor": rnd(session["intensity_factor"], 2), "estimated": session["estimated"],
    }


# ---------------------------------------------------------------------------
# Weeks and plan checks
# ---------------------------------------------------------------------------


def _outside(week: dict[str, Any]) -> list[dict[str, Any]]:
    outside = []
    low, high = PLAN_REFERENCES["ramp"]["range"]
    ramp = week["ramp"]
    if ramp is not None and ramp > high:
        outside.append({"metric": "ramp", "value": ramp, "position": "above", "range": [low, high]})
    if week["monotony"] is not None and week["monotony"] > PLAN_REFERENCES["monotony"]["high"]:
        outside.append({"metric": "monotony", "value": week["monotony"], "position": "above", "range": [None, 2.0]})
    if week["complete"] and week["rest_days"] < PLAN_REFERENCES["rest_days"]["min"]:
        outside.append({"metric": "rest_days", "value": week["rest_days"], "position": "below", "range": [1, None]})
    return outside


def _longest(sessions: list[Session], race_minutes: float | None) -> dict[str, Any] | None:
    timed = [s for s in sessions if s["minutes"]]
    if not timed:
        return None
    longest = max(timed, key=lambda s: (s["minutes"], s["load"] or 0.0))
    return {
        "date": longest["date"].isoformat(), "minutes": rnd(longest["minutes"], 0), "sport": longest["sport"],
        "load": rnd(longest["load"], 0), "source": longest["source"],
        "pct_of_target_event": rnd(longest["minutes"] / race_minutes * 100, 0) if race_minutes else None,
    }


def plan_weeks(  # pylint: disable=too-many-locals
    rows: list[dict[str, Any]], loads: dict[date, float], sessions: list[Session], race_minutes: float | None = None
) -> list[dict[str, Any]]:
    """ISO weeks of projected days: model values at the week's end and plan statistics.

    ``rows`` are ``project_fitness`` rows (one per day), ``loads`` the daily loads behind
    them and ``sessions`` the sessions on those days. Rest days are days without any
    session (a session without load is not a rest day). Monotony needs at least 5 days and
    3 days with load, the rest-day check a complete week. Weeks outside a commonly cited
    range (ramp above 5-8, monotony above 2.0, no rest day) are listed in
    ``outside_commonly_cited_range`` (statistics, no verdict).
    """
    by_day: dict[date, list[Session]] = defaultdict(list)
    for session in sessions:
        by_day[session["date"]].append(session)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(iso_week(date.fromisoformat(row["date"])), []).append(row)
    out = []
    for label, week_rows in grouped.items():
        days = [date.fromisoformat(r["date"]) for r in week_rows]
        daily = [loads.get(d, 0.0) for d in days]
        week_sessions = [s for d in days for s in by_day.get(d, [])]
        sports: dict[str, list[float]] = defaultdict(list)
        for session in week_sessions:
            sports[sport_family(session["sport"])] += [] if session["load"] is None else [session["load"]]
        minutes = [s["minutes"] for s in week_sessions if s["minutes"]]
        last, lowest = week_rows[-1], min(week_rows, key=lambda r: r["form"])
        week_monotony = (
            monotony(daily) if len(days) >= MIN_WEEK_DAYS_MONOTONY and sum(1 for v in daily if v > 0) >= MIN_ACTIVE_DAYS_MONOTONY else None
        )
        week = {
            "week": label, "start": days[0].isoformat(), "end": days[-1].isoformat(), "days": len(days),
            "complete": len(days) == 7, "load": rnd(sum(daily), 0),
            "ctl": last["ctl"], "atl": last["atl"], "form": last["form"], "ramp": last["ramp"],
            "lowest_form": lowest["form"], "lowest_form_date": lowest["date"],
            "sessions": len(week_sessions), "sessions_without_load": sum(1 for s in week_sessions if s["load"] is None),
            "estimated_sessions": sum(1 for s in week_sessions if s["estimated"]),
            "by_sport": {f: rnd(sum(v), 0) if v else None for f, v in sorted(sports.items(), key=lambda kv: -sum(kv[1]))},
            "hours": rnd(sum(minutes) / 60, 1), "sessions_without_duration": len(week_sessions) - len(minutes),
            "longest_session": _longest(week_sessions, race_minutes),
            "rest_days": sum(1 for d in days if not by_day.get(d)),
            "monotony": rnd(week_monotony, 2),
        }
        week["outside_commonly_cited_range"] = _outside(week)
        out.append(week)
    return out


def flagged(weeks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per week and metric outside its commonly cited range."""
    return [{"week": w["week"], **entry} for w in weeks for entry in w["outside_commonly_cited_range"]]


def recent_weeks(activities: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Summary of completed ISO weeks (``weekly_rows`` output) with the longest activity, for comparison."""
    if not rows:
        return None
    start, end = date.fromisoformat(rows[0]["start"]), date.fromisoformat(rows[-1]["end"])
    timed = [a for a in activities if (d := activity_day(a)) is not None and start <= d <= end and num(a.get("moving_time"))]
    longest = max(timed, key=lambda a: num(a.get("moving_time")) or 0.0) if timed else None
    count = len(rows)
    return {
        "weeks": count, "start": start.isoformat(), "end": end.isoformat(),
        "mean_load": rnd(sum(r["load"] or 0 for r in rows) / count, 0), "max_load": max((r["load"] or 0) for r in rows),
        "mean_hours": rnd(sum(r["hours"] or 0 for r in rows) / count, 1),
        "mean_rest_days": rnd(sum(r["rest_days"] for r in rows) / count, 1),
        "longest_session": {
            "date": (activity_day(longest) or start).isoformat(), "minutes": rnd((num(longest.get("moving_time")) or 0) / 60, 0),
            "sport": longest.get("type"), "load": rnd(activity_load(longest), 0),
        } if longest else None,
        "weekly": [{k: r[k] for k in ("week", "load", "hours", "sessions", "rest_days", "monotony")} for r in rows],
    }


# ---------------------------------------------------------------------------
# Target day and the search for the load before it
# ---------------------------------------------------------------------------


def state_at_start(rows_by_date: dict[str, dict[str, Any]], day: date) -> dict[str, Any] | None:
    """CTL/ATL/form at the start of ``day`` (the end of the day before) from projection rows."""
    row = rows_by_date.get((day - timedelta(days=1)).isoformat())
    if row is None:
        return None
    ctl, form = row["ctl"], row["form"]
    return {
        "ctl": ctl, "atl": row["atl"], "form": form,
        "form_pct_of_ctl": rnd(form / ctl * 100, 0) if ctl and form is not None else None,
    }


def _value(ctl: float, atl: float, unit: str) -> float | None:
    if unit == "pct":
        return (ctl - atl) / ctl * 100 if ctl > 0 else None
    return ctl - atl


def _search(points: list[tuple[float, float, float]], low: float, high: float, unit: str) -> dict[str, Any]:
    """Summarise grid points (x, ctl, atl): the x range whose form lies in [low, high]."""
    rows = [(x, ctl, atl, _value(ctl, atl, unit)) for x, ctl, atl in points]
    valid = [r for r in rows if r[3] is not None]

    def view(row: tuple[float, float, float, float | None]) -> dict[str, Any]:
        x, ctl, atl, _ = row
        return {"x": x, "ctl": rnd(ctl, 1), "atl": rnd(atl, 1), "form": rnd(ctl - atl, 1),
                "form_pct_of_ctl": rnd((ctl - atl) / ctl * 100, 0) if ctl > 0 else None}

    inside = [r for r in valid if low - 1e-9 <= (r[3] or 0.0) <= high + 1e-9]
    result: dict[str, Any] = {"reached": bool(inside), "searched": [rows[0][0], rows[-1][0]]}
    if inside:
        result["from"], result["to"] = view(inside[0]), view(inside[-1])
    if valid:
        result["highest_form"] = view(max(valid, key=lambda r: r[3] or 0.0))
        result["lowest_form"] = view(min(valid, key=lambda r: r[3] or 0.0))
    return result


def solve_target(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    start: tuple[float, float], base_day: date, loads: dict[date, float], target: date, first_day: date,
    taper_days: int, form_range: tuple[float, float, str], ctl_days: float, atl_days: float,
) -> dict[str, Any]:
    """Grid search over the load of the ``taper_days`` days before ``target`` (never before ``first_day``).

    Two one-dimensional searches, everything else unchanged: (a) the load planned in those
    days scaled to 0-200 % in 1 % steps; (b) a constant weekly load, spread evenly over those
    days, in steps of 5. The model is linear in the load, so the form at the start of
    ``target`` is monotonic in either search value and the matching values form one interval.
    """
    window_start, window_end = max(target - timedelta(days=taper_days), first_day), target - timedelta(days=1)
    low, high, unit = form_range
    base = {"range": [low, high], "unit": unit, "taper_days": taper_days}
    if window_start > window_end:
        return {**base, "available": False, "note": "no day between tomorrow and the target day to vary"}
    a_ctl, a_atl = 1 - math.exp(-1 / ctl_days), 1 - math.exp(-1 / atl_days)
    ctl, atl = start
    for day in day_range(base_day + timedelta(days=1), window_start - timedelta(days=1)):
        load = loads.get(day, 0.0)
        ctl, atl = ctl + (load - ctl) * a_ctl, atl + (load - atl) * a_atl
    window = day_range(window_start, window_end)
    planned = [loads.get(d, 0.0) for d in window]

    def run(values: list[float]) -> tuple[float, float]:
        c, a = ctl, atl
        for load in values:
            c, a = c + (load - c) * a_ctl, a + (load - a) * a_atl
        return c, a

    planned_sum = sum(planned)
    pct = _search([(p, *run([v * p / 100 for v in planned])) for p in range(0, MAX_PCT + 1)], low, high, unit) if planned_sum > 0 else None
    if pct is not None:
        for key in ("from", "to", "highest_form", "lowest_form"):
            if key in pct:
                pct[key]["window_load"] = rnd(planned_sum * pct[key]["x"] / 100, 0)
    weekly_max = max(MIN_WEEKLY_MAX, int(math.ceil(ctl * 21 / 50.0)) * 50)
    weekly = _search([(w, *run([w / 7] * len(window))) for w in range(0, weekly_max + 1, WEEKLY_STEP)], low, high, unit)
    for key in ("from", "to", "highest_form", "lowest_form"):
        if key in weekly:
            weekly[key]["per_day"] = rnd(weekly[key]["x"] / 7, 0)
    return {
        **base, "available": True,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat(), "days": len(window)},
        "planned_window_load": rnd(planned_sum, 0),
        "state_before_window": {"date": (window_start - timedelta(days=1)).isoformat(), "ctl": rnd(ctl, 1), "atl": rnd(atl, 1),
                                "form": rnd(ctl - atl, 1)},
        "pct_of_planned": pct,
        "constant_weekly": weekly,
    }
