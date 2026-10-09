"""
Custom item helpers for Intervals.icu MCP Server.

Athletes can define custom items on Intervals.icu (ACTIVITY_FIELD, INTERVAL_FIELD,
ACTIVITY_STREAM, INPUT_FIELD, ...). The API does not return their values in a
dedicated structure:

- custom activity fields are top-level keys of the activity JSON, named by the
  item's ``content.code``;
- custom interval fields are keys of each interval in ``icu_intervals``;
- custom streams are extra entries in the streams list whose ``type`` is the code;
- custom wellness fields (INPUT_FIELD) are keys of each wellness entry.

This module turns the custom item definitions (``/athlete/{id}/custom-item``) into
lookups keyed by code and renders values together with their technical code,
display name, units and, for select fields, the option label. Everything is
data-driven: whatever the athlete has defined is picked up, nothing is hard-coded
for a particular device or vendor.
"""

import json
import math
import re
from typing import Any

ACTIVITY_FIELD = "ACTIVITY_FIELD"
ACTIVITY_STREAM = "ACTIVITY_STREAM"
INTERVAL_FIELD = "INTERVAL_FIELD"
INPUT_FIELD = "INPUT_FIELD"

# code -> normalized definition
CustomFieldDefs = dict[str, dict[str, Any]]
# item type -> definitions
CustomItemIndex = dict[str, CustomFieldDefs]


def normalize_custom_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce a raw custom item to the attributes needed to render its values.

    Returns None for items without a ``content.code`` (charts, maps, panels ...).
    """
    content = item.get("content")
    if not isinstance(content, dict):
        return None
    code = content.get("code")
    if not code:
        return None
    options = content.get("options")
    units = content.get("units")
    return {
        "id": item.get("id"),
        "type": item.get("type"),
        "code": str(code),
        "name": item.get("name") or content.get("name") or str(code),
        "units": units.strip() if isinstance(units, str) and units.strip() else None,
        "value_type": content.get("type"),
        "options": options if isinstance(options, list) else None,
        "description": item.get("description") or content.get("short_description"),
        "fit_source": content.get("fit_session_field") or content.get("fit_record_field"),
        "has_script": bool(content.get("script")),
        "aggregate": content.get("aggregate"),
        "updated": item.get("updated"),
    }


def index_custom_items(items: Any) -> CustomItemIndex:
    """Group custom item definitions by item type and code.

    Accepts the raw ``/athlete/{id}/custom-item`` response. Anything that is not a
    list of items with a code is ignored, so an error payload yields an empty index.
    """
    index: CustomItemIndex = {}
    if not isinstance(items, list):
        return index
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = normalize_custom_item(item)
        if normalized is None:
            continue
        by_code = index.setdefault(str(normalized["type"]), {})
        by_code.setdefault(normalized["code"], normalized)
    return index


def is_missing(value: Any) -> bool:
    """True for null and NaN, which Intervals.icu uses for 'no value'.

    The API serialises NaN either as a bare token (parsed to float nan) or as the
    string "NaN"; both mean that no value is stored.
    """
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    return isinstance(value, str) and value.strip().lower() == "nan"


def format_value(value: Any) -> str:
    """Render a scalar as stored (only float noise beyond 6 decimals is trimmed)."""
    if is_missing(value):
        return "no value"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        rounded = round(value, 6)
        return str(int(rounded)) if rounded.is_integer() else str(rounded)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _same_number(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if math.isnan(left) or math.isnan(right):
            return False
        return float(left) == float(right)
    return False


def select_label(definition: dict[str, Any], value: Any) -> str | None:
    """Resolve the option text of a select field for the stored value."""
    for option in definition.get("options") or []:
        if not isinstance(option, dict):
            continue
        option_value = option.get("value")
        if option_value == value or _same_number(option_value, value):
            text = option.get("text")
            return str(text) if text is not None else None
    return None


def _is_zero(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def format_field_value(definition: dict[str, Any], value: Any) -> str:
    """Render a custom field value with its units, select label and a 'possibly missing' marker.

    Intervals.icu stores 0 for a field that is filled from the device file when the
    source field is absent from the file. Such a 0 cannot be told apart from a real
    measurement, so it is reported as stored but marked.
    """
    text = format_value(value)
    if is_missing(value):
        return text
    units = definition.get("units")
    if units:
        text = f"{text} {units}"
    label = select_label(definition, value)
    if label:
        text = f"{text} ({label})"
    return text


def value_status(_definition: dict[str, Any] | None, payload: dict[str, Any], code: str) -> str:
    """Classify a custom field on a payload: 'value', 'zero', 'missing' (null/NaN) or 'absent' (no key).

    The definition is accepted for symmetry with the other helpers (future per-type rules).
    """
    if code not in payload:
        return "absent"
    value = payload[code]
    if is_missing(value):
        return "missing"
    if _is_zero(value):
        return "zero"
    return "value"


def is_device_file_field(definition: dict[str, Any]) -> bool:
    """True when the field is filled from the device file (FIT field or script)."""
    return bool(definition.get("fit_source") or definition.get("has_script"))


def field_source(definition: dict[str, Any]) -> str:
    """Where the value comes from: 'fit:<field>', 'script' or 'manual/input'."""
    if definition.get("fit_source"):
        return f"fit:{definition['fit_source']}"
    if definition.get("has_script"):
        return "script"
    return "manual/input"


def custom_fields_json(
    payload: dict[str, Any], defs: CustomFieldDefs, assigned: set[str] | None = None
) -> list[dict[str, Any]]:
    """Machine-readable view of every defined custom field for a payload.

    Each entry carries code, name, value (null for missing/absent), units, select label,
    status ('value' | 'zero' | 'missing' | 'absent'), source and item type, so that a
    consumer can tell a stored 0 from a missing value and from an undefined field.
    """
    rows: list[dict[str, Any]] = []
    for code, definition in defs.items():
        status = value_status(definition, payload, code)
        value = payload.get(code)
        rows.append(
            {
                "code": code,
                "assigned_to_sport": None if assigned is None else code in assigned,
                "name": definition.get("name"),
                "value": None if status in ("absent", "missing") else value,
                "units": definition.get("units"),
                "units_source": definition.get("units_source", "definition"),
                "label": select_label(definition, value) if status in ("value", "zero") else None,
                "status": status,
                "zero_ambiguous": status == "zero" and is_device_file_field(definition),
                "source": field_source(definition),
                "item_type": definition.get("type"),
                "value_type": definition.get("value_type"),
            }
        )
    return rows


def apply_units_overrides(index: CustomItemIndex, overrides: dict[str, str]) -> CustomItemIndex:
    """Apply operator-configured display units (code -> units) to the definitions in place.

    Used for definitions that carry no or unspecific units (e.g. a stream defined with
    units 'point' that is known to be a percentage). The override is marked with
    units_source='override' so outputs can show where the unit came from.
    """
    if not overrides:
        return index
    for defs in index.values():
        for code, definition in defs.items():
            if code in overrides:
                definition["units"] = overrides[code]
                definition["units_source"] = "override"
    return index


_TEMPERATURE_UNITS = {"°c": "°C", "c": "°C", "celsius": "°C", "°f": "°F", "f": "°F", "fahrenheit": "°F"}


def _is_temperature(definition: dict[str, Any]) -> bool:
    text = f"{definition.get('name') or ''} {definition.get('code') or ''}".lower()
    return "temperature" in text or bool(re.search(r"(^|[^a-z])temp([^a-z]|$)", text))


def infer_temperature_units(index: CustomItemIndex) -> CustomItemIndex:
    """Give numeric temperature fields without units the unit of the other temperature fields.

    A device bridge may define "Min. Temperature" and "Avg. Temperature" with °C but leave the
    unit of "Max. Temperature" empty. When all temperature fields of an item type that carry a
    unit agree on it, the ones without a unit get it, marked units_source='inferred'. Nothing
    is assumed when no temperature field has a unit or the units differ.
    """
    for defs in index.values():
        temperatures = [d for d in defs.values() if d.get("value_type") in (None, "numeric") and _is_temperature(d)]
        known = {_TEMPERATURE_UNITS.get(str(d.get("units")).strip().lower()) for d in temperatures if d.get("units")}
        if len(known) != 1 or None in known:
            continue
        unit = known.pop()
        for definition in temperatures:
            if not definition.get("units"):
                definition["units"] = unit
                definition["units_source"] = "inferred"
    return index


def assigned_codes(defs: CustomFieldDefs, field_ids: Any) -> set[str] | None:
    """Codes of the definitions whose item id is listed in a sport setting's activity_field_ids.

    Returns None when the setting lists no fields (unknown / no restriction), so callers can
    tell "not assigned" from "no information".
    """
    if not isinstance(field_ids, list) or not field_ids:
        return None
    ids = {str(i) for i in field_ids}
    return {code for code, definition in defs.items() if str(definition.get("id")) in ids}


def format_custom_field_lines(
    payload: dict[str, Any], defs: CustomFieldDefs, prefix: str = "- ", only: set[str] | None = None
) -> list[str]:
    """One line per custom field present on the payload, in definition order.

    ``only`` restricts the output to the given codes (e.g. the fields assigned to the sport).
    """
    lines: list[str] = []
    for code, definition in defs.items():
        if code not in payload or (only is not None and code not in only):
            continue
        rendered = format_field_value(definition, payload[code])
        lines.append(f"{prefix}{definition['name']} [{code}]: {rendered}")
    return lines


def format_custom_activity_fields(
    activity: dict[str, Any], defs: CustomFieldDefs, assigned: set[str] | None = None
) -> str:
    """Render the 'Custom Activity Fields' section of an activity.

    When ``assigned`` (the codes configured for the activity's sport in Intervals.icu) is
    known, fields outside that set are listed separately so that e.g. running metrics on a
    ride are not mistaken for sport-relevant values.
    """
    lines = ["Custom Activity Fields:"]
    if not defs:
        lines.append("- (no custom activity field definitions found for this athlete)")
        return "\n".join(lines)

    field_lines = format_custom_field_lines(activity, defs, only=assigned)
    other_lines = format_custom_field_lines(activity, defs) if assigned is None else [
        line for line in format_custom_field_lines(activity, defs, prefix="") if line.split(" [")[0] not in {
            definition["name"] for code, definition in defs.items() if code in assigned
        }
    ]
    if not field_lines and not other_lines:
        lines.append(
            f"- (none of the {len(defs)} defined custom activity fields is present on this activity)"
        )
        return "\n".join(lines)

    lines.extend(field_lines)
    if assigned is not None and other_lines:
        lines.append(
            "Not assigned to this sport in the Intervals.icu settings (values as stored, treat with care): "
            + "; ".join(other_lines)
        )
    absent = sorted(code for code in defs if code not in activity)
    if absent:
        lines.append(f"Defined but not present on this activity: {', '.join(absent)}")
    ambiguous = sorted(
        code
        for code, definition in defs.items()
        if value_status(definition, activity, code) == "zero" and is_device_file_field(definition)
    )
    if ambiguous:
        lines.append(
            "Zero values in device-file fields (a real 0 or an absent source field; Intervals.icu "
            "stores 0 for both, so treat with care): " + ", ".join(ambiguous)
        )
    lines.append("Note: 'no value' = null/NaN in Intervals.icu; values are reported exactly as stored.")
    return "\n".join(lines)
