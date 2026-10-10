"""
Write safety for the write tools: dry runs, duplicate checks before creating events, read-back
of what Intervals.icu stored, and the echo of deleted objects.

- ``dry_run_answer``: the exact request a write tool would send (method, path, query parameters,
  body after all defaults and merges) and the validation result, as compact JSON. Nothing is
  sent: the tool returns before its write, and the tool guard refuses every non-GET request of
  a dry run in the API client as well.
- ``check_duplicates``: reads the events of every day an event is about to be created on (one
  GET per distinct day) and finds an event of the same category with the same name
  (case/whitespace-insensitive) or the same workout content.
- ``read_back_event`` / ``read_back_workout`` / ``stored_summary`` / ``parse_warnings``: what
  Intervals.icu stored after a write (date, name, category, sport, duration, load, the timed
  steps it parsed from the workout text) compared with what was sent.

The request function is passed in by the tool, so a tool module's (mocked) request function
serves every request of the tool.
"""

import json
import re
from collections.abc import Awaitable, Callable, Iterable
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
TARGET_KINDS = ("power", "hr", "pace", "cadence")
# Unit spellings that mean the same target (the workout text writes cadence as "rpm").
_SAME_UNITS = {"cadence": "rpm"}
_REPEAT_LINE = re.compile(r"^\s*(\d+)\s*x\b", re.I)
_STRUCTURE_LINE = re.compile(r"^\s*(?:-|\d+\s*x\b)", re.I)


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


def workout_signature(text: Any) -> str:
    """The content of a workout text: its step and repeat lines when it has steps (so another
    introduction does not hide the same workout), else the whole text; normalised."""
    raw = str(text or "")
    if step_lines(raw):
        return "\n".join(normalized(line) for line in raw.splitlines() if _STRUCTURE_LINE.match(line))
    return normalized(raw)


def _same(body: dict[str, Any], other: dict[str, Any]) -> str | None:
    """'name' or 'workout' when *other* duplicates *body* (same category first), else None."""
    if str(other.get("category") or "").upper() != str(body.get("category") or "WORKOUT").upper():
        return None
    name = normalized(body.get("name"))
    if name and normalized(other.get("name")) == name:
        return "name"
    content = workout_signature(body.get("description"))
    if content and workout_signature(other.get("description")) == content:
        return "workout"
    return None


def _day(body: dict[str, Any]) -> str:
    return str(body.get("start_date_local") or "")[:10]


async def day_events(request: Request, athlete_id: str, day: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The events of one day (any category), or an error message."""
    result = await request(url=f"/athlete/{seg(athlete_id)}/events", params={"oldest": day, "newest": day})
    if isinstance(result, dict) and "error" in result:
        return None, str(result.get("message") or "unknown error")
    if isinstance(result, list):
        return [event for event in result if isinstance(event, dict)], None
    if not result:
        return [], None
    return None, "unexpected answer from Intervals.icu"


async def check_duplicates(
    request: Request, athlete_id: str, bodies: list[dict[str, Any]], *, reserve: int = 2
) -> tuple[list[dict[str, Any] | None], str | None]:
    """For each event body: None, or what it duplicates on its day (an existing event, or an
    earlier body of the same request); plus an error when the check could not be done.

    One GET per distinct day. *reserve* requests of the tool call's budget are kept for the
    write and the read-back; a check that would not fit is not started.
    """
    days = list(dict.fromkeys(_day(body) for body in bodies))
    left = remaining_requests()
    if left is not None and len(days) > left - reserve:
        return [], (
            f"the duplicate check needs {len(days)} requests (one per day), more than this tool call may still send; "
            "split the list"
        )
    existing: dict[str, list[dict[str, Any]]] = {}
    for day in days:
        events, error = await day_events(request, athlete_id, day)
        if events is None:
            return [], f"could not read the events of {day} to check for duplicates ({error})"
        existing[day] = events
    return _match_duplicates(bodies, existing), None


def _match_duplicates(bodies: list[dict[str, Any]], existing: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any] | None]:
    """Per body: the existing event of its day it duplicates, else an earlier body of the list, else None."""
    found: list[dict[str, Any] | None] = []
    earlier: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, body in enumerate(bodies):
        day = _day(body)
        row = next(
            ({"date": day, "category": event.get("category"), "same": same, "existing_id": event.get("id"),
              "existing_name": event.get("name")}
             for event in existing.get(day, []) if (same := _same(body, event))),
            None,
        ) or next(
            ({"date": day, "category": other.get("category"), "same": same, "duplicate_of_index": other_index,
              "existing_name": other.get("name")}
             for other_index, other in earlier.get(day, []) if (same := _same(body, other))),
            None,
        )
        earlier.setdefault(day, []).append((index, body))
        found.append(row)
    return found


def duplicate_refusal(duplicate: dict[str, Any], update_tool: str = "add_or_update_event") -> str:
    """Answer of a create that was refused because the day already has the same event."""
    return (
        f"Error: {duplicate['date']} already has a {duplicate['category']} with the same {duplicate['same']}: "
        f"event {duplicate['existing_id']} '{duplicate['existing_name']}'. Nothing was written. Change that event "
        f"({update_tool} with event_id={duplicate['existing_id']}) or, only if the athlete wants a second one, "
        "call again with allow_duplicate=true."
    )


def duplicate_check_failed(error: str) -> str:
    """Answer of a create whose duplicate check could not be done."""
    return (
        f"Error: {error}. Nothing was written. Try again, or pass allow_duplicate=true to create it "
        "without the check."
    )


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
        reps = [int(m.group(1)) for line in str(text).splitlines() if (m := _REPEAT_LINE.match(line))]
        return {"steps": None, "count": len(lines), "repeats": reps, "duration_s": None}
    return None


def _units(target: Any) -> str | None:
    if not isinstance(target, dict):
        return None
    units = str(target.get("units") or "")
    return _SAME_UNITS.get(units, units) or None


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
        for kind in TARGET_KINDS:
            a_units, b_units = _units(mine.get(kind)), _units(theirs.get(kind))
            if a_units and not isinstance(theirs.get(kind), dict):
                warnings.append(f"step {number}: {kind} target ({a_units}) not stored")
            elif a_units and b_units and a_units != b_units:
                warnings.append(f"step {number}: {kind} target units {a_units} sent, {b_units} stored")
            elif not a_units and isinstance(theirs.get(kind), dict) and kind != "cadence":
                warnings.append(f"step {number}: {kind} target stored ({b_units}) that was not sent")
    return warnings


def _workout_doc(stored: dict[str, Any]) -> dict[str, Any]:
    doc = stored.get("workout_doc")
    return doc if isinstance(doc, dict) else {}


def stored_summary(stored: dict[str, Any], sent: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compact view of a stored event or library workout: what Intervals.icu stored and parsed."""
    doc = _workout_doc(stored)
    leaves, reps = flat_steps(doc.get("steps"))
    moving = _number(stored.get("moving_time"))
    summary: dict[str, Any] = {"id": stored.get("id")}
    for key in ("start_date_local", "category", "type", "name", "folder_id", "indoor"):
        if stored.get(key) is not None:
            summary[key] = stored.get(key)
    summary["moving_time"] = int(moving) if moving is not None else None
    summary["duration"] = hms(moving) if moving else None
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


def read_back_text(stored: dict[str, Any] | None, warnings: list[str], error: str | None, what: str) -> str:
    """One compact paragraph (at most ~600 characters) on what Intervals.icu stored."""
    if stored is None:
        return (
            f"Read-back: the write succeeded but {what} could not be verified ({error}); check it with "
            "get_event_by_id or get_library_workout."
        )[:READ_BACK_CHARS]
    parts = [str(stored.get(key)) for key in ("start_date_local", "category", "type") if stored.get(key) is not None]
    head = " ".join(parts) + (" " if parts else "") + f"'{stored.get('name') or 'unnamed'}'"
    details = [stored["duration"] or "no planned duration", f"load {stored['load'] if stored.get('load') is not None else 'n/a'}"]
    steps = f"{stored['steps']} steps parsed"
    if "steps_sent" in stored:
        steps += f" ({stored['steps_sent']} sent)"
    details.append(steps)
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
