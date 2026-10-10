"""
Completeness of today's wellness record.

In the morning today's Intervals.icu wellness record is usually incomplete: the official device
sync delivers sleep, HRV and resting HR soon after the watch syncs, other values (sleeping HR,
respiration, SpO2, custom fields written by a sync bridge ...) arrive later in the day. Such a
value must be read as "not yet available", never as normal or 0.

``today_completeness`` compares today's record with the fields usually present: those with a
value on at least 80 % of the 14 days before today. Null, NaN, empty text and a stored 0 are no
value (a stored 0 is a placeholder, as everywhere in this project). Metadata and the values
Intervals.icu computes itself (ctl, atl, ramp rate ...) are not checked. The tools load the 14
days by widening the start of their wellness request (``completeness_start``).
"""

from datetime import date, datetime, timedelta
from typing import Any

from intervals_mcp_server.utils.custom_fields import CustomFieldDefs, is_missing
from intervals_mcp_server.utils.dates import athlete_local_time

REFERENCE_DAYS = 14
USUAL_SHARE = 0.8

# Not checked: metadata, flags and the values Intervals.icu computes itself.
NOT_CHECKED = frozenset({
    "id", "date", "updated", "locked", "tempWeight", "tempRestingHR", "sportInfo",
    "ctl", "atl", "rampRate", "ctlLoad", "atlLoad", "menstrualPhasePredicted",
})
# Native fields of the night and the morning (listed first) and day totals (listed last).
MORNING_FIELDS: dict[str, str] = {
    "restingHR": "resting HR", "hrv": "HRV", "hrvSDNN": "HRV SDNN", "avgSleepingHR": "sleeping HR",
    "sleepSecs": "sleep duration", "sleepScore": "sleep score", "sleepQuality": "sleep quality",
    "readiness": "readiness", "respiration": "respiration", "spO2": "SpO2", "baevskySI": "Baevsky stress index",
    "weight": "weight", "bodyFat": "body fat", "abdomen": "abdomen", "systolic": "systolic BP",
    "diastolic": "diastolic BP", "bloodGlucose": "blood glucose", "lactate": "lactate", "vo2max": "VO2max",
    "soreness": "soreness", "fatigue": "fatigue", "stress": "stress", "mood": "mood", "motivation": "motivation",
    "injury": "injury", "menstrualPhase": "menstrual phase", "comments": "comments",
}
DAY_TOTAL_FIELDS: dict[str, str] = {
    "steps": "steps", "floorsClimbed": "floors climbed", "kcalConsumed": "kcal consumed",
    "carbohydrates": "carbohydrates", "protein": "protein", "fatTotal": "fat",
    "hydrationVolume": "hydration volume", "hydration": "hydration score",
}
NOTE = (
    "The missing_usual_fields are not yet available today: treat them as missing, not as normal or 0; "
    "they usually arrive later in the day."
)
NO_HISTORY_NOTE = (
    f"No field has values on {USUAL_SHARE:.0%} of the {REFERENCE_DAYS} days before today; completeness unknown."
)


def has_value(value: Any) -> bool:
    """True for a stored value; null, NaN, empty text or lists and a stored 0 are no value."""
    if is_missing(value):
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value != 0
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
    return code in MORNING_FIELDS or code in DAY_TOTAL_FIELDS


def field_name(code: str, field_definitions: CustomFieldDefs | None = None) -> str:
    """Display name: the custom definition's name, the native label or the code."""
    definition = (field_definitions or {}).get(code) or {}
    return str(definition.get("name") or MORNING_FIELDS.get(code) or DAY_TOTAL_FIELDS.get(code) or code)


def _ordered(fields: set[str], field_definitions: CustomFieldDefs, seen: list[str]) -> list[str]:
    """Night/morning fields first, then custom fields (definition order), then day totals."""
    defined = {code: index for index, code in enumerate(field_definitions)}
    first_seen = {code: index for index, code in enumerate(seen)}
    custom = sorted(
        (code for code in fields if not is_native(code)),
        key=lambda code: (defined.get(code, len(defined)), first_seen.get(code, len(first_seen)), code),
    )
    return (
        [code for code in MORNING_FIELDS if code in fields] + custom
        + [code for code in DAY_TOTAL_FIELDS if code in fields]
    )


def today_completeness(
    entries: list[dict[str, Any]], today: date, field_definitions: CustomFieldDefs | None = None
) -> dict[str, Any]:
    """Usual fields that today's record does not have yet.

    *entries* must hold the records of today and the 14 days before it with all fields (others are
    ignored). Returns the date, whether today's record exists, its ``updated`` time (as stored and
    in the athlete's local time), how many fields are usual, the usual fields still missing today
    (code and display name, night/morning values first) and a note.
    """
    by_day = {_day(entry): entry for entry in entries if isinstance(entry, dict) and _day(entry)}
    counts: dict[str, int] = {}
    seen: list[str] = []
    for offset in range(1, REFERENCE_DAYS + 1):
        for key, value in (by_day.get((today - timedelta(days=offset)).isoformat()) or {}).items():
            if key in NOT_CHECKED or not has_value(value):
                continue
            if key not in counts:
                seen.append(key)
            counts[key] = counts.get(key, 0) + 1
    usual = {key for key, count in counts.items() if count >= USUAL_SHARE * REFERENCE_DAYS}
    record = by_day.get(today.isoformat())
    missing = _ordered({key for key in usual if not has_value((record or {}).get(key))}, field_definitions or {}, seen)
    moment = _parse_time(record.get("updated")) if record else None
    return {
        "date": today.isoformat(),
        "exists": record is not None,
        "updated": record.get("updated") if record else None,
        "updated_local": athlete_local_time(moment).isoformat(timespec="minutes") if moment else None,
        "reference_days": REFERENCE_DAYS,
        "usual_fields": len(usual),
        "missing_usual_fields": [{"field": code, "name": field_name(code, field_definitions)} for code in missing],
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


def completeness_line(info: dict[str, Any] | None, max_names: int | None = None, more_hint: str = "") -> str | None:
    """One line naming today's missing usual fields; None when nothing is missing.

    At most *max_names* names are listed (all by default); *more_hint* follows the count of the rest.
    """
    if not info:
        return None
    if info.get("error"):
        return f"Today {info['date']}: completeness not checked ({info['error']})."
    names = [item["name"] for item in info.get("missing_usual_fields") or []]
    if not names:
        return None
    shown = names if max_names is None else names[:max_names]
    listed = ", ".join(shown) + (f" and {len(names) - len(shown)} more{more_hint}" if len(names) > len(shown) else "")
    if not info.get("exists"):
        head = f"Today {info['date']} has no wellness record yet"
    else:
        clock = _clock(info.get("updated_local"), info["date"])
        head = f"Today {info['date']} is incomplete" + (f" (last updated {clock} local)" if clock else "")
    return (
        f"{head}: not yet available: {listed}. Treat them as missing, not as normal; "
        "they usually arrive later in the day."
    )
