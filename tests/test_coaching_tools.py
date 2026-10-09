"""
Tool-level tests for the coaching extensions: athlete profile / sport settings / zones,
gear details, events (categories, JSON, resolved targets), activity listing filters and
JSON output, workout execution analysis, power stream comparison, climb analysis,
recovery snapshot, wellness trends, nutrition summary, training summary, workout
validation and the server status tool. All API calls are routed to synthetic fixtures.
"""

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.server import (  # pylint: disable=wrong-import-position
    analyze_climbs,
    analyze_workout_execution,
    compare_power_streams,
    get_activities,
    get_activity_details,
    get_activity_intervals,
    get_athlete_profile,
    get_event_by_id,
    get_events,
    get_gear_details,
    get_nutrition_summary,
    get_recovery_snapshot,
    get_server_status,
    get_sport_settings,
    get_training_summary,
    get_training_zones,
    get_wellness_trends,
    preview_workout,
    validate_workout,
)
from intervals_mcp_server.tools import athlete as athlete_module  # pylint: disable=wrong-import-position
from intervals_mcp_server.tools import custom_items as custom_items_module  # pylint: disable=wrong-import-position
from intervals_mcp_server.tools import gear as gear_module  # pylint: disable=wrong-import-position
from tests.sample_data import (  # pylint: disable=wrong-import-position
    ACTIVITIES_DATA,
    ACTIVITY_WITH_CUSTOM_FIELDS,
    ATHLETE_DATA,
    CUSTOM_ITEMS_DATA,
    EVENT_DATA,
    EXECUTION_ACTIVITY,
    EXECUTION_INTERVALS,
    EXECUTION_STREAMS,
    GEAR_DATA,
    SPORT_SETTINGS_DATA,
    STREAMS_DATA,
    WELLNESS_SERIES,
)

PATCH_TARGETS = (
    "intervals_mcp_server.api.client.make_intervals_request",
    "intervals_mcp_server.tools.activities.make_intervals_request",
    "intervals_mcp_server.tools.wellness.make_intervals_request",
    "intervals_mcp_server.tools.gear.make_intervals_request",
    "intervals_mcp_server.tools.custom_items.make_intervals_request",
    "intervals_mcp_server.tools.events.make_intervals_request",
    "intervals_mcp_server.tools.analysis.make_intervals_request",
    "intervals_mcp_server.tools.climbs.make_intervals_request",
    "intervals_mcp_server.tools.wellness_insights.make_intervals_request",
    "intervals_mcp_server.tools.summary.make_intervals_request",
    "intervals_mcp_server.tools.workout_check.make_intervals_request",
)


ROUTE_ORDER = (
    "/custom-item", "/sport-settings", "/gear", "/wellness", "/events/", "/events", "/streams",
    "/intervals", "/activities", "/athlete/i1", "/activity/",
)
DEFAULT_ROUTES = {
    "/custom-item": CUSTOM_ITEMS_DATA,
    "/sport-settings": SPORT_SETTINGS_DATA,
    "/gear": GEAR_DATA,
    "/wellness": WELLNESS_SERIES,
    "/events/": EVENT_DATA,
    "/events": [EVENT_DATA],
    "/streams": STREAMS_DATA,
    "/intervals": EXECUTION_INTERVALS,
    "/activities": ACTIVITIES_DATA,
    "/athlete/i1": ATHLETE_DATA,
    "/activity/": ACTIVITY_WITH_CUSTOM_FIELDS,
}


def _install_router(monkeypatch, overrides=None, calls=None):
    """Route every API call by URL fragment (most specific first) to fixtures; `overrides` replace payloads."""
    routes = dict(DEFAULT_ROUTES)
    routes.update(overrides or {})
    order = [f for f in (overrides or {}) if f not in ROUTE_ORDER] + list(ROUTE_ORDER)

    async def fake_request(url=None, **kwargs):
        if calls is not None:
            calls.append((url, kwargs.get("params"), kwargs.get("method", "GET")))
        for fragment in order:
            if fragment == "/athlete/i1":
                if (url or "").rstrip("/").endswith("/athlete/i1"):
                    return routes[fragment]
                continue
            if fragment in (url or ""):
                return routes[fragment]
        return {}

    for target in PATCH_TARGETS:
        monkeypatch.setattr(target, fake_request)
    custom_items_module._CUSTOM_ITEMS_CACHE.clear()  # pylint: disable=protected-access
    gear_module._GEAR_RAW_CACHE.clear()  # pylint: disable=protected-access
    athlete_module._ATHLETE_CACHE.clear()  # pylint: disable=protected-access
    athlete_module._SPORT_SETTINGS_CACHE.clear()  # pylint: disable=protected-access
    return fake_request


# ------------------------------------------------------------ athlete tools
def test_get_athlete_profile_hides_secrets(monkeypatch):
    """The profile lists coaching fields and gear but never e-mail or API key."""
    _install_router(monkeypatch)
    result = asyncio.run(get_athlete_profile())
    assert "Athlete profile i1" in result
    assert "- timezone: Europe/Vienna" in result
    assert "- icu_weight: 83.48" in result
    assert "bike Canyon Ultimate (b1), 350.4 km [primary]" in result
    assert "Ride (id 1): FTP 234 W, indoor FTP 230 W, LTHR 165, max HR 188" in result
    assert "threshold pace 5:10/km" in result
    assert "secret@example.com" not in result
    assert "SECRET" not in result
    payload = json.loads(asyncio.run(get_athlete_profile(output_format="json")))
    assert "email" not in payload["profile"]
    assert payload["sport_settings"][0]["ftp"] == 234


def test_get_sport_settings_with_zones_and_eftp(monkeypatch):
    """Sport settings show thresholds, zones with absolute ranges, default gear and the eFTP estimate."""
    _install_router(monkeypatch)
    result = asyncio.run(get_sport_settings(sport_type="Ride"))
    assert "Sport setting 1 for Ride:" in result
    assert "Power: FTP 234 W, indoor FTP 230 W, W' 18000 J, Pmax 1036 W" in result
    assert "eFTP (Ride, Intervals.icu estimate as of 2026-10-09): 229 W" in result
    assert "Z2 Endurance: 55-75% = 130-176 W" in result
    assert "Z7 Neuromuscular: >150% = >352 W" in result
    assert "Z1 Recovery: ≤133 bpm" in result
    assert "Sweet spot: 84-97% = 197-227 W" in result
    assert "default gear Canyon Ultimate (b1)" in result
    assert asyncio.run(get_sport_settings(sport_type="Swim")).startswith("No sport setting covers 'Swim'")
    run = asyncio.run(get_sport_settings(sport_type="TrailRun"))
    assert "Threshold pace: 5:10/km" in run
    assert "Z4 Zone 4: 94.3-100%" in run
    payload = json.loads(asyncio.run(get_sport_settings(output_format="json")))
    assert payload["sport_settings"][0]["zones"]["power"][1]["max_watts"] == 176
    assert payload["sport_settings"][0]["default_gear_name"] == "Canyon Ultimate"


def test_get_training_zones(monkeypatch):
    """Zones can be restricted by sport and kind."""
    _install_router(monkeypatch)
    result = asyncio.run(get_training_zones(sport_type="Run", zone_type="pace"))
    assert "Zones for Run, TrailRun (setting 2):" in result
    assert "Pace zones (% of threshold speed):" in result
    assert "Z1 Zone 1: ≤77.5% = slower to 6:40/km" in result
    assert "Power zones" not in result
    assert asyncio.run(get_training_zones(zone_type="watts")).startswith("Error: zone_type")
    payload = json.loads(asyncio.run(get_training_zones(sport_type="Ride", output_format="json")))
    assert payload["training_zones"][0]["zones"]["hr"][0]["upper_bound"] == 133


def test_get_gear_details_components(monkeypatch):
    """Gear details show usage, filters and components; components know their parent."""
    _install_router(monkeypatch)
    result = asyncio.run(get_gear_details("b1"))
    assert "Gear b1: Canyon Ultimate (Bike)" in result
    assert "- Usage: 350.4 km, 12.3 h, 8 activities (moving time)" in result
    assert "- Auto-assignment filters: type in [Ride]" in result
    assert "- PowerMeter Shimano FC-R9200P (30303): 350.4 km, 12.3 h, 8 activities" in result
    assert "Component of: Canyon Ultimate (b1)" in asyncio.run(get_gear_details("30303"))
    assert asyncio.run(get_gear_details("nope")).startswith("No gear with id nope")


# ---------------------------------------------------------------- events
def test_get_events_category_filter_and_json(monkeypatch):
    """Events show category and sport, accept a category filter and have a JSON form."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(get_events(start_date="2026-10-06", end_date="2026-10-07", categories="workout"))
    assert "Type: Workout (Ride)" in result
    assert "Planned Time: 29:00" in result
    assert "Paired Activity: i1" in result
    assert calls[0][1]["category"] == "WORKOUT"
    payload = json.loads(asyncio.run(get_events(start_date="2026-10-06", end_date="2026-10-07", output_format="json")))
    assert payload["events"][0]["type_label"] == "Workout (Ride)"
    assert "workout_doc" not in payload["events"][0]


def test_get_event_by_id_uses_plural_endpoint_and_renders_doc(monkeypatch):
    """The single-event endpoint is /events/{id}; details render the workout document."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(get_event_by_id("5", resolve_targets=True))
    assert calls[0][0] == "/athlete/i1/events/5"
    assert calls[0][1] == {"resolve": "true"}
    assert "Type: Workout (Ride)" in result
    assert "Workout Document:" in result
    assert "Planned Duration: 29:00" in result
    assert "2x" in result and "240W-250W 90rpm" in result
    assert "Planned Time in Zones: Z4: 10:00" in result
    assert "Other fields: training_availability=LIMITED" in result
    payload = json.loads(asyncio.run(get_event_by_id("5", output_format="json")))
    assert payload["workout_doc"]["duration"] == 1740


# ------------------------------------------------------------- activities
def test_get_activities_filters_sort_and_pagination(monkeypatch):
    """Sport and gear filters, sorting, offsets and compact output work together."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(get_activities(start_date="2026-10-01", end_date="2026-10-09", sport_types="Ride,GravelRide", detail_level="compact"))
    assert result.startswith("Activities 1-2 of 2")
    assert "'Grail gravel'" in result and "'Easy run'" not in result
    assert calls[0][1]["fields"].startswith("id,start_date_local")
    by_gear = asyncio.run(get_activities(start_date="2026-10-01", end_date="2026-10-09", gear_id="b2", detail_level="compact"))
    assert "i11" in by_gear and "i10" not in by_gear
    assert "gear Canyon Grail" in by_gear
    paged = asyncio.run(get_activities(start_date="2026-10-01", end_date="2026-10-09", sort_by="load", limit=1, detail_level="compact", include_unnamed=True))
    assert "Activities 1-1 of 3" in paged and "i11" in paged and "Next page: offset=1" in paged
    second = asyncio.run(get_activities(start_date="2026-10-01", end_date="2026-10-09", sort_by="load", limit=1, offset=1, detail_level="compact"))
    assert "i10" in second
    assert asyncio.run(get_activities(sort_by="random")).startswith("Error: sort_by")


def test_get_activities_json_records(monkeypatch):
    """JSON output has explicit units, both start times and the resolved gear name."""
    _install_router(monkeypatch)
    payload = json.loads(asyncio.run(get_activities(start_date="2026-10-01", end_date="2026-10-09", output_format="json")))
    assert payload["total"] == 3
    first = payload["activities"][0]
    assert first["id"] == "i12"
    assert first["moving_time_s"] == 2700
    ride = next(a for a in payload["activities"] if a["id"] == "i10")
    assert ride["gear_name"] == "Canyon Ultimate"
    assert ride["training_load_intervals"] == 90
    assert ride["start_time_local"] == "2026-10-06T17:36:22"


def test_get_activity_details_json_and_thresholds(monkeypatch):
    """Details JSON carries the raw activity, custom fields with status and the thresholds snapshot."""
    _install_router(monkeypatch, {"/activity/": EXECUTION_ACTIVITY})
    text = asyncio.run(get_activity_details("i1"))
    assert "Date: 2026-10-06T17:36:22 local / 2026-10-06T15:36:22Z UTC" in text
    assert "Thresholds used for this activity" in text
    assert "FTP 234 W (icu_ftp, setting at the time)" in text
    assert "Power zones (% FTP, upper bounds): Z1 ≤55% (0-129 W)" in text
    assert "power meter Shimano FC-R9200P, serial 123" in text
    payload = json.loads(asyncio.run(get_activity_details("i1", output_format="json")))
    assert payload["times"]["start_time_utc"] == "2026-10-06T15:36:22Z"
    assert payload["thresholds"]["icu_ftp"] == 234
    epoc = next(f for f in payload["custom_fields"] if f["code"] == "EPOC")
    assert epoc["status"] == "value" and epoc["units"] == "ml/kg"


def test_get_activity_intervals_json_with_stream_metrics(monkeypatch):
    """Intervals JSON includes custom fields and per-interval stream statistics."""
    _install_router(monkeypatch, {"/streams": EXECUTION_STREAMS})
    payload = json.loads(asyncio.run(get_activity_intervals("i1", stream_types="Stamina", output_format="json")))
    work = payload["intervals"][1]
    assert work["stream_metrics"]["Stamina"]["name"] == "Garmin Stamina"
    assert work["stream_metrics"]["Stamina"]["first"] == 88.0
    assert work["stream_metrics"]["Stamina"]["samples"] == 300
    assert payload["groups"] == []


# ----------------------------------------------------------- analysis tools
def test_analyze_workout_execution_with_plan(monkeypatch):
    """Planned steps are aligned with intervals and per-step metrics are reported."""
    _install_router(monkeypatch, {"/activity/": EXECUTION_ACTIVITY, "/streams": EXECUTION_STREAMS})
    result = asyncio.run(analyze_workout_execution("i1"))
    assert "Workout execution for Threshold Ride (i1, Ride" in result
    assert "Planned workout: 2x5 min Threshold (event 5, 2026-10-06)" in result
    assert "Intervals.icu compliance 101%, RPE 7/10, feel 3/5, load 41 (Intervals.icu)" in result
    assert "Device/custom fields: Aerobic Effect [AerobicEffect]: 3.1; EPOC [EPOC]: 80.5 ml/kg" in result
    assert "Plan: 6 steps, 29:00 planned | Actual: 6 intervals, 29:00 | matched 6" in result
    assert "work steps in target: 2/2" in result
    assert "[plan 2] work (rep 1/2) 5:00 @ 240-250 W" in result
    assert "in range (99% of target)" in result
    assert "time in target range (±5%): 100%" in result
    assert "Stamina: start 88.0, end 82.0" in result
    assert "HR avg 155, max 160, start 150 -> end 159 bpm, drop in first 60 s of next interval" in result
    assert "[plan 3] rest (rep 1/2) 2:00 @ 110-130 W" in result
    payload = json.loads(asyncio.run(analyze_workout_execution("i1", output_format="json")))
    assert payload["summary"]["matched"] == 6
    assert payload["rows"][1]["adherence"]["status"] == "in range"


def test_analyze_workout_execution_without_plan(monkeypatch):
    """Without a paired event every interval is evaluated on its own."""
    activity = {k: v for k, v in EXECUTION_ACTIVITY.items() if k != "paired_event_id"}
    _install_router(monkeypatch, {"/activity/": activity, "/streams": EXECUTION_STREAMS})
    result = asyncio.run(analyze_workout_execution("i1"))
    assert "No planned workout paired with this activity; analysing intervals only." in result
    assert "No planned workout: 6 intervals, 29:00 in total" in result
    assert "[no planned step] -> Warmup (WORK) 10:00 from 0:00" in result
    assert ", fade " in result


def test_compare_power_streams_tool(monkeypatch):
    """The comparison reports device data, offsets and excluded samples; missing streams are explained."""
    primary = EXECUTION_STREAMS[1]["data"]
    streams = [
        EXECUTION_STREAMS[0],
        EXECUTION_STREAMS[1],
        {"type": "secondary_power", "name": "Power2", "custom": False, "data": [round(w * 1.03) for w in primary]},
    ]
    _install_router(monkeypatch, {"/activity/": EXECUTION_ACTIVITY, "/streams": streams})
    result = asyncio.run(compare_power_streams("i1"))
    assert "Power stream comparison for Threshold Ride" in result
    assert "Device data: device Edge 1040, power meter Shimano FC-R9200P, serial 123; power fields in file: power, Power2" in result
    assert "ratio" in result.lower()
    payload = json.loads(asyncio.run(compare_power_streams("i1", output_format="json")))
    assert 2.5 < payload["comparison"]["overall"]["mean_diff_pct"] < 3.5
    _install_router(monkeypatch, {"/activity/": EXECUTION_ACTIVITY, "/streams": EXECUTION_STREAMS})
    missing = asyncio.run(compare_power_streams("i1"))
    assert missing.startswith("Activity i1 has no 'secondary_power' stream.")


def test_analyze_climbs_tool(monkeypatch):
    """Climb analysis renders detected segments and explains missing altitude data."""
    time = list(range(1500))
    altitude = [600.0] * 300 + [600.0 + (t - 300) / 3 for t in range(300, 900)] + [800.0 - (t - 900) / 3 for t in range(900, 1500)]
    distance = [t * 3.0 for t in time]
    streams = [
        {"type": "time", "data": time},
        {"type": "altitude", "data": altitude},
        {"type": "distance", "data": distance},
        {"type": "watts", "data": [250 if 300 <= t < 900 else 120 for t in time]},
        {"type": "heartrate", "data": [150] * 1500},
        {"type": "velocity_smooth", "data": [3.0] * 1500},
        {"type": "Stamina", "custom": True, "data": [100 - t * 0.03 for t in time]},
    ]
    _install_router(monkeypatch, {"/activity/": EXECUTION_ACTIVITY, "/streams": streams})
    result = asyncio.run(analyze_climbs("i1"))
    assert "Climb analysis for Threshold Ride" in result
    assert "thresholds: climb >= 30 m and >= 2%" in result
    assert "Climb" in result and "Descent" in result
    assert "Garmin Stamina [Stamina]" in result
    payload = json.loads(asyncio.run(analyze_climbs("i1", output_format="json")))
    assert payload["result"]["summary"]["climbs"] == 1
    assert payload["result"]["summary"]["descents"] == 1
    no_alt = [s for s in streams if s["type"] != "altitude"]
    _install_router(monkeypatch, {"/activity/": EXECUTION_ACTIVITY, "/streams": no_alt})
    assert "Segment detection not possible" in asyncio.run(analyze_climbs("i1"))


# --------------------------------------------------------- wellness insights
def test_get_recovery_snapshot(monkeypatch):
    """The snapshot lists native and custom values per day, baselines, activities and planned events."""
    _install_router(monkeypatch)
    result = asyncio.run(get_recovery_snapshot(date_str="2026-10-09", days_back=2))
    assert "Recovery snapshot for athlete i1: 2026-10-09 and 2 day(s) before" in result
    assert "2026-10-09 (today, preliminary) | updated 2026-10-09T07:00:00+00:00 | locked no" in result
    assert "RHR 48 bpm | HRV 44 ms" in result
    assert "CTL 65.8 | ATL 66 | form -0.2 | ramp 1" in result
    assert "custom: Garmin Deep Sleep 85 min" in result
    assert "missing: HRV SDNN" in result
    assert "Baselines (last 42 days" in result
    assert "hrv: latest 44 (2026-10-09) | 7d mean" in result
    assert "42d baseline mean" in result
    assert "Activities 2026-10-07 to 2026-10-09 (3):" in result
    assert "'Grail gravel' (i11): 1:51:48, load 130 (Intervals.icu, P130/H50), IF 70%, feel 2/5, RPE 5/10 | device fields: Aerobic Effect 3.3" in result
    assert "Planned on 2026-10-09 (1):" in result
    assert "Workout (Ride) '2x5 min Threshold' (event 5), planned 29:00, planned load 40, done: i1" in result
    payload = json.loads(asyncio.run(get_recovery_snapshot(date_str="2026-10-09", days_back=1, output_format="json")))
    assert payload["days"][-1]["preliminary"] is True
    assert payload["days"][-1]["custom"]["GarminSleepDeepMinutes"] == 85.0
    assert payload["baselines"][0]["metric"] == "hrv"
    assert asyncio.run(get_recovery_snapshot(days_back=99)).startswith("Error: days_back")


def test_get_wellness_trends_and_correlations(monkeypatch):
    """Trends include baselines, eFTP per sport, weight slope and correlations."""
    _install_router(monkeypatch)
    result = asyncio.run(
        get_wellness_trends(start_date="2026-09-20", end_date="2026-10-09", metrics="hrv,weight,eftp_Ride",
                            windows="7,14", correlations="hrv:readiness,hrv:restingHR:1")
    )
    assert "Wellness trends for athlete i1, 2026-09-20 to 2026-10-09" in result
    assert "hrv" in result and "eftp_Ride" in result
    assert "kg/week" in result
    assert "statistical association only" in result
    payload = json.loads(asyncio.run(get_wellness_trends(metrics="hrv", windows="7", output_format="json")))
    assert payload["metrics"][0]["metric"] == "hrv"
    assert payload["metrics"][0]["baseline"]["n"] >= 20
    assert asyncio.run(get_wellness_trends(windows="x")).startswith("Error: windows")
    assert asyncio.run(get_wellness_trends(correlations="hrv")).startswith("Error: correlation")


def test_get_nutrition_summary(monkeypatch):
    """Nutrition totals exclude unlogged days and report the training load alongside."""
    _install_router(monkeypatch)
    result = asyncio.run(get_nutrition_summary(start_date="2026-09-12", end_date="2026-10-09", windows="7,28"))
    assert "Nutrition summary for athlete i1, 2026-09-12 to 2026-10-09" in result
    assert "Training load per day (Intervals.icu, last 7 days with activities): 2026-10-06 90, 2026-10-07 130, 2026-10-09 30; total in range 250" in result
    payload = json.loads(asyncio.run(get_nutrition_summary(start_date="2026-09-12", end_date="2026-10-09", windows="28", output_format="json")))
    window = payload["nutrition"]["windows"]["28"]
    assert window["days_missing_intake"] == 7
    assert window["days_logged"] == 21
    assert window["burn_mean_kcal"] == 2800.0
    assert payload["weight"]["windows"]["28"]["slope_kg_per_week"] < 0


# -------------------------------------------------------------- summaries
def test_get_training_summary_groups(monkeypatch):
    """Totals per week, sport and gear with separate loads and custom field aggregates."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(get_training_summary("2026-10-01", "2026-10-09", group_by="week"))
    assert "Training summary for athlete i1, 2026-10-01 to 2026-10-09, grouped by week:" in result
    assert "2026-10-05 (ISO 2026-W41): 3 sessions | 3:57:49 moving (4:08:40 elapsed) | 96.4 km | +800 m" in result
    assert "Load (Intervals.icu): total 250 = power 220 / HR 138 / pace 30" in result
    assert "Time in power zones: Z2 24:49, Z4 29:13" in result
    assert "- Ride: 1 sessions, 1:21:01, 40.4 km, load 90" in result
    assert "- gear Canyon Ultimate (b1): 1 sessions" in result
    assert "feel 2: 1, 3: 1, 4: 1 | RPE mean 5.0 | sessions >= 3 h: 0 | longest 1:51:48 ('Grail gravel')" in result
    assert "Custom fields (e.g. device loads, kept separate from Intervals.icu load): Aerobic Effect [AerobicEffect] mean 3.4 (n 2)" in result
    epoc = asyncio.run(get_training_summary("2026-10-01", "2026-10-09", group_by="total", output_format="json"))
    assert json.loads(epoc)["overall"]["custom_fields"].get("EPOC") is None  # no EPOC values in the fixtures
    assert "End of period (2026-10-09): CTL 65.8, ATL 66.0, form -0.2, ramp 1.0" in result
    activities_call = next(c for c in calls if "/activities" in c[0])
    assert "AerobicEffect" in activities_call[1]["fields"]  # custom codes are requested explicitly
    by_sport = asyncio.run(get_training_summary("2026-10-01", "2026-10-09", group_by="sport", sport_types="Run"))
    assert "Run: 1 sessions" in by_sport and "GravelRide" not in by_sport
    payload = json.loads(asyncio.run(get_training_summary("2026-10-01", "2026-10-09", group_by="gear", output_format="json")))
    assert {g["group"] for g in payload["groups"]} == {"Canyon Ultimate (b1)", "Canyon Grail (b2)", "no gear"}
    assert asyncio.run(get_training_summary("2026-10-01", group_by="year")).startswith("Error: group_by")


# ------------------------------------------------------- workout validation
def test_preview_and_validate_workout(monkeypatch):
    """Validation expands repeats, flags problems and checks the calendar for duplicates."""
    _install_router(monkeypatch)
    doc = EVENT_DATA["workout_doc"]
    preview = asyncio.run(preview_workout(doc, "Ride", moving_time=1740))
    assert preview.startswith("Workout validation (Ride): OK")
    assert "Totals: 6 steps (4 with targets, 0 open-ended), planned duration 29:00" in preview
    assert "Preview (Intervals.icu workout text):" in preview
    bad = {"steps": [{"power": {"value": 300, "units": "%ftp"}}, {"reps": 0, "steps": []}, {"duration": 60, "pace": {"value": 90, "units": "%pace"}}]}
    result = asyncio.run(validate_workout(bad, "Ride", name="2x5 min Threshold", start_date="2026-10-06"))
    assert "Workout validation (Ride): ERRORS" in result
    assert "ERROR: step 1: step needs a duration (seconds) or a distance (metres)" in result
    assert "warning: step 1: power target 300 %ftp is outside the plausible range 20-250" in result
    assert "ERROR: step 2: reps must be a positive integer" in result
    assert "warning: step 3: pace target on a Ride workout" in result
    assert "already exists on 2026-10-06 (id 5)" in result
    assert "Calendar on 2026-10-06: Workout (Ride) '2x5 min Threshold' (5, 29:00)" in result
    payload = json.loads(asyncio.run(validate_workout(doc, "Ride", output_format="json")))
    assert payload["ok"] is True and payload["totals"]["has_warmup"] is True


# ------------------------------------------------------------------ status
def test_get_server_status(monkeypatch):
    """Status reports permissions, tool classes, API reachability and custom item counts without secrets."""
    _install_router(monkeypatch)
    result = asyncio.run(get_server_status())
    assert "Futureweb Intervals MCP" in result
    assert "Permissions enabled: read (MCP_PERMISSIONS)" in result
    assert "hidden (class not enabled): add_activity_message [write]" in result
    assert "Intervals.icu API: OK - sport settings for 2 sport group(s) readable" in result
    assert "Custom items: 5 activity fields, 1 activity streams, 1 interval fields, 1 wellness fields (streams: Stamina)" in result
    assert "test" not in result.split("API key")[1][:40]
    payload = json.loads(asyncio.run(get_server_status(output_format="json")))
    assert payload["api"]["ok"] is True
    assert "get_recovery_snapshot" in payload["tools_by_class"]["read"]
    assert payload["tools_hidden"]["delete_event"] == "destructive"
