"""
Formatting utilities for Intervals.icu MCP Server

This module contains formatting functions for handling data from the Intervals.icu API.
"""

# pylint: disable=too-many-lines

import json
from datetime import datetime
from typing import Any

from intervals_mcp_server.utils.types import WorkoutDoc

from intervals_mcp_server.utils.custom_fields import (
    CustomFieldDefs,
    format_custom_activity_fields,
    format_custom_field_lines,
    format_value,
    is_missing,
    select_label,
)
from intervals_mcp_server.utils.sports import (
    format_pace,
    format_start_times,
    format_zone_table,
    hms,
)
from intervals_mcp_server.utils.streams import format_range_metrics


class _KeyTracker(dict):
    """A dict wrapper that records which keys are accessed."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        self.accessed: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:
        self.accessed.add(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> Any:
        self.accessed.add(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            self.accessed.add(key)
        return super().__contains__(key)


def format_activity_summary(activity: dict[str, Any]) -> str:
    """Format an activity into a readable string."""
    # Local and UTC start are reported side by side (P0: timezone clarity).
    start_time = format_start_times(activity)

    rpe = activity.get("perceived_exertion", None)
    if rpe is None:
        rpe = activity.get("icu_rpe", "N/A")
    if isinstance(rpe, (int, float)):
        rpe = f"{rpe}/10"

    feel = activity.get("feel", "N/A")
    if isinstance(feel, int):
        feel = f"{feel}/5"

    # Gear (bike, shoes) - ICU activity payloads include the gear ID but not the
    # gear name (which lives in /athlete/{id}/gear). The tools.gear module
    # resolves the name and injects it as `_resolved_gear_name` before this
    # formatter runs. Prefer the resolved name; otherwise fall back to whatever
    # the raw payload provides (typically just an ID).
    resolved_name = activity.get("_resolved_gear_name")
    gear_raw = activity.get("gear")
    if resolved_name:
        gear_name = resolved_name
        if isinstance(gear_raw, dict):
            gear_id = gear_raw.get("id", activity.get("gear_id", "N/A"))
        else:
            gear_id = activity.get("gear_id", "N/A")
    elif isinstance(gear_raw, dict):
        gear_name = gear_raw.get("name") or gear_raw.get("display_name") or "N/A"
        gear_id = gear_raw.get("id", "N/A")
    else:
        gear_name = activity.get("gear_name", "N/A")
        gear_id = activity.get("gear_id", "N/A")

    tags = activity.get("tags") or []
    tags = ", ".join(tags) if isinstance(tags, list) else str(tags)

    # API sub_type is an upper-case enum (e.g. COMMUTE, RACE) or null
    sub_type = activity.get("sub_type")
    sub_type = sub_type.replace("_", " ").capitalize() if isinstance(sub_type, str) and sub_type else "None"

    return f"""
Activity: {activity.get("name", "Unnamed")}
ID: {activity.get("id", "N/A")}
Type: {activity.get("type", "Unknown")}
Sub-type: {sub_type}
Date: {start_time}
Tags: {tags or "None"}
Description: {activity.get("description", "N/A")}
Distance: {activity.get("distance", 0)} meters
Duration: {activity.get("duration", activity.get("elapsed_time", 0))} seconds
Moving Time: {activity.get("moving_time", "N/A")} seconds
Elevation Gain: {activity.get("elevationGain", activity.get("total_elevation_gain", 0))} meters
Elevation Loss: {activity.get("total_elevation_loss", "N/A")} meters

Power Data:
Average Power: {activity.get("avgPower", activity.get("icu_average_watts", activity.get("average_watts", "N/A")))} watts
Weighted Avg Power: {activity.get("icu_weighted_avg_watts", "N/A")} watts
Training Load (Intervals.icu): {activity.get("trainingLoad", activity.get("icu_training_load", "N/A"))}
FTP: {activity.get("icu_ftp", "N/A")} watts
Kilojoules: {activity.get("icu_joules", "N/A")}
Intensity: {activity.get("icu_intensity", "N/A")}
Power:HR Ratio: {activity.get("icu_power_hr", "N/A")}
Variability Index: {activity.get("icu_variability_index", "N/A")}

Heart Rate Data:
Average Heart Rate: {activity.get("avgHr", activity.get("average_heartrate", "N/A"))} bpm
Max Heart Rate: {activity.get("max_heartrate", "N/A")} bpm
LTHR: {activity.get("lthr", "N/A")} bpm
Resting HR: {activity.get("icu_resting_hr", "N/A")} bpm
Decoupling: {activity.get("decoupling", "N/A")}

Other Metrics:
Cadence: {activity.get("average_cadence", "N/A")} rpm
Calories burned: {activity.get("calories", "N/A")} kcal
Average Speed: {activity.get("average_speed", "N/A")} m/s
Max Speed: {activity.get("max_speed", "N/A")} m/s
Average Stride: {activity.get("average_stride", "N/A")}
L/R Balance: {activity.get("avg_lr_balance", "N/A")}
Weight: {activity.get("icu_weight", "N/A")} kg
RPE: {rpe}
Session RPE: {activity.get("session_rpe", "N/A")}
Feel: {feel}

Environment:
Trainer: {activity.get("trainer", "N/A")}
Average Temp: {activity.get("average_temp", "N/A")}°C
Min Temp: {activity.get("min_temp", "N/A")}°C
Max Temp: {activity.get("max_temp", "N/A")}°C
Avg Wind Speed: {activity.get("average_wind_speed", "N/A")} km/h
Headwind %: {activity.get("headwind_percent", "N/A")}%
Tailwind %: {activity.get("tailwind_percent", "N/A")}%

Training Metrics (Intervals.icu; device loads such as a Garmin training load are custom fields):
Fitness (CTL): {activity.get("icu_ctl", "N/A")}
Fatigue (ATL): {activity.get("icu_atl", "N/A")}
TRIMP: {activity.get("trimp", "N/A")}
Polarization Index: {activity.get("polarization_index", "N/A")}
Power Load (Intervals.icu): {activity.get("power_load", "N/A")}
HR Load (Intervals.icu): {activity.get("hr_load", "N/A")}
Pace Load (Intervals.icu): {activity.get("pace_load", "N/A")}
Efficiency Factor: {activity.get("icu_efficiency_factor", "N/A")}

Device Info:
Device: {activity.get("device_name", "N/A")}
Power Meter: {activity.get("power_meter", "N/A")}
File Type: {activity.get("file_type", "N/A")}

Gear:
Name: {gear_name}
ID: {gear_id}
"""


def _format_activity_zones(activity: dict[str, Any]) -> str:
    """Render the zones block for payloads that carry a 'zones' object."""
    if "zones" not in activity:
        return ""
    zones = activity["zones"]
    text = "\nPower Zones:\n"
    for zone in zones.get("power", []):
        text += f"Zone {zone.get('number')}: {zone.get('secondsInZone')} seconds\n"
    text += "\nHeart Rate Zones:\n"
    for zone in zones.get("hr", []):
        text += f"Zone {zone.get('number')}: {zone.get('secondsInZone')} seconds\n"
    return text


# Payload keys that are never useful in a text view.
_ACTIVITY_OTHER_FIELDS_SKIP = {"skyline_chart_bytes"}
_OTHER_FIELD_MAX_CHARS = 400


def _format_activity_other_fields(activity: _KeyTracker) -> list[str]:
    """Every non-empty payload field that the standard sections did not render."""
    lines: list[str] = []
    for key, value in activity.items():
        if key in activity.accessed or key in _ACTIVITY_OTHER_FIELDS_SKIP or key.startswith("_"):
            continue
        if is_missing(value):
            continue
        text = format_value(value)
        if len(text) > _OTHER_FIELD_MAX_CHARS:
            text = f"{text[:_OTHER_FIELD_MAX_CHARS]}... (truncated, {len(text)} chars)"
        lines.append(f"- {key}: {text}")
    return lines


def _format_thresholds(activity: dict[str, Any]) -> str:  # pylint: disable=too-many-branches
    """Thresholds, zones and power source stored with the activity (historical snapshot)."""
    lines: list[str] = []
    ftp = activity.get("icu_ftp")
    if ftp is not None:
        parts = [f"FTP {ftp} W (icu_ftp, setting at the time)"]
        if activity.get("icu_rolling_ftp") is not None:
            parts.append(f"eFTP {activity['icu_rolling_ftp']} W (icu_rolling_ftp, Intervals.icu estimate)")
        if activity.get("icu_pm_ftp") is not None:
            parts.append(f"power-model FTP {activity['icu_pm_ftp']} W")
        lines.append("- Power: " + ", ".join(parts))
    if activity.get("icu_pm_cp") is not None or activity.get("icu_w_prime") is not None:
        lines.append(
            f"- Power model: CP {activity.get('icu_pm_cp', 'n/a')} W, W' {activity.get('icu_w_prime', 'n/a')} J "
            f"(model W' {activity.get('icu_pm_w_prime', 'n/a')} J), Pmax {activity.get('icu_pm_p_max', 'n/a')} W"
        )
    if activity.get("lthr") is not None or activity.get("athlete_max_hr") is not None:
        lines.append(
            f"- Heart rate: LTHR {activity.get('lthr', 'n/a')} bpm, max HR {activity.get('athlete_max_hr', 'n/a')} bpm, "
            f"resting HR {activity.get('icu_resting_hr', 'n/a')} bpm"
        )
    if activity.get("threshold_pace") is not None:
        lines.append(
            f"- Threshold pace: {activity['threshold_pace']} m/s = {format_pace(activity['threshold_pace'])}"
        )
    if activity.get("icu_weight") is not None:
        lines.append(f"- Weight: {activity['icu_weight']} kg")
    zones = format_zone_table("power", activity.get("icu_power_zones"), None, ftp=ftp)
    if zones:
        lines.append("- Power zones (% FTP, upper bounds): " + zones)
    zones = format_zone_table("hr", activity.get("icu_hr_zones"), None)
    if zones:
        lines.append("- HR zones (bpm, upper bounds): " + zones)
    zones = format_zone_table("pace", activity.get("pace_zones"), None, threshold_pace=activity.get("threshold_pace"))
    if zones:
        lines.append("- Pace zones (% threshold pace, upper bounds): " + zones)
    source = [
        f"{label} {activity[key]}"
        for key, label in (
            ("device_name", "device"),
            ("power_meter", "power meter"),
            ("power_meter_serial", "serial"),
            ("power_meter_battery", "battery"),
            ("power_field", "power field"),
        )
        if activity.get(key) not in (None, "")
    ]
    if activity.get("power_field_names"):
        source.append(f"power fields {', '.join(str(p) for p in activity['power_field_names'])}")
    if source:
        lines.append("- Power/device source: " + ", ".join(source))
    if not lines:
        return ""
    return "\nThresholds used for this activity (snapshot stored with the activity):\n" + "\n".join(lines) + "\n"


_RUN_DYNAMICS = (
    ("average_stance_time", "Ground contact time", "ms"),
    ("average_stance_time_balance", "GCT balance", "%"),
    ("average_stance_time_percent", "GCT percent", "%"),
    ("average_vertical_oscillation", "Vertical oscillation", "mm"),
    ("average_vertical_ratio", "Vertical ratio", "%"),
    ("average_step_length", "Step length", "mm"),
    ("average_leg_spring_stiffness", "Leg spring stiffness", "kN/m"),
    ("average_impact_loading_rate", "Impact loading rate", ""),
)


def format_running_dynamics(payload: dict[str, Any], indent: str = "") -> str:
    """Pace, GAP, cadence and running dynamics of an activity or interval (only present values)."""
    lines: list[str] = []
    speed = payload.get("average_speed")
    if isinstance(speed, (int, float)) and speed > 0:
        lines.append(f"{indent}Pace: {format_pace(speed)} ({speed:.3f} m/s)")
    gap = payload.get("gap")
    if isinstance(gap, (int, float)) and gap > 0:
        lines.append(f"{indent}GAP (grade adjusted pace): {format_pace(gap)} ({gap:.3f} m/s)")
    cadence = payload.get("average_cadence")
    if isinstance(cadence, (int, float)) and cadence > 0 and payload.get("average_step_length"):
        lines.append(f"{indent}Cadence: {cadence:.1f} rpm as stored (x2 = {cadence * 2:.0f} steps/min)")
    for key, label, units in _RUN_DYNAMICS:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not is_missing(value):
            lines.append(f"{indent}{label}: {format_value(value)}{(' ' + units) if units else ''}")
    if len(lines) <= 1 and not payload.get("average_step_length"):
        return ""
    return "\n".join(lines)


def format_activity_details(
    activity: dict[str, Any],
    custom_field_defs: CustomFieldDefs | None = None,
    include_all_fields: bool = False,
) -> str:
    """Detailed activity view: summary, zones, custom fields and optionally every other field.

    Args:
        activity: The raw activity payload from the Intervals.icu API
        custom_field_defs: ACTIVITY_FIELD definitions keyed by code. Custom fields present
            on the activity are rendered with name, code, value and units; None skips the section
        include_all_fields: Append every other non-empty payload field under "Other Fields"
    """
    data: dict[str, Any] = _KeyTracker(activity) if include_all_fields else activity
    view = format_activity_summary(data) + _format_activity_zones(data)
    view += _format_thresholds(data)
    dynamics = format_running_dynamics(data)
    if dynamics:
        view += "\nRunning Dynamics:\n" + dynamics + "\n"
    if custom_field_defs is not None:
        view += "\n" + format_custom_activity_fields(data, custom_field_defs) + "\n"
    if isinstance(data, _KeyTracker):
        other = _format_activity_other_fields(data)
        if other:
            view += "\nOther Fields:\n" + "\n".join(other) + "\n"
    return view


def format_workout(workout: dict[str, Any]) -> str:
    """Format a workout into a readable string."""
    return f"""
Workout: {workout.get("name", "Unnamed")}
Description: {workout.get("description", "No description")}
Sport: {workout.get("sport", "Unknown")}
Duration: {workout.get("duration", 0)} seconds
TSS: {workout.get("tss", "N/A")}
Intervals: {len(workout.get("intervals", []))}
"""


def _format_training_metrics(entries: dict[str, Any]) -> list[str]:
    """Format training metrics section."""
    training_metrics = []
    for k, label in [
        ("ctl", "Fitness (CTL)"),
        ("atl", "Fatigue (ATL)"),
        ("rampRate", "Ramp Rate"),
        ("ctlLoad", "CTL Load"),
        ("atlLoad", "ATL Load"),
    ]:
        if entries.get(k) is not None:
            training_metrics.append(f"- {label}: {entries[k]}")
    return training_metrics


def _format_sport_info(entries: dict[str, Any]) -> list[str]:
    """Format sport-specific info section."""
    sport_info_list = []
    if entries.get("sportInfo"):
        for sport in entries.get("sportInfo", []):
            if isinstance(sport, dict) and sport.get("eftp") is not None:
                sport_info_list.append(f"- {sport.get('type')}: eFTP = {sport['eftp']}")
    return sport_info_list


def _format_vital_signs(entries: dict[str, Any]) -> list[str]:
    """Format vital signs section."""
    vital_signs = []
    for k, label, unit in [
        ("weight", "Weight", "kg"),
        ("restingHR", "Resting HR", "bpm"),
        ("hrv", "HRV", ""),
        ("hrvSDNN", "HRV SDNN", ""),
        ("avgSleepingHR", "Average Sleeping HR", "bpm"),
        ("spO2", "SpO2", "%"),
        ("systolic", "Systolic BP", ""),
        ("diastolic", "Diastolic BP", ""),
        ("respiration", "Respiration", "breaths/min"),
        ("bloodGlucose", "Blood Glucose", "mmol/L"),
        ("lactate", "Lactate", "mmol/L"),
        ("vo2max", "VO2 Max", "ml/kg/min"),
        ("bodyFat", "Body Fat", "%"),
        ("abdomen", "Abdomen", "cm"),
        ("baevskySI", "Baevsky Stress Index", ""),
    ]:
        if entries.get(k) is not None:
            value = entries[k]
            if k == "systolic" and entries.get("diastolic") is not None:
                vital_signs.append(
                    f"- Blood Pressure: {entries['systolic']}/{entries['diastolic']} mmHg"
                )
            elif k not in ("systolic", "diastolic"):
                vital_signs.append(f"- {label}: {value}{(' ' + unit) if unit else ''}")
    return vital_signs


def _format_sleep_recovery(entries: dict[str, Any]) -> list[str]:
    """Format sleep and recovery section."""
    sleep_lines = []
    sleep_hours = None
    if entries.get("sleepSecs") is not None:
        sleep_hours = f"{entries['sleepSecs'] / 3600:.2f}"
    elif entries.get("sleepHours") is not None:
        sleep_hours = f"{entries['sleepHours']}"
    if sleep_hours is not None:
        sleep_lines.append(f"  Sleep: {sleep_hours} hours")

    if entries.get("sleepQuality") is not None:
        quality_value = entries["sleepQuality"]
        quality_labels = {1: "Great", 2: "Good", 3: "Average", 4: "Poor"}
        quality_text = quality_labels.get(quality_value, str(quality_value))
        sleep_lines.append(f"  Sleep Quality: {quality_value} ({quality_text})")

    if entries.get("sleepScore") is not None:
        sleep_lines.append(f"  Device Sleep Score: {entries['sleepScore']}/100")

    if entries.get("readiness") is not None:
        # Device readiness scores (e.g. Garmin Training Readiness) are 0-100; report as stored.
        sleep_lines.append(f"  Readiness: {entries['readiness']}")

    return sleep_lines


def _format_menstrual_tracking(entries: dict[str, Any]) -> list[str]:
    """Format menstrual tracking section."""
    menstrual_lines = []
    if entries.get("menstrualPhase") is not None:
        menstrual_lines.append(f"  Menstrual Phase: {str(entries['menstrualPhase']).capitalize()}")
    if entries.get("menstrualPhasePredicted") is not None:
        menstrual_lines.append(
            f"  Predicted Phase: {str(entries['menstrualPhasePredicted']).capitalize()}"
        )
    return menstrual_lines


# Value labels of the 1-4 subjective wellness scales in Intervals.icu.
_LEVEL_LABELS = {1: "Low", 2: "Avg", 3: "High", 4: "Extreme"}
SUBJECTIVE_SCALE_LABELS: dict[str, dict[int, str]] = {
    "soreness": _LEVEL_LABELS,
    "fatigue": _LEVEL_LABELS,
    "stress": _LEVEL_LABELS,
    "mood": {1: "Great", 2: "Good", 3: "OK", 4: "Grumpy"},
    "motivation": {1: "Extreme", 2: "High", 3: "Avg", 4: "Low"},
    "injury": {1: "None", 2: "Niggle", 3: "Poor", 4: "Injured"},
}


def _format_subjective_feelings(entries: dict[str, Any]) -> list[str]:
    """Format subjective feelings section."""
    subjective_lines = []
    for k, label in [
        ("soreness", "Soreness"),
        ("fatigue", "Fatigue"),
        ("stress", "Stress"),
        ("mood", "Mood"),
        ("motivation", "Motivation"),
        ("injury", "Injury Level"),
    ]:
        if entries.get(k) is not None:
            value = entries[k]
            text = SUBJECTIVE_SCALE_LABELS[k].get(value) if isinstance(value, int) else None
            subjective_lines.append(
                f"  {label}: {value} ({text})" if text else f"  {label}: {value}"
            )
    return subjective_lines


def _format_nutrition_hydration(entries: dict[str, Any]) -> list[str]:
    """Format nutrition and hydration section.

    Handles both legacy fields (kcalConsumed, hydrationVolume) and the native
    macro fields from the Intervals.icu API (carbohydrates, protein,
    fatTotal). All fields are rendered conditionally — a null/missing value
    hides the corresponding line for backward compatibility with older
    wellness records.
    """
    nutrition_lines = []
    for k, label, unit in [
        ("kcalConsumed", "Calories Consumed", ""),
        ("carbohydrates", "Carbohydrates", "g"),
        ("protein", "Protein", "g"),
        ("fatTotal", "Fat", "g"),
        ("hydrationVolume", "Hydration Volume", ""),
    ]:
        if entries.get(k) is not None:
            suffix = f" {unit}" if unit else ""
            nutrition_lines.append(f"- {label}: {entries[k]}{suffix}")

    if entries.get("hydration") is not None:
        nutrition_lines.append(f"  Hydration Score: {entries['hydration']}")

    return nutrition_lines


def _format_other_fields(
    entries: dict[str, Any],
    known_keys: set[str],
    field_definitions: CustomFieldDefs | None = None,
) -> list[str]:
    """Format any fields not already handled by the standard formatting sections.

    When custom item definitions are given, a field whose key matches a definition
    code is annotated with its display name, units and (select fields) option label.
    """
    other_lines = []
    for key, value in entries.items():
        if key in known_keys or value is None:
            continue
        text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        definition = (field_definitions or {}).get(key)
        if definition:
            meta = [
                part
                for part in (definition.get("name"), definition.get("units"), select_label(definition, value))
                if part and part != key
            ]
            if meta:
                text += f" ({', '.join(meta)})"
        other_lines.append(f"- {key}: {text}")
    return other_lines


def format_wellness_entry(
    entries: dict[str, Any],
    include_all_fields: bool = False,
    field_definitions: CustomFieldDefs | None = None,
) -> str:
    """Format wellness entry data into a readable string.

    Formats various wellness metrics including training metrics, vital signs,
    sleep data, menstrual tracking, subjective feelings, nutrition, and activity.

    Args:
        entries: Dictionary containing wellness data fields such as:
            - Training metrics: ctl, atl, rampRate, ctlLoad, atlLoad
            - Vital signs: weight, restingHR, hrv, hrvSDNN, avgSleepingHR, spO2,
              systolic, diastolic, respiration, bloodGlucose, lactate, vo2max,
              bodyFat, abdomen, baevskySI
            - Sleep: sleepSecs, sleepHours, sleepQuality, sleepScore, readiness
            - Menstrual: menstrualPhase, menstrualPhasePredicted
            - Subjective: soreness, fatigue, stress, mood, motivation, injury
            - Nutrition: kcalConsumed, carbohydrates, protein, fatTotal, hydrationVolume, hydration
            - Activity: steps
            - Other: comments, locked, date
        include_all_fields: If True, any fields not covered by the standard
            sections are appended under an "Other Fields" heading (default False).
        field_definitions: INPUT_FIELD definitions keyed by code, used to label custom
            wellness fields in "Other Fields" with name and units (optional).

    Returns:
        A formatted string representation of the wellness entry.
    """
    if include_all_fields:
        entries = _KeyTracker(entries)
        # Mark metadata/internal keys so they don't appear in "Other Fields"
        entries.get("date")
        entries.get("updated")
        entries.get("tempWeight")
        entries.get("tempRestingHR")

    lines = ["Wellness Data:"]
    lines.append(f"Date: {entries.get('id', 'N/A')}")
    lines.append("")

    training_metrics = _format_training_metrics(entries)
    if training_metrics:
        lines.append("Training Metrics:")
        lines.extend(training_metrics)
        lines.append("")

    sport_info_list = _format_sport_info(entries)
    if sport_info_list:
        lines.append("Sport-Specific Info:")
        lines.extend(sport_info_list)
        lines.append("")

    vital_signs = _format_vital_signs(entries)
    if vital_signs:
        lines.append("Vital Signs:")
        lines.extend(vital_signs)
        lines.append("")

    sleep_lines = _format_sleep_recovery(entries)
    if sleep_lines:
        lines.append("Sleep & Recovery:")
        lines.extend(sleep_lines)
        lines.append("")

    menstrual_lines = _format_menstrual_tracking(entries)
    if menstrual_lines:
        lines.append("Menstrual Tracking:")
        lines.extend(menstrual_lines)
        lines.append("")

    subjective_lines = _format_subjective_feelings(entries)
    if subjective_lines:
        lines.append("Subjective Feelings:")
        lines.extend(subjective_lines)
        lines.append("")

    nutrition_lines = _format_nutrition_hydration(entries)
    if nutrition_lines:
        lines.append("Nutrition & Hydration:")
        lines.extend(nutrition_lines)
        lines.append("")

    if entries.get("steps") is not None:
        lines.append("Activity:")
        lines.append(f"- Steps: {entries['steps']}")
        lines.append("")

    if entries.get("comments"):
        lines.append(f"Comments: {entries['comments']}")
    if "locked" in entries:
        lines.append(f"Status: {'Locked' if entries.get('locked') else 'Unlocked'}")

    if include_all_fields and isinstance(entries, _KeyTracker):
        other_lines = _format_other_fields(entries, entries.accessed, field_definitions)
        if other_lines:
            lines.append("")
            lines.append("Other Fields:")
            lines.extend(other_lines)

    return "\n".join(lines)


_EVENT_CATEGORY_LABELS = {
    "WORKOUT": "Workout",
    "RACE_A": "Race A",
    "RACE_B": "Race B",
    "RACE_C": "Race C",
    "NOTE": "Note",
    "PLAN": "Plan",
    "HOLIDAY": "Holiday",
    "SICK": "Sick",
    "INJURED": "Injured",
    "SET_EFTP": "Set eFTP",
    "SET_FITNESS": "Set fitness",
    "FITNESS_DAYS": "Fitness days",
    "SEASON_START": "Season start",
    "TARGET": "Target",
}


def event_type_label(event: dict[str, Any]) -> str:
    """Category (workout, race, note ...) and sport of an event, e.g. 'Workout (Ride)'.

    Falls back to the legacy workout/race keys for payloads without a category.
    """
    category = event.get("category")
    sport = event.get("type")
    if category:
        label = _EVENT_CATEGORY_LABELS.get(str(category), str(category).replace("_", " ").title())
        return f"{label} ({sport})" if sport else label
    if event.get("workout"):
        return f"Workout ({sport})" if sport else "Workout"
    if event.get("race"):
        return f"Race ({sport})" if sport else "Race"
    return f"Other ({sport})" if sport else "Other"


def _event_extra_lines(event: dict[str, Any]) -> list[str]:
    """Planned time, load, pairing and indoor flag of an event (only when present)."""
    lines: list[str] = []
    if event.get("category") == "WORKOUT" or event.get("moving_time") is not None:
        if event.get("moving_time") is not None:
            lines.append(f"Planned Time: {hms(event['moving_time'])}")
        if event.get("distance"):
            lines.append(f"Planned Distance: {event['distance']} m")
        if event.get("icu_training_load") is not None:
            lines.append(f"Planned Load (Intervals.icu): {event['icu_training_load']}")
    if event.get("paired_activity_id"):
        lines.append(f"Paired Activity: {event['paired_activity_id']}")
    if event.get("indoor"):
        lines.append("Indoor: yes")
    return lines


def format_event_summary(event: dict[str, Any]) -> str:
    """Format a basic event summary into a readable string."""

    # Update to check for "date" if "start_date_local" is not provided
    event_date = event.get("start_date_local", event.get("date", "Unknown"))
    event_name = event.get("name", "Unnamed")
    event_id = event.get("id", "N/A")
    event_desc = event.get("description", "No description")

    text = f"""Date: {event_date} (local)
ID: {event_id}
Type: {event_type_label(event)}
Name: {event_name}
Description: {event_desc}"""
    extra = _event_extra_lines(event)
    if extra:
        text += "\n" + "\n".join(extra)
    return text


_EVENT_RENDERED_KEYS = {
    "id", "start_date_local", "end_date_local", "category", "type", "name", "description",
    "moving_time", "distance", "icu_training_load", "paired_activity_id", "indoor", "tags",
    "updated", "workout_doc", "athlete_id", "uid", "created_by_id", "calendar_id", "color",
    "attachments", "push_errors", "plan_applied", "plan_athlete_id", "plan_folder_id",
    "plan_workout_id", "external_id", "oauth_client_id", "shared_event_id", "structure_read_only",
    "hide_from_athlete", "not_on_fitness_chart", "show_as_note", "show_on_ctl_line", "for_week",
    "athlete_cannot_edit", "entered",
}


def _event_other_scalar_fields(event: dict[str, Any]) -> list[str]:
    """Non-empty scalar event fields not rendered elsewhere (e.g. training availability settings)."""
    parts: list[str] = []
    for key, value in event.items():
        if key in _EVENT_RENDERED_KEYS or value is None or value == "" or isinstance(value, (dict, list)):
            if isinstance(value, list) and value and key not in _EVENT_RENDERED_KEYS:
                parts.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
            continue
        if value is False:
            continue
        parts.append(f"{key}={value}")
    return parts


def _format_workout_doc(doc: dict[str, Any]) -> str:
    """Render a workout_doc (duration, steps, zone times) as text."""
    lines: list[str] = ["", "Workout Document:"]
    if doc.get("duration") is not None:
        lines.append(f"Planned Duration: {hms(doc['duration'])}")
    if doc.get("distance"):
        lines.append(f"Planned Distance: {doc['distance']} m")
    for key, label in (("normalized_power", "Planned NP"), ("average_watts", "Planned Avg Power")):
        if doc.get(key) is not None:
            lines.append(f"{label}: {doc[key]} W")
    steps = doc.get("steps")
    if steps:
        try:
            lines.append("Steps:")
            lines.append(str(WorkoutDoc.from_dict({"steps": steps})).strip())
        except (ValueError, KeyError, TypeError):
            lines.append(f"Steps (raw): {json.dumps(steps, ensure_ascii=False)}")
    zone_times = doc.get("zoneTimes")
    if isinstance(zone_times, list) and zone_times:
        parts = [
            f"{z.get('id')}: {hms(z.get('secs', 0))}"
            for z in zone_times
            if isinstance(z, dict) and z.get("secs")
        ]
        if parts:
            lines.append("Planned Time in Zones: " + ", ".join(parts))
    return "\n".join(lines)


def format_event_details(event: dict[str, Any]) -> str:
    """Format detailed event information into a readable string."""

    event_details = f"""Event Details:

ID: {event.get("id", "N/A")}
Date: {event.get("start_date_local", event.get("date", "Unknown"))} (local)
Type: {event_type_label(event)}
Name: {event.get("name", "Unnamed")}
Description: {event.get("description", "No description")}"""
    extra = _event_extra_lines(event)
    if extra:
        event_details += "\n" + "\n".join(extra)
    if event.get("end_date_local"):
        event_details += f"\nEnd: {event['end_date_local']} (local)"
    if event.get("tags"):
        event_details += f"\nTags: {', '.join(str(t) for t in event['tags'])}"
    if event.get("updated"):
        event_details += f"\nUpdated: {event['updated']}"
    other = _event_other_scalar_fields(event)
    if other:
        event_details += "\nOther fields: " + ", ".join(other)
    if isinstance(event.get("workout_doc"), dict):
        event_details += _format_workout_doc(event["workout_doc"])

    # Only shown when the event carries the flag; it is null on most events.
    if event.get("indoor") is not None:
        event_details += f"""
Indoor: {event["indoor"]}"""

    # Check if it's a workout-based event
    if "workout" in event and event["workout"]:
        workout = event["workout"]
        event_details += f"""

Workout Information:
Workout ID: {workout.get("id", "N/A")}
Sport: {workout.get("sport", "Unknown")}
Duration: {workout.get("duration", 0)} seconds
TSS: {workout.get("tss", "N/A")}"""

        # Include interval count if available
        if "intervals" in workout and isinstance(workout["intervals"], list):
            event_details += f"""
Intervals: {len(workout["intervals"])}"""

    # Check if it's a race
    if event.get("race"):
        event_details += f"""

Race Information:
Priority: {event.get("priority", "N/A")}
Result: {event.get("result", "N/A")}"""

    # Include calendar information
    if "calendar" in event:
        cal = event["calendar"]
        event_details += f"""

Calendar: {cal.get("name", "N/A")}"""

    return event_details


def format_activity_message(message: dict[str, Any]) -> str:
    """Format an activity message/note into a readable string."""
    # Prefer the local timestamp when the API provides it; say which one is shown.
    created_local = message.get("created_local")
    created = created_local or message.get("created", "Unknown")
    zone = " (local)" if created_local else (" (UTC)" if message.get("created") else "")
    if isinstance(created, str) and len(created) > 10:
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            created = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    return f"""Author: {message.get("name", "Unknown")}
Date: {created}{zone}
Type: {message.get("type", "TEXT")}
Content: {message.get("content", "")}"""


def format_custom_item_details(item: dict[str, Any]) -> str:
    """Format detailed custom item information into a readable string."""
    lines = ["Custom Item Details:", ""]
    lines.append(f"ID: {item.get('id', 'N/A')}")
    lines.append(f"Name: {item.get('name', 'N/A')}")
    lines.append(f"Type: {item.get('type', 'N/A')}")

    if item.get("description"):
        lines.append(f"Description: {item['description']}")
    if item.get("visibility"):
        lines.append(f"Visibility: {item['visibility']}")
    if item.get("index") is not None:
        lines.append(f"Index: {item['index']}")
    if item.get("hide_script") is not None:
        lines.append(f"Hide Script: {item['hide_script']}")
    if item.get("content"):
        lines.append(f"Content: {json.dumps(item['content'], indent=2)}")

    return "\n".join(lines)


def _format_interval_block(index: int, interval: dict[str, Any]) -> str:
    """Render the standard metrics of one interval."""
    return f"""[{index}] {interval.get("label", f"Interval {index}")} ({interval.get("type", "Unknown")})
Duration: {interval.get("elapsed_time", 0)} seconds (moving: {interval.get("moving_time", 0)} seconds)
Distance: {interval.get("distance", 0)} meters
Start-End Indices: {interval.get("start_index", 0)}-{interval.get("end_index", 0)}

Power Metrics:
  Average Power: {interval.get("average_watts", 0)} watts ({interval.get("average_watts_kg", 0)} W/kg)
  Max Power: {interval.get("max_watts", 0)} watts ({interval.get("max_watts_kg", 0)} W/kg)
  Weighted Avg Power: {interval.get("weighted_average_watts", 0)} watts
  Intensity: {interval.get("intensity", 0)}
  Training Load: {interval.get("training_load", 0)}
  Joules: {interval.get("joules", 0)}
  Joules > FTP: {interval.get("joules_above_ftp", 0)}
  Power Zone: {interval.get("zone", "N/A")} ({interval.get("zone_min_watts", 0)}-{interval.get("zone_max_watts", 0)} watts)
  W' Balance: Start {interval.get("wbal_start", 0)}, End {interval.get("wbal_end", 0)}
  L/R Balance: {interval.get("avg_lr_balance", 0)}
  Variability: {interval.get("w5s_variability", 0)}
  Torque: Avg {interval.get("average_torque", 0)}, Min {interval.get("min_torque", 0)}, Max {interval.get("max_torque", 0)}

Heart Rate & Metabolic:
  Heart Rate: Avg {interval.get("average_heartrate", 0)}, Min {interval.get("min_heartrate", 0)}, Max {interval.get("max_heartrate", 0)} bpm
  Decoupling: {interval.get("decoupling", 0)}
  DFA α1: {interval.get("average_dfa_a1", 0)}
  Respiration: {interval.get("average_respiration", 0)} breaths/min
  EPOC: {interval.get("average_epoc", 0)}
  SmO2: {interval.get("average_smo2", 0)}% / {interval.get("average_smo2_2", 0)}%
  THb: {interval.get("average_thb", 0)} / {interval.get("average_thb_2", 0)}

Speed & Cadence:
  Speed: Avg {interval.get("average_speed", 0)}, Min {interval.get("min_speed", 0)}, Max {interval.get("max_speed", 0)} m/s
  GAP: {interval.get("gap", 0)} m/s
  Cadence: Avg {interval.get("average_cadence", 0)}, Min {interval.get("min_cadence", 0)}, Max {interval.get("max_cadence", 0)} rpm
  Stride: {interval.get("average_stride", 0)}

Elevation & Environment:
  Elevation Gain: {interval.get("total_elevation_gain", 0)} meters
  Altitude: Min {interval.get("min_altitude", 0)}, Max {interval.get("max_altitude", 0)} meters
  Gradient: {interval.get("average_gradient", 0)}%
  Temperature: {interval.get("average_temp", 0)}°C (Weather: {interval.get("average_weather_temp", 0)}°C, Feels like: {interval.get("average_feels_like", 0)}°C)
  Wind: Speed {interval.get("average_wind_speed", 0)} km/h, Gust {interval.get("average_wind_gust", 0)} km/h, Direction {interval.get("prevailing_wind_deg", 0)}°
  Headwind: {interval.get("headwind_percent", 0)}%, Tailwind: {interval.get("tailwind_percent", 0)}%

"""


def _format_group_block(index: int, group: dict[str, Any]) -> str:
    """Render the standard metrics of one interval group."""
    return f"""Group: {group.get("id", f"Group {index}")} (Contains {group.get("count", 0)} intervals)
Duration: {group.get("elapsed_time", 0)} seconds (moving: {group.get("moving_time", 0)} seconds)
Distance: {group.get("distance", 0)} meters
Start-End Indices: {group.get("start_index", 0)}-N/A

Power: Avg {group.get("average_watts", 0)} watts ({group.get("average_watts_kg", 0)} W/kg), Max {group.get("max_watts", 0)} watts
W. Avg Power: {group.get("weighted_average_watts", 0)} watts, Intensity: {group.get("intensity", 0)}
Heart Rate: Avg {group.get("average_heartrate", 0)}, Max {group.get("max_heartrate", 0)} bpm
Speed: Avg {group.get("average_speed", 0)}, Max {group.get("max_speed", 0)} m/s
Cadence: Avg {group.get("average_cadence", 0)}, Max {group.get("max_cadence", 0)} rpm

"""


def _interval_ranges(interval: dict[str, Any]) -> list[tuple[int, int]]:
    """Sample index range (end exclusive) of an interval, if it has one."""
    start, end = interval.get("start_index"), interval.get("end_index")
    if isinstance(start, bool) or isinstance(end, bool):
        return []
    if isinstance(start, int) and isinstance(end, int) and end >= start:
        return [(start, end)]
    return []


def _group_ranges(group: dict[str, Any], intervals: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Sample index ranges of all member intervals of a group."""
    group_id = group.get("id")
    if group_id is None:
        return []
    ranges: list[tuple[int, int]] = []
    for interval in intervals:
        if isinstance(interval, dict) and interval.get("group_id") == group_id:
            ranges.extend(_interval_ranges(interval))
    return ranges


def _format_interval_extras(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    payload: dict[str, Any],
    ranges: list[tuple[int, int]],
    interval_field_defs: CustomFieldDefs | None,
    streams: list[dict[str, Any]] | None,
    stream_defs: CustomFieldDefs | None,
    label: str,
) -> str:
    """Custom interval fields and per-range stream metrics of an interval or group."""
    lines: list[str] = []
    if interval_field_defs:
        custom_lines = format_custom_field_lines(payload, interval_field_defs, prefix="  ")
        if custom_lines:
            lines.append("Custom Interval Fields:")
            lines.extend(custom_lines)
    if streams is not None:
        if ranges:
            span = ", ".join(f"{start}-{end - 1}" for start, end in ranges)
            lines.append(f"Stream Metrics (samples {span}):")
            metric_lines = format_range_metrics(streams, stream_defs or {}, ranges)
            lines.extend(metric_lines or ["  (no metric streams in the selection)"])
        else:
            lines.append(f"Stream Metrics: not available ({label} has no sample indices)")
    if not lines:
        return ""
    return "\n".join(lines) + "\n\n"


def format_intervals(
    intervals_data: dict[str, Any],
    interval_field_defs: CustomFieldDefs | None = None,
    streams: list[dict[str, Any]] | None = None,
    stream_defs: CustomFieldDefs | None = None,
) -> str:
    """Format intervals data into a readable string with all available fields.

    Args:
        intervals_data: The intervals data from the Intervals.icu API
        interval_field_defs: INTERVAL_FIELD definitions keyed by code; custom interval
            fields present on an interval are listed with name, code, value and units
        streams: Sample-aligned streams of the activity; when given, statistics over
            each interval's start_index..end_index (and over all member intervals of a
            group) are appended per metric stream
        stream_defs: ACTIVITY_STREAM definitions keyed by code (labels and units)

    Returns:
        A formatted string representation of the intervals data
    """
    # Format basic intervals information
    result = f"""Intervals Analysis:

ID: {intervals_data.get("id", "N/A")}
Analyzed: {intervals_data.get("analyzed", "N/A")}

"""

    # Format individual intervals
    intervals = intervals_data.get("icu_intervals") or []
    if intervals:
        result += "Individual Intervals:\n\n"

        for i, interval in enumerate(intervals, 1):
            result += _format_interval_block(i, interval)
            dynamics = format_running_dynamics(interval, indent="  ")
            if dynamics:
                result += "Running Dynamics:\n" + dynamics + "\n\n"
            result += _format_interval_extras(
                interval,
                _interval_ranges(interval),
                interval_field_defs,
                streams,
                stream_defs,
                "interval",
            )

    # Format interval groups
    groups = intervals_data.get("icu_groups") or []
    if groups:
        result += "Interval Groups:\n\n"

        for i, group in enumerate(groups, 1):
            result += _format_group_block(i, group)
            result += _format_interval_extras(
                group,
                _group_ranges(group, intervals),
                interval_field_defs,
                streams,
                stream_defs,
                "group",
            )

    return result


def _format_duration_label(secs: int) -> str:
    """Format seconds into a concise human-readable label (e.g. 5s, 2m, 1h)."""
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        mins = secs // 60
        remainder = secs % 60
        if remainder:
            return f"{mins}m{remainder}s"
        return f"{mins}m"
    hours = secs // 3600
    remainder = (secs % 3600) // 60
    if remainder:
        return f"{hours}h{remainder}m"
    return f"{hours}h"


def format_power_curves(
    curves: list[dict[str, Any]],
    activity_type: str,
    include_normalised: bool,
) -> str:
    """Format extracted power curve data into a concise readable string.

    Args:
        curves: List of extracted curve data dicts with id, label, data_points.
        activity_type: The activity type used for the query.
        include_normalised: Whether W/kg data is included.

    Returns:
        A formatted string representation of the power curves.
    """
    lines: list[str] = [f"Power Curves ({activity_type}):", ""]

    for curve in curves:
        label = curve.get("label", curve.get("id", "Unknown"))
        start = curve.get("start", "")
        end = curve.get("end", "")
        date_range = ""
        if start and end:
            # Trim time portion if present
            start_short = start[:10] if len(start) > 10 else start
            end_short = end[:10] if len(end) > 10 else end
            date_range = f" ({start_short} to {end_short})"

        lines.append(f"{label}{date_range}:")

        data_points = curve.get("data_points", [])
        if not data_points:
            lines.append("  No data available for requested durations.")
            lines.append("")
            continue

        for point in data_points:
            dur_label = _format_duration_label(point["secs"])
            watts = point.get("watts")
            aid = point.get("activity_id", "")
            parts = [f"  {dur_label}: {watts}W"]
            if include_normalised and "watts_per_kg" in point:
                parts.append(f"{point['watts_per_kg']:.2f}W/kg")
                wkg_aid = point.get("wkg_activity_id", "")
                if wkg_aid and wkg_aid != aid:
                    parts.append(f"[{aid}|wkg:{wkg_aid}]")
                else:
                    parts.append(f"[{aid}]")
            else:
                parts.append(f"[{aid}]")
            lines.append(" ".join(parts))
        lines.append("")

    return "\n".join(lines)


def _curve_header(curve: dict[str, Any]) -> str:
    """Build the "label (start to end):" header line for a curve."""
    label = curve.get("label", curve.get("id", "Unknown"))
    start = curve.get("start", "")
    end = curve.get("end", "")
    date_range = ""
    if start and end:
        date_range = f" ({start[:10]} to {end[:10]})"
    return f"{label}{date_range}:"


def _format_clock(total_secs: float) -> str:
    """Format seconds as m:ss or h:mm:ss."""
    total = int(round(total_secs))
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def _format_distance_label(metres: float) -> str:
    """Format a distance in metres as e.g. 400m, 5km or 21.1km."""
    if metres < 1000:
        return f"{metres:g}m"
    km = metres / 1000
    return f"{round(km, 2):g}km"


def format_hr_curves(curves: list[dict[str, Any]], activity_type: str) -> str:
    """Format extracted heart rate curve data into a concise readable string.

    Args:
        curves: List of extracted curve data dicts with id, label, data_points.
        activity_type: The activity type used for the query.

    Returns:
        A formatted string representation of the HR curves.
    """
    lines: list[str] = [f"Heart Rate Curves ({activity_type}):", ""]
    for curve in curves:
        lines.append(_curve_header(curve))
        data_points = curve.get("data_points", [])
        if not data_points:
            lines.append("  No data available for requested durations.")
        for point in data_points:
            dur_label = _format_duration_label(point["secs"])
            lines.append(f"  {dur_label}: {point['bpm']}bpm [{point.get('activity_id', '')}]")
        missing = curve.get("missing", [])
        if data_points and missing:
            labels = ", ".join(_format_duration_label(s) for s in missing)
            lines.append(f"  Not available: {labels}")
        lines.append("")
    return "\n".join(lines)


def format_pace_curves(
    curves: list[dict[str, Any]],
    activity_type: str,
    gap: bool,
    per_100m: bool = False,
) -> str:
    """Format extracted pace curve data into a concise readable string.

    Args:
        curves: List of extracted curve data dicts with id, label, data_points
            (each with distance in metres and secs for that distance).
        activity_type: The activity type used for the query.
        gap: Whether gradient adjusted pace was requested.
        per_100m: Show pace per 100m (swims) instead of per km.

    Returns:
        A formatted string representation of the pace curves.
    """
    kind = "GAP" if gap else "Pace"
    unit = "/100m" if per_100m else "/km"
    scale = 100 if per_100m else 1000
    lines: list[str] = [f"{kind} Curves ({activity_type}):", ""]
    for curve in curves:
        lines.append(_curve_header(curve))
        data_points = curve.get("data_points", [])
        if not data_points:
            lines.append("  No data available for requested distances.")
        for point in data_points:
            dist = point["distance"]
            secs = point["secs"]
            pace = _format_clock(secs / dist * scale) if dist else "N/A"
            lines.append(
                f"  {_format_distance_label(dist)}: {_format_clock(secs)} "
                f"({pace}{unit}) [{point.get('activity_id', '')}]"
            )
        missing = curve.get("missing", [])
        if data_points and missing:
            labels = ", ".join(_format_distance_label(d) for d in missing)
            lines.append(f"  Not available: {labels}")
        lines.append("")
    return "\n".join(lines)
