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
    "intervals_mcp_server.tools.workout_library.make_intervals_request",
    "intervals_mcp_server.tools.training_review.make_intervals_request",
    "intervals_mcp_server.tools.hr_pace_curves.make_intervals_request",
    "intervals_mcp_server.tools.power_curves.make_intervals_request",
    "intervals_mcp_server.tools.report.make_intervals_request",
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
    assert "Plan source: event 5 ('2x5 min Threshold', 2026-10-06)" in result
    assert "Intervals.icu compliance 101%, RPE 7/10, feel 3/5, load 41 (Intervals.icu)" in result
    # Only the custom fields assigned to the sport (sport settings activity_field_ids) are listed.
    assert "Device/custom fields (assigned to the sport): EPOC [EPOC]: 80.5 ml/kg" in result
    assert "AerobicEffect" not in result
    assert "Plan: 6 steps, 29:00 planned | Actual: 6 intervals, 29:00 in total | matched 6" in result
    assert "extended beyond the plan" not in result
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
    assert "No planned workout paired with this activity (paired_event_id is empty); analysing intervals only." in result
    assert "Possible planned workouts (read-only suggestion, nothing was paired):" in result
    assert "- event 5 '2x5 min Threshold' 2026-10-06 Ride 29:00, score" in result
    assert "same sport" in result and "already paired with i1" in result
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
    assert "Custom fields (aggregated by units and meaning: sums only for additive values, device loads kept separate from the Intervals.icu load): Aerobic Effect [AerobicEffect] mean 3.3 (median 3.3, min 3.3, max 3.3; n 1) (1 value(s) from sports without this field ignored)" in result  # not assigned to Ride
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


def test_analyze_workout_execution_extended_ride_with_provided_doc(monkeypatch):
    """A caller-provided workout document is used when the event was deleted; extra riding after
    the plan is reported as additional training and not counted against the plan."""
    from tests.sample_data import EXTENDED_ACTIVITY, EXTENDED_INTERVALS, EXTENDED_STREAMS  # pylint: disable=import-outside-toplevel

    _install_router(monkeypatch, {"/activity/": EXTENDED_ACTIVITY, "/streams": EXTENDED_STREAMS,
                                  "/intervals": EXTENDED_INTERVALS, "/events": []})
    result = asyncio.run(analyze_workout_execution("i2", planned_workout_doc=EVENT_DATA["workout_doc"]))
    assert "Plan source: workout document provided by the caller" in result
    assert "Plan: 6 steps, 29:00 planned | Actual: 9 intervals, 1:27:00 in total | matched 6" in result
    assert "The activity was extended beyond the plan: plan part 29:00, additional training 58:00" in result
    assert "work steps in target: 2/2" in result
    assert "Additional training after the plan: 58:00 from 29:00 to 1:27:00 (3 interval(s))" in result
    assert "hardest part: 0:50 at avg 430 W (max 480 W), HR 165 bpm" in result
    assert "[no planned step]" not in result
    payload = json.loads(asyncio.run(analyze_workout_execution("i2", planned_workout_doc=EVENT_DATA["workout_doc"], output_format="json")))
    assert payload["summary"]["extended_beyond_plan"] is True
    assert payload["summary"]["extension_s"] == 3480
    assert payload["summary"]["plan_part_actual_s"] == 1740
    assert payload["extension"]["intervals"] == 3
    assert payload["plan_source"] == "workout document provided by the caller"
    # Without a plan and without candidate events the suggestion block says so.
    plain = asyncio.run(analyze_workout_execution("i2"))
    assert "No planned workouts found on the activity's day or the days around it." in plain
    assert "No planned workout: 9 intervals, 1:27:00 in total" in plain


# ----------------------------------------------------- phase 2: new tools and levels
def test_get_training_plan(monkeypatch):
    """The ATP view groups plan phases, weekly targets and races and reports the assigned plan."""
    from intervals_mcp_server.server import get_training_plan  # pylint: disable=import-outside-toplevel
    from tests.sample_data import PLAN_EVENTS  # pylint: disable=import-outside-toplevel

    calls = []
    _install_router(monkeypatch, {"/training-plan": {"training_plan_id": None}, "/events": PLAN_EVENTS,
                                  "/fitness-model-events": [{"start_date_local": "2026-09-01T00:00:00", "category": "SET_EFTP", "eftp": 230}]}, calls=calls)
    result = asyncio.run(get_training_plan(start_date="2026-10-01", end_date="2027-09-01"))
    assert "Assigned plan: none" in result
    assert "Phases / season markers (1):" in result and "- 2026-10-13 to 2026-11-09 Plan (Ride): Base 2 (event 901) | aerobic base" in result
    assert "Weekly targets (1):" in result and "load 450, time 10:00:00, weekly target" in result
    assert "Races (1):" in result and "Race A (Ride): Ötztaler (event 903)" in result
    assert "ignored" not in result
    assert "SET_EFTP" in result
    assert next(c for c in calls if "/events" in c[0])[1]["category"] == "PLAN,TARGET,RACE_A,RACE_B,RACE_C,SEASON_START"
    payload = json.loads(asyncio.run(get_training_plan(start_date="2026-10-01", end_date="2027-09-01", output_format="json")))
    assert [r["id"] for r in payload["races"]] == [903]
    # Fallback when the API rejects the category filter: all events are fetched and filtered.
    state = {"n": 0}
    async def flaky(url=None, **kwargs):
        if "/events" in url:
            state["n"] += 1
            return {"error": True, "message": "bad category"} if kwargs.get("params", {}).get("category") else PLAN_EVENTS
        if "/fitness-model-events" in url:
            return []
        return {}
    for target in PATCH_TARGETS:
        monkeypatch.setattr(target, flaky)
    result = asyncio.run(get_training_plan(start_date="2026-10-01", end_date="2027-09-01"))
    assert "Races (1):" in result and state["n"] == 2


def test_get_library_workout(monkeypatch):
    """A library workout is rendered with its steps; errors are reported."""
    from intervals_mcp_server.server import get_library_workout  # pylint: disable=import-outside-toplevel
    from tests.sample_data import LIBRARY_WORKOUT  # pylint: disable=import-outside-toplevel

    _install_router(monkeypatch, {"/workouts/": LIBRARY_WORKOUT})
    result = asyncio.run(get_library_workout("77"))
    assert "Library workout 77: SST 3x12 (Ride, folder 301129)" in result
    assert "Steps (rendered):" in result and "240W-250W" in result
    assert json.loads(asyncio.run(get_library_workout("77", output_format="json")))["id"] == 77
    _install_router(monkeypatch, {"/workouts/": {"error": True, "message": "404"}})
    assert asyncio.run(get_library_workout("77")).startswith("Error fetching library workout")


def test_update_sport_settings_validation_and_put(monkeypatch):
    """Only passed values are sent after range checks; LTHR must stay below max HR."""
    from intervals_mcp_server.server import update_sport_settings  # pylint: disable=import-outside-toplevel

    calls = []
    _install_router(monkeypatch, {"/sport-settings/1": {"id": 1, "ftp": 240, "lthr": 165}}, calls=calls)
    assert asyncio.run(update_sport_settings("Ride")).startswith("Error: pass at least one value")
    assert asyncio.run(update_sport_settings("Ride", ftp=5)).startswith("Error: ftp must be between 50 and 600 W")
    assert asyncio.run(update_sport_settings("Ride", lthr=190)).startswith("Error: LTHR (190) must be below max HR (188)")
    assert asyncio.run(update_sport_settings("Swim", ftp=200)).startswith("Error: expected exactly one sport setting for 'Swim'")
    result = asyncio.run(update_sport_settings("Ride", ftp=240))
    put = next(c for c in calls if c[2] == "PUT")
    assert put[0] == "/athlete/i1/sport-settings/1"
    assert "Updated sport setting 1 (Ride): ftp 234 -> 240" in result
    assert "Zones were not recalculated" in result


def test_get_gear_details_reminders(monkeypatch):
    """Maintenance reminders show usage against their target."""
    from tests.sample_data import GEAR_WITH_REMINDER  # pylint: disable=import-outside-toplevel

    _install_router(monkeypatch, {"/gear": GEAR_WITH_REMINDER})
    result = asyncio.run(get_gear_details("b1"))
    assert "- Maintenance reminders (1):" in result
    assert "  - Chain wax: 250/400 km (62% used), last reset 2026-08-01" in result


def test_get_activities_power_meter_filter(monkeypatch):
    """The power meter filter keeps activities whose power meter name contains the text."""
    _install_router(monkeypatch)
    result = asyncio.run(get_activities(start_date="2026-10-01", end_date="2026-10-09", power_meter="shimano", detail_level="compact"))
    assert "Activities 1-1 of 1" in result and "i10" in result and "PM Shimano FC-R9200P" in result
    assert "power meter contains 'shimano'" in result


def test_detail_levels_details_intervals_snapshot(monkeypatch):
    """compact / standard / full change the amount of output; sport-assigned fields are separated."""
    _install_router(monkeypatch, {"/activity/": EXECUTION_ACTIVITY, "/streams": EXECUTION_STREAMS})
    compact = asyncio.run(get_activity_details("i1", detail_level="compact"))
    assert compact.startswith("Threshold Ride (i1, Ride) 2026-10-06 17:36 local")
    assert "Load 41 (Intervals.icu; power n/a / HR n/a / pace n/a) | IF n/a% | NP n/a W" in compact
    assert "device Edge 1040, power meter Shimano FC-R9200P, power fields power, Power2" in compact
    assert "Custom fields (assigned to this sport): EPOC [EPOC]: 80.5 ml/kg" in compact
    assert "Aerobic Effect" not in compact
    assert "Data: 6 streams (1 custom)" in compact
    assert len(compact.splitlines()) <= 8
    standard = asyncio.run(get_activity_details("i1"))
    assert "- EPOC [EPOC]: 80.5 ml/kg" in standard
    assert "Not assigned to this sport in the Intervals.icu settings (values as stored, treat with care): Aerobic Effect [AerobicEffect]: 3.1" in standard
    full = asyncio.run(get_activity_details("i1", detail_level="full"))
    assert "Other Fields:" in full
    assert asyncio.run(get_activity_details("i1", detail_level="huge")).startswith("Error: detail_level")
    payload = json.loads(asyncio.run(get_activity_details("i1", output_format="json")))
    assert next(f for f in payload["custom_fields"] if f["code"] == "EPOC")["assigned_to_sport"] is True

    compact_iv = asyncio.run(get_activity_intervals("i1", detail_level="compact", stream_types="Stamina"))
    assert compact_iv.startswith("Intervals of i1 (analysed True):")
    assert "[2] WORK | 5:00 (10:00-15:00, idx 600-900) | avg 242 W NP n/a max 260 | HR 155/160 | cad 88 | streams Stamina 88→82.0 (min 82.0)" in compact_iv
    full_iv = asyncio.run(get_activity_intervals("i1", detail_level="full"))
    assert "Stream Metrics (samples 600-899):" in full_iv and "Garmin Stamina [Stamina]" in full_iv

    _install_router(monkeypatch)
    compact_snap = asyncio.run(get_recovery_snapshot(date_str="2026-10-09", days_back=1, detail_level="compact"))
    assert "custom:" not in compact_snap and "Baselines" in compact_snap
    standard_snap = asyncio.run(get_recovery_snapshot(date_str="2026-10-09", days_back=1))
    assert "custom: Garmin Deep Sleep 85 min" in standard_snap
    assert asyncio.run(get_recovery_snapshot(detail_level="x")).startswith("Error: detail_level")


def test_get_activity_report_single_call(monkeypatch):
    """The report combines overview, plan vs execution, power check, climbs and data notes."""
    from intervals_mcp_server.server import get_activity_report  # pylint: disable=import-outside-toplevel
    from tests.sample_data import EXTENDED_ACTIVITY, EXTENDED_INTERVALS, EXTENDED_STREAMS  # pylint: disable=import-outside-toplevel

    primary = EXECUTION_STREAMS[1]["data"]
    streams = EXECUTION_STREAMS + [{"type": "secondary_power", "name": "Power2", "custom": False, "data": [round(w * 1.03) for w in primary]}]
    calls = []
    with_power2 = dict(EXECUTION_ACTIVITY, stream_types=EXECUTION_ACTIVITY["stream_types"] + ["secondary_power"])
    _install_router(monkeypatch, {"/activity/": with_power2, "/streams": streams}, calls=calls)
    result = asyncio.run(get_activity_report("i1"))
    assert result.startswith("== Overview\nThreshold Ride (i1, Ride) 2026-10-06 17:36 local")
    assert "== Plan vs execution (event 5 ('2x5 min Threshold'))" in result
    assert "work steps in target: 2/2" in result
    assert "== Second power meter check (watts vs secondary_power, no calibration)" in result
    assert "mean diff +" in result and "ratio 1.0" in result
    assert "== Climbs" not in result  # intervals exist and little elevation
    assert "== Data quality" not in result  # complete device data, intervals present
    stream_calls = [c for c in calls if "/streams" in c[0]]
    assert len(stream_calls) == 1 and "secondary_power" in stream_calls[0][1]["types"] and "Stamina" in stream_calls[0][1]["types"]
    assert sum(1 for c in calls if "/activity/" in c[0] and "/streams" not in c[0] and "/intervals" not in c[0]) == 1

    _install_router(monkeypatch, {"/activity/": EXTENDED_ACTIVITY, "/streams": EXTENDED_STREAMS, "/intervals": EXTENDED_INTERVALS, "/events": []})
    plain = asyncio.run(get_activity_report("i2", planned_workout_doc=EVENT_DATA["workout_doc"], include_climbs=False))
    assert "== Plan vs execution (workout document provided by the caller)" in plain
    assert "additional training 58:00" in plain
    assert "- only one power stream recorded; no power meter comparison possible" in plain
    assert "- compliance 0 only means the activity is not paired with a planned workout" in plain
    payload = json.loads(asyncio.run(get_activity_report("i2", output_format="json")))
    assert payload["execution"]["summary"]["actual_intervals"] == 9
    assert payload["power_check"] is None
    assert payload["api_calls"] == 3


# ----------------------------------------------------- phase 3: wellness periods
def test_get_wellness_trends_separates_period_lookback_and_baseline(monkeypatch):
    """Regression: requested period, fetched history and baseline are stated separately and the
    day counts per metric refer to the requested period; native metrics carry units."""
    calls = []
    _install_router(monkeypatch, calls=calls)
    result = asyncio.run(get_wellness_trends(start_date="2026-09-20", end_date="2026-10-09", metrics="hrv,restingHR,readiness"))
    assert result.startswith(
        "Wellness trends for athlete i1, 2026-09-20 to 2026-10-09: 20 days requested; 62 days fetched (2026-08-09 to "
        "2026-10-09, including a 42-day lookback for rolling windows and the baseline); personal baseline = the 42 days ending 2026-10-09"
    )
    assert "hrv (ms): period 2026-09-20 to 2026-10-09, 20 days, 20 with values, 0 missing" in result
    assert "restingHR (bpm): period 2026-09-20 to 2026-10-09, 20 days" in result
    assert "readiness (/100): period" in result
    assert "Personal baseline (42 days 2026-08-29 to 2026-10-09, n=30)" in result
    wellness_call = next(c for c in calls if "/wellness" in c[0])
    assert wellness_call[1]["oldest"] == "2026-08-09"
    payload = json.loads(asyncio.run(get_wellness_trends(start_date="2026-09-20", end_date="2026-10-09", metrics="hrv", output_format="json")))
    assert payload["windows"]["requested"]["days"] == 20 and payload["windows"]["fetched"]["days"] == 62
    assert payload["metrics"][0]["days_total"] == 20 and payload["metrics"][0]["units"] == "ms"


# ------------------------------------------------- phase 3: report levels and checks
def test_get_activity_report_levels_and_data_quality(monkeypatch):
    """compact = core numbers + key findings + data quality; no fake power-meter comparison without
    a usable second stream; identical streams are reported, gear positions are not called teeth."""
    from intervals_mcp_server.server import get_activity_report  # pylint: disable=import-outside-toplevel

    gear_items = CUSTOM_ITEMS_DATA + [{"id": 20, "type": "ACTIVITY_STREAM", "name": "RearGear",
                                       "content": {"code": "RearGear", "type": "numeric", "units": "cog", "script": "..."}}]
    primary = EXECUTION_STREAMS[1]["data"]
    gear = {"type": "RearGear", "custom": True, "data": [(i // 100) % 11 + 1 for i in range(len(primary))]}
    identical = EXECUTION_STREAMS + [{"type": "secondary_power", "custom": False, "data": list(primary)}, gear]
    activity = dict(EXECUTION_ACTIVITY, stream_types=EXECUTION_ACTIVITY["stream_types"] + ["secondary_power", "RearGear"],
                    icu_training_load=41, TrainingLoad=55.0)
    _install_router(monkeypatch, {"/activity/": activity, "/streams": identical, "/custom-item": gear_items})
    standard = asyncio.run(get_activity_report("i1"))
    assert "== Second power meter check" not in standard
    assert "- both power streams are identical (the same sensor recorded twice); no comparison" in standard
    assert "- RearGear: values 1-11 look like gear positions (index), not tooth counts; units 'cog' from the definition are not applied" in standard
    compact = asyncio.run(get_activity_report("i1", detail_level="compact"))
    assert "== Key findings" in compact and "== Plan vs execution" not in compact
    assert "- Plan: 6/6 steps executed; work steps 242/240 W vs 240-250 W planned (2/2 within ±5%" in compact
    assert len(compact) < len(standard) / 2
    full = asyncio.run(get_activity_report("i1", detail_level="full"))
    assert "Aerobic Effect [AerobicEffect]" in full  # every custom field, not only the ones assigned to the sport
    assert "Aerobic Effect" not in standard
    sparse = EXECUTION_STREAMS + [{"type": "secondary_power", "custom": False, "data": [None] * (len(primary) - 100) + [200] * 100}]
    _install_router(monkeypatch, {"/activity/": activity, "/streams": sparse})
    assert "- second power stream has only 100 usable samples paired with the primary; no comparison" in asyncio.run(get_activity_report("i1"))
    payload = json.loads(asyncio.run(get_activity_report("i1", detail_level="compact", output_format="json")))
    assert payload["power_check"]["status"] == "insufficient" and payload["key_findings"]
    assert "rows" not in payload["execution"]
    assert asyncio.run(get_activity_report("i1", detail_level="x")).startswith("Error: detail_level")


def test_get_activity_intervals_planned_step_types(monkeypatch):
    """The Intervals.icu type is kept as stored; the planned step type is shown separately on request."""
    labelled_work = {"id": "i1", "analyzed": True, "icu_groups": [],
                     "icu_intervals": [dict(i, type="WORK") for i in EXECUTION_INTERVALS["icu_intervals"]]}
    calls = []
    _install_router(monkeypatch, {"/activity/": EXECUTION_ACTIVITY, "/intervals": labelled_work}, calls=calls)
    plain = asyncio.run(get_activity_intervals("i1"))
    assert "Planned step" not in plain and not any("/events/" in c[0] for c in calls)
    text = asyncio.run(get_activity_intervals("i1", include_planned_types=True, detail_level="compact"))
    assert "Planned step per interval (event 5 ('2x5 min Threshold'); the Intervals.icu type is kept as stored):" in text
    assert "  [3] Intervals.icu WORK | plan step 3 rest 2:00 <- type differs from the plan" in text
    assert "  [2] Intervals.icu WORK | plan step 2 work 5:00\n" in text
    payload = json.loads(asyncio.run(get_activity_intervals("i1", planned_workout_doc=EVENT_DATA["workout_doc"], output_format="json")))
    assert payload["intervals"][2]["type"] == "WORK" and payload["intervals"][2]["planned_step"]["kind"] == "rest"
    unpaired = {k: v for k, v in EXECUTION_ACTIVITY.items() if k != "paired_event_id"}
    _install_router(monkeypatch, {"/activity/": unpaired, "/intervals": labelled_work})
    assert "Planned step types: not available (no planned workout paired with this activity)." in asyncio.run(
        get_activity_intervals("i1", include_planned_types=True))


def test_get_training_plan_empty_and_unknown(monkeypatch):
    """No assigned plan and no plan events give a clear empty answer; an unreadable plan is 'unknown', not 'none'."""
    from intervals_mcp_server.server import get_training_plan  # pylint: disable=import-outside-toplevel

    _install_router(monkeypatch, {"/training-plan": {"training_plan_id": None}, "/events": [], "/fitness-model-events": []})
    text = asyncio.run(get_training_plan(start_date="2026-10-01", end_date="2027-01-01"))
    assert "Assigned plan: none (no Intervals.icu training plan is applied to this athlete)" in text
    assert "No plan phases, weekly targets, races or fitness-model events in this range." in text
    _install_router(monkeypatch, {"/training-plan": {"error": True, "message": "403 Forbidden"}, "/events": [], "/fitness-model-events": []})
    unknown = asyncio.run(get_training_plan(start_date="2026-10-01", end_date="2027-01-01"))
    assert "Assigned plan: unknown (the training plan could not be read: 403 Forbidden)" in unknown
    payload = json.loads(asyncio.run(get_training_plan(start_date="2026-10-01", end_date="2027-01-01", output_format="json")))
    assert payload["training_plan_error"] == "403 Forbidden" and payload["phases"] == []
