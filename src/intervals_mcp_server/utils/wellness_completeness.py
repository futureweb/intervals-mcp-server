"""
Completeness of today's wellness record.

In the morning today's Intervals.icu wellness record is usually incomplete: the official device
sync delivers sleep, HRV and resting HR soon after the watch syncs, other values (sleeping HR,
respiration, SpO2, custom fields written by a sync bridge ...) arrive later in the day. Such a
value must be read as "not yet available", never as normal or 0.

``today_completeness`` compares today's record with the fields usually present: those with a
value on at least 80 % of the 14 days before today. Null, NaN and empty text are no value; a
stored 0 is a placeholder (as everywhere in this project) except for signed fields, where 0 is
a real value: a field with a negative value in those 14 days, or whose code, name or description
says deviation, delta or change (e.g. a skin temperature deviation of 0.0 °C). Metadata and the
values Intervals.icu computes itself (ctl, atl, ramp rate ...) are not checked. The missing fields
are split into three groups: night/morning values (late when missing in the morning; values of the
night such as sleep, HRV or skin temperature first), daily metrics (device estimates computed once a
day such as VO2max, endurance and hill score, fitness age, race predictions or the acute training
load; they arrive with a device sync, not with the night) and day totals (steps, calories ...,
normally complete in the evening). The tools load the 14 days by widening the start of their
wellness request (``completeness_start``).
"""

import re
from datetime import date, datetime, timedelta
from typing import Any

from intervals_mcp_server.utils.custom_fields import CustomFieldDefs, is_missing
from intervals_mcp_server.utils.dates import athlete_local_time

REFERENCE_DAYS = 14
USUAL_SHARE = 0.8
NIGHT_MORNING = "night_morning"
DAILY_METRIC = "daily_metric"
DAY_TOTAL = "day_total"
# Order of the groups in the lists and lines.
GROUP_ORDER = {NIGHT_MORNING: 0, DAILY_METRIC: 1, DAY_TOTAL: 2}

# Not checked: metadata, flags and the values Intervals.icu computes itself.
NOT_CHECKED = frozenset({
    "id", "date", "updated", "locked", "tempWeight", "tempRestingHR", "sportInfo",
    "ctl", "atl", "rampRate", "ctlLoad", "atlLoad", "menstrualPhasePredicted",
})
# Native fields of the night and the morning (listed first), daily metrics and day totals (listed last).
MORNING_FIELDS: dict[str, str] = {
    "restingHR": "resting HR", "hrv": "HRV", "hrvSDNN": "HRV SDNN", "avgSleepingHR": "sleeping HR",
    "sleepSecs": "sleep duration", "sleepScore": "sleep score", "sleepQuality": "sleep quality",
    "readiness": "readiness", "respiration": "respiration", "spO2": "SpO2", "baevskySI": "Baevsky stress index",
    "weight": "weight", "bodyFat": "body fat", "abdomen": "abdomen", "systolic": "systolic BP",
    "diastolic": "diastolic BP", "bloodGlucose": "blood glucose", "lactate": "lactate",
    "soreness": "soreness", "fatigue": "fatigue", "stress": "stress", "mood": "mood", "motivation": "motivation",
    "injury": "injury", "menstrualPhase": "menstrual phase", "comments": "comments",
}
DAILY_METRIC_FIELDS: dict[str, str] = {"vo2max": "VO2max"}
DAY_TOTAL_FIELDS: dict[str, str] = {
    "steps": "steps", "floorsClimbed": "floors climbed", "kcalConsumed": "kcal consumed",
    "carbohydrates": "carbohydrates", "protein": "protein", "fatTotal": "fat",
    "hydrationVolume": "hydration volume", "hydration": "hydration score",
}
# Custom daily metrics: device values computed once a day (by code or name), e.g. Garmin Endurance
# Score, Hill Score / Strength / Endurance, (achievable) Fitness Age, predicted race times, VO2max,
# the acute training load and daily goals (steps, hydration). They are not values of the night and
# not totals that grow during the day.
_DAILY_METRIC_WORDS = re.compile(
    r"endurance ?score|hill ?(?:score|strength|endurance)|fitness ?age|predict|race ?time|vo2 ?max|"
    r"acute ?(?:training ?)?load|training ?status|load ?focus|goal",
    re.IGNORECASE,
)
# Custom fields that add up over the day (by code, name or units), unless they belong to the night or are goals.
_DAY_TOTAL_WORDS = re.compile(r"calorie|kcal|steps|floors|intensity|active|sweat|hydration|drain|stress ?av", re.IGNORECASE)
_NOT_DAY_TOTAL_WORDS = re.compile(r"sleep|night|morning|goal", re.IGNORECASE)
# Custom values of the night (listed before other night/morning values such as scores or predictions).
_OVERNIGHT_WORDS = re.compile(r"sleep|night|hrv|skin|temp|spo2|respirat|readiness|resting|battery|recovery", re.IGNORECASE)
# Signed quantities, where a stored 0 is a real value.
_SIGNED_CODE = re.compile(r"deviation|delta|change", re.IGNORECASE)
_SIGNED_WORDS = re.compile(r"\b(deviation|delta|change)\b", re.IGNORECASE)
NOTE = (
    "The missing_usual_fields are not yet available today: treat them as missing, not as normal or 0; "
    "night_morning values usually arrive later in the morning or day, daily_metric values (device estimates "
    "computed once a day) arrive with a device sync, day_total values are normally complete in the evening."
)
NO_HISTORY_NOTE = (
    f"No field has values on {USUAL_SHARE:.0%} of the {REFERENCE_DAYS} days before today; completeness unknown."
)
TREAT = "Treat them as missing, not as normal; they usually arrive later in the day."


def has_value(value: Any, zero_is_value: bool = False) -> bool:
    """True for a stored value; null, NaN, empty text or lists are no value, nor is a stored 0
    unless *zero_is_value* (signed fields)."""
    if is_missing(value):
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return zero_is_value or value != 0
    if isinstance(value, (str, list, dict)):
        return bool(value.strip() if isinstance(value, str) else value)
    return True


def completeness_start(today: date) -> date:
    """First day the wellness request must include for the check of today."""
    return today - timedelta(days=REFERENCE_DAYS)


def _day(entry: dict[str, Any]) -> str:
    return str(entry.get("id") or entry.get("date") or "")[:10]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def is_native(code: str) -> bool:
    """True for a native Intervals.icu wellness field (not a custom field)."""
    return code in MORNING_FIELDS or code in DAILY_METRIC_FIELDS or code in DAY_TOTAL_FIELDS


def field_name(code: str, field_definitions: CustomFieldDefs | None = None) -> str:
    """Display name: the custom definition's name, the native label or the code."""
    definition = (field_definitions or {}).get(code) or {}
    return str(
        definition.get("name") or MORNING_FIELDS.get(code) or DAILY_METRIC_FIELDS.get(code)
        or DAY_TOTAL_FIELDS.get(code) or code
    )


def field_group(code: str, field_definitions: CustomFieldDefs | None = None) -> str:
    """NIGHT_MORNING, DAILY_METRIC (a device estimate computed once a day, e.g. VO2max or an
    endurance score) or DAY_TOTAL (a value that adds up over the day, e.g. steps or calories)."""
    if code in DAY_TOTAL_FIELDS:
        return DAY_TOTAL
    if code in DAILY_METRIC_FIELDS:
        return DAILY_METRIC
    if code in MORNING_FIELDS:
        return NIGHT_MORNING
    definition = (field_definitions or {}).get(code) or {}
    named = " ".join(str(part) for part in (code, definition.get("name")) if part)
    if _DAILY_METRIC_WORDS.search(named):
        return DAILY_METRIC
    text = " ".join(str(part) for part in (code, definition.get("name"), definition.get("units")) if part)
    return DAY_TOTAL if _DAY_TOTAL_WORDS.search(text) and not _NOT_DAY_TOTAL_WORDS.search(text) else NIGHT_MORNING


def _signed(code: str, values: list[Any], definition: dict[str, Any]) -> bool:
    """A signed quantity: a negative value in the window, or deviation/delta/change in code, name or description."""
    if any(isinstance(v, (int, float)) and not isinstance(v, bool) and v < 0 for v in values):
        return True
    words = " ".join(str(definition.get(key) or "") for key in ("name", "description"))
    return bool(_SIGNED_CODE.search(code) or _SIGNED_WORDS.search(words))


def _overnight(code: str, definition: dict[str, Any]) -> bool:
    return bool(_OVERNIGHT_WORDS.search(f"{code} {definition.get('name') or ''}"))


def _ordered(fields: set[str], field_definitions: CustomFieldDefs, seen: list[str]) -> list[str]:
    """Night/morning values, daily metrics, day totals; within a group native fields, then custom
    fields (values of the night first, then definition order)."""
    defined = {code: index for index, code in enumerate(field_definitions)}
    first_seen = {code: index for index, code in enumerate(seen)}
    custom = sorted(
        (code for code in fields if not is_native(code)),
        key=lambda code: (
            not _overnight(code, field_definitions.get(code) or {}), defined.get(code, len(defined)),
            first_seen.get(code, len(first_seen)), code,
        ),
    )
    ordered = (
        [code for code in MORNING_FIELDS if code in fields] + [code for code in DAILY_METRIC_FIELDS if code in fields]
        + custom + [code for code in DAY_TOTAL_FIELDS if code in fields]
    )
    return sorted(ordered, key=lambda code: GROUP_ORDER[field_group(code, field_definitions)])  # stable


def today_completeness(
    entries: list[dict[str, Any]], today: date, field_definitions: CustomFieldDefs | None = None
) -> dict[str, Any]:
    """Usual fields that today's record does not have yet.

    *entries* must hold the records of today and the 14 days before it with all fields (others are
    ignored). Returns the date, whether today's record exists, its ``updated`` time (as stored and
    in the athlete's local time), how many fields are usual, the usual fields still missing today
    (code, display name and group: night_morning, daily_metric or day_total) and a note.
    """
    defs = field_definitions or {}
    by_day = {_day(entry): entry for entry in entries if isinstance(entry, dict) and _day(entry)}
    window: dict[str, list[Any]] = {}
    for offset in range(1, REFERENCE_DAYS + 1):
        for key, value in (by_day.get((today - timedelta(days=offset)).isoformat()) or {}).items():
            if key not in NOT_CHECKED:
                window.setdefault(key, []).append(value)
    signed = {key for key, values in window.items() if _signed(key, values, defs.get(key) or {})}
    usual = {
        key for key, values in window.items()
        if sum(has_value(v, key in signed) for v in values) >= USUAL_SHARE * REFERENCE_DAYS
    }
    record = by_day.get(today.isoformat())
    missing = _ordered(
        {key for key in usual if not has_value((record or {}).get(key), key in signed)}, defs, list(window)
    )
    moment = _parse_time(record.get("updated")) if record else None
    return {
        "date": today.isoformat(),
        "exists": record is not None,
        "updated": record.get("updated") if record else None,
        "updated_local": athlete_local_time(moment).isoformat(timespec="minutes") if moment else None,
        "reference_days": REFERENCE_DAYS,
        "usual_fields": len(usual),
        "missing_usual_fields": [
            {"field": code, "name": field_name(code, defs), "group": field_group(code, defs)} for code in missing
        ],
        "note": NOTE if missing else (None if usual else NO_HISTORY_NOTE),
    }


def has_missing_custom_fields(info: dict[str, Any] | None) -> bool:
    """True when custom fields are missing (their display names need the definitions)."""
    return any(not is_native(item["field"]) for item in (info or {}).get("missing_usual_fields") or [])


def _clock(updated_local: str | None, day: str) -> str | None:
    moment = _parse_time(updated_local)
    if moment is None:
        return None
    return moment.strftime("%H:%M") if moment.date().isoformat() == day else moment.strftime("%Y-%m-%d %H:%M")


def _listed(names: list[str], max_names: int | None, more_hint: str) -> str:
    shown = names if max_names is None else names[:max_names]
    rest = len(names) - len(shown)
    return ", ".join(shown) + (f" and {rest} more{more_hint}" if rest else "")


def completeness_line(info: dict[str, Any] | None, max_names: int | None = None, more_hint: str = "") -> str | None:
    """One to three lines naming today's missing usual fields; None when nothing is missing.

    Night/morning values come first, then daily metrics (device estimates computed once a day) and
    day totals (normally complete in the evening), each group on its own line. At most *max_names*
    names per group are listed (all by default); *more_hint* follows the count of the rest.
    """
    if not info:
        return None
    if info.get("error"):
        return f"Today {info['date']}: completeness not checked ({info['error']})."
    missing = info.get("missing_usual_fields") or []
    groups = {
        group: [item["name"] for item in missing if (item.get("group") or NIGHT_MORNING) == group]
        for group in GROUP_ORDER
    }
    labels = {
        NIGHT_MORNING: "night/morning values not yet available",
        DAILY_METRIC: "daily metrics not yet available (device estimates computed once a day)",
        DAY_TOTAL: "day totals not yet available (normally complete in the evening)",
    }
    parts = [f"{labels[group]}: {_listed(names, max_names, more_hint)}" for group, names in groups.items() if names]
    if not parts:
        return None
    if not info.get("exists"):
        head = f"Today {info['date']} has no wellness record yet"
    else:
        clock = _clock(info.get("updated_local"), info["date"])
        head = f"Today {info['date']} is incomplete" + (f" (last updated {clock} local)" if clock else "")
    return f"{head}: {parts[0]}. {TREAT}" + "".join(f"\n{part[0].upper()}{part[1:]}." for part in parts[1:])
