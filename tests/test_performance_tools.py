"""
Tests for the performance tools: best efforts of one activity and across activities, the
interval search wrapper, histograms, workout comparison, power:HR efficiency and fatigue
resistance. Every API call is routed to synthetic fixtures defined here; no network access.
"""

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.tools import gear as gear_module  # pylint: disable=wrong-import-position
from intervals_mcp_server.tools.performance import (  # pylint: disable=wrong-import-position
    compare_best_efforts,
    compare_workouts,
    find_similar_intervals,
    get_activity_histogram,
    get_best_efforts,
    get_fatigue_resistance,
    get_power_hr_efficiency,
)

ERROR = {"error": True, "message": "boom"}

# 3600 samples, 200 s recording pause after sample 1799
TIME_DATA = list(range(0, 1800)) + list(range(2000, 3800))
STREAMS_TIME = [{"type": "time", "data": TIME_DATA}]

BEST_EFFORTS_BY_DURATION = {
    5: [{"start_index": 100, "end_index": 105, "average": 812.0, "duration": 5, "distance": None}],
    300: [
        {"start_index": 1700, "end_index": 2000, "average": 301.456, "duration": 300, "distance": None},
        {"start_index": 400, "end_index": 700, "average": 290.0, "duration": 300, "distance": None},
    ],
    3600: [],
}
BEST_EFFORTS_BY_DISTANCE = {
    1000: [{"start_index": 200, "end_index": 443, "average": 4.115, "duration": 243, "distance": 1000.0}],
}


def _best_efforts(_url, params):
    """Best-effort fixture keyed by the requested duration or distance."""
    if "duration" in params:
        return {"efforts": BEST_EFFORTS_BY_DURATION.get(params["duration"], [])}
    return {"efforts": BEST_EFFORTS_BY_DISTANCE.get(params["distance"], [])}


def _activity(aid, day, sport, gear, name, **extra):
    base = {
        "id": aid, "name": name, "start_date_local": f"{day}T10:00:00", "type": sport,
        "gear": {"id": gear} if gear else None, "icu_ftp": 230, "icu_training_load": 80,
        "icu_intensity": 85, "moving_time": 3600, "icu_rpe": 6, "feel": 3, "compliance": 95.0,
        "interval_summary": ["3x 10m 250w"],
    }
    base.update(extra)
    return base


ACTIVITIES = [
    _activity("a1", "2026-09-01", "Ride", "b1", "Sweet Spot 3x15", icu_ftp=225, icu_rpe=7, icu_training_load=70),
    _activity("a2", "2026-09-10", "Run", None, "Tempo run"),
    _activity("a3", "2026-09-20", "GravelRide", "b2", "Gravel endurance", icu_rpe=None, feel=None),
    _activity("a4", "2026-10-01", "Ride", "b1", "Sweet Spot 3x20", icu_ftp=235, icu_rpe=5, icu_training_load=90),
]
ACTIVITY_BY_ID = {a["id"]: a for a in ACTIVITIES}
GEAR = [{"id": "b1", "name": "Road Bike", "type": "Bike"}, {"id": "b2", "name": "Gravel Bike", "type": "Bike"}]

COMPARE_EFFORTS = {
    "a1": {60: 380.0, 300: 300.0, 1200: 250.0},
    "a3": {60: 350.0, 300: 280.0, 1200: None},
    "a4": {60: 410.0, 300: 320.0, 1200: 265.0},
}


def _activity_id_of(url):
    return url.split("/")[2]


def _compare_efforts(url, params):
    """Best average per activity and duration for the comparison tools."""
    value = COMPARE_EFFORTS.get(_activity_id_of(url), {}).get(params["duration"])
    if value is None:
        return {"efforts": []}
    return {"efforts": [{"start_index": 0, "end_index": params["duration"], "average": value, "duration": params["duration"]}]}


def _interval(kind, secs, watts, nwatts, hr, cad):
    return {
        "type": kind, "elapsed_time": secs, "average_watts": watts, "weighted_average_watts": nwatts,
        "average_heartrate": hr, "average_cadence": cad,
    }


INTERVALS = {
    "a1": [_interval("WORK", 900, 220, 222, 150, 88)] * 3 + [_interval("RECOVERY", 300, 120, 125, 120, 80)],
    "a2": [_interval("WORK", 1200, None, None, 160, 170)],
    "a3": [_interval("WORK", 1800, 180, 185, 135, 85), _interval("WORK", 1200, 260, 262, 158, 90), _interval("WORK", 120, 300, 305, 165, 95)],
    "a4": [_interval("WORK", 1200, 240, 243, 155, 90)] * 3 + [_interval("RECOVERY", 300, 110, 112, 118, 78)],
}


def _intervals(url, _params):
    return {"icu_intervals": INTERVALS.get(_activity_id_of(url), []), "icu_groups": []}


def _activity_by_id(url, _params):
    return ACTIVITY_BY_ID.get(_activity_id_of(url), {})


CURVE_SECS = [5, 60, 300, 1200, 3600]
CURVE_FRESH = [900, 410, 320, 270, 240]
CURVE_KJ0 = [850, 380, 300, 255, None]
CURVE_KJ1 = [800, 350, 280, 240, None]


def _activity_curve(_url, params):
    values = {None: CURVE_FRESH, "kj0": CURVE_KJ0, "kj1": CURVE_KJ1}[(params or {}).get("fatigue")]
    return {"secs": CURVE_SECS, "values": values, "watts_per_kg": [v / 80 if v else None for v in values], "vo2max_5m": 60.1}


ATHLETE_CURVES = {
    "list": [
        {"id": "42d", "label": "42 days", "after_kj": 0, "secs": CURVE_SECS, "values": CURVE_FRESH},
        {"id": "42d-kj0", "label": "42 days kj0", "after_kj": 1000, "secs": CURVE_SECS, "values": [850, 380, 300, 255, 230]},
        {"id": "42d-kj1", "label": "42 days kj1", "after_kj": 2000, "secs": CURVE_SECS, "values": [800, 350, 280, 240, 220]},
    ]
}
SPORT_SETTINGS_CONFIGURED = [
    {"id": 2, "types": ["Run"], "ftp": 400, "after_kj0": None, "after_kj1": None},
    {"id": 1, "types": ["Ride", "VirtualRide"], "ftp": 230, "after_kj0": 1000, "after_kj1": 2000},
]
SPORT_SETTINGS_UNSET = [{"id": 1, "types": ["Ride"], "ftp": 230, "after_kj0": None, "after_kj1": None}]

POWER_HISTOGRAM = [
    {"min": 150, "max": 174, "secs": 1800},
    {"min": 100, "max": 124, "secs": 600},
    {"min": 125, "max": 149, "secs": 0},
    {"min": 175, "max": 199, "secs": 1200},
]
PACE_HISTOGRAM = [{"min": 3.0, "max": 3.25, "secs": 720}, {"min": 3.25, "max": 3.5, "secs": 480}]


def _histogram(url, _params):
    return PACE_HISTOGRAM if "pace" in url or "gap" in url else POWER_HISTOGRAM


# Most specific fragment first; "/activities" must come after the search endpoints.
DEFAULT_ROUTES = {
    "/best-efforts": _best_efforts,
    "/streams": STREAMS_TIME,
    "/intervals": _intervals,
    "/interval-search": ACTIVITIES,
    "/search-full": ACTIVITIES,
    "/power-curve.json": _activity_curve,
    "/power-curves.json": ATHLETE_CURVES,
    "/sport-settings": SPORT_SETTINGS_CONFIGURED,
    "/gear": GEAR,
    "-histogram": _histogram,
    "/activities": ACTIVITIES,
    "/activity/": _activity_by_id,
}


def _install_router(monkeypatch, overrides=None, calls=None):
    """Route every API call by URL fragment to a fixture or a (url, params) callable."""
    routes = dict(DEFAULT_ROUTES)
    extra = {k: v for k, v in (overrides or {}).items() if k not in routes}
    routes.update({k: v for k, v in (overrides or {}).items() if k in routes})
    ordered = list(extra.items()) + list(routes.items())

    async def fake_request(url=None, **kwargs):
        params = kwargs.get("params") or {}
        if calls is not None:
            calls.append((url, params))
        for fragment, payload in ordered:
            exact = fragment.endswith("$")  # "...$" matches the whole URL only
            if (url == fragment[:-1]) if exact else fragment in (url or ""):
                return payload(url, params) if callable(payload) else payload
        return {}

    monkeypatch.setattr("intervals_mcp_server.tools.performance.make_intervals_request", fake_request)
    monkeypatch.setattr("intervals_mcp_server.tools.gear.make_intervals_request", fake_request)
    gear_module._GEAR_RAW_CACHE.clear()  # pylint: disable=protected-access
    return fake_request


# ------------------------------------------------------------- get_best_efforts
def test_get_best_efforts_text(monkeypatch):
    """Efforts show the average, h:mm:ss positions from the time stream (pause aware), indices and pace for distances."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(get_best_efforts("a1", durations="5,300,3600", count=2))
    assert result.startswith("Best efforts for activity a1, stream watts (count 2):")
    # Regression: positions are elapsed time (the clock includes pauses), not "pauses excluded"
    assert "Positions are elapsed h:mm:ss from the time stream (the clock includes recording pauses" in result
    assert "pauses excluded" not in result
    assert "  5 s: #1 812.0 W from 1:40 to 1:45 (samples 100-105)\n" in result
    assert "  5 min: #1 301.5 W from 28:20 to 36:40 (samples 1700-2000) [window spans 3:20 of recording pause]" in result
    assert "#2 290.0 W from 6:40 to 11:40 (samples 400-700)" in result
    assert "  60 min: not available (longer than the activity or no data in this stream)" in result
    assert result.endswith("API calls: 4")
    assert calls[0][0] == "/activity/a1/streams" and calls[0][1] == {"types": "time"}
    assert calls[1][1] == {"stream": "watts", "count": 2, "duration": 5}
    run = asyncio.run(get_best_efforts("a2", stream="velocity_smooth", durations=None, distances="1000"))
    assert "Best efforts for activity a2, stream velocity_smooth (count 1):" in run
    assert "  1000 m: #1 4.12 m/s (4:03/km) in 4:03 from 3:20 to 7:23 (samples 200-443)" in run
    assert run.endswith("API calls: 2")
    assert calls[-1][1] == {"stream": "velocity_smooth", "count": 1, "distance": 1000}


def test_get_best_efforts_json_and_query_params(monkeypatch):
    """JSON lists every effort with indices and seconds; exclude_intervals and index bounds are passed through."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    raw = asyncio.run(get_best_efforts(
        "a1", stream="heartrate", durations="300", exclude_intervals=True, start_index=10, end_index=3000, output_format="json",
    ))
    payload = json.loads(raw)
    assert payload["time_stream"] is True
    assert payload["api_calls"] == 2
    first = payload["efforts"][0]
    assert first["requested"] == {"duration": 300}
    assert first["units"] == "bpm" and first["pace"] is None
    assert first["start_secs"] == 1700 and first["end_secs"] == 2200 and first["paused_s_in_window"] == 200
    assert first["rank"] == 1 and payload["efforts"][1]["rank"] == 2
    assert calls[1][1] == {
        "stream": "heartrate", "count": 1, "excludeIntervals": "true", "startIndex": 10, "endIndex": 3000, "duration": 300,
    }


def test_get_best_efforts_without_time_stream(monkeypatch):
    """Without a time stream the positions fall back to sample indices."""
    _install_router(monkeypatch, overrides={"/streams": ERROR})
    result = asyncio.run(get_best_efforts("a1", durations="5"))
    assert "Time stream not available; positions are sample indices." in result
    assert "#1 812.0 W from index 100 to index 105 (samples 100-105)" in result
    payload = json.loads(asyncio.run(get_best_efforts("a1", durations="5", output_format="json")))
    assert payload["time_stream"] is False
    assert payload["efforts"][0]["start"] == "index 100"


def test_get_best_efforts_validation_and_api_error(monkeypatch):
    """Bad durations, counts and an API error on every effort call produce readable errors."""
    _install_router(monkeypatch)
    assert asyncio.run(get_best_efforts("a1", durations="5,abc")).startswith("Error: durations must be")
    assert asyncio.run(get_best_efforts("a1", durations="0")).startswith("Error: durations must be positive")
    assert asyncio.run(get_best_efforts("a1", durations=None)).startswith("Error: at least one duration")
    assert asyncio.run(get_best_efforts("a1", count=0)).startswith("Error: count must be between 1 and 20")
    _install_router(monkeypatch, overrides={"/best-efforts": ERROR})
    assert asyncio.run(get_best_efforts("a1", durations="5,300")) == "Error fetching best efforts: boom"


# --------------------------------------------------------- compare_best_efforts
def test_compare_best_efforts_range_with_filters(monkeypatch):
    """The matrix keeps only the requested sports and gear, newest first, with gear names, FTP and the best line."""
    calls = []
    _install_router(monkeypatch, calls=calls, overrides={"/best-efforts": _compare_efforts})
    result = asyncio.run(compare_best_efforts(
        start_date="2026-09-01", end_date="2026-10-09", sport_types="ride,GravelRide", durations="60,300,1200",
    ))
    lines = result.split("\n")
    assert lines[0].startswith("Best efforts comparison for athlete i1, stream watts: 3 activities, newest first (source 2026-09-01 to 2026-10-09; dates 2026-09-01 to 2026-10-09, sports ride,GravelRide; limit 10):")
    assert lines[1] == "Date | Sport | Gear | FTP | 1 min | 5 min | 20 min | Activity"
    assert lines[2] == "2026-10-01 | Ride | Road Bike (b1) | 235 W | 410 W | 320 W | 265 W | 'Sweet Spot 3x20' (a4)"
    assert lines[3] == "2026-09-20 | GravelRide | Gravel Bike (b2) | 230 W | 350 W | 280 W | n/a | 'Gravel endurance' (a3)"
    assert lines[4] == "2026-09-01 | Ride | Road Bike (b1) | 225 W | 380 W | 300 W | 250 W | 'Sweet Spot 3x15' (a1)"
    assert "Tempo run" not in result
    assert "Best per duration: 1 min 410 W (2026-10-01 'Sweet Spot 3x20', a4); 5 min 320 W (2026-10-01 'Sweet Spot 3x20', a4); 20 min 265 W (2026-10-01 'Sweet Spot 3x20', a4)" in result
    assert "not calibrated against each other" in result
    assert result.endswith("API calls: 10 (plus 1 for the gear catalog unless cached)")
    assert calls[0][0] == "/athlete/i1/activities"
    assert calls[0][1]["oldest"] == "2026-09-01" and "icu_ftp" in calls[0][1]["fields"]
    gear_only = asyncio.run(compare_best_efforts(start_date="2026-09-01", end_date="2026-10-09", gear_id="b2", durations="60"))
    assert "'Gravel endurance' (a3)" in gear_only and "(a4)" not in gear_only


def test_compare_best_efforts_ids_json(monkeypatch):
    """Activity ids are fetched one by one; JSON holds the matrix rows and the best per duration."""
    calls = []
    _install_router(monkeypatch, calls=calls, overrides={"/best-efforts": _compare_efforts})
    payload = json.loads(asyncio.run(compare_best_efforts(activity_ids="a1,a4", durations="60,1200", output_format="json")))
    assert payload["source"] == "2 activity id(s)"
    assert [row["id"] for row in payload["activities"]] == ["a4", "a1"]
    assert payload["activities"][0]["gear_name"] == "Road Bike"
    assert payload["activities"][0]["efforts"] == [{"duration": 60, "average": 410.0}, {"duration": 1200, "average": 265.0}]
    assert payload["best"][0] == {"duration": 60, "average": 410.0, "activity_id": "a4", "name": "Sweet Spot 3x20", "date": "2026-10-01"}
    assert payload["api_calls"] == 6
    assert [c[0] for c in calls[:2]] == ["/activity/a1", "/activity/a4"]


def test_compare_best_efforts_limit_cap_and_errors(monkeypatch):
    """The limit is capped at 25 activities; API errors are surfaced as text."""
    many = [_activity(f"x{i}", f"2026-07-{(i % 28) + 1:02d}", "Ride", "b1", f"Ride {i}") for i in range(30)]
    _install_router(monkeypatch, overrides={"/activities": many, "/best-efforts": _compare_efforts})
    result = asyncio.run(compare_best_efforts(start_date="2026-07-01", end_date="2026-07-31", durations="60", limit=100))
    assert "25 activities, newest first" in result and "limit 25, capped from 100" in result
    assert result.endswith("API calls: 26 (plus 1 for the gear catalog unless cached)")
    _install_router(monkeypatch, overrides={"/activities": ERROR})
    assert asyncio.run(compare_best_efforts()) == "Error fetching activities: boom"
    _install_router(monkeypatch, overrides={"/best-efforts": ERROR})
    assert asyncio.run(compare_best_efforts(activity_ids="a1", durations="60")) == "Error fetching best efforts: boom"
    assert asyncio.run(compare_best_efforts(durations="x")).startswith("Error: durations must be")
    assert asyncio.run(compare_best_efforts(start_date="2026-13-01")).startswith("Error: Invalid date format")
    _install_router(monkeypatch, overrides={"/activities": []})
    assert asyncio.run(compare_best_efforts(sport_types="Swim")).startswith("No activities found")


# --------------------------------------------------------- find_similar_intervals
def test_find_similar_intervals_params_and_text(monkeypatch):
    """The search parameters are mapped to the API names; by default only the dominant sport family is kept
    (the run is counted, not silently dropped); each activity gets a summary block with gear, power meter
    and a comparability score."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(find_similar_intervals(480, 720, 95, 105, target="power", min_reps=3, limit=10))
    assert calls[0][0] == "/athlete/i1/activities/interval-search"
    assert calls[0][1] == {"minSecs": 480, "maxSecs": 720, "minIntensity": 95, "maxIntensity": 105, "limit": 50, "type": "POWER", "minReps": 3}
    assert result.startswith("Interval search for athlete i1: 480-720 s at 95-105% of FTP, target POWER, reps 3-any, limit 10")
    assert "API returned 4 activities; filters (sport family cycling (default from most results)) keep 3; newest first." in result
    assert "Also found in other sports (not shown, % of FTP is sport-specific; sport_types='all' to include): running 1" in result
    assert "2026-10-01 Ride 'Sweet Spot 3x20' (a4)\n  intervals: 3x 10m 250w, moving 1:00:00, load 90, intensity 85 %, FTP 235 W, gear Road Bike (b1), power meter unknown, compliance 95 %" in result
    assert "comparability 100/100: newest result (context); best match 3x 10:00 250w (106% FTP); same gear" in result
    assert "other gear Gravel Bike (b2): watts from another power meter, compare % FTP only" in result
    assert "Tempo run" not in result
    assert "Absolute watts of different bikes / power meters are not comparable" in result
    assert result.endswith("API calls: 1 (plus 1 for the gear catalog unless cached)")
    everything = asyncio.run(find_similar_intervals(480, 720, 95, 105, sport_types="all"))
    assert "2026-09-10 Run 'Tempo run' (a2)" in everything and "gear no gear" in everything


def test_find_similar_intervals_client_filters_and_json(monkeypatch):
    """Dates, sport types and gear are filtered client-side (more results requested from the API)."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(find_similar_intervals(
        480, 720, 95, 105, start_date="2026-09-05", end_date="2026-09-30", sport_types="run,gravelride", limit=5,
    ))
    assert calls[0][1]["limit"] == 25
    assert "API returned 4 activities; filters (dates 2026-09-05 to 2026-09-30, sports run,gravelride) keep 2; newest first." in result
    assert "(a3)" in result and "(a2)" in result and "(a1)" not in result and "(a4)" not in result
    payload = json.loads(asyncio.run(find_similar_intervals(480, 720, 95, 105, gear_id="b1", limit=5, output_format="json")))
    assert [a["id"] for a in payload["activities"]] == ["a4", "a1"]
    assert payload["activities"][0]["gear_name"] == "Road Bike"
    assert payload["activities"][0]["interval_summary"] == ["3x 10m 250w"]
    assert payload["returned_by_api"] == 4 and payload["filters"].startswith("gear b1")
    assert payload["api_params"]["limit"] == 25
    assert payload["activities"][1]["comparability"]["score"] == 100  # FTP 225 vs 235 W is within 5 %
    strict = json.loads(asyncio.run(find_similar_intervals(480, 720, 95, 105, gear_id="b1", ftp_tolerance_pct=2, output_format="json")))
    assert "other FTP context 225 vs 235 W (-4%)" in strict["activities"][1]["comparability"]["notes"]
    assert strict["activities"][1]["comparability"]["score"] < 100
    narrow = json.loads(asyncio.run(find_similar_intervals(480, 720, 95, 105, ftp_range="230-240", output_format="json")))
    assert {a["id"] for a in narrow["activities"]} == {"a3", "a4"}


def test_find_similar_intervals_reference_activity(monkeypatch):
    """A reference activity defines the window (interval length ±25 %, intensity ±8 % of FTP), the sport
    family and the ranking by comparability."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    payload = json.loads(asyncio.run(find_similar_intervals(reference_activity_id="a4", output_format="json")))
    assert [c[0] for c in calls[:3]] == ["/activity/a4", "/activity/a4/intervals", "/athlete/i1/activities/interval-search"]
    assert payload["api_params"]["minSecs"] == 900 and payload["api_params"]["maxSecs"] == 1500
    assert payload["api_params"]["minIntensity"] == 94 and payload["api_params"]["maxIntensity"] == 111
    assert payload["reference"]["pattern"] == {"count": 3, "secs": 1200, "pct_ftp": 102.1}
    assert payload["sort_by"] == "comparability" and payload["other_sport_families"] == {"running": 1}
    assert [a["id"] for a in payload["activities"]][0] == "a4"
    text = asyncio.run(find_similar_intervals(reference_activity_id="a4", min_secs=500))
    assert "Reference a4: 3 x 20:00 @ 102% FTP" in text and "500-1500 s" in text
    assert asyncio.run(find_similar_intervals()).startswith("Error: pass min_secs, max_secs")
    assert asyncio.run(find_similar_intervals(reference_activity_id="a2")).startswith("Error: reference activity a2 has no power-based work intensity")
    _install_router(monkeypatch, overrides={"/intervals": {"icu_intervals": []}})
    assert asyncio.run(find_similar_intervals(reference_activity_id="a4")).startswith("Reference activity a4 has no WORK intervals")
    assert asyncio.run(find_similar_intervals(480, 720, 95, 105, sort_by="x")).startswith("Error: sort_by")
    _install_router(monkeypatch, overrides={"/activity/": ERROR})
    assert asyncio.run(find_similar_intervals(reference_activity_id="zz")) == "Error fetching activity zz: boom"


def test_find_similar_intervals_validation_and_api_error(monkeypatch):
    """Range, intensity, target, reps and limit are validated; an API error is reported."""
    _install_router(monkeypatch)
    assert asyncio.run(find_similar_intervals(0, 60, 90, 100)).startswith("Error: min_secs must be greater than 0")
    assert asyncio.run(find_similar_intervals(120, 60, 90, 100)).startswith("Error: min_secs must be greater than 0")
    assert asyncio.run(find_similar_intervals(60, 120, 110, 100)).startswith("Error: intensities must satisfy")
    assert asyncio.run(find_similar_intervals(60, 120, 90, 400)).startswith("Error: intensities must satisfy")
    assert asyncio.run(find_similar_intervals(60, 120, 90, 100, target="Ride")).startswith("Error: target must be one of POWER, HR, PACE")
    assert asyncio.run(find_similar_intervals(60, 120, 90, 100, min_reps=0)).startswith("Error: min_reps and max_reps must be at least 1")
    assert asyncio.run(find_similar_intervals(60, 120, 90, 100, min_reps=5, max_reps=2)).startswith("Error: min_reps must not be greater")
    assert asyncio.run(find_similar_intervals(60, 120, 90, 100, limit=0)).startswith("Error: limit must be at least 1")
    assert asyncio.run(find_similar_intervals(60, 120, 90, 100, start_date="bad")).startswith("Error: Invalid date format")
    _install_router(monkeypatch, overrides={"/interval-search": ERROR})
    assert asyncio.run(find_similar_intervals(60, 120, 90, 100)) == "Error fetching interval search: boom"
    _install_router(monkeypatch, overrides={"/interval-search": []})
    assert "No matching activities." in asyncio.run(find_similar_intervals(60, 120, 90, 100))


# --------------------------------------------------------- get_activity_histogram
def test_get_activity_histogram_power(monkeypatch):
    """Power buckets are sorted, empty buckets hidden in text, percentages computed; JSON keeps all buckets."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(get_activity_histogram("a1"))
    assert calls[0] == ("/activity/a1/power-histogram", {"bucketSize": 25})
    lines = result.split("\n")
    assert lines[0] == "Power histogram for activity a1 (bucket 25 W, total 1:00:00 in 4 buckets):"
    assert lines[1] == "  100-124 W: 10:00 (16.7 %)"
    assert lines[2] == "  150-174 W: 30:00 (50.0 %)"
    assert lines[3] == "  175-199 W: 20:00 (33.3 %)"
    assert lines[4] == "API calls: 1"
    payload = json.loads(asyncio.run(get_activity_histogram("a1", metric="hr", bucket_size=10, output_format="json")))
    assert calls[-1] == ("/activity/a1/hr-histogram", {"bucketSize": 10})
    assert payload["metric"] == "hr" and payload["units"] == "bpm" and payload["total_secs"] == 3600
    assert [b["percent"] for b in payload["buckets"]] == [16.7, 0.0, 50.0, 33.3]


def test_get_activity_histogram_pace_validation_and_error(monkeypatch):
    """Pace/GAP rows show the pace range; bucket_size and metric are validated; API errors are reported."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(get_activity_histogram("a2", metric="pace"))
    assert calls[0] == ("/activity/a2/pace-histogram", {})
    assert "Pace histogram for activity a2 (total 20:00 in 2 buckets):" in result
    assert "Buckets are speeds in m/s; the pace range is shown slowest to fastest." in result
    assert "  3.00-3.25 m/s (5:33/km to 5:08/km): 12:00 (60.0 %)" in result
    assert "  3.25-3.50 m/s (5:08/km to 4:46/km): 8:00 (40.0 %)" in result
    gap = json.loads(asyncio.run(get_activity_histogram("a2", metric="GAP", output_format="json")))
    assert gap["metric"] == "gap" and gap["bucket_size"] is None
    assert gap["buckets"][0]["pace_slowest"] == "5:33/km"
    assert asyncio.run(get_activity_histogram("a2", metric="pace", bucket_size=5)).startswith("Error: bucket_size only applies")
    assert asyncio.run(get_activity_histogram("a2", metric="power", bucket_size=0)).startswith("Error: bucket_size must be positive")
    assert asyncio.run(get_activity_histogram("a2", metric="cadence")).startswith("Error: metric must be one of power, hr, pace, gap")
    _install_router(monkeypatch, overrides={"-histogram": ERROR})
    assert asyncio.run(get_activity_histogram("a2")) == "Error fetching power histogram: boom"


# ------------------------------------------------------------- compare_workouts
def _iv(kind, secs, watts, hr, max_hr=None, cad=90, start=0):
    return {"type": kind, "elapsed_time": secs, "moving_time": secs, "average_watts": watts, "weighted_average_watts": watts,
            "average_heartrate": hr, "max_heartrate": max_hr, "average_cadence": cad, "start_time": start}


# Modelled on a 3 x 10 min threshold ride extended by a 50 s sprint and easy riding (FTP 234 W).
THRESHOLD_RIDE = [
    _iv("RECOVERY", 1021, 170, 122), _iv("WORK", 602, 249, 153, 161, 94, 1021), _iv("RECOVERY", 238, 121, 128),
    _iv("WORK", 600, 249, 156, 165, 94, 1861), _iv("RECOVERY", 255, 130, 131), _iv("WORK", 601, 253, 160, 169, 96, 2716),
    _iv("RECOVERY", 1078, 149, 125), _iv("WORK", 50, 432, 159, 169, 88, 4395), _iv("RECOVERY", 799, 180, 135),
]
THRESHOLD_ACTIVITIES = [
    _activity("t1", "2026-08-01", "Ride", "b1", "Threshold 3x10", icu_ftp=230, icu_rpe=6),
    _activity("t2", "2026-08-20", "GravelRide", "b2", "Threshold 4x6", icu_ftp=225, power_meter="Assioma"),
    _activity("t3", "2026-09-01", "Run", None, "Threshold Run 3x10", icu_ftp=415),
    _activity("t4", "2026-09-15", "Ride", "b1", "Threshold 3x10 again", icu_ftp=234, icu_rpe=5),
    _activity("t5", "2026-10-09", "Ride", "b1", "Threshold 3x10 extended", icu_ftp=234, icu_rpe=3),
]
THRESHOLD_INTERVALS = {
    "t1": [_iv("RECOVERY", 900, 150, 120), *[_iv("WORK", 600, 240, 158, 166, 92)] * 3, _iv("RECOVERY", 600, 140, 125)],
    "t2": [*[_iv("WORK", 360, 240, 160, 168, 90)] * 4],
    "t3": [*[_iv("WORK", 600, 390, 165, 172, 82)] * 3],
    "t4": [*[_iv("WORK", 600, 245, 155, 163, 93)] * 3, _iv("WORK", 30, 600, 150, 160, 100)],
    "t5": THRESHOLD_RIDE,
}


def _threshold_routes():
    return {
        "/search-full": THRESHOLD_ACTIVITIES,
        "/intervals": lambda url, _p: {"icu_intervals": THRESHOLD_INTERVALS.get(_activity_id_of(url), [])},
        "/activity/": lambda url, _p: {a["id"]: a for a in THRESHOLD_ACTIVITIES}.get(_activity_id_of(url), {}),
    }


def test_compare_workouts_separates_surges_and_sports(monkeypatch):
    """Regression: 3 x 10 min (249/249/253 W) are no longer averaged with a 50 s 432 W sprint (296 W);
    runs and gravel 4 x 6 min are not mixed in; means are time-weighted; RPE is whole-activity."""
    calls = []
    _install_router(monkeypatch, overrides=_threshold_routes(), calls=calls)
    payload = json.loads(asyncio.run(compare_workouts(query="Threshold", output_format="json")))
    rows = {row["id"]: row for row in payload["activities"]}
    assert set(rows) == {"t1", "t2", "t4", "t5"}  # cycling family of the newest activity; the run is not fetched
    t5 = rows["t5"]["main_set"]
    assert t5["count"] == 3 and t5["avg_watts"] == round((602 * 249 + 600 * 249 + 601 * 253) / 1803, 1)
    assert [i["max_hr"] for i in t5["intervals"]] == [161, 165, 169] and t5["max_hr"] == 169
    assert rows["t5"]["surges"] == [{"secs": 50, "avg_watts": 432, "max_watts": None, "avg_hr": 159, "start_time": 4395}]
    assert rows["t4"]["surges"][0]["avg_watts"] == 600 and rows["t4"]["main_set"]["avg_watts"] == 245
    assert rows["t1"]["excluded"] == [] and rows["t1"]["main_set"]["count"] == 3
    assert rows["t2"]["comparable"] is False and "interval length 6.0 min vs 10.0 min" in rows["t2"]["not_comparable_reason"]
    assert payload["reference"] == {"activity_id": "t5", "pattern": {"count": 3, "secs": 601, "pct_ftp": 107.0}, "explicit": False}
    trends = {t["key"]: t for t in payload["trends"]}
    assert trends["avg_watts"]["first"] == 240 and trends["avg_watts"]["last"] == 250.3 and trends["avg_watts"]["n"] == 3
    assert trends["rpe_whole_activity"]["first"] == 6 and trends["rpe_whole_activity"]["last"] == 3
    assert not any("t3" in c[0] for c in calls)
    text = asyncio.run(compare_workouts(query="Threshold"))
    assert "Reference pattern: 3 x 10:01 @ 107% FTP from the newest activity t5 (2026-10-09)" in text
    assert "not averaged (short surges/sprints): 0:50 @ 432 W" in text
    assert "HR 156/max 169 bpm" in text
    assert "sport family cycling (default)" in text
    assert "RPE 3 (whole activity)" in text and "RPE (whole activity, not per interval): 6 -> 3" in text
    assert "power (time-weighted avg): 240 W -> 250 W (+4.3 %), range 240 W-250 W (n 3)" in text
    assert "296" not in text
    assert "Not comparable (left out of the trends): 2026-08-20 'Threshold 4x6' (t2, GravelRide): interval length 6.0 min vs 10.0 min" in text
    assert "Note: FTP changed over the period (230, 234 W)" in text
    assert "power meter unknown, gear Road Bike (b1)" in text


def test_compare_workouts_filters_reference_and_flags(monkeypatch):
    """Reference activity, explicit sport 'all', duration/intensity/reps/FTP filters and gear flags."""
    _install_router(monkeypatch, overrides=_threshold_routes())
    payload = json.loads(asyncio.run(compare_workouts(query="Threshold", reference_activity_id="t2", output_format="json")))
    assert payload["reference"]["explicit"] is True and payload["reference"]["pattern"]["count"] == 4
    assert [r["id"] for r in payload["activities"] if r["comparable"]] == ["t2"]
    every = json.loads(asyncio.run(compare_workouts(query="Threshold", sport_types="all", comparable_only=False, output_format="json")))
    assert {r["id"] for r in every["activities"]} == {"t1", "t2", "t3", "t4", "t5"}
    assert next(r for r in every["activities"] if r["id"] == "t3")["main_set"]["pct_ftp"] == 94.0
    reps = json.loads(asyncio.run(compare_workouts(query="Threshold", sport_types="Ride,GravelRide", min_interval_secs=300,
                                                   max_interval_secs=700, min_reps=4, output_format="json")))
    assert [r["id"] for r in reps["activities"] if r["comparable"]] == []
    assert next(r for r in reps["activities"] if r["id"] == "t5")["not_comparable_reason"] == "3 repetitions outside the requested range"
    ftp = json.loads(asyncio.run(compare_workouts(query="Threshold", ftp_range="233-240", output_format="json")))
    assert {r["id"] for r in ftp["activities"]} == {"t4", "t5"}
    high = json.loads(asyncio.run(compare_workouts(query="Threshold", min_intensity=105, output_format="json")))
    assert next(r for r in high["activities"] if r["id"] == "t1")["excluded"][0]["reason"] == "outside the requested intensity range"
    assert asyncio.run(compare_workouts(query="Threshold", ftp_range="x")).startswith("Error: ftp_range")
    assert asyncio.run(compare_workouts(query="Threshold", min_reps=3, max_reps=2)).startswith("Error: min_reps")
    # A comparable session on another bike (other power meter) is flagged, not silently mixed.
    other_bike = _activity("t6", "2026-09-20", "GravelRide", "b2", "Threshold 3x10 gravel", icu_ftp=234, power_meter="Assioma Duo")
    routes = _threshold_routes()
    routes["/search-full"] = THRESHOLD_ACTIVITIES + [other_bike]
    intervals = dict(THRESHOLD_INTERVALS, t6=[_iv("WORK", 600, 238, 157, 164, 90)] * 3)
    routes["/intervals"] = lambda url, _p: {"icu_intervals": intervals.get(_activity_id_of(url), [])}
    _install_router(monkeypatch, overrides=routes)
    mixed = asyncio.run(compare_workouts(query="Threshold"))
    assert "power meter Assioma Duo, gear Gravel Bike (b2)" in mixed
    assert "Note: Different gear (b1, b2) / power meters (Assioma Duo): watts come from sensors that are not calibrated against each other" in mixed
    assert "power on the reference gear only: 240 W -> 250 W" in mixed
    same_bike = json.loads(asyncio.run(compare_workouts(query="Threshold", gear_id="b1", output_format="json")))
    assert {r["id"] for r in same_bike["activities"]} == {"t1", "t4", "t5"}
    assert not any(note.startswith("Different gear") for note in same_bike["notes"])


def test_compare_workouts_ids_limit_and_trend_reliability(monkeypatch):
    """Ids keep their own sport family; the limit is capped at 12; two activities give a not-reliable trend."""
    _install_router(monkeypatch)
    payload = json.loads(asyncio.run(compare_workouts(activity_ids="a4,a1", output_format="json")))
    assert [row["id"] for row in payload["activities"]] == ["a1", "a4"]
    assert payload["activities"][0]["main_set"]["avg_watts"] == 220 and payload["activities"][1]["main_set"]["avg_hr"] == 155
    trend = {t["key"]: t for t in payload["trends"]}["avg_watts"]
    assert trend["n"] == 2 and trend["reliable"] is False
    text = asyncio.run(compare_workouts(activity_ids="a4,a1"))
    assert "only 2 activities, not reliable" in text
    assert "Different gear" not in text  # both on b1
    many = [_activity(f"w{i}", f"2026-08-{i + 1:02d}", "Ride", "b1", f"Workout {i}") for i in range(20)]
    _install_router(monkeypatch, overrides={"/activities": many})
    result = asyncio.run(compare_workouts(start_date="2026-08-01", end_date="2026-08-31", limit=50))
    assert "12 of 20 activities analysed, oldest first; limit 12, capped from 50" in result
    assert "'Workout 19' (w19, Ride)" in result and "'Workout 7' (w7" not in result
    assert result.endswith("API calls: 13 (plus 1 for the gear catalog unless cached)")


def test_compare_workouts_errors(monkeypatch):
    """Search, listing and interval errors are surfaced; an empty selection is reported."""
    _install_router(monkeypatch, overrides={"/search-full": ERROR})
    assert asyncio.run(compare_workouts(query="x")) == "Error fetching activity search: boom"
    _install_router(monkeypatch, overrides={"/intervals": ERROR})
    assert asyncio.run(compare_workouts(activity_ids="a1")) == "Error fetching intervals of activity a1: boom"
    _install_router(monkeypatch, overrides={"/activity/": ERROR})
    assert asyncio.run(compare_workouts(activity_ids="a9")) == "Error fetching activity a9: boom"
    _install_router(monkeypatch)
    assert asyncio.run(compare_workouts(query="Sweet Spot", sport_types="Swim")) == "No activities found (name search 'Sweet Spot'; sports Swim)."
    assert asyncio.run(compare_workouts(start_date="2026-10-02", end_date="2026-10-01")).startswith("Error: start_date must not be after end_date")
    assert asyncio.run(compare_workouts(query="Sweet Spot", reference_activity_id="zz")).startswith("Reference activity zz not found")


# -------------------------------------------------------- get_power_hr_efficiency
def test_get_power_hr_efficiency_text(monkeypatch):
    """Steady WORK intervals are bucketed by average power (time-weighted); with two bikes the trends are
    per gear and too few independent activities give no trend (regression: no +X % from one activity)."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(get_power_hr_efficiency(
        start_date="2026-09-01", end_date="2026-10-09", sport_types="Ride,GravelRide", power_bands="150-200,200-250,250-300",
    ))
    assert calls[0][0] == "/athlete/i1/activities" and calls[0][1]["newest"] == "2026-10-09"
    assert calls[1][0] == "/athlete/i1/gear"
    assert [c[0] for c in calls[2:5]] == ["/activity/a1/intervals", "/activity/a3/intervals", "/activity/a4/intervals"]
    lines = result.split("\n")
    assert lines[0] == (
        "Power:HR efficiency for athlete i1, 2026-09-01 to 2026-10-09, sports Ride,GravelRide: 3 activities (oldest first; limit 30), "
        "WORK intervals of at least 5:00 with power and HR, bands 150-200 W, 200-250 W, 250-300 W (low <= W < high; time-weighted means)."
    )
    assert lines[1] == "2026-09-01 Ride 'Sweet Spot 3x15' (a1), gear Road Bike (b1), FTP 225 W: 200-250 W: 3 x 220 W / 150 bpm = 1.47 W/bpm"
    assert lines[2] == "2026-09-20 GravelRide 'Gravel endurance' (a3), gear Gravel Bike (b2), FTP 230 W: 150-200 W: 1 x 180 W / 135 bpm = 1.33 W/bpm; 250-300 W: 1 x 260 W / 158 bpm = 1.65 W/bpm"
    assert lines[3] == "2026-10-01 Ride 'Sweet Spot 3x20' (a4), gear Road Bike (b1), FTP 235 W: 200-250 W: 3 x 240 W / 155 bpm = 1.55 W/bpm"
    assert lines[4].startswith("Trend per band and gear (power meters are not mixed)")
    assert "  200-250 W on gear Road Bike (b1): not enough independent activities (2; a trend needs at least 6) - no trend, not reliable" in lines
    assert "+5.5 %" not in result
    assert "a single activity with a higher W/bpm does not show an improvement" in result
    assert result.endswith("API calls: 4 (plus 1 for the gear catalog unless cached)")


def test_get_power_hr_efficiency_reliable_trend_and_filters(monkeypatch):
    """Enough independent activities give a trend that is compared with the day-to-day variation;
    gear, indoor/outdoor and interval position filters apply."""
    days = [f"2026-08-{d:02d}" for d in range(1, 13)]
    rides = [_activity(f"r{i}", day, "Ride" if i % 4 else "VirtualRide", "b1", f"Ride {i}", trainer=i % 4 == 0)
             for i, day in enumerate(days)]
    hrs = {f"r{i}": 160 - i * 1.5 + (2 if i % 2 else -2) for i in range(12)}

    def intervals(url, _params):
        aid = _activity_id_of(url)
        return {"icu_intervals": [
            {"type": "WORK", "elapsed_time": 600, "start_time": 300, "average_watts": 230, "average_heartrate": hrs.get(aid, 150)},
            {"type": "WORK", "elapsed_time": 600, "start_time": 3000, "average_watts": 230, "average_heartrate": 175},
        ]}

    _install_router(monkeypatch, overrides={"/activities": rides, "/intervals": intervals})
    payload = json.loads(asyncio.run(get_power_hr_efficiency(
        start_date="2026-08-01", end_date="2026-08-31", power_bands="200-250", max_start_minutes=30, output_format="json",
    )))
    trend = payload["trends"][0]
    assert trend["reliable"] is True and trend["activities"] == 12 and trend["group_size"] == 4
    assert trend["change_pct"] > 5 and trend["beyond_variation"] is True
    assert payload["activities"][0]["bands"]["200-250 W"]["n"] == 1  # the late interval (50 min in) is excluded
    outdoor = json.loads(asyncio.run(get_power_hr_efficiency(
        start_date="2026-08-01", end_date="2026-08-31", power_bands="200-250", environment="outdoor", output_format="json",
    )))
    assert len(outdoor["activities"]) == 9 and not any(a["indoor"] for a in outdoor["activities"])
    flat = {f"r{i}": 150 + (3 if i % 2 else -3) for i in range(12)}
    hrs.clear()
    hrs.update(flat)
    text = asyncio.run(get_power_hr_efficiency(start_date="2026-08-01", end_date="2026-08-31", power_bands="200-250", max_start_minutes=30))
    assert "within the day-to-day variation, no meaningful change" in text
    assert asyncio.run(get_power_hr_efficiency(environment="garage")).startswith("Error: environment")
    assert asyncio.run(get_power_hr_efficiency(power_bands="150-210,200-250")).startswith("Error: power bands 150-210 and 200-... overlap")
    assert asyncio.run(get_power_hr_efficiency(min_activities_per_group=0)).startswith("Error: min_activities_per_group")


def test_get_power_hr_efficiency_json_validation_and_errors(monkeypatch):
    """JSON holds per-activity band stats and trends; bands, min length, dates and API errors are validated."""
    _install_router(monkeypatch)
    payload = json.loads(asyncio.run(get_power_hr_efficiency(
        start_date="2026-09-01", end_date="2026-10-09", power_bands="100-300", min_interval_secs=100, output_format="json",
    )))
    assert payload["bands"] == ["100-300 W"] and payload["sport_types"] == "Ride,GravelRide,VirtualRide"
    gravel = payload["activities"][1]["bands"]["100-300 W"]
    # bands are half-open: the 300 W interval of a3 is not in 100-300
    # time-weighted: 1800 s at 180 W / 135 bpm and 1200 s at 260 W / 158 bpm (simple means gave 220 W / 146.5 bpm)
    assert gravel == {"n": 2, "secs": 3000.0, "watts": 212.0, "hr": 144.2, "w_per_bpm": 1.47}
    assert payload["band_rule"] == "low <= W < high"
    trend = payload["trends"][0]
    assert trend["activities"] == 2 and trend["reliable"] is False  # per gear, two activities on b1
    assert asyncio.run(get_power_hr_efficiency(power_bands="150-200,abc")).startswith("Error: power_bands must look like")
    assert asyncio.run(get_power_hr_efficiency(power_bands="200-150")).startswith("Error: power band '200-150'")
    assert asyncio.run(get_power_hr_efficiency(power_bands="")).startswith("Error: at least one power band")
    assert asyncio.run(get_power_hr_efficiency(min_interval_secs=0)).startswith("Error: min_interval_secs must be positive")
    assert asyncio.run(get_power_hr_efficiency(start_date="2026/09/01")).startswith("Error: Invalid date format")
    assert asyncio.run(get_power_hr_efficiency(sport_types="Swim", start_date="2026-09-01", end_date="2026-10-09")).startswith("No activities found between 2026-09-01 and 2026-10-09 for sports Swim")
    _install_router(monkeypatch, overrides={"/activities": ERROR})
    assert asyncio.run(get_power_hr_efficiency()) == "Error fetching activities: boom"
    _install_router(monkeypatch, overrides={"/intervals": ERROR})
    assert asyncio.run(get_power_hr_efficiency(start_date="2026-09-01", end_date="2026-10-09")) == "Error fetching intervals of activity a1: boom"
    many = [_activity(f"e{i}", f"2026-08-{i + 1:02d}", "Ride", "b1", f"Ride {i}") for i in range(25)]
    _install_router(monkeypatch, overrides={"/activities": many})
    capped = asyncio.run(get_power_hr_efficiency(start_date="2026-08-01", end_date="2026-08-31", limit=10))
    assert "10 activities (oldest first; limit 10)" in capped and capped.endswith("API calls: 11 (plus 1 for the gear catalog unless cached)")


# --------------------------------------------------------- get_fatigue_resistance
def test_get_fatigue_resistance_activity(monkeypatch):
    """With configured thresholds an activity's fresh curve is compared with its kJ0/kJ1 curves."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(get_fatigue_resistance(activity_id="a1", durations="60,300,1200,3600"))
    assert [(c[0], c[1]) for c in calls] == [
        ("/athlete/i1/sport-settings", {}),
        ("/activity/a1/power-curve.json", {}),
        ("/activity/a1/power-curve.json", {"fatigue": "kj0"}),
        ("/activity/a1/power-curve.json", {"fatigue": "kj1"}),
    ]
    lines = result.split("\n")
    assert lines[0] == "Fatigue resistance for athlete i1: power fresh vs after kJ thresholds (Ride)."
    assert lines[1] == "Sport setting for Ride: after kJ0 = 1000 kJ, after kJ1 = 2000 kJ."
    assert lines[2] == "Activity a1 (Ride):"
    assert lines[3] == "  Duration | fresh | after 1000 kJ | after 2000 kJ"
    assert lines[4] == "  1 min | 410 W | 380 W (-7.3 %) | 350 W (-14.6 %)"
    assert lines[5] == "  5 min | 320 W | 300 W (-6.2 %) | 280 W (-12.5 %)"
    assert lines[6] == "  20 min | 270 W | 255 W (-5.6 %) | 240 W (-11.1 %)"
    assert lines[7] == "  60 min | 240 W | n/a | n/a"
    assert "Suggestion" not in result
    assert result.endswith("API calls: 4")


def test_get_fatigue_resistance_not_configured_shows_no_pseudo_values(monkeypatch):
    """Regression: without after_kj0/after_kj1 no kJ curves are requested and no '+0.0 %' values are shown;
    the configuration is explained and thresholds are suggested (read-only)."""
    calls = []
    athlete = {"id": "i1", "icu_weight": 80.0}
    rides = [_activity(f"k{i}", f"2026-09-{i + 1:02d}", "Ride", "b1", "Ride", icu_joules=kj * 1000, moving_time=7200)
             for i, kj in enumerate([900, 1100, 1300, 1500, 1700])]
    _install_router(monkeypatch, calls=calls, overrides={"/sport-settings": SPORT_SETTINGS_UNSET, "/athlete/i1/activities": rides,
                                                         "/athlete/i1$": athlete})
    result = asyncio.run(get_fatigue_resistance(curves="42d", durations="60,330"))
    assert calls[0][0] == "/athlete/i1/sport-settings"
    assert calls[1] == ("/athlete/i1/power-curves.json", {"type": "Ride", "curves": "42d"})
    assert "after_kj0 / after_kj1 are empty" in result and "so no fatigue values are shown" in result
    assert "set the thresholds in Intervals.icu" in result and "This tool never changes settings." in result
    assert "Curve 42 days (42d, Ride) - fresh curve for reference: 1 min 410 W, 330 s n/a" not in result
    assert "Curve 42 days (42d, Ride) - fresh curve for reference: 1 min 410 W" in result
    assert "%" not in result.split("fresh curve for reference")[1].split("\n")[0]
    assert "Suggestion only (not saved): kJ0 ≈ 1250 kJ, kJ1 ≈ 2500 kJ (15 and 30 kJ/kg at 80 kg); your rides of 1 h+ in the last 90 days: median 1300 kJ, 75th percentile 1500 kJ (n 5)" in result
    assert "so kJ0 ≈ 1000 kJ / kJ1 ≈ 1500 kJ fit your current rides better" in result
    assert all(c[2] if len(c) > 2 else True for c in calls)
    assert not any(c[0].endswith("sport-settings/1") for c in calls)  # nothing written
    payload = json.loads(asyncio.run(get_fatigue_resistance(curves="42d", output_format="json")))
    assert payload["configured"] == {"kj0": False, "kj1": False}
    assert payload["suggestion"]["median_ride_kj"] == 1300
    assert "kj0" not in payload["curves"][0]["rows"][0]
    quiet = asyncio.run(get_fatigue_resistance(curves="42d", suggest_thresholds=False))
    assert "Suggestion" not in quiet
    missing = asyncio.run(get_fatigue_resistance(activity_type="Swim"))
    assert "No sport setting covers 'Swim'; kJ thresholds unknown." in missing


def test_get_fatigue_resistance_missing_and_identical_curves(monkeypatch):
    """A fatigued curve without data is 'missing', one equal to the fresh curve 'identical'; no change is computed."""
    curves = {"list": [
        {"id": "42d", "label": "42 days", "secs": CURVE_SECS, "values": CURVE_FRESH},
        {"id": "42d-kj0", "label": "42 days kj0", "secs": CURVE_SECS, "values": CURVE_FRESH},
    ]}
    only_kj0 = [{"id": 1, "types": ["Ride"], "ftp": 230, "after_kj0": 800, "after_kj1": None}]
    _install_router(monkeypatch, overrides={"/power-curves.json": curves, "/sport-settings": only_kj0, "/athlete/i1$": {"icu_weight": None},
                                            "/athlete/i1/activities": []})
    result = asyncio.run(get_fatigue_resistance(durations="60,300"))
    assert "Sport setting for Ride: after kJ0 = 800 kJ, after kJ1 not configured." in result
    assert "  Duration | fresh | after 800 kJ (identical to fresh)" in result
    assert "  1 min | 410 W | n/a" in result
    assert "kj0: identical to the fresh curve at every duration" in result
    assert "Suggestion only (not saved): kJ0 ≈ 750 kJ, kJ1 ≈ 1250 kJ (1.5 h and 3 h at about 55 % of FTP 230 W)" in result
    curves["list"] = curves["list"][:1]
    gone = json.loads(asyncio.run(get_fatigue_resistance(durations="60", output_format="json", suggest_thresholds=False)))
    assert gone["curves"][0]["status"] == {"kj0": "missing"} and gone["curves"][0]["rows"][0]["kj0"] is None


def test_get_fatigue_resistance_json_validation_and_errors(monkeypatch):
    """JSON carries thresholds, note and rows; validation and API errors are reported."""
    _install_router(monkeypatch)
    payload = json.loads(asyncio.run(get_fatigue_resistance(curves="42d,90d", durations="300", output_format="json")))
    assert payload["thresholds"] == {"after_kj0": 1000, "after_kj1": 2000, "ftp": 230}
    assert payload["note"] == "Sport setting for Ride: after kJ0 = 1000 kJ, after kJ1 = 2000 kJ."
    assert [c["id"] for c in payload["curves"]] == ["42d", "90d"]
    assert payload["curves"][0]["rows"][0] == {
        "duration": 300, "secs_used": 300, "fresh": 320.0, "kj0": 300.0, "kj0_change_pct": -6.2, "kj1": 280.0, "kj1_change_pct": -12.5,
    }
    assert payload["curves"][1]["rows"][0]["fresh"] is None
    assert payload["api_calls"] == 2 and payload["suggestion"] is None
    activity = json.loads(asyncio.run(get_fatigue_resistance(activity_id="a1", durations="3600", output_format="json")))
    assert activity["curves"][0]["rows"][0]["kj0"] is None and activity["curves"][0]["rows"][0]["kj0_change_pct"] is None
    assert asyncio.run(get_fatigue_resistance(durations="abc")).startswith("Error: durations must be")
    assert asyncio.run(get_fatigue_resistance(curves="")).startswith("Error: at least one curve id")
    _install_router(monkeypatch, overrides={"/power-curve.json": ERROR})
    assert asyncio.run(get_fatigue_resistance(activity_id="a1")) == "Error fetching power curve (fresh): boom"
    _install_router(monkeypatch, overrides={"/power-curves.json": ERROR})
    assert asyncio.run(get_fatigue_resistance()) == "Error fetching athlete power curves: boom"
    _install_router(monkeypatch, overrides={"/sport-settings": ERROR})
    assert "Error fetching sport settings: boom; kJ thresholds unknown." in asyncio.run(get_fatigue_resistance())


def test_find_similar_intervals_sends_whole_numbers(monkeypatch):
    """Float bounds (MCP clients send 90 as 90.0) become integers; fractions widen the range."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(find_similar_intervals(480.0, 720.0, 90.0, 105.4))  # type: ignore[arg-type]
    params = calls[0][1]
    assert params["minIntensity"] == 90 and params["maxIntensity"] == 106
    assert all(isinstance(params[k], int) for k in ("minSecs", "maxSecs", "minIntensity", "maxIntensity", "limit"))
    assert "480-720 s at 90-106% of FTP" in result


def test_find_similar_intervals_through_mcp_layer(monkeypatch):
    """Calling the registered tool via FastMCP (argument validation included) sends integer intensities."""
    from intervals_mcp_server.mcp_instance import mcp  # pylint: disable=import-outside-toplevel

    calls = []
    _install_router(monkeypatch, calls=calls)
    asyncio.run(mcp.call_tool("find_similar_intervals", {"min_secs": 480, "max_secs": 720, "min_intensity": 90, "max_intensity": 105}))
    params = calls[0][1]
    assert params["minIntensity"] == 90 and isinstance(params["minIntensity"], int)
    assert params["maxIntensity"] == 105 and isinstance(params["maxIntensity"], int)


def test_find_similar_intervals_reference_default_window(monkeypatch):
    """Phase 5 (B): with a reference and no start_date only the 365 days before it count; start_date widens it."""
    old = _activity("a0", "2023-06-02", "Ride", "b1", "Old threshold", icu_ftp=226, interval_summary=["3x 20m 240w"])
    routes = {"/interval-search": ACTIVITIES + [old], "/activity/": lambda url, _p: {**ACTIVITY_BY_ID, "a0": old}.get(_activity_id_of(url), {})}
    _install_router(monkeypatch, overrides=routes)
    payload = json.loads(asyncio.run(find_similar_intervals(reference_activity_id="a4", output_format="json")))
    assert "a0" not in {a["id"] for a in payload["activities"]}
    assert payload["window"] == {"start": "2025-10-01", "end": None, "default_lookback_days": 365, "older_matches_outside_window": 1}
    text = asyncio.run(find_similar_intervals(reference_activity_id="a4"))
    assert (
        "Date window: 2025-10-01 to latest (default window: 365 days before the reference activity (2025-10-01 onwards); "
        "pass start_date for another range); 1 older match(es) before 2025-10-01 not shown"
    ) in text
    assert "dates 2025-10-01 to ..." in text
    explicit = json.loads(asyncio.run(find_similar_intervals(reference_activity_id="a4", start_date="2020-01-01", output_format="json")))
    assert "a0" in {a["id"] for a in explicit["activities"]} and explicit["window"]["default_lookback_days"] is None
    # Without a reference nothing changes: no date limit unless given.
    plain = json.loads(asyncio.run(find_similar_intervals(900, 1500, 95, 110, output_format="json")))
    assert "a0" in {a["id"] for a in plain["activities"]} and plain["window"]["start"] is None


def test_compare_workouts_query_with_reference_default_window(monkeypatch):
    """Phase 5 (B): a name search anchored on a reference keeps the 365 days before it."""
    old = _activity("t0", "2024-05-01", "Ride", "b1", "Threshold 3x10 old", icu_ftp=220)
    routes = _threshold_routes()
    routes["/search-full"] = THRESHOLD_ACTIVITIES + [old]
    intervals = dict(THRESHOLD_INTERVALS, t0=[_iv("WORK", 600, 230, 155, 162, 90)] * 3)
    routes["/intervals"] = lambda url, _p: {"icu_intervals": intervals.get(_activity_id_of(url), [])}
    _install_router(monkeypatch, overrides=routes)
    payload = json.loads(asyncio.run(compare_workouts(query="Threshold", reference_activity_id="t5", output_format="json")))
    assert "t0" not in {r["id"] for r in payload["activities"]}
    assert payload["window"] == {"start": "2025-10-09", "end": None, "default_lookback_days": 365}
    assert "default window: 365 days before the reference activity (2025-10-09 onwards)" in payload["filters"]
    wide = json.loads(asyncio.run(compare_workouts(query="Threshold", reference_activity_id="t5", start_date="2024-01-01", output_format="json")))
    assert "t0" in {r["id"] for r in wide["activities"]} and wide["window"]["default_lookback_days"] is None
    no_reference = json.loads(asyncio.run(compare_workouts(query="Threshold", output_format="json")))
    assert "t0" in {r["id"] for r in no_reference["activities"]}  # unchanged without a reference
