"""
Aggregation policy for numeric custom fields across activities.

Custom activity fields carry per-activity values (a device training load, sweat loss,
VO2max, stamina at the end, ground contact time ...). Summing them over a week only
makes sense for additive quantities. The field definition's ``aggregate`` (SUM, MAX,
MIN, AVERAGE) is one input, but it is chosen for a single activity's intervals and is
often SUM for values that must never be added across activities (percentages, ground
contact time in ms, vertical oscillation in cm, balance, scores).

The policy is derived generically from the definition, its units and the words of its
name and code; nothing is tied to a vendor:

- ``sum``: additive quantities - energy (kcal, kJ), volume (ml, l), distance, time,
  counts. Reported as total plus mean per session.
- ``device_load_sum``: device training loads (training load, EPOC, TRIMP, impact load),
  summed but labelled as a device scale separate from the Intervals.icu load.
- ``trend``: estimates and states (VO2max, performance condition, recovery time,
  detected thresholds, fitness age, predictions): latest value, first-to-last change,
  mean and range.
- ``mean``: intensive values (percentages, scores, training effects, running dynamics,
  heart rate, power, cadence, temperatures): mean, median, min and max. A definition
  aggregate of MIN or MAX makes that the primary value (e.g. the minimum temperature).
- ``none``: text/select fields and numbers without units or a recognisable meaning whose
  definition asks for a SUM: no aggregate is reported rather than a meaningless sum.

Fields named "... at start" / "... at end" with the same stem are paired so that the
typical start-to-end change (e.g. stamina) is reported where both values exist. Missing
values (null, NaN) are skipped; a stored 0 counts as a value. Operator overrides
(``CUSTOM_AGGREGATE_OVERRIDES="Code=mean,Other=sum"``) take precedence.
"""

import statistics
from typing import Any

from intervals_mcp_server.utils.custom_fields import CustomFieldDefs, is_missing

POLICIES = ("sum", "device_load_sum", "trend", "mean", "none")

_ENERGY = {"kcal", "cal", "kj", "j", "kilojoules", "calories"}
_VOLUME = {"ml", "l", "liter", "litre", "liters", "litres", "fl oz", "oz"}
_DISTANCE = {"m", "km", "mi", "miles", "ft", "metres", "meters"}
_TIME = {"s", "sec", "secs", "seconds", "min", "mins", "minutes", "h", "hr", "hrs", "hours"}
_COUNT = {"steps", "reps", "count", "strokes", "laps"}
_SUMMABLE_UNITS = _ENERGY | _VOLUME | _DISTANCE | _TIME | _COUNT
_INTENSIVE_UNITS = {
    "%", "percent", "percentage", "point", "points", "bpm", "w", "watts", "rpm", "spm", "ms", "cm", "mm",
    "°c", "c", "°f", "f", "ratio", "index", "score", "/min", "breaths/min", "m/s", "km/h", "kph", "mph",
    "ml/kg/min", "ml/min/kg", "ml/kg", "w/kg", "kg", "lb", "lbs", "mmol/l", "nm", "deg", "°",
}
_LOAD_WORDS = {"load", "epoc", "trimp", "tss", "strain"}
_STATE_PHRASES = (
    ("recovery", "time"), ("fitness", "age"), ("performance", "condition"), ("vo2",), ("vo2max",),
    ("predicted",), ("prediction",), ("ftp",), ("lthr",), ("ltp",), ("detected",), ("threshold",),
)
_TEMPERATURE_WORDS = {"temperature", "temp"}
_MEAN_WORDS = {
    "effect", "stride", "cadence", "gct", "contact", "oscillation", "ratio", "balance", "flight", "stamina",
    "score", "hr", "heart", "power", "speed", "pace", "effectiveness",
}
_SUM_WORDS = {"calories", "kcal", "energy", "distance", "duration", "steps", "sweat", "fluid", "elapsed", "moving", "count"}
_START_WORDS = {"start", "begin", "beginning", "initial"}
_END_WORDS = {"end", "finish", "final"}
_FILLER_WORDS = {"at", "of", "the"}


def _words(text: Any) -> list[str]:
    """Lower-case words of a display name or camelCase / snake_case code ("VO2 Max" -> vo2, max)."""
    if not isinstance(text, str):
        return []
    spaced = ""
    for index, char in enumerate(text):
        if char.isupper() and index and text[index - 1].islower():
            spaced += " "
        spaced += char if char.isalnum() else " "
    words = [word.lower() for word in spaced.split() if word]
    joined = " ".join(words)
    if "vo2 max" in joined or "vo2max" in joined:
        words.append("vo2")
    return words


def field_words(definition: dict[str, Any]) -> set[str]:
    """Words of a field's name and code."""
    return set(_words(definition.get("name"))) | set(_words(definition.get("code")))


def _units(definition: dict[str, Any]) -> str:
    units = definition.get("units")
    return units.strip().lower() if isinstance(units, str) else ""


def _has_phrase(words: set[str], phrases: tuple[tuple[str, ...], ...]) -> bool:
    return any(all(part in words for part in phrase) for phrase in phrases)


def aggregation_policy(definition: dict[str, Any], override: str | None = None) -> dict[str, Any]:
    """Policy {"policy", "primary", "reason"} for aggregating a field across activities.

    ``primary`` names the headline statistic: "sum", "mean", "max", "min" or "latest".
    """
    policy = _policy(definition, override)
    declared = str(definition.get("aggregate") or "").upper()
    if declared == "SUM" and policy["policy"] not in ("sum", "device_load_sum") and not policy["reason"].startswith("operator"):
        policy["reason"] += " (the definition's SUM is not applied across activities)"
    return policy


def _policy(definition: dict[str, Any], override: str | None) -> dict[str, Any]:  # pylint: disable=too-many-return-statements,too-many-branches
    declared = str(definition.get("aggregate") or "").upper()
    if override:
        policy = override.strip().lower()
        if policy in POLICIES:
            primary = {"sum": "sum", "device_load_sum": "sum", "trend": "latest", "mean": "mean", "none": None}[policy]
            return {"policy": policy, "primary": primary, "reason": "operator override (CUSTOM_AGGREGATE_OVERRIDES)"}
    if definition.get("value_type") not in (None, "numeric"):
        return {"policy": "none", "primary": None, "reason": f"{definition.get('value_type')} field"}
    words = field_words(definition)
    units = _units(definition)
    if words & _LOAD_WORDS and units not in ("%", "percent"):
        return {"policy": "device_load_sum", "primary": "sum", "reason": "device training load (own scale, kept apart from the Intervals.icu load)"}
    if _has_phrase(words, _STATE_PHRASES):
        return {"policy": "trend", "primary": "latest", "reason": "estimate / state value: latest, change and range, never summed"}
    if words & _TEMPERATURE_WORDS or units in ("°c", "°f"):
        primary = {"MIN": "min", "MAX": "max"}.get(declared, "mean")
        return {"policy": "mean", "primary": primary, "reason": "temperature"}
    if words & _MEAN_WORDS:
        return {"policy": "mean", "primary": "mean", "reason": "per-activity value (effect, score, running dynamics, rate, percentage), not additive"}
    if units in _INTENSIVE_UNITS:
        primary = {"MIN": "min", "MAX": "max"}.get(declared, "mean")
        return {"policy": "mean", "primary": primary, "reason": f"values in '{definition.get('units')}' are not additive across activities"}
    if units in _SUMMABLE_UNITS or (not units and words & _SUM_WORDS):
        if declared in ("", "SUM"):
            return {"policy": "sum", "primary": "sum", "reason": "additive quantity"}
    if declared in ("AVERAGE", "AVG", "MEAN"):
        return {"policy": "mean", "primary": "mean", "reason": "definition aggregate AVERAGE"}
    if declared in ("MAX", "MIN"):
        return {"policy": "mean", "primary": declared.lower(), "reason": f"definition aggregate {declared}"}
    if units in _SUMMABLE_UNITS:
        return {"policy": "sum", "primary": "sum", "reason": "additive quantity"}
    return {"policy": "none", "primary": None, "reason": "no units or recognisable meaning; a sum would not be meaningful"}


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or is_missing(value):
        return None
    return float(value)


AssignedByType = dict[str, set[str] | None]


def _values(
    activities: list[dict[str, Any]], code: str, assigned_by_type: AssignedByType | None = None
) -> tuple[list[tuple[str, float]], int]:
    """(date, value) pairs of a field, oldest first, and how many values came from sports without the field."""
    out: list[tuple[str, float]] = []
    foreign = 0
    for activity in activities:
        value = _number(activity.get(code))
        if value is None:
            continue
        assigned = (assigned_by_type or {}).get(str(activity.get("type") or ""))
        if assigned is not None and code not in assigned:
            foreign += 1
            continue
        out.append((str(activity.get("start_date_local") or ""), value))
    out.sort(key=lambda item: item[0])
    return out, foreign


def aggregate_field(  # pylint: disable=too-many-arguments
    activities: list[dict[str, Any]], code: str, definition: dict[str, Any], override: str | None = None,
    assigned_by_type: AssignedByType | None = None,
) -> dict[str, Any] | None:
    """Aggregate one custom field over activities according to its policy; None without values.

    ``assigned_by_type`` maps an activity type to the field codes assigned to its sport in the
    Intervals.icu sport settings (None = unknown); values on activities of a sport without the
    field (e.g. running dynamics stored as 0 on rides) are left out and counted. For estimates
    (trend policy) on a never-negative scale a stored 0 is the device's "no value" and is left out
    as well; elsewhere zeros are kept as stored and counted.
    """
    dated, foreign = _values(activities, code, assigned_by_type)
    if not dated:
        return None
    policy = aggregation_policy(definition, override)
    zeros = sum(1 for _, value in dated if value == 0)
    excluded_zeros = 0
    if policy["policy"] == "trend" and zeros and not any(value < 0 for _, value in dated):
        dated = [(day, value) for day, value in dated if value != 0]
        excluded_zeros = zeros
        if not dated:
            return None
    values = [value for _, value in dated]
    out: dict[str, Any] = {
        "name": definition.get("name"), "units": definition.get("units"), "n": len(values),
        "policy": policy["policy"], "primary": policy["primary"], "reason": policy["reason"],
        "zero_values": zeros - excluded_zeros, "zeros_excluded": excluded_zeros, "other_sport_values_ignored": foreign,
    }
    kind = policy["policy"]
    if kind == "none":
        return out
    if kind in ("sum", "device_load_sum"):
        out["sum"] = _round(sum(values))
        out["per_session_mean"] = _round(sum(values) / len(values))
        return out
    out.update(
        mean=_round(statistics.fmean(values)), median=_round(statistics.median(values)),
        min=_round(min(values)), max=_round(max(values)),
    )
    if kind == "trend":
        out.update(
            latest=_round(values[-1]), latest_date=dated[-1][0][:10],
            first=_round(values[0]), first_date=dated[0][0][:10],
            change=_round(values[-1] - values[0]) if len(values) > 1 else None,
        )
    return out


def start_end_pairs(defs: CustomFieldDefs) -> list[tuple[str, str, str]]:
    """(start code, end code, stem label) for fields named "<stem> at start" / "<stem> at end"."""
    starts: dict[tuple[str, ...], str] = {}
    ends: dict[tuple[str, ...], str] = {}
    for code, definition in defs.items():
        if definition.get("value_type") not in (None, "numeric"):
            continue
        words = _words(definition.get("name"))
        stem = tuple(w for w in words if w not in _START_WORDS | _END_WORDS | _FILLER_WORDS)
        if not stem:
            continue
        if set(words) & _START_WORDS:
            starts[stem] = code
        elif set(words) & _END_WORDS:
            ends[stem] = code
    return [(code, ends[stem], " ".join(stem)) for stem, code in starts.items() if stem in ends]


def pair_changes(activities: list[dict[str, Any]], defs: CustomFieldDefs) -> list[dict[str, Any]]:
    """Typical start-to-end change of paired fields over the activities where both exist."""
    out = []
    for start_code, end_code, stem in start_end_pairs(defs):
        changes = []
        for activity in activities:
            first, last = _number(activity.get(start_code)), _number(activity.get(end_code))
            if first is not None and last is not None:
                changes.append(last - first)
        if changes:
            out.append({
                "stem": stem, "start_code": start_code, "end_code": end_code, "n": len(changes),
                "units": defs[start_code].get("units"),
                "median_change": _round(statistics.median(changes)), "mean_change": _round(statistics.fmean(changes)),
                "largest_drop": _round(min(changes)),
            })
    return out


def aggregate_custom_fields(
    activities: list[dict[str, Any]], defs: CustomFieldDefs, overrides: dict[str, str] | None = None,
    assigned_by_type: AssignedByType | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate every numeric custom field present on the activities (code -> aggregate)."""
    out: dict[str, dict[str, Any]] = {}
    for code, definition in defs.items():
        if definition.get("value_type") not in (None, "numeric"):
            continue
        aggregate = aggregate_field(activities, code, definition, (overrides or {}).get(code), assigned_by_type)
        if aggregate is not None:
            out[code] = aggregate
    return out


def _num_text(value: Any) -> str:
    if value is None:
        return "n/a"
    rounded = round(float(value), 2)
    return str(int(rounded)) if rounded.is_integer() else str(rounded)


def format_aggregate(code: str, agg: dict[str, Any], show_reason: bool = False) -> str:
    """One compact text item for an aggregate (units appended, policy-specific wording)."""
    units = f" {agg['units']}" if agg.get("units") else ""
    head = f"{agg.get('name') or code} [{code}]"
    kind = agg["policy"]
    if kind == "none":
        text = f"{head}: no aggregate (n {agg['n']})"
    elif kind == "sum":
        text = f"{head} sum {_num_text(agg['sum'])}{units} (n {agg['n']}, {_num_text(agg['per_session_mean'])}{units}/session)"
    elif kind == "device_load_sum":
        text = f"{head} device load sum {_num_text(agg['sum'])}{units} (n {agg['n']}, device scale)"
    elif kind == "trend":
        change = f", change {agg['change']:+g}{units} since {agg['first_date']}" if agg.get("change") is not None else ""
        text = (
            f"{head} latest {_num_text(agg['latest'])}{units} ({agg['latest_date']}){change}, "
            f"mean {_num_text(agg['mean'])}, range {_num_text(agg['min'])}-{_num_text(agg['max'])}{units} (n {agg['n']})"
        )
    else:
        primary = agg.get("primary") or "mean"
        lead = f"{primary} {_num_text(agg[primary])}{units}"
        rest = [f"{k} {_num_text(agg[k])}" for k in ("mean", "median", "min", "max") if k != primary]
        text = f"{head} {lead} ({', '.join(rest)}{units}; n {agg['n']})"
    extras = []
    if agg.get("zeros_excluded"):
        extras.append(f"{agg['zeros_excluded']} stored 0 left out as 'no value'")
    if agg.get("other_sport_values_ignored"):
        extras.append(f"{agg['other_sport_values_ignored']} value(s) from sports without this field ignored")
    if extras:
        text += f" ({'; '.join(extras)})"
    if show_reason:
        text += f" [{agg['reason']}]"
    return text


def format_pair(pair: dict[str, Any]) -> str:
    """Text of a start-to-end change."""
    units = f" {pair['units']}" if pair.get("units") else ""
    return (
        f"{pair['stem']} start→end: median change {pair['median_change']:+g}{units}, "
        f"largest drop {pair['largest_drop']:+g}{units} (n {pair['n']} activities with both values)"
    )
