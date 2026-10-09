"""
Client-side validation and preview of workout documents before they are written to the
Intervals.icu calendar. Intervals.icu has no parse/validate endpoint, so the checks here
mirror what the workout builder expects: durations or distances per step, repeat blocks,
target units per sport, ramps and ranges, warm-up/cool-down, and the total duration.
"""

from typing import Any

from intervals_mcp_server.utils.sports import hms
from intervals_mcp_server.utils.types import ValueUnits, WorkoutDoc

RUN_TYPES = ("Run", "TrailRun", "VirtualRun", "Walk", "Hike")
RIDE_TYPES = ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide")
SWIM_TYPES = ("Swim", "OpenWaterSwim")
POWER_UNITS = {"w", "%ftp", "power_zone", "%mmp"}
HR_UNITS = {"%hr", "%lthr", "hr_zone"}
PACE_UNITS = {"%pace", "pace_zone", "MINS_KM", "MINS_MILE", "SECS_100M", "SECS_100Y", "SECS_500M"}
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


def _check_value(kind: str, value: Any, path: str, errors: list[str], warnings: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path}: {kind} target must be an object with value/start/end and units")
        return
    units = value.get("units")
    valid = {u.value for u in ValueUnits}
    if units not in valid:
        errors.append(f"{path}: {kind} units {units!r} are not supported (use one of {', '.join(sorted(valid))})")
        return
    numbers = [value.get(k) for k in ("value", "start", "end") if value.get(k) is not None]
    if not numbers:
        errors.append(f"{path}: {kind} target has neither value nor start/end")
        return
    if (value.get("start") is None) != (value.get("end") is None):
        errors.append(f"{path}: {kind} range needs both start and end")
    low, high = PLAUSIBLE.get(str(units), (None, None))
    for number in numbers:
        if not isinstance(number, (int, float)) or isinstance(number, bool):
            errors.append(f"{path}: {kind} target {number!r} is not a number")
        elif low is not None and high is not None and not low <= number <= high:
            warnings.append(f"{path}: {kind} target {number} {units} is outside the plausible range {low}-{high}")


def _check_step(  # pylint: disable=too-many-branches
    step: dict[str, Any], path: str, sport: str, errors: list[str], warnings: list[str], totals: dict[str, Any]
) -> None:
    duration, distance = step.get("duration"), step.get("distance")
    reps, children = step.get("reps"), step.get("steps")
    if reps is not None or children:
        if not isinstance(reps, int) or isinstance(reps, bool) or reps < 1:
            errors.append(f"{path}: reps must be a positive integer")
            reps = 0
        if not isinstance(children, list) or not children:
            errors.append(f"{path}: a repeat block needs nested steps")
            return
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
            warnings.append(f"{path}: open-ended step (until lap press / free ride); its duration is not counted")
        elif step.get("text") and not any(step.get(k) for k in ("power", "hr", "pace", "cadence")):
            pass  # a pure text/comment step
        else:
            errors.append(f"{path}: step needs a duration (seconds) or a distance (metres)")
    if duration is not None:
        if not isinstance(duration, (int, float)) or duration <= 0:
            errors.append(f"{path}: duration must be a positive number of seconds")
        else:
            totals["duration"] += float(duration)
    if distance is not None:
        if not isinstance(distance, (int, float)) or distance <= 0:
            errors.append(f"{path}: distance must be a positive number of metres")
        else:
            totals["distance"] += float(distance)
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
    has_warmup = any(isinstance(s, dict) and s.get("warmup") for s in steps)
    has_cooldown = any(isinstance(s, dict) and s.get("cooldown") for s in steps)
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
    try:
        preview = str(WorkoutDoc.from_dict({"steps": steps, "description": doc.get("description")})).strip()
    except (ValueError, TypeError, KeyError) as exc:
        errors.append(f"workout_doc cannot be rendered: {exc!r}")
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
