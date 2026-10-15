"""
Unit tests for formatting utilities in intervals_mcp_server.utils.formatting.

These tests verify that the formatting functions produce expected output strings for activities, workouts, wellness entries, events, and intervals.
"""

import json
from intervals_mcp_server.utils.formatting import (
    format_activity_summary,
    format_workout,
    format_wellness_entry,
    format_event_summary,
    format_event_details,
    format_intervals,
    format_power_curves,
)
from tests.sample_data import INTERVALS_DATA


def test_format_activity_summary():
    """
    Test that format_activity_summary returns a string containing the activity name and ID.
    """
    data = {
        "name": "Morning Ride",
        "id": 1,
        "type": "Ride",
        "startTime": "2024-01-01T08:00:00Z",
        "distance": 1000,
        "duration": 3600,
    }
    result = format_activity_summary(data)
    assert "Activity: Morning Ride" in result
    assert "ID: 1" in result
    assert "Tags: None" in result
    assert "Sub-type: None" in result


def test_format_activity_summary_tags():
    """
    Test that format_activity_summary lists activity tags, comma-separated.
    """
    data = {"name": "Morning Ride", "id": "i1", "type": "Ride", "tags": ["tempo", "outdoor"]}
    assert "Tags: tempo, outdoor" in format_activity_summary(data)
    assert "Tags: None" in format_activity_summary({**data, "tags": []})
    assert "Tags: None" in format_activity_summary({**data, "tags": None})


def test_format_activity_summary_sub_type():
    """
    Test that format_activity_summary shows the activity sub_type (e.g. COMMUTE) in readable form.
    """
    data = {"name": "Morning Ride", "id": "i1", "type": "Ride", "sub_type": "COMMUTE", "trainer": True}
    assert "Sub-type: Commute" in format_activity_summary(data)
    assert "Sub-type: Race" in format_activity_summary({**data, "sub_type": "RACE"})


def test_format_workout():
    """
    Test that format_workout returns a string containing the workout name and interval count.
    """
    workout = {
        "name": "Workout1",
        "description": "desc",
        "sport": "Ride",
        "duration": 3600,
        "tss": 50,
        "intervals": [1, 2, 3],
    }
    result = format_workout(workout)
    assert "Workout: Workout1" in result
    assert "Intervals: 3" in result


def test_format_wellness_entry():
    """
    Test that format_wellness_entry returns a string containing the date and fitness (CTL).
    """
    with open("tests/ressources/wellness_entry.json", "r", encoding="utf-8") as f:
        entry = json.load(f)
    result = format_wellness_entry(entry)

    with open("tests/ressources/wellness_entry_formatted.txt", "r", encoding="utf-8") as f:
        expected_result = f.read()
    assert result == expected_result


def test_format_wellness_entry_include_all_fields():
    """
    Test that format_wellness_entry with include_all_fields=True includes additional unknown fields.
    """
    entry = {
        "id": "2024-06-01",
        "ctl": 80,
        "weight": 75,
        "customField1": "hello",
        "customField2": 42,
        "updated": "2024-06-01T10:00:00Z",
    }
    result = format_wellness_entry(entry, include_all_fields=True)
    assert "Date: 2024-06-01" in result
    assert "Fitness (CTL): 80" in result
    assert "Weight: 75 kg" in result
    assert "Other Fields:" in result
    assert "customField1: hello" in result
    assert "customField2: 42" in result
    # "updated" is a known built-in field, should not appear in Other Fields
    assert "updated:" not in result


def test_format_wellness_entry_no_extra_fields_by_default():
    """
    Test that format_wellness_entry without include_all_fields does not include additional fields.
    """
    entry = {
        "id": "2024-06-01",
        "ctl": 80,
        "customField1": "hello",
    }
    result = format_wellness_entry(entry)
    assert "Other Fields:" not in result
    assert "customField1" not in result


def test_format_wellness_entry_macros_populated():
    """
    Test that format_wellness_entry renders native nutrition macros
    (carbohydrates, protein, fatTotal) in grams when present.
    """
    entry = {
        "id": "2026-04-08",
        "carbohydrates": 310,
        "protein": 145,
        "fatTotal": 72,
    }
    result = format_wellness_entry(entry)
    assert "Nutrition & Hydration:" in result
    assert "- Carbohydrates: 310 g" in result
    assert "- Protein: 145 g" in result
    assert "- Fat: 72 g" in result


def test_format_wellness_entry_macros_null_hidden():
    """
    Test that format_wellness_entry hides macro lines when the fields are null,
    preserving backward compatibility with older wellness records.
    """
    entry = {
        "id": "2026-04-08",
        "ctl": 80,
        "carbohydrates": None,
        "protein": None,
        "fatTotal": None,
    }
    result = format_wellness_entry(entry)
    assert "Carbohydrates" not in result
    assert "Protein" not in result
    # "Fat" could legitimately appear inside e.g. "Body Fat" elsewhere, so
    # anchor the negative assertion on the line-prefix form we would emit.
    assert "- Fat:" not in result


def test_format_event_summary():
    """
    Test that format_event_summary returns a string containing the event date and type.
    """
    event = {
        "start_date_local": "2024-01-01",
        "id": "e1",
        "name": "Event1",
        "description": "desc",
        "race": True,
    }
    summary = format_event_summary(event)
    assert "Date: 2024-01-01" in summary
    assert "Type: Race" in summary


def test_format_event_details():
    """
    Test that format_event_details returns a string containing event and workout details.
    """
    event = {
        "id": "e1",
        "date": "2024-01-01",
        "name": "Event1",
        "description": "desc",
        "workout": {
            "id": "w1",
            "sport": "Ride",
            "duration": 3600,
            "tss": 50,
            "intervals": [1, 2],
        },
        "race": True,
        "priority": "A",
        "result": "1st",
        "calendar": {"name": "Main"},
    }
    details = format_event_details(event)
    assert "Event Details:" in details
    assert "Workout Information:" in details


def test_format_intervals():
    """
    Test that format_intervals returns a string containing interval analysis and the interval label.
    """
    result = format_intervals(INTERVALS_DATA)
    assert "Intervals Analysis:" in result
    assert "Rep 1" in result


def test_format_power_curves():
    """
    Test that format_power_curves returns a concise string with curve labels,
    power values, W/kg values, and activity IDs.
    """
    curves = [
        {
            "id": "s0",
            "label": "This season",
            "start": "2025-09-29T00:00:00",
            "end": "2026-03-14T00:00:00",
            "data_points": [
                {"secs": 5, "watts": 780, "activity_id": "i100", "watts_per_kg": 10.4, "wkg_activity_id": "i100"},
                {"secs": 60, "watts": 380, "activity_id": "i102", "watts_per_kg": 5.07, "wkg_activity_id": "i102"},
                {"secs": 3600, "watts": 210, "activity_id": "i107", "watts_per_kg": 2.8, "wkg_activity_id": "i107"},
            ],
        },
    ]
    result = format_power_curves(curves, "Ride", include_normalised=True)
    assert "Power Curves (Ride):" in result
    assert "This season" in result
    assert "5s: 780W" in result
    assert "10.40W/kg" in result
    assert "1m: 380W" in result
    assert "1h: 210W" in result
    assert "i100" in result
    assert "i107" in result


def test_format_power_curves_without_normalised():
    """
    Test that format_power_curves without normalised data does not include W/kg.
    """
    curves = [
        {
            "id": "s0",
            "label": "This season",
            "start": "",
            "end": "",
            "data_points": [
                {"secs": 5, "watts": 780, "activity_id": "i100"},
            ],
        },
    ]
    result = format_power_curves(curves, "Ride", include_normalised=False)
    assert "780W" in result
    assert "W/kg" not in result


# ---------------------------------------------------------------- missing values are n/a, never 0 (P1-6)
def _block(interval, activity_type="Ride"):
    from intervals_mcp_server.utils.formatting import _format_interval_block  # pylint: disable=import-outside-toplevel
    return _format_interval_block(1, interval, activity_type)


def test_interval_block_shows_missing_values_as_na_never_0():
    """Absent keys, null and NaN are 'n/a' (before: absent keys printed 0, null printed None)."""
    for missing in ({}, dict.fromkeys(("average_watts", "average_dfa_a1", "decoupling", "joules_above_ftp",
                                       "average_heartrate", "average_wind_speed", "elapsed_time"), None),
                    {"average_watts": float("nan"), "average_dfa_a1": "NaN", "decoupling": float("nan")}):
        block = _block({"type": "WORK", **missing})
        for line in ("Average Power: n/a (n/a)", "DFA α1: n/a", "Decoupling: n/a", "Joules > FTP: n/a",
                     "Heart Rate: Avg n/a, Min n/a, Max n/a bpm", "Wind: Speed n/a, Gust n/a, Direction n/a",
                     "Duration: n/a (moving: n/a)", "Power Zone: n/a (n/a)", "SmO2: n/a / n/a", "GAP: n/a"):
            assert line in block, (line, missing)
        assert "None" not in block and ": 0" not in block and " 0 " not in block and "nan" not in block.lower()


def test_interval_block_keeps_real_zeros_and_marks_impossible_ones():
    """A stored 0 stays 0 where it is a real value; a heart rate or DFA α1 of 0 is no measurement."""
    block = _block({"type": "WORK", "elapsed_time": 720, "moving_time": 720, "joules_above_ftp": 0, "min_cadence": 0,
                    "average_cadence": 88.0, "max_cadence": 110, "min_speed": 0.0, "average_heartrate": 0, "max_heartrate": 151,
                    "average_dfa_a1": 0, "average_gradient": 0, "average_watts": 230, "average_watts_kg": 2.76,
                    "average_wind_speed": 1.8895506, "average_wind_gust": 4.0149145, "prevailing_wind_deg": 337,
                    "zone": 3, "zone_min_watts": 176, "zone_max_watts": 211, "average_smo2": 61.5})
    assert "Joules > FTP: 0\n" in block and "Cadence: Avg 88, Min 0, Max 110 rpm" in block
    assert "Speed: Avg n/a, Min 0, Max n/a m/s" in block and "Gradient: 0%" in block
    assert "Heart Rate: Avg n/a (0 stored), Min n/a, Max 151 bpm" in block and "DFA α1: n/a (0 stored)" in block
    assert "Average Power: 230 watts (2.76 W/kg)" in block and "Power Zone: 3 (176-211 watts)" in block
    # Intervals.icu stores wind in m/s (the block used to label the stored value km/h).
    assert "Wind: Speed 1.9 m/s (7 km/h), Gust 4.0 m/s (14 km/h), Direction 337°" in block
    assert "SmO2: 61.5% / n/a" in block and "Duration: 720 seconds (moving: 720 seconds)" in block


def test_short_segment_caveat_for_decoupling_and_dfa():
    """Under 10 min the per-interval decoupling and DFA α1 are marked; one neutral note under the list."""
    from intervals_mcp_server.utils.execution import DRIFT_MIN_SECS  # pylint: disable=import-outside-toplevel
    from intervals_mcp_server.utils.formatting import SHORT_SEGMENT_NOTE, SHORT_SEGMENT_SECS  # pylint: disable=import-outside-toplevel

    assert SHORT_SEGMENT_SECS == DRIFT_MIN_SECS == 600
    short = _block({"type": "RECOVERY", "elapsed_time": 240, "decoupling": -22.161798, "average_dfa_a1": 0.81})
    assert "Decoupling: -22.2 (short segment, under 10 min)" in short
    assert "DFA α1: 0.81 (short segment, under 10 min)" in short
    long = _block({"type": "WORK", "elapsed_time": 720, "decoupling": 3.44, "average_dfa_a1": 0.62})
    assert "Decoupling: 3.4\n" in long and "DFA α1: 0.62\n" in long
    moving_only = _block({"type": "WORK", "moving_time": 300, "decoupling": 1.0})
    assert "Decoupling: 1 (short segment, under 10 min)" in moving_only
    data = {"id": "i1", "icu_intervals": [{"type": "WORK", "elapsed_time": 720, "decoupling": 3.4},
                                          {"type": "RECOVERY", "elapsed_time": 240, "decoupling": -22.2},
                                          {"type": "WORK", "elapsed_time": 1, "decoupling": None}]}
    text = format_intervals(data)
    assert text.count(SHORT_SEGMENT_NOTE) == 1 and "mostly reflect the shape of the effort" in SHORT_SEGMENT_NOTE
    assert "not aerobic drift or intensity" in SHORT_SEGMENT_NOTE and "Shown as stored" in SHORT_SEGMENT_NOTE
    # No note when no short segment carries one of the two values (a stored DFA α1 of 0 is no value).
    assert SHORT_SEGMENT_NOTE not in format_intervals({"icu_intervals": [data["icu_intervals"][0], data["icu_intervals"][2]]})
    assert SHORT_SEGMENT_NOTE not in format_intervals({"icu_intervals": [{"type": "WORK", "elapsed_time": 60, "average_dfa_a1": 0}]})


def test_group_block_and_summaries_show_missing_as_na():
    """Groups, the activity summary and workouts: missing durations and distances are n/a, not 0."""
    text = format_intervals({"icu_groups": [{"id": "g1"}]})
    assert "Group: g1 (Contains n/a intervals)" in text and "Duration: n/a (moving: n/a)" in text
    assert "Power: Avg n/a (n/a), Max n/a" in text and "Heart Rate: Avg n/a, Max n/a bpm" in text and " 0 " not in text
    summary = format_activity_summary({"name": "Indoor", "id": "i1", "distance": None, "moving_time": None})
    assert "Distance: N/A meters" in summary and "Duration: N/A seconds" in summary and "Moving Time: N/A seconds" in summary
    assert "Elevation Gain: N/A meters" in summary and "meters\nDuration: N/A" in summary
    assert "Duration: 3600 seconds" in format_activity_summary({"elapsed_time": 3600})
    assert "Distance: 0 meters" in format_activity_summary({"distance": 0})  # a stored 0 stays
    assert "Duration: N/A seconds" in format_workout({"name": "x"})
    assert "Duration: N/A seconds" in format_event_details({"workout": {"id": 1}})


def test_compact_intervals_and_weekly_load_never_print_none():
    """The compact interval lines and the weekly summary show n/a for null values."""
    from intervals_mcp_server.tools.activities import _compact_intervals  # pylint: disable=import-outside-toplevel
    from intervals_mcp_server.tools.training_review import _format_week  # pylint: disable=import-outside-toplevel

    compact = _compact_intervals({"id": "i1", "icu_intervals": [
        {"type": "WORK", "average_watts": 230, "weighted_average_watts": None, "average_heartrate": 150, "max_heartrate": None}],
        "icu_groups": [{"id": "g", "count": 3, "average_watts": None}]}, {}, None, {})
    assert "avg 230 W NP n/a max n/a" in compact and "HR 150/n/a" in compact and "avg n/a W, HR n/a" in compact
    assert "None" not in compact
    # A stored heart rate of 0 is no measurement, also in the compact line (not "HR 0/0").
    zero = _compact_intervals({"id": "i1", "icu_intervals": [{"type": "WORK", "average_heartrate": 0, "max_heartrate": 0},
                                                            {"type": "WORK", "average_heartrate": 140, "max_heartrate": 0}],
                               "icu_groups": [{"id": "g", "average_heartrate": 0}]}, {}, None, {})
    assert "HR 0" not in zero and "| HR n/a (0 stored)" in zero and "HR 140/n/a (0 stored)" in zero
    assert "HR n/a (0 stored)" in zero.splitlines()[-1]
    week = _format_week({"date": "2026-10-05", "count": 0, "training_load": None, "byCategory": [{"category": "Ride", "count": 1}]})
    assert "load n/a" in week and "load 0" not in week and "None" not in week


def test_float_noise_is_rounded_for_display():
    """R39-14: stored floats are rounded for display (known metrics to their usual decimals, other
    values to 4 significant digits); missing values stay N/A, real zeros 0, JSON is not affected."""
    summary = format_activity_summary({
        "distance": 29012.64, "total_elevation_gain": 2.400024, "total_elevation_loss": 11.999878, "icu_training_load": 13.399596,
        "icu_intensity": 91.02564, "icu_power_hr": 1.2857143, "icu_variability_index": 1.1833333, "decoupling": 7.2646327,
        "average_speed": 8.2491, "average_stride": 2.8159044, "avg_lr_balance": 45.389656, "icu_weight": 83.019,
        "headwind_percent": 31.532557, "tailwind_percent": 0.0, "icu_ctl": 67.02364, "icu_atl": 78.275314, "trimp": 87.10906,
        "icu_efficiency_factor": 1.5214286, "icu_average_watts": 180.27, "average_heartrate": 140.4,
    })
    for expected in ("Distance: 29012.6 meters", "Elevation Gain: 2.4 meters", "Elevation Loss: 12 meters",
                     "Training Load (Intervals.icu): 13.4", "Intensity: 91\n", "Power:HR Ratio: 1.29", "Variability Index: 1.18",
                     "Decoupling: 7.3", "Average Speed: 8.249 m/s", "Average Stride: 2.82", "L/R Balance: 45.4",
                     "Weight: 83.02 kg", "Headwind %: 31.5%", "Tailwind %: 0%", "Fitness (CTL): 67\n", "Fatigue (ATL): 78.3",
                     "TRIMP: 87.1", "Efficiency Factor: 1.52", "Average Power: 180 watts", "Average Heart Rate: 140 bpm",
                     "Max Speed: N/A m/s", "Polarization Index: N/A"):
        assert expected in summary, expected
    block = _block({"type": "WORK", "elapsed_time": 600, "training_load": 13.399596, "total_elevation_gain": 2.400024,
                    "average_torque": 18.373035, "min_torque": 0.540526, "w5s_variability": 0.039962, "average_respiration": 28.267124,
                    "average_speed": 8.481457, "decoupling": -1.340999, "average_gradient": 0.001984, "intensity": 91.02564})
    for expected in ("Training Load: 13.4", "Elevation Gain: 2.4 meters", "Torque: Avg 18.4, Min 0.5, Max n/a", "Variability: 0.04\n",
                     "Respiration: 28.3 breaths/min", "Speed: Avg 8.481", "Decoupling: -1.3\n", "Gradient: 0.002%", "Intensity: 91\n"):
        assert expected in block, expected
    details = format_event_details({"id": 1, "start_date_local": "2026-10-13T00:00:00", "icu_atl": 76.81097, "ss_p_max": 0.47685498,
                                    "icu_ctl": 66.0})
    assert "Other fields: icu_atl=76.81, ss_p_max=0.4769, icu_ctl=66" in details


def test_rounding_follow_ups_kj_form_ramp_and_baselines():
    """R40-6/7/8: work in kJ (icu_joules is J), form/ramp never "-0.0", baselines by metric."""
    from intervals_mcp_server.tools.summary import _one  # pylint: disable=import-outside-toplevel
    from intervals_mcp_server.tools.wellness_insights import _baseline_lines  # pylint: disable=import-outside-toplevel

    assert "Work: 872 kJ" in format_activity_summary({"icu_joules": 871775}) and "Work: N/A kJ" in format_activity_summary({})
    assert (_one(-0.04), _one(0.04), _one(-0.0), _one(58.0), _one(-6.134842), _one(None)) == ("-0.04", "0.04", "0.0", "58.0", "-6.1", "n/a")
    entries = [{"id": f"2026-09-{day:02d}", "hrv": 40 + day % 3, "lactate": 0.97 + (day % 3) / 100} for day in range(1, 31)]
    entries += [{"id": f"2026-10-{day:02d}", "hrv": 40 + day % 3, "lactate": 0.97 + (day % 3) / 100} for day in range(1, 16)]
    lines = _baseline_lines(entries, ["hrv", "lactate"])
    hrv, lactate = lines[0], lines[1]
    assert "7d mean 40.9 | 42d baseline mean 41, median 41, sd 0.8" in hrv  # known metric: 1 decimal
    assert "7d mean 0.9786 | 42d baseline mean 0.98, median 0.98, sd 0.008264" in lactate  # other metric: 4 significant digits
