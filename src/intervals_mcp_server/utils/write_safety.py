"""
Write safety for the write tools: dry runs, duplicate checks before creating events, read-back
of what Intervals.icu stored, and the echo of deleted objects.

- ``dry_run_answer``: the exact request a write tool would send (method, path, query parameters,
  body after all defaults and merges) and the validation result, as compact JSON. Nothing is
  sent: the tool returns before its write, and the tool guard refuses every non-GET request of
  a dry run in the API client as well.
- ``check_duplicates``: reads the events of the days events are about to be created on (one GET
  over the date range) and finds an event of the same category and sport with the same name
  (case/whitespace-insensitive) or the same non-trivial content (at least two step lines, or a
  repeat, or at least two lines of text). Within one bulk list only identical repeats count.
- ``stored_summary`` / ``parse_warnings`` / ``verify_write``: what Intervals.icu stored after a
  write (date, name, category, sport, duration, load, the timed steps it parsed from the workout
  text; for notes and other events without a sport: date, name, category, text length) compared
  with what was sent.

The request function is passed in by the tool, so a tool module's (mocked) request function
serves every request of the tool.
"""

import json
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import date, timedelta
from typing import Any

from intervals_mcp_server.api.client import remaining_requests, seg
from intervals_mcp_server.utils.sports import hms
from intervals_mcp_server.utils.workout_validation import DURATION_TOLERANCE_S, step_lines

Request = Callable[..., Awaitable[Any]]

DRY_RUN_MESSAGE = (
    "Dry run: nothing was written. This is the exact request the tool would send; call it again "
    "without dry_run (after the athlete confirmed) to write it."
)
READ_BACK_CHARS = 600
SHORT_WARNING_CHARS = 120
TARGET_KINDS = ("power", "hr", "pace", "cadence")
# Categories with a sport, workout steps, planned duration and load; the others (notes, holidays,
# sick and injured days) are read back with date, name, category and text length only.
SPORT_CATEGORIES = ("WORKOUT", "RACE_A", "RACE_B", "RACE_C")
# Unit spellings that mean the same target: the workout text writes cadence as "rpm", and
# Intervals.icu stores a parsed absolute pace in seconds per distance ("5:35/km Pace" sent as
# MINS_KM is stored as secs/km).
_SAME_UNITS = {
    "cadence": "rpm",
    "mins_km": "secs/km",
    "mins_mile": "secs/mile",
    "secs_100m": "secs/100m",
    "secs_100y": "secs/100y",
    "secs_500m": "secs/500m",
}
_PER_KM_OR_MILE = ("mins_km", "mins_mile")
PACE_TOLERANCE_S = 2.0
# A repeat header: "3x", "3x Main set" or "Main set 3x" (a line that is not a step).
_LEADING_REPEAT = re.compile(r"^(\d+)\s*x\b", re.I)
_TRAILING_REPEAT = re.compile(r"\b(\d+)\s*x$", re.I)


def repeat_count(line: str) -> int | None:
    """The repeat count of a repeat header line ("3x", "3x Main set", "Main set 3x"), else None."""
    stripped = line.strip()
    if not stripped or stripped.startswith("-"):
        return None
    found = _LEADING_REPEAT.match(stripped) or _TRAILING_REPEAT.search(stripped)
    return int(found.group(1)) if found else None


def compact_json(value: Any) -> str:
    """JSON without spaces between items (what a dry run shows)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def dry_run_answer(  # pylint: disable=too-many-arguments
    method: str,
    path: str,
    body: Any = None,
    *,
    params: dict[str, Any] | None = None,
    warnings: Iterable[str] = (),
    extra: dict[str, Any] | None = None,
) -> str:
    """Compact JSON of the request a write tool would send, with the validation result."""
    request: dict[str, Any] = {"method": method, "path": path}
    if params:
        request["params"] = params
    if body is not None:
        request["body"] = body
    payload: dict[str, Any] = {
        "dry_run": True,
        "message": DRY_RUN_MESSAGE,
        "request": request,
        "validation": {"ok": True, "warnings": list(warnings)},
    }
    payload.update(extra or {})
    return compact_json(payload)


# ------------------------------------------------------------------ duplicates
def normalized(text: Any) -> str:
    """Text compared case- and whitespace-insensitively."""
    return " ".join(str(text or "").split()).casefold()


def _content(text: Any) -> tuple[str, str] | None:
    """('steps' | 'text', signature) of non-trivial content, else None.

    Workout text counts with at least two step lines or a repeat (its step and repeat lines are
    compared, so another introduction does not hide the same workout); plain text with at least
    two lines. A single generic line ("- 45m Z2 HR", "Rest day") is not compared.
    """
    raw = str(text or "")
    steps = step_lines(raw)
    lines = raw.splitlines()
    if steps:
        structure = [normalized(line) for line in lines if line.strip().startswith("-") or repeat_count(line) is not None]
        if len(steps) < 2 and not any(repeat_count(line) is not None for line in lines):
            return None
        return "steps", "\n".join(structure)
    plain = [normalized(line) for line in lines if line.strip()]
    return ("text", "\n".join(plain)) if len(plain) >= 2 else None


def workout_signature(text: Any) -> str:
    """Normalised non-trivial content of a workout or note text ('' when trivial or empty)."""
    content = _content(text)
    return content[1] if content else ""


def _same_kind(body: dict[str, Any], other: dict[str, Any]) -> bool:
    """Same category, and the same sport when both have one."""
    if str(other.get("category") or "").upper() != str(body.get("category") or "WORKOUT").upper():
        return False
    mine, theirs = normalized(body.get("type")), normalized(other.get("type"))
    return not (mine and theirs and mine != theirs)


def _same(body: dict[str, Any], other: dict[str, Any]) -> str | None:
    """'name', 'steps' or 'text' when *other* duplicates *body* (same category and sport), else None."""
    if not _same_kind(body, other):
        return None
    name = normalized(body.get("name"))
    if name and normalized(other.get("name")) == name:
        return "name"
    content = _content(body.get("description"))
    if content and _content(other.get("description")) == content:
        return content[0]
    return None


def _identity(body: dict[str, Any]) -> tuple[Any, ...]:
    """Everything that makes two bodies of one list the same event."""
    return (
        str(body.get("category") or "WORKOUT").upper(), normalized(body.get("type")), normalized(body.get("name")),
        normalized(body.get("description")), body.get("moving_time"), body.get("distance"), _day(body),
    )


def _day(body: dict[str, Any]) -> str:
    return str(body.get("start_date_local") or "")[:10]


def _covered_days(event: dict[str, Any]) -> list[str]:
    """Days an event lies on: its start day, for a multi-day event every day up to its end
    (an end at midnight belongs to the day before)."""
    start = str(event.get("start_date_local") or "")[:10]
    end_local = str(event.get("end_date_local") or "")
    try:
        first = date.fromisoformat(start)
    except ValueError:
        return []
    try:
        last = date.fromisoformat(end_local[:10])
    except ValueError:
        return [start]
    if last > first and end_local[11:19] in ("", "00:00:00"):
        last -= timedelta(days=1)
    return [(first + timedelta(days=offset)).isoformat() for offset in range(min(max((last - first).days, 0), 366) + 1)]


async def events_by_day(
    request: Request, athlete_id: str, first: str, last: str
) -> tuple[dict[str, list[dict[str, Any]]] | None, str | None]:
    """The events from *first* to *last* (one GET, any category) grouped by the days they lie on."""
    result = await request(url=f"/athlete/{seg(athlete_id)}/events", params={"oldest": first, "newest": last})
    if isinstance(result, dict) and "error" in result:
        return None, str(result.get("message") or "unknown error")
    if not isinstance(result, list):
        return (None, "unexpected answer from Intervals.icu") if result else ({}, None)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in result:
        if isinstance(event, dict):
            for day in _covered_days(event):
                grouped.setdefault(day, []).append(event)
    return grouped, None


async def check_duplicates(
    request: Request, athlete_id: str, bodies: list[dict[str, Any]], *, reserve: int = 2
) -> tuple[list[dict[str, Any] | None], str | None]:
    """For each event body: None, or what it duplicates (an existing event of its day, or an
    identical earlier body of the same list); plus an error when the check could not run.

    One GET over the date range of the bodies. *reserve* requests of the tool call's budget are
    kept for the write and the read-back; a check that would not fit is not started.
    """
    days = sorted({_day(body) for body in bodies})
    left = remaining_requests()
    if left is not None and left < 1 + reserve:
        return [], "this tool call has no requests left for the duplicate check"
    existing, error = await events_by_day(request, athlete_id, days[0], days[-1])
    if existing is None:
        return [], f"the events of {days[0]}{' to ' + days[-1] if days[-1] != days[0] else ''} could not be read ({error})"
    found: list[dict[str, Any] | None] = []
    earlier: dict[tuple[Any, ...], int] = {}
    for index, body in enumerate(bodies):
        row = _existing_duplicate(body, existing.get(_day(body), []))
        identity = _identity(body)
        if row is None and identity in earlier:
            row = {"date": _day(body), "category": body.get("category"), "type": body.get("type"), "same": "entry",
                   "duplicate_of_index": earlier[identity], "existing_name": body.get("name")}
        earlier.setdefault(identity, index)
        found.append(row)
    return found, None


def _existing_duplicate(body: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first event of the body's day that it duplicates, as a refusal row."""
    for event in events:
        same = _same(body, event)
        if same:
            return {"date": _day(body), "category": event.get("category"), "type": event.get("type"), "same": same,
                    "existing_id": event.get("id"), "existing_name": event.get("name")}
    return None


def second_sessions(bodies: list[dict[str, Any]]) -> dict[int, int]:
    """Index -> earlier index for bodies of one list with the same day, category, sport and name
    but different content: planned double sessions, created both (the answer notes it)."""
    seen: dict[tuple[str, str, str, str], int] = {}
    doubles: dict[int, int] = {}
    for index, body in enumerate(bodies):
        key = (_day(body), str(body.get("category") or "WORKOUT").upper(), normalized(body.get("type")), normalized(body.get("name")))
        if key in seen and _identity(bodies[seen[key]]) != _identity(body):
            doubles[index] = seen[key]
        seen.setdefault(key, index)
    return doubles


def duplicate_refusal(duplicate: dict[str, Any], update_tool: str = "add_or_update_event") -> str:
    """Answer of a create that was refused because the day already has the same event."""
    kind = f"{duplicate['category']} ({duplicate['type']})" if duplicate.get("type") else str(duplicate["category"])
    return (
        f"Error: {duplicate['date']} already has a {kind} with the same {duplicate['same']}: "
        f"event {duplicate['existing_id']} '{duplicate['existing_name']}'. Nothing was written. Change that event "
        f"({update_tool} with event_id={duplicate['existing_id']}) or, only if the athlete wants a second one, "
        "call again with allow_duplicate=true."
    )


def duplicate_check_failed(error: str) -> str:
    """Answer of a create whose duplicate check could not run."""
    return f"Error: the duplicate check could not run: {error}. Nothing was written; try again in a moment."


# ------------------------------------------------------------------ read-back
def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _timed(step: dict[str, Any]) -> bool:
    return bool(_number(step.get("duration")) or _number(step.get("distance")))


def _has_target(step: dict[str, Any]) -> bool:
    return any(isinstance(step.get(kind), dict) for kind in TARGET_KINDS)


def flat_steps(steps: Any) -> tuple[list[dict[str, Any]], list[int]]:
    """(steps that carry a duration, distance or target, in order; reps of the repeat blocks).

    Steps inside a repeat block count once; text-only steps (comments) are left out.
    """
    leaves: list[dict[str, Any]] = []
    reps: list[int] = []
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        if isinstance(step.get("steps"), list) and step.get("reps") is not None:
            reps.append(int(_number(step.get("reps")) or 0))
            inner, _ = flat_steps(step["steps"])
            leaves.extend({**leaf, "_reps": int(_number(step.get("reps")) or 1)} for leaf in inner)
        elif _timed(step) or _has_target(step):
            leaves.append(step)
    return leaves, reps


def sent_shape(doc: Any = None, text: Any = None) -> dict[str, Any] | None:
    """What a write sent as workout: steps and repeats of the workout_doc, else the step lines
    of the workout text; None when no workout was sent."""
    steps = doc.get("steps") if isinstance(doc, dict) else None
    if steps:
        leaves, reps = flat_steps(steps)
        total: float | None = 0.0
        for leaf in leaves:
            if _number(leaf.get("duration")) is None:
                total = None  # a distance step: the planned time is an estimate
                break
            total = (total or 0.0) + float(leaf["duration"]) * leaf.get("_reps", 1)
        return {"steps": leaves, "count": len([s for s in leaves if _timed(s)]), "repeats": reps, "duration_s": total or None}
    lines = step_lines(str(text or ""))
    if lines:
        reps = [count for line in str(text).splitlines() if (count := repeat_count(line)) is not None]
        return {"steps": None, "count": len(lines), "repeats": reps, "duration_s": None}
    return None


def _units(target: Any) -> str | None:
    """Target units in one spelling (lower case; MINS_KM and secs/km are the same)."""
    if not isinstance(target, dict):
        return None
    units = str(target.get("units") or "").strip().lower()
    return _SAME_UNITS.get(units, units) or None


def _pace_range(target: dict[str, Any], *, minutes_allowed: bool) -> tuple[float, float] | None:
    """An absolute pace target as (low, high) seconds per distance; per-km/mile values below 60 sent
    by us are decimal minutes (as the workout text renders them)."""
    values = [v for v in (_number(target.get("value")), _number(target.get("start")), _number(target.get("end"))) if v is not None]
    if not values:
        return None
    seconds = [v * 60 if minutes_allowed and v < 60 else v for v in values]
    return min(seconds), max(seconds)


def _pace_text(bounds: tuple[float, float], units: str) -> str:
    suffix = units.split("/", 1)[-1]
    low, high = (hms(v) for v in bounds)
    return f"{low}/{suffix}" if low == high else f"{low}-{high}/{suffix}"


def _pace_warning(number: int, mine: dict[str, Any], theirs: dict[str, Any], units: str) -> str | None:
    """A changed absolute pace (after converting minutes to seconds; 2 s tolerance, start/end in either order)."""
    sent = _pace_range(mine, minutes_allowed=str(mine.get("units") or "").strip().lower() in _PER_KM_OR_MILE)
    stored = _pace_range(theirs, minutes_allowed=str(theirs.get("units") or "").strip().lower() in _PER_KM_OR_MILE)
    if sent is None or stored is None or all(abs(a - b) <= PACE_TOLERANCE_S for a, b in zip(sent, stored, strict=True)):
        return None
    return f"step {number}: pace {_pace_text(sent, units)} sent, {_pace_text(stored, units)} stored"


def _step_warnings(sent: list[dict[str, Any]], stored: list[dict[str, Any]]) -> list[str]:
    """Differences between the sent and the stored steps, pairwise in order."""
    warnings: list[str] = []
    for number, (mine, theirs) in enumerate(zip(sent, stored, strict=False), 1):
        for key, label, unit in (("duration", "duration", "s"), ("distance", "distance", "m")):
            a, b = _number(mine.get(key)), _number(theirs.get(key))
            if a is not None and b is None and not (key == "duration" and _number(theirs.get("distance"))):
                warnings.append(f"step {number}: no {label} stored ({a:g} {unit} sent)")
            elif a is not None and b is not None and abs(a - b) > max(1.0, a * 0.01):
                shown = (hms(a), hms(b)) if key == "duration" else (f"{a:g} m", f"{b:g} m")
                warnings.append(f"step {number}: {label} {shown[0]} sent, {shown[1]} stored")
        warnings.extend(_target_warnings(number, mine, theirs))
    return warnings


def _target_warnings(number: int, mine: dict[str, Any], theirs: dict[str, Any]) -> list[str]:
    """Targets of one step that were not stored, stored in other units, or added (cadence aside)."""
    warnings: list[str] = []
    for kind in TARGET_KINDS:
        a_units, b_units = _units(mine.get(kind)), _units(theirs.get(kind))
        if a_units and not isinstance(theirs.get(kind), dict):
            warnings.append(f"step {number}: {kind} target ({a_units}) not stored")
        elif a_units and b_units and a_units != b_units:
            warnings.append(f"step {number}: {kind} target units {a_units} sent, {b_units} stored")
        elif a_units and a_units == b_units and a_units.startswith("secs/"):
            changed = _pace_warning(number, mine[kind], theirs[kind], a_units)
            if changed:
                warnings.append(changed)
        elif not a_units and isinstance(theirs.get(kind), dict) and kind != "cadence":
            warnings.append(f"step {number}: {kind} target stored ({b_units}) that was not sent")
    return warnings


def _workout_doc(stored: dict[str, Any]) -> dict[str, Any]:
    doc = stored.get("workout_doc")
    return doc if isinstance(doc, dict) else {}


def has_sport(stored: dict[str, Any]) -> bool:
    """True for workouts, races and library workouts (no category); False for notes, holidays,
    sick and injured days."""
    category = str(stored.get("category") or "").upper()
    return not category or category in SPORT_CATEGORIES


def stored_summary(stored: dict[str, Any], sent: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compact view of a stored event or library workout: what Intervals.icu stored and parsed.

    Workouts and races: date, category, sport, name, planned duration, load, timed steps parsed
    (and sent). Notes and other events without a sport: date, category, name and text length.
    Fields without a value are left out.
    """
    summary: dict[str, Any] = {"id": stored.get("id")}
    for key in ("start_date_local", "category", "type", "name", "folder_id", "indoor"):
        if stored.get(key) is not None:
            summary[key] = stored.get(key)
    if not has_sport(stored):
        summary.pop("type", None)
        summary["text_chars"] = len(str(stored.get("description") or ""))
        return summary
    leaves, reps = flat_steps(_workout_doc(stored).get("steps"))
    moving = _number(stored.get("moving_time"))
    if moving:
        summary["moving_time"] = int(moving)
        summary["duration"] = hms(moving)
    if stored.get("icu_training_load") is not None:
        summary["load"] = stored.get("icu_training_load")
    summary["steps"] = len([leaf for leaf in leaves if _timed(leaf)])
    if reps:
        summary["repeats"] = reps
    if sent is not None:
        summary["steps_sent"] = sent["count"]
    if stored.get("paired_activity_id"):
        summary["paired_activity_id"] = stored.get("paired_activity_id")
    return summary


_COMPARED_FIELDS = (("start_date_local", "date"), ("name", "name"), ("category", "category"), ("type", "sport"), ("indoor", "indoor"))


def _field_warnings(body: dict[str, Any], stored: dict[str, Any]) -> list[str]:
    """Fields (date, name, category, sport, indoor, planned moving time) stored with another value than sent."""
    warnings: list[str] = []
    for key, label in _COMPARED_FIELDS:
        if key in body and body[key] is not None and stored.get(key) is not None:
            mine, theirs = body[key], stored[key]
            if key == "start_date_local":  # the time of day counts only when it was sent
                theirs = str(theirs)[: len(str(mine))]
            if (normalized(mine) if isinstance(mine, str) else mine) != (normalized(theirs) if isinstance(theirs, str) else theirs):
                warnings.append(f"{label} {body[key]!r} sent, {stored[key]!r} stored")
    moving = _number(stored.get("moving_time"))
    sent_moving = _number(body.get("moving_time"))
    if sent_moving is not None and moving is not None and abs(sent_moving - moving) > DURATION_TOLERANCE_S:
        warnings.append(f"moving_time {hms(sent_moving)} sent, {hms(moving)} stored (Intervals.icu uses the workout steps)")
    return warnings


def parse_warnings(body: dict[str, Any], stored: dict[str, Any], sent: dict[str, Any] | None) -> list[str]:  # pylint: disable=too-many-branches
    """What Intervals.icu stored differently from what was sent, and what it could not parse."""
    warnings = _field_warnings(body, stored)
    if not has_sport(stored):
        return warnings  # a note's dash lines are not a workout
    moving = _number(stored.get("moving_time"))
    doc = _workout_doc(stored)
    leaves, reps = flat_steps(doc.get("steps"))
    timed = [leaf for leaf in leaves if _timed(leaf)]
    untimed = [number for number, leaf in enumerate(leaves, 1) if not _timed(leaf)]
    if untimed:
        warnings.append(f"step(s) {', '.join(map(str, untimed[:5]))} stored without duration or distance")
    if sent is None:
        return warnings
    if sent["count"] and not timed:
        warnings.append(f"no workout steps stored ({sent['count']} sent): Intervals.icu did not read the text as a workout")
        return warnings
    if len(timed) < sent["count"]:
        warnings.append(f"Intervals.icu parsed {len(timed)} steps of {sent['count']} sent: steps were dropped or merged")
    elif len(timed) > sent["count"]:
        warnings.append(f"Intervals.icu parsed {len(timed)} steps, {sent['count']} sent: other lines were read as steps")
    if reps != sent["repeats"]:
        shown = ", ".join(f"{r}x" for r in sent["repeats"]) or "none"
        warnings.append(f"repeats {shown} sent, {', '.join(f'{r}x' for r in reps) or 'none'} stored")
    if sent["steps"] is not None and len(sent["steps"]) == len(leaves):
        warnings.extend(_step_warnings(sent["steps"], leaves))
    if sent["count"] and not moving:
        warnings.append("no planned duration stored")
    elif sent["duration_s"] and moving and abs(sent["duration_s"] - moving) > DURATION_TOLERANCE_S:
        warnings.append(f"planned time {hms(moving)} stored, {hms(sent['duration_s'])} in the steps sent")
    return warnings


async def read_back(request: Request, path: str) -> tuple[dict[str, Any] | None, str | None]:
    """GET the stored object (event or library workout); (object, None) or (None, error)."""
    result = await request(url=path)
    if isinstance(result, dict) and "error" in result:
        return None, str(result.get("message") or "unknown error")
    stored = result[0] if isinstance(result, list) and result else result
    if not isinstance(stored, dict) or not stored:
        return None, "Intervals.icu returned nothing"
    return stored, None


def _when(value: Any) -> str:
    """'2026-10-12' or '2026-10-12 07:30' (the time only when it is not midnight)."""
    text = str(value or "")
    time_of_day = text[11:16] if len(text) >= 16 and text[10] == "T" else ""
    return text[:10] + (f" {time_of_day}" if time_of_day and time_of_day != "00:00" else "")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def read_back_text(stored: dict[str, Any] | None, warnings: list[str], error: str | None, what: str) -> str:
    """One compact paragraph (at most ~600 characters) on what Intervals.icu stored."""
    if stored is None:
        return (
            f"Read-back: the write succeeded but {what} could not be verified ({error}); check it with "
            "get_event_by_id or get_library_workout."
        )[:READ_BACK_CHARS]
    parts = [_when(stored["start_date_local"])] if stored.get("start_date_local") else []
    parts += [str(stored[key]) for key in ("category", "type") if stored.get(key) is not None]
    head = " ".join(parts) + (" " if parts else "") + f"'{stored.get('name') or 'unnamed'}'"
    if "text_chars" in stored:
        details = [f"text {_plural(stored['text_chars'], 'character')}"]
    else:
        details = [stored.get("duration") or "no planned duration", f"load {stored.get('load', 'n/a')}"]
        if stored.get("steps") or "steps_sent" in stored:
            steps = f"{_plural(stored['steps'], 'step')} parsed"
            details.append(steps + (f" ({stored['steps_sent']} sent)" if "steps_sent" in stored else ""))
    if stored.get("paired_activity_id"):
        details.append(f"paired with {stored['paired_activity_id']}")
    text = f"Read-back: Intervals.icu stored {head}, {', '.join(details)}. Parse warnings: "
    shown: list[str] = []
    for warning in warnings:
        if len(text) + len("; ".join([*shown, warning])) + 20 > READ_BACK_CHARS:
            break
        shown.append(warning)
    text += ("; ".join(shown) if shown else "none") + (f" (+{len(warnings) - len(shown)} more)" if len(shown) < len(warnings) else "") + "."
    return text[:READ_BACK_CHARS]


def short_warnings(warnings: list[str], limit: int = 3) -> list[str]:
    """At most *limit* warnings, each cut to SHORT_WARNING_CHARS, plus '+N more' (compact answers)."""
    shown = [w if len(w) <= SHORT_WARNING_CHARS else w[: SHORT_WARNING_CHARS - 3] + "..." for w in warnings[:limit]]
    return shown + ([f"+{len(warnings) - limit} more"] if len(warnings) > limit else [])


async def verify_write(
    request: Request, path: str, body: dict[str, Any], sent: dict[str, Any] | None, what: str
) -> tuple[dict[str, Any] | None, list[str], str]:
    """Read back one written object: (stored summary or None, parse warnings, text)."""
    stored, error = await read_back(request, path)
    if stored is None:
        return None, [], read_back_text(None, [], error, what)
    summary = stored_summary(stored, sent)
    warnings = parse_warnings(body, stored, sent)
    return summary, warnings, read_back_text(summary, warnings, None, what)


def deletion_row(event: dict[str, Any]) -> dict[str, Any]:
    """What a deleted event was: id, date, category, sport, name and the paired activity."""
    return {
        "id": event.get("id"),
        "date": str(event.get("start_date_local") or "")[:10],
        "category": event.get("category"),
        "type": event.get("type"),
        "name": event.get("name"),
        "paired_activity_id": event.get("paired_activity_id"),
    }


def is_not_found(result: Any) -> bool:
    """True for an API error that means the object does not exist (HTTP 404)."""
    return isinstance(result, dict) and "error" in result and result.get("status_code") == 404
