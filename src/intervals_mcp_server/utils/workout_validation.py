"""
Client-side validation and preview of workout documents before they are written to the
Intervals.icu calendar. Intervals.icu has no parse/validate endpoint, so the checks here
mirror what the workout builder expects: durations or distances per step, repeat blocks,
target units per sport, ramps and ranges, warm-up/cool-down, and the total duration.
"""

import re
from typing import Any

from pydantic import TypeAdapter, ValidationError

from intervals_mcp_server.utils.sports import hms
from intervals_mcp_server.utils.types import ValueUnits, WorkoutDoc

RUN_TYPES = ("Run", "TrailRun", "VirtualRun", "Walk", "Hike")
RIDE_TYPES = ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide")
SWIM_TYPES = ("Swim", "OpenWaterSwim")
POWER_UNITS = {"w", "%ftp", "power_zone", "%mmp"}
HR_UNITS = {"%hr", "%lthr", "hr_zone"}
PACE_UNITS = {"%pace", "pace_zone", "MINS_KM", "MINS_MILE", "SECS_100M", "SECS_100Y", "SECS_500M"}
CADENCE_UNITS = {"cadence", "rpm"}
# Units each target kind may use: an HR target in %ftp would be written as a power target.
UNITS_BY_KIND = {"power": POWER_UNITS, "hr": HR_UNITS, "pace": PACE_UNITS, "cadence": CADENCE_UNITS}
# Plausible absolute paces in seconds per unit distance (2:30-20:00/km, 0:50-6:00/100m ...);
# outside these the units are almost certainly wrong (e.g. 1.75 SECS_100M = 0:02/100m).
ABSOLUTE_PACE_SECONDS = {
    "MINS_KM": (150, 1200),
    "MINS_MILE": (240, 1930),
    "SECS_100M": (50, 360),
    "SECS_100Y": (45, 330),
    "SECS_500M": (75, 300),
}
SWIM_PACE_UNITS = {"SECS_100M", "SECS_100Y"}
PLAUSIBLE = {
    "w": (0, 2500),
    "%ftp": (20, 250),
    "%mmp": (20, 150),
    "power_zone": (1, 8),
    "%hr": (30, 110),
    "%lthr": (30, 130),
    "hr_zone": (1, 8),
    "%pace": (30, 160),
    "pace_zone": (1, 8),
    "cadence": (20, 140),
    "rpm": (20, 140),
}
DURATION_TOLERANCE_S = 60

# Step labels are written BEFORE the duration ("- Sprint 40mtr Z5 HR"), unescaped: anything
# Intervals.icu reads as workout syntax would change the step. Labels must stay plain words.
_LABEL_SYNTAX = (
    (re.compile(r"[\r\n]"), "a line break (starts a new step)"),
    (re.compile(r"\d+\s*x(?![a-z])", re.I), "a repeat count such as 3x"),
    (re.compile(r"\d+(?:\.\d+)?\s*(?:h|hr|hrs|m|min|mins|s|sec|secs)(?![a-z])", re.I), "a duration such as 2m or 30s"),
    (re.compile(r"\d+(?:\.\d+)?\s*(?:km|mi|mtr|y|yd|yds)(?![a-z])", re.I), "a distance such as 400mtr"),
    (re.compile(r"\d+(?:\.\d+)?\s*%"), "a % target"),
    (re.compile(r"\d+\s*w(?![a-z])", re.I), "a power target in watts"),
    (re.compile(r"\d+\s*(?:rpm|bpm|spm)(?![a-z])", re.I), "a cadence or heart rate"),
    (re.compile(r"(?<![a-z0-9])z\d", re.I), "a zone such as Z2"),
    (re.compile(r"\d+:\d\d"), "a pace or time such as 4:30"),
    (re.compile(r"(?<![a-z])(?:ramp|freeride|free ride|maxeffort|max effort|hidepower|intensity=)", re.I),
     "a workout keyword (ramp, freeride, max effort, hidepower, intensity=)"),
)


def label_problem(text: Any) -> str | None:
    """Why a step label would be read as workout syntax, or None when it is a plain label."""
    if not isinstance(text, str):
        return "must be a string"
    for pattern, meaning in _LABEL_SYNTAX:
        found = pattern.search(text)
        if found:
            return f"contains {found.group(0).strip() or found.group(0)!r}, which Intervals.icu would read as {meaning}"
    return None


def _pace_seconds(units: str, number: float) -> float:
    """Absolute pace in seconds per unit distance, as the writer renders it."""
    if units in ("MINS_KM", "MINS_MILE") and number < 60:
        return number * 60
    return number


def _check_value(kind: str, value: Any, path: str, errors: list[str], warnings: list[str]) -> None:  # pylint: disable=too-many-branches
    if not isinstance(value, dict):
        errors.append(f"{path}: {kind} target must be an object with value/start/end and units")
        return
    units = value.get("units")
    valid = {u.value for u in ValueUnits}
    if units not in valid:
        errors.append(f"{path}: {kind} units {units!r} are not supported (use one of {', '.join(sorted(valid))})")
        return
    allowed = UNITS_BY_KIND.get(kind)
    if allowed is not None and units not in allowed:
        errors.append(f"{path}: {kind} target cannot use units {units!r} (use one of {', '.join(sorted(allowed))})")
        return
    numbers = [value.get(k) for k in ("value", "start", "end") if value.get(k) is not None]
    if not numbers:
        errors.append(f"{path}: {kind} target has neither value nor start/end")
        return
    if (value.get("start") is None) != (value.get("end") is None):
        errors.append(f"{path}: {kind} range needs both start and end")
    if value.get("value") is not None and (value.get("start") is not None or value.get("end") is not None):
        errors.append(f"{path}: {kind} target has both a value and a start/end range; give one of them")
    low, high = PLAUSIBLE.get(str(units), (None, None))
    for number in numbers:
        if not isinstance(number, (int, float)) or isinstance(number, bool):
            errors.append(f"{path}: {kind} target {number!r} is not a number")
        elif str(units) in ABSOLUTE_PACE_SECONDS:
            fastest, slowest = ABSOLUTE_PACE_SECONDS[str(units)]
            seconds = _pace_seconds(str(units), float(number))
            if not fastest <= seconds <= slowest:
                errors.append(
                    f"{path}: pace {number} {units} = {int(seconds) // 60}:{int(seconds) % 60:02d} per unit distance is "
                    f"implausible (expected {fastest // 60}:{fastest % 60:02d}-{slowest // 60}:{slowest % 60:02d}); check the units"
                )
        elif low is not None and high is not None and not low <= number <= high:
            warnings.append(f"{path}: {kind} target {number} {units} is outside the plausible range {low}-{high}")


def _check_label(step: dict[str, Any], path: str, errors: list[str]) -> None:
    if step.get("text") is None:
        return
    problem = label_problem(step["text"])
    if problem:
        errors.append(
            f"{path}: text {step['text']!r} {problem}; keep labels to plain words and put durations, "
            "distances, targets and repeats into the step fields"
        )


def _check_number(name: str, value: Any, path: str, errors: list[str], *, whole: bool = False) -> float | None:
    """A positive number (seconds or metres), or None after adding the error."""
    unit = "seconds" if name == "duration" else "metres"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        errors.append(f"{path}: {name} must be a positive number of {unit}")
        return None
    if whole and not float(value).is_integer():
        errors.append(f"{path}: {name} must be a whole number of {unit} (got {value})")
        return None
    return float(value)


def _check_step(  # pylint: disable=too-many-branches,too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-statements
    step: dict[str, Any], path: str, sport: str, errors: list[str], warnings: list[str], totals: dict[str, Any]
) -> None:
    duration, distance = step.get("duration"), step.get("distance")
    reps, children = step.get("reps"), step.get("steps")
    _check_label(step, path, errors)
    if reps is not None or children:
        if not isinstance(reps, int) or isinstance(reps, bool) or reps < 1:
            errors.append(f"{path}: reps must be a positive integer")
            reps = 0
        if not isinstance(children, list) or not children:
            errors.append(f"{path}: a repeat block needs nested steps")
            return
        if step.get("warmup") or step.get("cooldown"):
            warnings.append(f"{path}: warmup/cooldown on a repeat block is ignored by the workout text; set it on a single step")
        for key in ("duration", "distance", "power", "hr", "pace", "cadence"):
            if step.get(key) is not None:
                errors.append(f"{path}: a repeat block cannot have its own {key}; put it on the nested steps")
        for index, child in enumerate(children, 1):
            if not isinstance(child, dict):
                errors.append(f"{path}.{index}: step must be an object")
                continue
            for _ in range(max(reps, 1)):
                _check_step(child, f"{path}.{index}", sport, [], [], totals)
            _check_step(child, f"{path}.{index}", sport, errors, warnings, {"duration": 0, "distance": 0, "open": 0, "work": 0, "steps": 0})
        return
    totals["steps"] += 1
    if duration is None and distance is None:
        if step.get("until_lap_press") or step.get("freeride"):
            totals["open"] += 1
            errors.append(
                f"{path}: open-ended step (until lap press / free ride without duration) cannot be written as "
                "Intervals.icu workout text; give it a duration or distance"
            )
        elif step.get("text") and not any(step.get(k) for k in ("power", "hr", "pace", "cadence")):
            pass  # a pure text/comment step
        else:
            errors.append(f"{path}: step needs a duration (seconds) or a distance (metres)")
    if duration is not None and distance is not None:
        errors.append(f"{path}: give either a duration or a distance, not both (the distance would be dropped)")
    if duration is not None:
        seconds = _check_number("duration", duration, path, errors, whole=True)
        if seconds is not None:
            totals["duration"] += seconds
    if distance is not None:
        metres = _check_number("distance", distance, path, errors)
        if metres is not None:
            totals["distance"] += metres
    if step.get("ramp") and not any(isinstance(step.get(k), dict) and step[k].get("start") is not None for k in ("power", "hr", "pace")):
        errors.append(f"{path}: a ramp needs a start/end range target")
    has_target = False
    for kind in ("power", "hr", "pace", "cadence"):
        if step.get(kind) is not None:
            has_target = True
            _check_value(kind, step[kind], path, errors, warnings)
    if has_target and not (step.get("warmup") or step.get("cooldown")):
        totals["work"] += 1
    units = {str(step[k].get("units")) for k in ("power", "pace") if isinstance(step.get(k), dict)}
    if sport in RUN_TYPES and units & POWER_UNITS and not units & PACE_UNITS:
        warnings.append(f"{path}: power target on a {sport} workout; runs are usually planned by pace or HR")
    if sport in RIDE_TYPES and units & PACE_UNITS:
        warnings.append(f"{path}: pace target on a {sport} workout; rides are usually planned by power or HR")
    if sport in SWIM_TYPES and units & (set(ABSOLUTE_PACE_SECONDS) - SWIM_PACE_UNITS):
        warnings.append(f"{path}: pace per km/mile/500m on a {sport} workout; swims are usually paced per 100m or 100y")
    if sport in RUN_TYPES and units & (SWIM_PACE_UNITS | {"SECS_500M"}):
        warnings.append(f"{path}: swim/row pace units on a {sport} workout; runs are usually paced per km or mile")


def validate_workout_doc(  # pylint: disable=too-many-locals
    workout_doc: Any, workout_type: str, moving_time: int | None = None
) -> dict[str, Any]:
    """Validate a workout document and render a preview.

    Returns {"ok", "errors", "warnings", "totals": {duration_s, distance_m, steps, work_steps,
    open_steps, has_warmup, has_cooldown}, "preview"}.
    """
    errors: list[str] = []
    warnings: list[str] = []
    doc = workout_doc.to_dict() if isinstance(workout_doc, WorkoutDoc) else workout_doc
    if not isinstance(doc, dict):
        return {"ok": False, "errors": ["workout_doc must be an object with a 'steps' list"], "warnings": [], "totals": {}, "preview": ""}
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("workout_doc.steps must be a non-empty list")
        steps = []
    totals: dict[str, Any] = {"duration": 0.0, "distance": 0.0, "open": 0, "work": 0, "steps": 0}
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            errors.append(f"step {index}: must be an object")
            continue
        _check_step(step, f"step {index}", workout_type, errors, warnings, totals)
    # Only single steps get a Warmup/Cooldown section in the workout text (not repeat blocks).
    has_warmup = any(isinstance(s, dict) and s.get("warmup") and not (s.get("reps") or s.get("steps")) for s in steps)
    has_cooldown = any(isinstance(s, dict) and s.get("cooldown") and not (s.get("reps") or s.get("steps")) for s in steps)
    if steps and not has_warmup:
        warnings.append("no warm-up step (warmup: true)")
    if steps and not has_cooldown:
        warnings.append("no cool-down step (cooldown: true)")
    if moving_time is not None and totals["duration"] and abs(totals["duration"] - moving_time) > DURATION_TOLERANCE_S:
        warnings.append(
            f"sum of step durations {hms(totals['duration'])} differs from moving_time {hms(moving_time)} by more than {DURATION_TOLERANCE_S} s"
        )
    if workout_type not in RUN_TYPES + RIDE_TYPES + SWIM_TYPES:
        warnings.append(f"workout_type {workout_type!r} is not a common Intervals.icu activity type (Ride, GravelRide, Run, Swim ...)")
    preview = ""
    if not errors:
        try:
            preview = str(WorkoutDoc.from_dict({"steps": steps, "description": doc.get("description")})).strip()
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            errors.append(f"workout_doc cannot be rendered: {exc}")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "totals": {
            "duration_s": totals["duration"],
            "distance_m": totals["distance"],
            "steps": totals["steps"],
            "work_steps": totals["work"],
            "open_steps": totals["open"],
            "has_warmup": has_warmup,
            "has_cooldown": has_cooldown,
        },
        "preview": preview,
    }


def format_validation(result: dict[str, Any], workout_type: str) -> str:
    """Readable validation report."""
    totals = result.get("totals") or {}
    lines = [f"Workout validation ({workout_type}): {'OK' if result['ok'] else 'ERRORS'}"]
    if totals:
        lines.append(
            f"Totals: {totals['steps']} steps ({totals['work_steps']} with targets, {totals['open_steps']} open-ended), "
            f"planned duration {hms(totals['duration_s'])}"
            + (f", distance {totals['distance_m']:.0f} m" if totals["distance_m"] else "")
            + f", warm-up {'yes' if totals['has_warmup'] else 'no'}, cool-down {'yes' if totals['has_cooldown'] else 'no'}"
        )
    for error in result["errors"]:
        lines.append(f"ERROR: {error}")
    for warning in result["warnings"]:
        lines.append(f"warning: {warning}")
    if result.get("preview"):
        lines.append("Preview (Intervals.icu workout text):")
        lines.append(result["preview"])
    return "\n".join(lines)


_WORKOUT_DOC_ADAPTER: TypeAdapter[WorkoutDoc] = TypeAdapter(WorkoutDoc)


def coerce_workout_doc(raw: Any) -> tuple[WorkoutDoc | None, str | None]:
    """A WorkoutDoc from a dict with the same type checks the single-event tools get from
    their parameter schema (a string where a list belongs, 90.5 s, "70" ...); (doc, error)."""
    if raw is None or isinstance(raw, WorkoutDoc):
        return raw, None
    if not isinstance(raw, dict):
        return None, "'workout_doc' must be an object"
    try:
        return _WORKOUT_DOC_ADAPTER.validate_python(raw), None
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in item['loc']) or 'workout_doc'}: {item['msg']}" for item in exc.errors()[:5]
        ]
        return None, "invalid 'workout_doc': " + "; ".join(problems)


def _doc_dict(workout_doc: Any) -> Any:
    return workout_doc.to_dict() if isinstance(workout_doc, WorkoutDoc) else workout_doc


def is_blank_workout_doc(workout_doc: Any) -> bool:
    """True for None, {} or a doc without steps and without a description (an LLM placeholder)."""
    data = _doc_dict(workout_doc)
    if data is None:
        return True
    if not isinstance(data, dict):
        return False
    description = data.get("description")
    return not data.get("steps") and not (isinstance(description, str) and description.strip())


def workout_text_for_write(  # pylint: disable=too-many-return-statements
    workout_doc: Any, workout_type: str | None, *, allow_text_only: bool = False
) -> tuple[str | None, str | None]:
    """The workout text to send for a workout_doc that is about to be WRITTEN: (text, problem).

    - A blank doc (None, {}, {"steps": []}) gives (None, None): treated as not given, so an
      update never wipes the planned workout with an empty description.
    - A doc without steps but with a description is refused unless allow_text_only (on create
      it is the event's text; on update it would replace the structured workout with prose).
    - Any validation error (validate_workout_doc) refuses the write.

    The problem is a plain sentence; the tools add "Error:" and "Nothing was written.".
    """
    if is_blank_workout_doc(workout_doc):
        return None, None
    data = _doc_dict(workout_doc)
    if not isinstance(data, dict):
        return None, "workout_doc must be an object with a 'steps' list"
    if not data.get("steps"):
        if allow_text_only:
            return str(data.get("description")).strip(), None
        return None, (
            "workout_doc has no steps (only a description); writing it would replace the planned workout "
            "with text only. Pass the steps, or leave workout_doc out to keep the existing workout"
        )
    result = validate_workout_doc(data, workout_type or "")
    if result["errors"]:
        return None, "workout_doc is not valid: " + "; ".join(result["errors"])
    totals = result["totals"]
    if not totals.get("duration_s") and not totals.get("distance_m"):
        return None, "workout_doc has no step with a duration or distance"
    text = str(workout_doc) if isinstance(workout_doc, WorkoutDoc) else str(WorkoutDoc.from_dict(data))
    return text, None


def write_refusal(problem: str) -> str:
    """Tool answer for a workout that is not written."""
    return f"Error: {problem}. Nothing was written."
