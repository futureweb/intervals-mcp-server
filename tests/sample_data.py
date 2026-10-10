"""
Sample data for testing Intervals.icu MCP server functions.

This module contains test data structures used across the test suite.
"""

from typing import Any

INTERVALS_DATA = {
    "id": "i1",
    "analyzed": True,
    "icu_intervals": [
        {
            "type": "work",
            "label": "Rep 1",
            "elapsed_time": 60,
            "moving_time": 60,
            "distance": 100,
            "average_watts": 200,
            "max_watts": 300,
            "average_watts_kg": 3.0,
            "max_watts_kg": 5.0,
            "weighted_average_watts": 220,
            "intensity": 0.8,
            "training_load": 10,
            "average_heartrate": 150,
            "max_heartrate": 160,
            "average_cadence": 90,
            "max_cadence": 100,
            "average_speed": 6,
            "max_speed": 8,
        }
    ],
}

POWER_CURVES_DATA = {
    "list": [
        {
            "id": "s0",
            "label": "This season",
            "start_date_local": "2025-09-29T00:00:00",
            "end_date_local": "2026-03-14T00:00:00",
            "days": 167,
            "weight": 75.0,
            "secs": [1, 2, 3, 4, 5, 10, 15, 30, 60, 120, 300, 600, 1200, 3600],
            "values": [900, 850, 820, 800, 780, 650, 550, 450, 380, 320, 280, 260, 245, 210],
            "activity_id": [
                "i100", "i100", "i100", "i100", "i100",
                "i101", "i101", "i101", "i102",
                "i103", "i104", "i105", "i106", "i107",
            ],
            "watts_per_kg": [
                12.0, 11.33, 10.93, 10.67, 10.4,
                8.67, 7.33, 6.0, 5.07,
                4.27, 3.73, 3.47, 3.27, 2.8,
            ],
            "wkg_activity_id": [
                "i100", "i100", "i100", "i100", "i100",
                "i101", "i101", "i101", "i102",
                "i103", "i104", "i105", "i106", "i107",
            ],
        },
        {
            "id": "s1",
            "label": "Last season",
            "start_date_local": "2024-09-29T00:00:00",
            "end_date_local": "2025-09-28T00:00:00",
            "days": 365,
            "weight": 76.0,
            "secs": [1, 2, 3, 4, 5, 10, 15, 30, 60, 120, 300, 600, 1200, 3600],
            "values": [870, 830, 800, 770, 750, 630, 520, 430, 360, 300, 265, 250, 235, 200],
            "activity_id": [
                "i200", "i200", "i200", "i200", "i200",
                "i201", "i201", "i201", "i202",
                "i203", "i204", "i205", "i206", "i207",
            ],
            "watts_per_kg": [
                11.45, 10.92, 10.53, 10.13, 9.87,
                8.29, 6.84, 5.66, 4.74,
                3.95, 3.49, 3.29, 3.09, 2.63,
            ],
            "wkg_activity_id": [
                "i200", "i200", "i200", "i200", "i200",
                "i201", "i201", "i201", "i202",
                "i203", "i204", "i205", "i206", "i207",
            ],
        },
    ]
}

# Custom item definitions as returned by /athlete/{id}/custom-item (reduced).
CUSTOM_ITEMS_DATA = [
    {
        "id": 1,
        "type": "ACTIVITY_FIELD",
        "name": "Aerobic Effect",
        "content": {"code": "AerobicEffect", "type": "numeric", "units": None,
                    "fit_session_field": "total_training_effect"},
    },
    {
        "id": 2,
        "type": "ACTIVITY_FIELD",
        "name": "EPOC",
        "content": {"code": "EPOC", "type": "numeric", "units": "ml/kg", "fit_session_field": "178", "aggregate": "SUM"},
    },
    {
        "id": 3,
        "type": "ACTIVITY_FIELD",
        "name": "Training Effect",
        "content": {
            "code": "TrainingEffectSelect",
            "type": "select",
            "options": [{"text": "Recovery", "value": 1.0}, {"text": "Base", "value": 2.0}],
        },
    },
    {
        "id": 4,
        "type": "ACTIVITY_FIELD",
        "name": "Flight Time",
        "content": {"code": "FlightTime", "type": "numeric"},
    },
    {
        "id": 5,
        "type": "ACTIVITY_FIELD",
        "name": "Sweat loss",
        "content": {"code": "Sweatloss", "type": "numeric", "units": "ml", "fit_session_field": "178"},
    },
    {
        "id": 6,
        "type": "INTERVAL_FIELD",
        "name": "Elapsed Time",
        "content": {"code": "ElapsedTime", "type": "numeric", "script": "interval.elapsed_time"},
    },
    {
        "id": 7,
        "type": "ACTIVITY_STREAM",
        "name": "Garmin Stamina",
        "description": "Stamina",
        "content": {"code": "Stamina", "type": "numeric", "units": "point", "script": "..."},
    },
    {
        "id": 8,
        "type": "INPUT_FIELD",
        "name": "Garmin Deep Sleep",
        "content": {"code": "GarminSleepDeepMinutes", "type": "numeric", "units": "min"},
    },
    {"id": 9, "type": "FITNESS_CHART", "name": "Some chart", "content": {"x": 1}},
]

# Activity payload carrying custom field values as top-level keys.
ACTIVITY_WITH_CUSTOM_FIELDS = {
    "id": "i1",
    "name": "Sweet Spot",
    "type": "Ride",
    "start_date_local": "2026-10-06T17:36:22",
    "icu_athlete_id": "i1",
    "distance": 40000,
    "elapsed_time": 4967,
    "moving_time": 4861,
    "AerobicEffect": 3.5,
    "EPOC": 129.58348,
    "TrainingEffectSelect": 2.0,
    "FlightTime": float("nan"),
    "Sweatloss": 0.0,
    "stream_types": ["time", "watts", "heartrate", "secondary_power", "Stamina"],
    "power_field_names": ["power", "Power2"],
    "icu_zone_times": [{"id": "Z1", "secs": 910}, {"id": "Z2", "secs": 1489}],
}

# Sample-aligned streams (12 samples, recording pause between index 5 and 6).
STREAMS_DATA = [
    {"type": "time", "name": None, "custom": False,
     "data": [0, 1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15]},
    {"type": "watts", "name": None, "custom": False,
     "data": [100, 110, 120, 130, 140, 150, 200, 210, 220, 230, 240, 250]},
    {"type": "heartrate", "name": None, "custom": False,
     "data": [120, 121, 122, None, 124, 125, 140, 141, 142, 143, 144, 145]},
    {"type": "latlng", "name": None, "custom": False,
     "data": [47.1, 47.11, 47.12, 47.13, 47.14, 47.15, 47.2, 47.21, 47.22, 47.23, 47.24, 47.25],
     "data2": [12.1, 12.11, 12.12, 12.13, 12.14, 12.15, 12.2, 12.21, 12.22, 12.23, 12.24, 12.25]},
    {"type": "hrv", "name": None, "custom": False, "valueTypeIsArray": True,
     "data": [[736], [731], [712, 680], [691], [700], [702], [650], [640], [630], [620], [610], [600]]},
    {"type": "secondary_power", "name": "Power2", "custom": False,
     "data": [98, 108, 118, 128, 138, 148, 198, 208, 218, 228, 238, 248]},
    {"type": "Stamina", "name": None, "custom": True,
     "data": [100, 100, 99, 99, 98, 98, 97, 96, 95, 94, 93, float("nan")]},
]

# Intervals with sample indices matching STREAMS_DATA.
INTERVALS_WITH_INDICES = {
    "id": "i1",
    "analyzed": True,
    "icu_intervals": [
        {"type": "WORK", "label": "Warmup", "start_index": 0, "end_index": 6, "start_time": 0,
         "end_time": 10, "elapsed_time": 10, "moving_time": 6, "group_id": "g1",
         "ElapsedTime": 10.0, "average_watts": 125},
        {"type": "WORK", "label": "Rep 1", "start_index": 6, "end_index": 12, "start_time": 10,
         "end_time": 16, "elapsed_time": 6, "moving_time": 6, "group_id": "g1",
         "ElapsedTime": 6.0, "average_watts": 225},
    ],
    "icu_groups": [
        {"id": "g1", "count": 2, "start_index": 0, "elapsed_time": 16, "moving_time": 12,
         "average_watts": 175},
    ],
}

# ---------------------------------------------------------------- coaching tool fixtures
SPORT_SETTINGS_DATA = [
    {
        "id": 1, "types": ["Ride"], "ftp": 234, "indoor_ftp": 230, "w_prime": 18000, "p_max": 1036,
        "lthr": 165, "max_hr": 188, "threshold_pace": None, "pace_units": None,
        "power_zones": [55, 75, 90, 105, 120, 150, 999],
        "power_zone_names": ["Active Recovery", "Endurance", "Tempo", "Threshold", "VO2 Max", "Anaerobic", "Neuromuscular"],
        "hr_zones": [133, 147, 153, 164, 169, 174, 188],
        "hr_zone_names": ["Recovery", "Aerobic", "Tempo", "SubThreshold", "SuperThreshold", "Aerobic Capacity", "Anaerobic"],
        "sweet_spot_min": 84, "sweet_spot_max": 97, "hr_load_type": "HRSS", "load_order": "POWER_HR_PACE",
        "default_gear_id": "b1", "default_indoor_gear_id": None, "warmup_time": 600, "cooldown_time": 600,
        "updated": "2026-09-18T15:31:34.039+00:00", "activity_field_ids": [2, 3],
    },
    {
        "id": 2, "types": ["Run", "TrailRun"], "ftp": 415, "lthr": 165, "max_hr": 188,
        "threshold_pace": 3.2258065, "pace_units": "MINS_KM", "w_prime": 30000, "p_max": 850,
        "power_zones": [55, 75, 90, 105, 120, 150, 999], "hr_zones": [139, 147, 155, 164, 169, 174, 188],
        "pace_zones": [77.5, 87.7, 94.3, 100.0, 103.4, 111.5, 999.0],
        "pace_zone_names": ["Zone 1", "Zone 2", "Zone 3", "Zone 4", "Zone 5a", "Zone 5b", "Zone 5c"],
        "pace_load_type": "RUN", "gap_model": "STRAVA_RUN", "updated": "2026-05-10T21:16:43.484+00:00",
    },
]

ATHLETE_DATA = {
    "id": "i1", "name": "Test Athlete", "sex": "M", "timezone": "Europe/Vienna", "locale": "de",
    "measurement_preference": "meters", "icu_weight": 83.48, "height": 1.8, "icu_resting_hr": 48,
    "email": "secret@example.com", "icu_api_key": "SECRET", "bikes": [{"id": "b1", "name": "Canyon Ultimate", "distance": 350378.0, "primary": True}],
    "shoes": [], "sportSettings": SPORT_SETTINGS_DATA,
}

GEAR_DATA = [
    {"id": "b1", "type": "Bike", "name": "Canyon Ultimate", "distance": 350378.06, "time": 44311.0, "activities": 8,
     "purchased": "2026-09-14", "component_ids": ["30303"], "activity_filters": [{"id": 1, "field_id": "type", "value": ["Ride"], "not": False}]},
    {"id": "30303", "type": "PowerMeter", "name": "Shimano FC-R9200P", "distance": 350378.06, "time": 44311.0, "activities": 8, "component": True},
    {"id": "b2", "type": "Bike", "name": "Canyon Grail", "distance": 11465244.0, "time": 1879473.0, "activities": 215},
]

EVENT_DATA: dict[str, Any] = {
    "id": 5, "category": "WORKOUT", "type": "Ride", "name": "2x5 min Threshold", "start_date_local": "2026-10-06T00:00:00",
    "end_date_local": "2026-10-07T00:00:00", "moving_time": 1740, "icu_training_load": 40, "indoor": None,
    "paired_activity_id": "i1", "description": "Threshold session", "updated": "2026-10-05T10:00:00+00:00",
    "training_availability": "LIMITED",
    "workout_doc": {
        "duration": 1740,
        "steps": [
            {"duration": 600, "warmup": True, "ramp": True, "power": {"start": 115, "end": 175, "units": "w"}},
            {"reps": 2, "steps": [
                {"duration": 300, "power": {"start": 240, "end": 250, "units": "w"}, "cadence": {"value": 90, "units": "rpm"}},
                {"duration": 120, "power": {"start": 110, "end": 130, "units": "w"}},
            ]},
            {"duration": 300, "cooldown": True, "power": {"value": 50, "units": "%ftp"}},
        ],
        "zoneTimes": [{"id": "Z4", "secs": 600}],
    },
}


def _execution_streams():
    """1 Hz streams (1740 samples) matching EXECUTION_INTERVALS: warm-up, 2x(work, rest), cool-down."""
    time = list(range(1740))
    watts, heartrate, cadence, speed, stamina = [], [], [], [], []
    for t in time:
        if t < 600:
            w, h = 120 + t * 0.1, 100 + t * 0.05
        elif t < 900:
            w, h = 245 - (t - 600) * 0.02, 150 + (t - 600) * 0.03
        elif t < 1020:
            w, h = 120, 160 - (t - 900) * 0.3
        elif t < 1320:
            w, h = 244 - (t - 1020) * 0.03, 152 + (t - 1020) * 0.03
        elif t < 1440:
            w, h = 118, 160 - (t - 1320) * 0.3
        else:
            w, h = 100, 120
        watts.append(round(w))
        heartrate.append(round(h))
        cadence.append(0 if t % 50 == 0 else 90)
        speed.append(8.0)
        stamina.append(max(0.0, 100 - t * 0.02))
    return [
        {"type": "time", "custom": False, "data": time},
        {"type": "watts", "custom": False, "data": watts},
        {"type": "heartrate", "custom": False, "data": heartrate},
        {"type": "cadence", "custom": False, "data": cadence},
        {"type": "velocity_smooth", "custom": False, "data": speed},
        {"type": "Stamina", "custom": True, "data": stamina},
    ]


EXECUTION_STREAMS = _execution_streams()

EXECUTION_INTERVALS = {
    "id": "i1", "analyzed": True,
    "icu_intervals": [
        {"type": "WORK", "label": "Warmup", "start_index": 0, "end_index": 600, "start_time": 0, "end_time": 600, "elapsed_time": 600, "average_watts": 150, "average_heartrate": 115},
        {"type": "WORK", "label": None, "start_index": 600, "end_index": 900, "start_time": 600, "end_time": 900, "elapsed_time": 300, "average_watts": 242, "max_watts": 260, "average_heartrate": 155, "max_heartrate": 160, "average_cadence": 88},
        {"type": "RECOVERY", "label": None, "start_index": 900, "end_index": 1020, "start_time": 900, "end_time": 1020, "elapsed_time": 120, "average_watts": 120, "average_heartrate": 140},
        {"type": "WORK", "label": None, "start_index": 1020, "end_index": 1320, "start_time": 1020, "end_time": 1320, "elapsed_time": 300, "average_watts": 240, "max_watts": 255, "average_heartrate": 157, "max_heartrate": 161, "average_cadence": 89},
        {"type": "RECOVERY", "label": None, "start_index": 1320, "end_index": 1440, "start_time": 1320, "end_time": 1440, "elapsed_time": 120, "average_watts": 118, "average_heartrate": 141},
        {"type": "WORK", "label": "Cooldown", "start_index": 1440, "end_index": 1740, "start_time": 1440, "end_time": 1740, "elapsed_time": 300, "average_watts": 100, "average_heartrate": 120},
    ],
    "icu_groups": [],
}

EXECUTION_ACTIVITY = {
    "id": "i1", "name": "Threshold Ride", "type": "Ride", "start_date_local": "2026-10-06T17:36:22",
    "start_date": "2026-10-06T15:36:22Z", "icu_athlete_id": "i1", "paired_event_id": 5, "compliance": 101.0,
    "icu_rpe": 7, "feel": 3, "icu_training_load": 41, "icu_ftp": 234, "lthr": 165, "athlete_max_hr": 188,
    "icu_power_zones": [55, 75, 90, 105, 120, 150, 999], "icu_hr_zones": [133, 147, 153, 164, 169, 174, 188],
    "stream_types": ["time", "watts", "heartrate", "cadence", "velocity_smooth", "Stamina"],
    "power_meter": "Shimano FC-R9200P", "power_meter_serial": "123", "power_field_names": ["power", "Power2"],
    "device_name": "Edge 1040", "elapsed_time": 1740, "moving_time": 1700, "distance": 14000,
    "AerobicEffect": 3.1, "EPOC": 80.5,
}

ACTIVITIES_DATA = [
    {"id": "i10", "name": "Ultimate ride", "type": "Ride", "start_date_local": "2026-10-06T17:36:22", "moving_time": 4861,
     "elapsed_time": 4967, "distance": 40360.0, "total_elevation_gain": 375.0, "icu_training_load": 90, "power_load": 90,
     "hr_load": 60, "icu_intensity": 80, "gear": {"id": "b1"}, "feel": 3, "icu_rpe": 6, "icu_zone_times": [{"id": "Z2", "secs": 1489}, {"id": "Z4", "secs": 1753}, {"id": "SS", "secs": 1200}],
     "icu_hr_zone_times": [0, 2000, 0, 0, 0, 0, 0], "TrainingLoad": 129.5, "AerobicEffect": 3.5,
     "power_meter": "Shimano FC-R9200P"},
    {"id": "i11", "name": "Grail gravel", "type": "GravelRide", "start_date_local": "2026-10-07T12:26:55", "moving_time": 6708,
     "elapsed_time": 7153, "distance": 48000.0, "total_elevation_gain": 375.0, "icu_training_load": 130, "power_load": 130,
     "hr_load": 50, "icu_intensity": 70, "gear": {"id": "b2"}, "feel": 2, "icu_rpe": 5, "TrainingLoad": 124.7, "AerobicEffect": 3.3},
    {"id": "i12", "name": "Easy run", "type": "Run", "start_date_local": "2026-10-09T12:53:40", "moving_time": 2700,
     "elapsed_time": 2800, "distance": 8000.0, "total_elevation_gain": 50.0, "icu_training_load": 30, "pace_load": 30,
     "hr_load": 28, "icu_intensity": 65, "feel": 4, "icu_rpe": 4},
]


def _wellness_series():
    """30 days ending 2026-10-09 with HRV/RHR/sleep, nutrition on most days and custom bridge fields."""
    from datetime import date, timedelta  # pylint: disable=import-outside-toplevel

    entries = []
    for i in range(30):
        day = (date(2026, 9, 10) + timedelta(days=i)).isoformat()
        entry = {
            "id": day, "updated": f"{day}T07:00:00+00:00", "locked": False,
            "hrv": 40 + (i % 5), "restingHR": 50 - (i % 3), "avgSleepingHR": 52, "sleepSecs": 7 * 3600 + i * 60,
            "sleepScore": 80 + (i % 10), "readiness": 70 + i, "spO2": 96, "respiration": 15.5,
            "weight": 84.0 - i * 0.05, "steps": 5000 + i * 10, "ctl": 60 + i * 0.2, "atl": 65 + (i % 7), "rampRate": 1.0,
            "sleepQuality": 2, "fatigue": None,
            "kcalConsumed": None if i % 4 == 3 else 2500 + i, "carbohydrates": 300, "protein": 140, "fatTotal": 80,
            "GarminTotalCalories": 2800.0, "GarminActiveCalories": 700.0, "GarminSleepDeepMinutes": 85.0,
            "GarminTrainingReadiness": 70 + i,
            "sportInfo": [{"type": "Ride", "eftp": 226.0 + i * 0.1}],
        }
        entries.append(entry)
    return entries


WELLNESS_SERIES = _wellness_series()


def _extended_ride():
    """A 65-minute plan ridden in 87 minutes: plan intervals, then easy riding and a 50 s sprint."""
    base_streams = _execution_streams()  # 1740 samples (plan part)
    extra = 1740 + 1500 + 50 + 1930  # +25 min easy, 50 s sprint, ~32 min home = 5220 samples
    time = list(range(extra))
    watts, heartrate, cadence, speed, stamina = (list(s["data"]) for s in base_streams[1:6])
    for t in range(1740, extra):
        if 3240 <= t < 3290:
            w, h = 430, 165
        elif t < 3240:
            w, h = 150, 125
        else:
            w, h = 140, 120
        watts.append(w)
        heartrate.append(h)
        cadence.append(85)
        speed.append(8.0)
        stamina.append(max(0.0, stamina[-1] - 0.01))
    streams = [
        {"type": "time", "custom": False, "data": time},
        {"type": "watts", "custom": False, "data": watts},
        {"type": "heartrate", "custom": False, "data": heartrate},
        {"type": "cadence", "custom": False, "data": cadence},
        {"type": "velocity_smooth", "custom": False, "data": speed},
        {"type": "Stamina", "custom": True, "data": stamina},
    ]
    intervals = {
        "id": "i2", "analyzed": True,
        "icu_intervals": list(EXECUTION_INTERVALS["icu_intervals"]) + [
            {"type": "RECOVERY", "label": None, "start_index": 1740, "end_index": 3240, "start_time": 1740,
             "end_time": 3240, "elapsed_time": 1500, "average_watts": 150, "average_heartrate": 125},
            {"type": "WORK", "label": None, "start_index": 3240, "end_index": 3290, "start_time": 3240,
             "end_time": 3290, "elapsed_time": 50, "average_watts": 430, "max_watts": 480, "average_heartrate": 165},
            {"type": "RECOVERY", "label": None, "start_index": 3290, "end_index": 5220, "start_time": 3290,
             "end_time": 5220, "elapsed_time": 1930, "average_watts": 140, "average_heartrate": 120},
        ],
        "icu_groups": [],
    }
    return streams, intervals


EXTENDED_STREAMS, EXTENDED_INTERVALS = _extended_ride()

EXTENDED_ACTIVITY = {
    **{k: v for k, v in EXECUTION_ACTIVITY.items() if k != "paired_event_id"},
    "id": "i2", "name": "Threshold + extra riding", "elapsed_time": 5220, "moving_time": 5200,
    "paired_event_id": None, "compliance": 0.0,
}


PLAN_EVENTS = [
    {"id": 901, "category": "PLAN", "type": "Ride", "name": "Base 2", "start_date_local": "2026-10-13T00:00:00", "end_date_local": "2026-11-09T00:00:00", "description": "aerobic base"},
    {"id": 902, "category": "TARGET", "name": "Week target", "start_date_local": "2026-10-13T00:00:00", "for_week": True, "load_target": 450, "time_target": 36000},
    {"id": 903, "category": "RACE_A", "type": "Ride", "name": "Ötztaler", "start_date_local": "2027-08-29T00:00:00"},
    {"id": 904, "category": "WORKOUT", "type": "Ride", "name": "ignored", "start_date_local": "2026-10-14T00:00:00"},
]
LIBRARY_WORKOUT = {"id": 77, "name": "SST 3x12", "type": "Ride", "folder_id": 301129, "moving_time": 4080, "icu_training_load": 70,
                   "description": "- 10m 55%\n3x\n- 12m 90%\n- 4m 50%", "workout_doc": {"steps": EVENT_DATA["workout_doc"]["steps"]}}
GEAR_WITH_REMINDER = [dict(GEAR_DATA[0], reminders=[{"id": 1, "name": "Chain wax", "distance": 400000, "distance_used": 250000, "percent_used": 62.5, "last_reset": "2026-08-01"}])] + GEAR_DATA[1:]


# ---------------------------------------------------------------- phase 6: fatigue, tests, sensors
LONG_RIDE_THRESHOLDS = (400.0, 600.0)
LONG_RIDE_PAUSE_S = 300


def long_ride_streams(hr_step: float = 10.0, with_pause: bool = True, power: float = 190.0) -> list[dict[str, Any]]:  # pylint: disable=too-many-locals
    """1 Hz long ride: 10 min warm-up 150 W, 15 min steady, 1 min coasting, 2 min at 300 W (FTP 250),
    1 min coasting, a 5 min recording pause and 30 min steady with a 120 m climb in its second half.

    Heart rate is 130 bpm before 400 kJ, +hr_step after 400 kJ and +2*hr_step after 600 kJ (cumulative
    work of the samples), so the matched-power drift per phase is known exactly. Stamina falls by
    0.01 per second, potential stamina by 0.005.
    """
    blocks = [(600, 150.0), (900, power), (60, 0.0), (120, 300.0), (60, 0.0), (1800, power)]
    names = ("time", "watts", "heartrate", "cadence", "temp", "altitude", "distance", "velocity_smooth")
    data: dict[str, list[Any]] = {name: [] for name in (*names, "Stamina", "PotentialStamina")}
    moment, work, distance, altitude, samples = 0, 0.0, 0.0, 500.0, 0
    for number, (secs, watts) in enumerate(blocks):
        if with_pause and number == 5:
            moment += LONG_RIDE_PAUSE_S
        for k in range(secs):
            work += watts / 1000
            climbing = number == 5 and 600 <= k < 1800
            altitude += 0.1 if climbing else 0.0
            distance += 4.0
            data["time"].append(moment)
            data["watts"].append(watts)
            data["heartrate"].append(130 + hr_step * sum(1 for t in LONG_RIDE_THRESHOLDS if work >= t))
            data["cadence"].append(90 if watts > 0 else 0)
            data["temp"].append(20.0)
            data["altitude"].append(round(altitude, 2))
            data["distance"].append(distance)
            data["velocity_smooth"].append(4.0)
            data["Stamina"].append(round(100 - samples * 0.01, 2))
            data["PotentialStamina"].append(round(100 - samples * 0.005, 2))
            moment += 1
            samples += 1
    streams = [{"type": name, "custom": False, "data": data[name]} for name in names]
    streams += [{"type": "Stamina", "custom": True, "data": data["Stamina"]},
                {"type": "PotentialStamina", "custom": True, "data": data["PotentialStamina"]}]
    return streams


def long_ride_activity(aid: str, day: str, gear: str = "b1", **extra: Any) -> dict[str, Any]:
    """Activity payload of a long ride (FTP 250 W, 80 kg, 639 kJ as in long_ride_streams)."""
    base = {
        "id": aid, "name": f"Long ride {aid}", "type": "Ride", "start_date_local": f"{day}T08:00:00",
        "gear": {"id": gear}, "icu_ftp": 250, "icu_weight": 80.0, "icu_joules": 639000, "moving_time": 3540,
        "elapsed_time": 3840, "trainer": False,
        "stream_types": ["time", "watts", "heartrate", "cadence", "temp", "altitude", "distance", "velocity_smooth",
                         "Stamina", "PotentialStamina"],
    }
    base.update(extra)
    return base


def submax_streams(test_watts: float = 248.0, after_watts: float = 100.0, before_watts: float = 150.0) -> list[dict[str, Any]]:
    """1 Hz streams: 15 min before the test, 180 s test (samples 900-1079), then 5 min after.

    HR rises from 120 to 150 bpm during the test and drops by 0.5 bpm per second when the
    rider eases off afterwards (after_watts below half the target), else it stays at 150.
    """
    time, watts, heart = [], [], []
    for t in range(1380):
        if t < 900:
            w, h = before_watts, 120.0
        elif t < 1080:
            w, h = test_watts, 120.0 + (t - 900) * 30 / 179
        else:
            w = after_watts
            h = max(100.0, 150.0 - (t - 1079) * 0.5) if after_watts < 123 else 150.0
        time.append(t)
        watts.append(w)
        heart.append(round(h, 1))
    return [{"type": "time", "data": time}, {"type": "watts", "data": watts}, {"type": "heartrate", "data": heart}]


def submax_activity(aid: str, day: str, **test: Any) -> dict[str, Any]:
    """Activity with an Intervals.icu submax_fatigue_test (power, 180 s, target 246 W); ``test`` overrides fields."""
    block = {
        "type": "POWER", "start_index": 900, "end_index": 1080, "end_index_hrrc": 1140, "duration": 180,
        "average_watts": 248, "average_mps": 0.0, "cv": 5.0, "final_bpm": 150, "hrrc": 28, "target": 246.0,
        "max_cv_percent": 10, "tolerance_percent": 5, "rpe": 5, "tte_mins": 5, "ignore": False,
        "efficiency_factor": round(248 / 150, 4),
    }
    block.update(test)
    return {"id": aid, "name": f"SFT {aid}", "type": "Ride", "start_date_local": f"{day}T07:00:00", "gear": {"id": "b1"},
            "trainer": False, "icu_ftp": 234, "submax_fatigue_test": block}


SFT_SPORT_SETTINGS = [
    {**SPORT_SETTINGS_DATA[0], "sft_type": "POWER", "sft_duration": 180, "sft_max_start_secs": 1800,
     "sft_target_percent": 105, "sft_tolerance_percent": 5, "sft_max_cv_percent": 10, "sft_ftp": None,
     "sft_threshold_pace": None},
]


# ---------------------------------------------------- phase 6: data quality, fueling, context
FUELING_ITEMS = CUSTOM_ITEMS_DATA + [
    {"id": 30, "type": "ACTIVITY_FIELD", "name": "Fluid intake", "content": {"code": "FluidIntake", "type": "numeric", "units": "L"}},
    {"id": 31, "type": "ACTIVITY_FIELD", "name": "Sodium", "content": {"code": "SodiumMg", "type": "numeric", "units": "mg"}},
    {"id": 32, "type": "ACTIVITY_FIELD", "name": "Performance Condition", "updated": "2025-01-01T00:00:00Z",
     "content": {"code": "PerformanceCondition", "type": "numeric", "fit_session_field": "140.17"}},
    {"id": 33, "type": "ACTIVITY_FIELD", "name": "Stamina at start",
     "content": {"code": "Staminaatstart", "type": "numeric", "units": "%", "fit_session_field": "205"}},
    {"id": 34, "type": "ACTIVITY_FIELD", "name": "Stamina at end",
     "content": {"code": "Staminaatend", "type": "numeric", "units": "%", "fit_session_field": "206"}},
    {"id": 35, "type": "ACTIVITY_FIELD", "name": "New Metric", "updated": "2026-10-09T12:00:00Z",
     "content": {"code": "NewMetric", "type": "numeric", "fit_session_field": "250"}},
    {"id": 36, "type": "ACTIVITY_STREAM", "name": "Carbs eaten", "content": {"code": "CarbsEaten", "type": "numeric", "units": "g"}},
]
FUELING_SPORT_SETTINGS = [dict(SPORT_SETTINGS_DATA[0], activity_field_ids=[2, 3, 5, 32, 33, 34, 35]), SPORT_SETTINGS_DATA[1]]

FUELING_INTERVALS = [
    {"type": "WORK", "label": "Warmup", "start_index": 0, "end_index": 200, "start_time": 0, "end_time": 200, "elapsed_time": 200,
     "average_watts": 150, "wbal_start": 20000, "wbal_end": 19800},
    {"type": "WORK", "label": None, "start_index": 200, "end_index": 300, "start_time": 200, "end_time": 300, "elapsed_time": 100,
     "average_watts": 330, "wbal_start": 19800, "wbal_end": 9000},
    {"type": "RECOVERY", "label": None, "start_index": 300, "end_index": 600, "start_time": 300, "end_time": 700, "elapsed_time": 400,
     "average_watts": 140, "wbal_start": 9000, "wbal_end": 19900},
    {"type": "WORK", "label": None, "start_index": 600, "end_index": 650, "start_time": 700, "end_time": 750, "elapsed_time": 50,
     "average_watts": 450, "wbal_start": 19900, "wbal_end": 4000},
    {"type": "RECOVERY", "label": None, "start_index": 650, "end_index": 1200, "start_time": 750, "end_time": 1300, "elapsed_time": 550,
     "average_watts": 160, "wbal_start": 4000, "wbal_end": 19500},
]

FUELING_ACTIVITY: dict[str, Any] = {
    "id": "i50", "name": "Long ride", "type": "Ride", "icu_athlete_id": "i1",
    "start_date_local": "2026-10-08T09:00:00", "start_date": "2026-10-08T07:00:00Z",
    "elapsed_time": 7400, "moving_time": 7200, "icu_recording_time": 7250, "coasting_time": 300,
    "distance": 60000.0, "total_elevation_gain": 800.0, "icu_ftp": 234, "icu_intensity": 72.0, "icu_training_load": 150,
    "icu_average_watts": 180, "icu_weighted_avg_watts": 195, "icu_weight": 75.0, "average_heartrate": 140, "max_heartrate": 170,
    "carbs_used": 300, "carbs_ingested": 120, "calories": 1800, "icu_joules": 1500000,
    "Sweatloss": 1400.0, "FluidIntake": 1.0, "SodiumMg": 800.0, "PerformanceCondition": 0.0, "Staminaatstart": 100.0,
    "Staminaatend": 55.0, "EPOC": 150.0,
    "average_weather_temp": 18.0, "min_weather_temp": 15.0, "max_weather_temp": 21.0, "average_feels_like": 17.0,
    "min_feels_like": 14.0, "max_feels_like": 20.0, "average_wind_speed": 2.5, "average_wind_gust": 5.0, "prevailing_wind_deg": 225,
    "headwind_percent": 40.0, "tailwind_percent": 35.0, "average_clouds": 50, "max_rain": 0.5, "max_snow": 0.0,
    "average_temp": 22.0, "min_temp": 16, "max_temp": 30, "has_weather": True,
    "icu_w_prime": 20000, "icu_pm_w_prime": 21000, "icu_max_wbal_depletion": 16000, "icu_joules_above_ftp": 40000,
    "source": "GARMIN_CONNECT", "external_id": "123456789", "strava_id": "987", "file_type": "fit",
    "created": "2026-10-08T09:05:00Z", "icu_sync_date": "2026-10-08T12:00:00Z", "analyzed": "2026-10-08T12:00:00Z",
    "recording_stops": [599], "icu_lap_count": 3, "icu_intervals_edited": True, "icu_median_time_delta": 1,
    "device_name": "Edge 1040", "power_meter": None, "power_field_names": ["power"], "device_watts": True, "has_heartrate": True,
    "route_id": 77, "stream_types": ["time", "watts", "heartrate", "latlng", "Stamina"],
    "icu_intervals": FUELING_INTERVALS,
}


def _fueling_streams():
    """1200 samples, a 100 s recording gap after sample 599; W′bal dips below 50 % twice; HR with gaps and zeros."""
    time = list(range(600)) + list(range(700, 1300))
    w_bal = [9000 if 200 <= i < 300 else 4000 if 600 <= i < 650 else 20000 for i in range(1200)]
    heartrate = [None if i < 30 else 0 if i < 45 else 140 for i in range(1200)]
    return [
        {"type": "time", "custom": False, "data": time},
        {"type": "watts", "custom": False, "data": [200] * 1200},
        {"type": "heartrate", "custom": False, "data": heartrate},
        {"type": "latlng", "custom": False, "data": [47.0] * 1200, "data2": [12.0] * 1200},
        {"type": "Stamina", "custom": True, "data": [100 - i / 30 for i in range(1200)]},
        {"type": "w_bal", "custom": False, "data": w_bal},
    ]


FUELING_STREAMS = _fueling_streams()

STRAVA_STUB = {"id": "i77", "icu_athlete_id": "i1", "start_date_local": "2026-10-01T10:00:00", "type": "Ride", "name": "Morning Ride",
               "source": "STRAVA", "_note": "STRAVA activities are not available via the API"}


def _listed(aid, day, kind="Ride", **extra):
    base = {"id": aid, "name": f"{kind} {aid}", "type": kind, "start_date_local": f"{day}T09:00:00", "start_date": f"{day}T07:00:00Z",
            "elapsed_time": 7000, "moving_time": 6800, "source": "GARMIN_CONNECT", "stream_types": ["time", "watts", "heartrate", "secondary_power", "Stamina"],
            "PerformanceCondition": 2.0, "Sweatloss": 1000.0}
    base.update(extra)
    return base


# Recent activities (list with a field selection) for the audit: the reference, three earlier rides
# with a second power stream and Performance Condition, and a run.
AUDIT_LIST = [
    {k: v for k, v in FUELING_ACTIVITY.items() if k != "icu_intervals"},
    _listed("i51", "2026-10-05"), _listed("i52", "2026-10-02"), _listed("i53", "2026-09-28", power_meter="Assioma"),
    _listed("i54", "2026-09-30", "Run", stream_types=["time", "heartrate"]),
]
# A Garmin Connect copy of i50 that Intervals.icu filters as a duplicate (not in the list).
DUPLICATE_ACTIVITY = {**{k: v for k, v in FUELING_ACTIVITY.items() if k != "icu_intervals"}, "id": "i55", "source": "GARMIN_CONNECT",
                      "external_id": "123456789", "icu_training_load": None, "icu_intervals": []}
AUDIT_LIST_WITH_UPLOAD = [dict(AUDIT_LIST[0], source="UPLOAD", external_id="123456789_ACTIVITY.fit")] + AUDIT_LIST[1:]

# Activities on route 77 (list with route_id): the reference, two comparable earlier rides, a longer
# variant, a run on the same route and a later ride.
ROUTE_LIST = [
    {k: v for k, v in FUELING_ACTIVITY.items() if k != "icu_intervals"},
    _listed("i40", "2026-09-20", moving_time=7500, distance=60500.0, total_elevation_gain=790.0, icu_average_watts=170,
            icu_weighted_avg_watts=185, icu_weight=76.0, average_heartrate=138, average_weather_temp=12.0, headwind_percent=30.0,
            Staminaatstart=100.0, Staminaatend=60.0),
    _listed("i41", "2026-08-10", "GravelRide", moving_time=7000, distance=59500.0, total_elevation_gain=820.0, icu_average_watts=190,
            icu_weighted_avg_watts=200, icu_weight=76.0, average_heartrate=145, average_weather_temp=25.0),
    _listed("i42", "2026-07-01", moving_time=9000, distance=80000.0, total_elevation_gain=1100.0, icu_average_watts=160),
    _listed("i43", "2026-06-01", "Run", moving_time=20000, distance=60000.0),
    _listed("i60", "2026-10-09", moving_time=7100, distance=60000.0),
]


def _fueling_period():
    """Long sessions with logged, zero and missing intake (period mode of get_fueling_analysis)."""
    rows = []
    specs = [  # id, day, type, moving s, IF %, carbs used, ingested, sweat ml
        ("p1", "2026-09-01", "Ride", 5400, 60.0, 200, 40, 600.0),
        ("p2", "2026-09-05", "Ride", 9000, 70.0, 350, 150, 1500.0),
        ("p3", "2026-09-10", "GravelRide", 12600, 78.0, 500, 240, 2500.0),
        ("p4", "2026-09-15", "Ride", 16200, 72.0, 600, 320, 3000.0),
        ("p5", "2026-09-20", "Ride", 7200, 88.0, 330, 0, 900.0),
        ("p6", "2026-09-25", "Ride", 6000, 66.0, 230, None, None),
        ("p7", "2026-09-26", "Ride", 3000, 90.0, 120, 20, 300.0),
        ("p8", "2026-09-28", "Run", 6000, 75.0, None, 30, 1200.0),
    ]
    for aid, day, kind, moving, intensity, used, ingested, sweat in specs:
        row: dict[str, Any] = {"id": aid, "name": f"{kind} {aid}", "type": kind, "start_date_local": f"{day}T09:00:00", "moving_time": moving,
                               "elapsed_time": moving + 100, "icu_intensity": intensity, "calories": moving / 4, "icu_joules": moving * 200}
        if used is not None:
            row["carbs_used"] = used
        if ingested is not None:
            row["carbs_ingested"] = ingested
        if sweat is not None:
            row["Sweatloss"] = sweat
        rows.append(row)
    return rows


FUELING_PERIOD = _fueling_period()


# Today's wellness completeness: "today" is 2026-10-10 in Europe/Vienna. The days before have the
# official sync values, the bridge's custom fields and the native values the bridge writes; this
# morning's record only has what the official sync delivered right after the watch sync.
COMPLETENESS_TODAY = "2026-10-10"
COMPLETENESS_ITEMS = CUSTOM_ITEMS_DATA + [
    {"id": 40, "type": "INPUT_FIELD", "name": "Garmin Skin Temperature Deviation",
     "content": {"code": "GarminSkinTempDeviationC", "type": "numeric", "units": "°C"}},
    {"id": 41, "type": "INPUT_FIELD", "name": "Garmin Sleep Stress Avg", "content": {"code": "GarminSleepStressAvg", "type": "numeric"}},
    {"id": 42, "type": "INPUT_FIELD", "name": "Garmin Morning Training Readiness",
     "content": {"code": "GarminTrainingReadiness", "type": "numeric"}},
    {"id": 43, "type": "INPUT_FIELD", "name": "Body Battery Max", "content": {"code": "BodyBatteryMax", "type": "numeric"}},
    {"id": 44, "type": "INPUT_FIELD", "name": "Garmin Cycling VO2max", "content": {"code": "GarminVO2MaxCycling", "type": "numeric"}},
    {"id": 45, "type": "INPUT_FIELD", "name": "Garmin Total Calories",
     "content": {"code": "GarminTotalCalories", "type": "numeric", "units": "kcal"}},
]
COMPLETENESS_TODAY_RECORD = {
    "id": COMPLETENESS_TODAY, "updated": "2026-10-10T07:46:01.604+00:00", "ctl": 63.7, "atl": 64.1, "rampRate": 2.6,
    "ctlLoad": 0.0, "atlLoad": 0.0, "sportInfo": [{"type": "Ride", "eftp": 226.9}], "restingHR": 49, "hrv": 38.0,
    "sleepSecs": 28080, "sleepScore": 85.0, "avgSleepingHR": None, "respiration": None, "spO2": None,
    "kcalConsumed": 0, "steps": 257, "locked": None, "tempWeight": False, "BodyBatteryMax": 87,
}


def completeness_day(day: str, index: int) -> dict[str, Any]:
    """A complete day before today (index 0 = 30 days before today)."""
    return {
        "id": day, "updated": f"{day}T20:21:13.526+00:00", "ctl": 60 + index * 0.1, "atl": 62.0, "rampRate": 1.0,
        "ctlLoad": 0.0, "atlLoad": 0.0, "sportInfo": [{"type": "Ride", "eftp": 226.0}], "locked": False, "tempWeight": False,
        "restingHR": 48 + index % 3, "hrv": 40.0 + index % 5, "sleepSecs": 27000 + index * 60, "sleepScore": 80.0 + index % 7,
        "avgSleepingHR": 52.0, "respiration": 14.5, "spO2": 95.0, "steps": 6000 + index, "floorsClimbed": 8,
        "kcalConsumed": 0,  # never logged: a stored 0 placeholder, so not a usual field
        "BodyBatteryMax": 80, "GarminSleepDeepMinutes": 80.0, "GarminSkinTempDeviationC": (-0.2, -0.1, 0.1)[index % 3],
        "GarminSleepStressAvg": 20.0, "GarminTrainingReadiness": 70.0, "GarminTotalCalories": 2700.0 + index,
        "GarminVO2MaxCycling": 55.0 if index % 5 == 0 else None,  # rarely present
    }


def completeness_wellness(today: dict[str, Any] | bool | None = True) -> list[dict[str, Any]]:
    """30 complete days before 2026-10-10 and today's record (True: this morning's, None: none yet)."""
    from datetime import date, timedelta  # pylint: disable=import-outside-toplevel

    end = date.fromisoformat(COMPLETENESS_TODAY)
    entries = [completeness_day((end - timedelta(days=30 - i)).isoformat(), i) for i in range(30)]
    record = COMPLETENESS_TODAY_RECORD if today is True else today
    return entries + ([dict(record)] if isinstance(record, dict) else [])
