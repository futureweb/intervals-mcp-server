"""
Phase 6 tests: data audit and provenance, fueling, W′ balance, weather and same-route history.

Pure helpers (utils.provenance, utils.fueling, utils.activity_context) are tested directly; the
tools (get_activity_data_audit, get_fueling_analysis, get_activity_report, get_activity_details,
get_durability) run against synthetic fixtures through the router of test_coaching_tools.
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
    get_activity_data_audit,
    get_activity_details,
    get_activity_report,
    get_durability,
    get_fueling_analysis,
)
from intervals_mcp_server.utils.activity_context import (  # pylint: disable=wrong-import-position
    compass,
    route_history,
    route_history_lines,
    weather_line,
    weather_summary,
    wprime_line,
    wprime_summary,
)
from intervals_mcp_server.utils.custom_fields import index_custom_items  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.field_policy import start_end_pairs  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.fueling import (  # pylint: disable=wrong-import-position
    activity_fueling,
    correlation_text,
    fueling_fields,
    fueling_line,
    intake_distribution,
    period_fueling,
)
from intervals_mcp_server.utils.provenance import (  # pylint: disable=wrong-import-position
    coverage_summary,
    field_inventory,
    freshness,
    garmin_activity_id,
    is_strava_stub,
    laps_and_intervals,
    listing_status,
    provenance_notes,
    recording_summary,
    sensor_summary,
    source_summary,
    span,
    stream_inventory,
)
from tests.sample_data import (  # pylint: disable=wrong-import-position
    AUDIT_LIST,
    AUDIT_LIST_WITH_UPLOAD,
    DUPLICATE_ACTIVITY,
    FUELING_ACTIVITY,
    FUELING_INTERVALS,
    FUELING_ITEMS,
    FUELING_PERIOD,
    FUELING_SPORT_SETTINGS,
    FUELING_STREAMS,
    ROUTE_LIST,
    STRAVA_STUB,
)
from tests.test_coaching_tools import _install_router  # pylint: disable=wrong-import-position

DEFS = index_custom_items(FUELING_ITEMS)
FIELDS = DEFS["ACTIVITY_FIELD"]
STREAM_DEFS = DEFS["ACTIVITY_STREAM"]
RIDE_EXPECTED = {"EPOC", "TrainingEffectSelect", "Sweatloss", "PerformanceCondition", "Staminaatstart", "Staminaatend", "NewMetric"}
TIME = FUELING_STREAMS[0]["data"]
W_BAL = FUELING_STREAMS[5]["data"]


def _routes(monkeypatch, calls=None, **overrides):
    base = {"/custom-item": FUELING_ITEMS, "/sport-settings": FUELING_SPORT_SETTINGS, "/activity/": FUELING_ACTIVITY,
            "/streams": FUELING_STREAMS, "/activities": AUDIT_LIST, "/routes/": {"route_id": 77, "name": "Lake loop"},
            "/intervals": {"icu_intervals": FUELING_INTERVALS, "icu_groups": []}}
    base.update(overrides)
    return _install_router(monkeypatch, base, calls=calls)


# ------------------------------------------------------------------------------- provenance
def test_source_and_garmin_ids():
    """Garmin sync, a Garmin export upload and the bridge's upload mode are told apart; Strava stubs are detected."""
    assert garmin_activity_id(FUELING_ACTIVITY) == ("123456789", "Garmin Connect sync")
    assert garmin_activity_id({"source": "UPLOAD", "external_id": "42_ACTIVITY.fit"})[0] == "42"
    assert garmin_activity_id({"source": "UPLOAD", "external_id": "garmin:43"}) == ("43", "Garmin Intervals Bridge upload (original FIT)")
    assert garmin_activity_id({"source": "UPLOAD", "external_id": "ride.fit"}) == (None, None)
    info = source_summary(FUELING_ACTIVITY)
    assert info["text"] == "Garmin Connect sync, FIT file, Garmin activity 123456789, also on Strava (987)"
    assert source_summary({"source": "UPLOAD", "external_id": "42_ACTIVITY.fit", "file_type": "fit"})["text"] == (
        "upload of the Garmin export (original FIT), Garmin activity 42")
    assert source_summary({"source": "ZWIFT", "external_id": "z.fit"})["text"] == "Zwift sync, external id z.fit"
    assert is_strava_stub(STRAVA_STUB)
    assert is_strava_stub({"id": "i1", "source": "STRAVA"})
    assert not is_strava_stub(dict(STRAVA_STUB, _note=None, moving_time=100))
    assert not is_strava_stub(FUELING_ACTIVITY)
    assert (span(None), span(45), span(960), span(4500), span(3 * 86400)) == ("n/a", "45 s", "16 min", "1:15 h", "3.0 days")


def test_freshness_and_fields_defined_after_the_analysis():
    """Upload delay after the end, re-analysis and fields whose definition is newer than the analysis."""
    info = freshness(FUELING_ACTIVITY, FIELDS, RIDE_EXPECTED)
    assert info["upload_delay_s"] == 100 and info["analysed_after_upload_s"] == 10500 and info["reanalysed"]
    assert info["fields_changed_after_analysis"] == ["NewMetric"]
    assert freshness({"start_date": "x"})["upload_delay_s"] is None


def test_recording_laps_and_sensors():
    """Recording stops and gaps, laps vs intervals with edits, sensor identity notes."""
    rec = recording_summary(FUELING_ACTIVITY, TIME)
    assert rec["not_recorded_s"] == 150 and rec["recording_stops"] == 1 and rec["stationary_while_recording_s"] == 50
    gaps = rec.get("gaps") or {}
    assert gaps.get("count") == 1 and gaps.get("longest") == {"at_s": 599, "duration_s": 101}
    laps = laps_and_intervals(FUELING_ACTIVITY, FUELING_INTERVALS)
    assert laps["laps"] == 3 and laps["intervals"] == 5 and laps["work_intervals"] == 3 and laps["edited"]
    assert laps["notes"][0].startswith("intervals were edited by hand")
    assert "3 FIT lap(s) vs 5 interval(s)" in laps["notes"][1]
    assert laps_and_intervals({"icu_lap_count": 1}, [])["notes"] == ["no intervals (Intervals.icu detected none, or the file was not processed)"]
    assert not laps_and_intervals({"icu_lap_count": 2}, None)["notes"]
    sensors = sensor_summary(FUELING_ACTIVITY)
    assert sensors["power"] and sensors["heart_rate"] and sensors["gps"] and not sensors["second_power_stream"]
    assert "power meter name/serial not in the file (identity unknown)" in sensors["notes"]
    estimated = sensor_summary({"stream_types": ["watts"], "device_watts": False, "power_meter_battery": "LOW", "device_name": "x"})
    assert set(estimated["notes"]) == {"power meter name/serial not in the file (identity unknown)",
                                       "power is not from a power meter (estimated)", "power meter battery status LOW"}


def test_stream_and_field_inventory():
    """Streams usual for the sport but missing, coverage issues, zero placeholders vs real zeros."""
    baseline = {"n": 3, "streams": {"secondary_power": 3, "Stamina": 3, "watts": 3}, "fields": {"PerformanceCondition": 3}, "label": "Ride"}
    streams = stream_inventory(FUELING_ACTIVITY, STREAM_DEFS, FUELING_STREAMS, baseline)
    assert streams["custom"] == ["Stamina"] and streams["custom_defined_absent"] == ["CarbsEaten"]
    assert streams["usually_present_missing"] == [{"type": "secondary_power", "present_on": 3}]
    assert not streams["listed_not_returned"]
    assert "heartrate: 2.5 % of the samples without a value (dropouts or pauses)" in streams["issues"]
    assert "heartrate: 1.2 % of the samples are 0 (sensor contact lost?)" in streams["issues"]
    gone = stream_inventory(FUELING_ACTIVITY, STREAM_DEFS, [], None)
    assert gone["issues"] == ["the streams endpoint returned none of the listed streams (file not retained, e.g. a filtered duplicate)"]
    partial = stream_inventory(FUELING_ACTIVITY, STREAM_DEFS, [{"type": "time", "data": [0, 1]}, {"type": "watts", "data": [None, None]}])
    assert partial["issues"][:2] == ["listed but not returned: heartrate, latlng, Stamina", "watts: listed but empty (no values)"]
    fields = field_inventory(FUELING_ACTIVITY, FIELDS, RIDE_EXPECTED, baseline)
    assert fields["zero_placeholder"] == ["PerformanceCondition"] and fields["absent"] == ["TrainingEffectSelect", "NewMetric"]
    assert set(fields["value"]) == {"EPOC", "Sweatloss", "Staminaatstart", "Staminaatend"}
    assert fields["device_file_without_value"] == ["PerformanceCondition", "NewMetric"]
    assert fields["usually_present_missing"] == [{"code": "PerformanceCondition", "present_on": 3}]
    assert set(fields["values_outside_scope"]) == {"FluidIntake", "SodiumMg"}
    unassigned = field_inventory({"FlightTime": 0.0, "EPOC": 1.0}, FIELDS, None)
    assert unassigned["expected_known"] is False and unassigned["zero"] == ["FlightTime"] and unassigned["value"] == ["EPOC"]


def test_listing_status_and_duplicates():
    """Listed, filtered duplicate (with the same Garmin activity), overlap counted twice, not listed."""
    assert listing_status(FUELING_ACTIVITY, AUDIT_LIST)["status"] == "listed"
    duplicate = listing_status(DUPLICATE_ACTIVITY, AUDIT_LIST_WITH_UPLOAD)
    assert duplicate["status"] == "filtered_duplicate" and duplicate["overlapping"][0]["same_garmin_activity"]
    assert duplicate["text"].startswith("NOT in the activity list: filtered duplicate of i50 ('Long ride', UPLOAD, same Garmin activity)")
    twice = listing_status(FUELING_ACTIVITY, AUDIT_LIST + [dict(AUDIT_LIST[0], id="i56", elapsed_time=7300)])
    assert twice["status"] == "listed_with_overlap" and "i56 starts within 2 min" in twice["text"]
    assert listing_status(FUELING_ACTIVITY, [dict(AUDIT_LIST[0], id="i57", elapsed_time=3000)])["status"] == "not_listed"


def test_provenance_notes_for_the_report():
    """Short notes from the payload: source, recording, interval edits, zero placeholders, late definitions."""
    notes = provenance_notes(FUELING_ACTIVITY, FIELDS, RIDE_EXPECTED, FUELING_INTERVALS)
    assert notes[0].startswith("source: Garmin Connect sync, FIT file, Garmin activity 123456789, also on Strava (987); uploaded 2 min after the end")
    assert "2:30 not recorded (elapsed vs recorded time)" in notes[1]
    assert notes[2].startswith("intervals were edited by hand")
    assert notes[3].endswith("Performance Condition [PerformanceCondition]")
    assert notes[4] == ("fields without a value whose definition was changed or created after this activity was analysed "
                        "(no value until a reprocess or a write): New Metric [NewMetric]")
    short = provenance_notes(FUELING_ACTIVITY, FIELDS, RIDE_EXPECTED, FUELING_INTERVALS, compact=True)
    assert short == ["1 recording stop(s), 2:30 not recorded",
                     "device-file fields stored as 0 (real or placeholder): Performance Condition [PerformanceCondition]",
                     "fields changed after the analysis, no value: New Metric [NewMetric]"]
    assert provenance_notes(STRAVA_STUB, FIELDS, None)[0].startswith("Strava import: the Intervals.icu API returns only an empty stub")
    assert not provenance_notes({"id": "x"}, FIELDS, None)


def test_coverage_summary_per_type():
    """Per type: sources, power/HR/GPS counts, custom streams and the expected fields' status."""
    summary = coverage_summary(AUDIT_LIST + [STRAVA_STUB], FIELDS, {"Ride": RIDE_EXPECTED, "Run": None}, STREAM_DEFS)
    ride = summary["Ride"]
    assert ride["n"] == 5 and ride["sources"] == {"GARMIN_CONNECT": 4, "STRAVA": 1}
    assert ride["counts"]["power"] == 4 and ride["counts"]["power_meter_named"] == 1 and ride["counts"]["second_power"] == 3
    assert ride["counts"]["strava_stubs"] == 1 and ride["custom_streams"] == {"Stamina": 4}
    assert ride["expected_fields"]["PerformanceCondition"] == {"name": "Performance Condition", "value": 3, "zero_placeholder": 1, "no_value": 1}
    assert summary["Run"]["expected_fields_known"] is False and summary["Run"]["expected_fields"] == {}


# --------------------------------------------------------------------------------- fueling
def test_fueling_fields_found_by_units_and_words():
    """Fluid intake, sodium, sweat loss and carb fields are recognised generically."""
    defs = {
        "Drink": {"name": "Bottles drunk", "units": "ml", "value_type": "numeric"},
        "Salt": {"name": "Salt", "units": "g", "value_type": "numeric"},
        "Sweat": {"name": "Sweat loss", "units": "L", "value_type": "numeric"},
        "FluidLoss": {"name": "Fluid loss", "units": "ml", "value_type": "numeric"},
        "Gels": {"name": "Gels carbs", "units": "g", "value_type": "numeric"},
        "SweatRate": {"name": "Sweat rate", "units": "%", "value_type": "numeric"},
        "Note": {"name": "Water note", "units": None, "value_type": "text"},
    }
    assert fueling_fields(defs) == {"fluid_intake": ["Drink"], "sodium": ["Salt"], "sweat_loss": ["Sweat", "FluidLoss"], "carbs": ["Gels"]}


def test_activity_fueling_rates_and_line():
    """Per-hour rates, units converted to ml/mg, ingested share of used, fluid minus sweat, statuses."""
    figures = activity_fueling(FUELING_ACTIVITY, FIELDS)
    assert figures["carbs_used_g_per_h"] == 150 and figures["carbs_ingested_g_per_h"] == 60 and figures["ingested_pct_of_used"] == 40
    assert figures["fluid_intake"][0]["amount"] == 1000 and figures["fluid_intake"][0]["per_hour"] == 500
    assert figures["sodium"][0]["per_hour"] == 400 and figures["fluid_minus_sweat_ml"] == -400 and figures["work_kj"] == 1500
    assert fueling_line(figures) == (
        "Fueling: carbs used ~300 g (Intervals.icu estimate, 150 g/h) | ingested 120 g (60 g/h, 40 % of used) | "
        "fluid 1000 ml (Fluid intake, 500 ml/h) | sweat loss 1400 ml (Sweat loss, 700 ml/h) | sodium 800 mg (Sodium, 400 mg/h) | "
        "fluid minus sweat loss -400 ml | 1800 kcal, 1500 kJ work"
    )
    placeholder = activity_fueling({"moving_time": 7200, "Sweatloss": 0.0, "FluidIntake": 1.0}, FIELDS)
    assert placeholder["sweat_loss"][0]["status"] == "zero_placeholder" and placeholder["sweat_loss"][0]["amount"] is None
    assert placeholder["fluid_minus_sweat_ml"] is None
    line = fueling_line(placeholder) or ""
    assert "sweat loss 0 stored (Sweat loss: placeholder or real 0, not counted)" in line and "fluid minus" not in line
    manual = activity_fueling({"moving_time": 7200, "Sweatloss": 900.0, "FluidIntake": 0.0}, FIELDS)
    assert manual["fluid_intake"][0]["status"] == "zero" and manual["fluid_minus_sweat_ml"] == -900
    zero = activity_fueling({"moving_time": 3600, "carbs_used": 100, "carbs_ingested": 0}, FIELDS)
    assert zero["carbs_ingested_status"] == "zero" and "ingested 0 g (0 g/h, 0 % of used) (a stored 0)" in fueling_line(zero)
    missing = activity_fueling({"moving_time": 3600, "carbs_used": 100, "Sweatloss": "NaN"}, FIELDS)
    assert missing["carbs_ingested_status"] == "not logged" and missing["sweat_loss"][0]["status"] == "not logged"
    assert "ingested not logged" in fueling_line(missing)
    assert fueling_line(activity_fueling({"moving_time": 60}, {})) is None


def test_intake_distribution_cumulative_and_events():
    """An intake stream gives intake per hour, cumulative or per event."""
    time = [0, 1800, 3600, 5400, 7200]
    assert intake_distribution(time, [0, 30, 60, 60, 120])["per_hour"] == [{"hour": 1, "amount": 30.0}, {"hour": 2, "amount": 30.0}, {"hour": 3, "amount": 60.0}]
    events = intake_distribution(time, [25, 0, 30, 20, 0])
    assert events["mode"] == "events" and events["per_hour"] == [{"hour": 1, "amount": 25.0}, {"hour": 2, "amount": 50.0}]
    assert intake_distribution([0, 1], [None, None]) is None


def test_period_fueling_groups_and_coverage():
    """Long sessions only; logged, zero and missing intake counted; buckets by duration and intensity; Spearman from 5 pairs."""
    placeholder = {"id": "p9", "type": "Ride", "start_date_local": "2026-09-29T09:00:00", "moving_time": 7200, "Sweatloss": 0.0}
    result = period_fueling(FUELING_PERIOD + [placeholder], FIELDS, 90 * 60)
    overall = result["overall"]
    assert overall["sessions"] == 8 and overall["ingested_logged"] == 5 and overall["ingested_zero"] == 1 and overall["ingested_not_logged"] == 2
    assert overall["ingested_g_per_h"]["n"] == 5 and overall["with_carbs_used"] == 6
    assert overall["zero_placeholders"] == 1 and overall["sweat_loss_ml_per_h"]["n"] == 6
    assert set(result["by_sport_family"]) == {"cycling", "running"} and result["rates"] == "per moving hour"
    cycling = result["by_sport_family"]["cycling"]
    assert cycling["by_duration"]["< 2 h"]["sessions"] == 2 and cycling["by_duration"][">= 4 h"]["sessions"] == 1
    assert cycling["by_intensity"]["IF >= 0.85"]["ingested_zero"] == 1
    assert cycling["correlations"]["ingested_vs_duration"] == {"n": 4, "rho": None} and cycling["correlations"]["min_n"] == 8
    assert result["by_sport_family"]["running"]["by_duration"]["< 2 h"]["sessions"] == 1
    rides = [{"id": f"r{i}", "type": "Ride", "start_date_local": f"2026-08-{i + 1:02d}T09:00:00", "moving_time": 3600 * (1.5 + i / 2),
              "icu_intensity": 60 + i, "carbs_ingested": 30 * (1.5 + i / 2) * (1 + i / 10)} for i in range(9)]
    correlations = period_fueling(rides, {}, 0)["by_sport_family"]["cycling"]["correlations"]
    assert correlations["ingested_vs_duration"] == {"n": 9, "rho": 1.0} and correlations["ingested_vs_intensity"] == {"n": 9, "rho": 1.0}
    assert correlation_text(correlations) == "Spearman ingested g/h vs duration 1.00 (n 9), vs IF 1.00 (n 9) (association only)"


# ---------------------------------------------------------------------- weather, W′, route
def test_weather_summary_and_line():
    """Wind in m/s with km/h, compass direction, head/tail/crosswind, rain in mm/h, device vs weather temperature."""
    summary = weather_summary(FUELING_ACTIVITY)
    assert summary["wind_km_h"] == 9.0 and summary["wind_from"] == "SW" and summary["crosswind_pct"] == 25
    assert summary["device_minus_weather_c"] == 4.0
    assert weather_line(summary) == (
        "Weather 18.0 °C (15.0-21.0, feels 17.0 °C), wind 9 km/h from SW, gusts 18 km/h, headwind 40 % / tailwind 35 % of the time, "
        "clouds 50 %, rain up to 0.5 mm/h, device sensor 22.0 °C"
    )
    indoor = weather_line(weather_summary({"type": "VirtualRide", "average_temp": 21.0}))
    assert indoor == "Device sensor 21.0 °C (indoor activity: outdoor weather does not apply)"
    assert weather_summary({"id": "x"}) is None and weather_line(None) is None
    assert (compass(0), compass(350), compass(None)) == ("N", "N", None)


def test_wprime_summary_with_stream_and_intervals():
    """Max depletion from the payload, time below 75/50/25 % from w_bal (pauses count 1 s), lowest interval."""
    summary = wprime_summary(FUELING_ACTIVITY, FUELING_INTERVALS, W_BAL, TIME)
    assert summary["max_depletion_pct"] == 80 and summary["min_w_bal_j"] == 4000
    assert summary["stream"]["seconds_below_pct"] == {"75": 150.0, "50": 150.0, "25": 50.0}
    assert summary["stream"]["dips_below_50_pct"] == 2 and summary["stream"]["min_at_s"] == 700
    assert summary["lowest_interval"]["interval"] == 4
    assert wprime_line(summary) == (
        "W′ 20.0 kJ (power model 21.0 kJ): max depletion 16.0 kJ (80 %), lowest W′bal 4.0 kJ (20 %); "
        "time below 75 % 2:30, 50 % 2:30, 25 % 0:50 (2 dip(s) below 50 %); lowest at the end of interval 4 (11:40, 0:50 @ 450 W): 4.0 kJ"
    )
    assert summary["model_mismatch"] is False and summary["note"] is None
    mismatch = wprime_summary({"icu_w_prime": 18000, "icu_max_wbal_depletion": 27200}, None, [18000, -100, -9200, 5000], [0, 1, 2, 3])
    assert mismatch["model_mismatch"] and mismatch["stream"]["seconds_below_zero"] == 2
    text = wprime_line(mismatch) or ""
    assert text.startswith("W′ 18.0 kJ: max depletion 27.2 kJ, W′bal below 0 for 0:02. W′bal fell below 0: this ride exceeded the W′/CP model")
    assert "151 %" not in text and "below 25 %" not in text
    assert wprime_summary({"icu_w_prime": 18000, "icu_max_wbal_depletion": 19000})["model_mismatch"]
    calm = wprime_summary({"icu_w_prime": 20000, "icu_max_wbal_depletion": 1000}, None, [20000, 19000], [0, 1])
    assert wprime_line(calm).endswith("W′bal never below 75 %")
    assert wprime_summary({"type": "Hike"}) is None and wprime_line(None) is None


def test_route_history_comparison():
    """Earlier activities of the same family; other sports counted; distance outliers not compared; rank and medians."""
    pairs = start_end_pairs(FIELDS)
    history = route_history(FUELING_ACTIVITY, ROUTE_LIST, pairs)
    assert [row["id"] for row in history["earlier"]] == ["i40", "i41", "i42"]
    assert history["other_sports"] == 1 and history["stats"]["n"] == 2
    assert history["earlier"][2]["not_comparable"] == ["distance +33 %", "elevation gain +38 %"]
    assert history["stats"]["rank_by_moving_time"] == 2 and history["stats"]["of"] == 3
    assert history["stats"]["median_avg_watts"] == 180.0 and history["stats"]["reference_minus_median_avg_watts"] == 0.0
    assert history["earlier"][0]["w_per_kg"] == 2.24 and history["earlier"][0]["pairs"]["stamina"]["change"] == -40.0
    history["route_name"] = "Lake loop"
    lines = route_history_lines(history)
    assert lines[0].startswith("Route 77 'Lake loop': 3 earlier activities of the same sport family (1 of other sports not compared); 2 comparable")
    assert lines[1] == "This activity: moving 2:00:00, rank 2 of 3 by moving time (fastest earlier 1:56:40, median 2:00:50, -0:50 vs median)"
    assert "  2026-09-20 i40 Ride | moving 2:05:00 | 60.5 km | +790 m | avg 170 W / NP 185 W (2.24 W/kg) | HR 138 | 12 °C, headwind 30 % | stamina 100→60" in lines
    assert route_history_lines(route_history(FUELING_ACTIVITY, ROUTE_LIST[:1]))[0] == "Route 77: no earlier activity of the same sport family on this route."
    capped = route_history_lines(dict(route_history(FUELING_ACTIVITY, ROUTE_LIST, pairs, truncated=True), route_name="Lake loop"))
    assert capped[0].startswith("Route 77 'Lake loop': the 3 most recent earlier activities of the same sport family (only the latest 16 "
                                "activities on the route are loaded; older ones are not compared)")
    assert capped[1].startswith("This activity: moving 2:00:00, rank 2 of 3 by moving time among them (fastest earlier")
    failed = dict(route_history(FUELING_ACTIVITY, []), error="boom")
    assert route_history_lines(failed) == ["Route 77: the activity list could not be loaded (boom)."]


# ------------------------------------------------------------------------------- tools
def test_get_activity_data_audit_single(monkeypatch):
    """One activity: three requests (activity with intervals, recent list, streams) and every audit section."""
    calls = []
    _routes(monkeypatch, calls)
    result = asyncio.run(get_activity_data_audit("i50"))
    assert result.startswith("Data audit of i50 'Long ride' (Ride) 2026-10-08 09:00 local (UTC+02:00)")
    assert "Source: Garmin Connect sync, FIT file, Garmin activity 123456789, also on Strava (987); device Edge 1040" in result
    assert "Freshness: uploaded 2 min after the end; last analysed 2:55 h after the upload (re-analysed later" in result
    assert ("fields without a value whose definition was changed or created after the last analysis (the API gives only the last "
            "change time; no value until a reprocess or a write): New Metric [NewMetric]") in result
    assert "Listing: listed in the activity list; no other listed activity starts within 2 min" in result
    assert "Recording: elapsed 2:03:20, recorded 2:00:50, moving 2:00:00; 1 recording stop(s), 2:30 not recorded (gaps > 5 s in the time stream: 1, longest 1:41 at 9:59)" in result
    assert "Laps and intervals: 3 FIT lap(s), 5 Intervals.icu interval(s) (3 work), edited by hand" in result
    assert "usually present on recent Ride (n 3) but missing here: secondary_power (3/3)" in result
    assert "zero placeholders (a real 0 or a source missing from the file): Performance Condition [PerformanceCondition]" in result
    assert "usually with a value on recent activities of the sport but not here: Performance Condition [PerformanceCondition] (3)" in result
    assert "Device-file fields with a 0, no value or no key: Performance Condition [PerformanceCondition], New Metric [NewMetric]; the Garmin Intervals Bridge" in result
    assert "Context data present: weather, route, carbs used (estimate), carbs ingested, W′ depletion" in result
    list_call = next(c for c in calls if c[0].endswith("/activities"))
    assert list_call[1]["oldest"] == "2026-09-10" and list_call[1]["newest"] == "2026-10-08" and "PerformanceCondition" in list_call[1]["fields"]
    assert any(c[1] == {"intervals": "true"} for c in calls if c[0] == "/activity/i50")
    assert sum(1 for c in calls if "/streams" in c[0]) == 1
    payload = json.loads(asyncio.run(get_activity_data_audit("i50", output_format="json")))
    assert payload["listing"]["status"] == "listed" and payload["api_calls"] == 3 and "_defs" not in payload
    assert payload["streams"]["coverage"][2]["valid_pct"] == 97.5
    calls.clear()
    compact = asyncio.run(get_activity_data_audit("i50", detail_level="compact"))
    assert "coverage" not in compact and not any("/streams" in c[0] for c in calls)
    assert asyncio.run(get_activity_data_audit("i50", detail_level="x")).startswith("Error: detail_level")


def test_get_activity_data_audit_duplicate_and_stub(monkeypatch):
    """A filtered duplicate without streams and a Strava stub are named as such."""
    _routes(monkeypatch, **{"/activity/": DUPLICATE_ACTIVITY, "/activities": AUDIT_LIST_WITH_UPLOAD, "/streams": []})
    result = asyncio.run(get_activity_data_audit("i55"))
    assert "Listing: NOT in the activity list: filtered duplicate of i50 ('Long ride', UPLOAD, same Garmin activity)" in result
    assert "- the streams endpoint returned none of the listed streams (file not retained, e.g. a filtered duplicate)" in result
    assert "- no intervals (Intervals.icu detected none, or the file was not processed)" in result
    _routes(monkeypatch, **{"/activity/": dict(FUELING_ACTIVITY, stream_types=["time", "watts", "secondary_power"])})
    assert "power fields power, second power stream recorded" in asyncio.run(get_activity_data_audit("i50", detail_level="compact"))
    _routes(monkeypatch, **{"/activity/": STRAVA_STUB})
    stub = asyncio.run(get_activity_data_audit("i77"))
    assert stub.splitlines()[1] == "Source: Strava import"
    assert "- Strava import: the Intervals.icu API returns only an empty stub" in stub
    _routes(monkeypatch, **{"/activity/": {"error": True, "message": "boom"}})
    assert asyncio.run(get_activity_data_audit("i9")) == "Error fetching the activity: boom"


def test_get_activity_data_audit_reports_a_failed_streams_request(monkeypatch):
    """A failed streams request is an API error, never "no streams" (stage 3 follow-up)."""
    _routes(monkeypatch, **{"/streams": {"error": True, "status_code": 429, "message": "429 Too Many Requests"}})
    result = asyncio.run(get_activity_data_audit("i50"))
    assert "- Error fetching the streams: 429 Too Many Requests; stream coverage not checked" in result
    assert "returned none of the listed streams" not in result and "coverage (valid samples)" not in result
    payload = json.loads(asyncio.run(get_activity_data_audit("i50", output_format="json")))
    assert payload["streams"]["error"] == "Error fetching the streams: 429 Too Many Requests"
    assert payload["streams"]["coverage"] is None and payload["streams"]["issues"] == []
    _routes(monkeypatch)
    assert json.loads(asyncio.run(get_activity_data_audit("i50", output_format="json")))["streams"]["error"] is None


def test_get_activity_data_audit_period_coverage(monkeypatch):
    """Without an activity: one list request with every custom field code, per type counts."""
    calls = []
    _routes(monkeypatch, calls, **{"/activities": AUDIT_LIST + [STRAVA_STUB]})
    result = asyncio.run(get_activity_data_audit(start_date="2026-09-01", end_date="2026-10-09"))
    assert result.startswith("Data coverage for athlete i1, 2026-09-01 to 2026-10-09: 6 activities")
    assert ("Ride (n 5; GARMIN_CONNECT 4, STRAVA 1): power 4 (meter named 1, second power 3), HR 4, GPS 1, weather 1, intervals edited 1, "
            "carbs used 1, carbs ingested logged 1, Strava stubs 1 (no data via the API)") in result
    assert "Performance Condition [PerformanceCondition] 3/1/1" in result
    assert "Run (n 1; GARMIN_CONNECT 1)" in result and "no custom fields assigned to this sport or its family" in result
    fields = next(c for c in calls if c[0].endswith("/activities"))[1]["fields"]
    assert "NewMetric" in fields and "TrainingEffectSelect" in fields
    payload = json.loads(asyncio.run(get_activity_data_audit(start_date="2026-09-01", end_date="2026-10-09", sport_types="Run", output_format="json")))
    assert payload["activities"] == 1 and list(payload["by_type"]) == ["Run"]
    assert asyncio.run(get_activity_data_audit(start_date="2026-10-09", end_date="2026-09-01")).startswith("Error")


def test_get_fueling_analysis_single_and_period(monkeypatch):
    """One activity (with an intake stream) and a period with groups, sample sizes and notes."""
    calls = []
    activity = dict(FUELING_ACTIVITY, stream_types=FUELING_ACTIVITY["stream_types"] + ["CarbsEaten"])
    intake = [{"type": "time", "data": [0, 1800, 3600, 5400]}, {"type": "CarbsEaten", "data": [0, 30, 60, 120]}]
    _routes(monkeypatch, calls, **{"/activity/": activity, "/streams": intake})
    result = asyncio.run(get_fueling_analysis("i50"))
    assert result.startswith("Fueling of i50 'Long ride' (Ride) 2026-10-08 09:00 local (UTC+02:00), moving 2:00:00, IF 0.72; rates per moving hour")
    assert "Fueling: carbs used ~300 g (Intervals.icu estimate, 150 g/h)" in result
    assert "Intake over time from Carbs eaten [CarbsEaten] (cumulative): hour 1 30 g, hour 2 90 g" in result
    assert "Note: carbs_used is Intervals.icu's estimate" in result
    assert calls[-1][1] == {"types": "time,CarbsEaten"}
    _routes(monkeypatch, calls)
    plain = asyncio.run(get_fueling_analysis("i50", detail_level="compact"))
    assert plain.endswith("cannot be shown.") and "Note:" not in plain
    payload = json.loads(asyncio.run(get_fueling_analysis("i50", output_format="json")))
    assert payload["fueling"]["carbs_ingested_g_per_h"] == 60 and payload["timing"]["available"] is False

    calls.clear()
    _routes(monkeypatch, calls, **{"/activities": FUELING_PERIOD})
    period = asyncio.run(get_fueling_analysis(start_date="2026-09-01", end_date="2026-09-30"))
    assert period.startswith("Fueling for athlete i1, 2026-09-01 to 2026-09-30: 7 sessions of at least 90 min (of 8 activities); rates per moving hour")
    assert "Overall (all sports): 7 sessions, intake logged on 5 (0 g stored on 1, not logged on 1); ingested median" in period
    assert "\ncycling: 6 sessions" in period and "\nrunning: 1 session," in period
    assert "  >= 4 h (moving time): 1 session, intake logged on 1" in period
    assert "  Spearman ingested g/h vs duration not computed (n 4 < 8), vs IF not computed (n 4 < 8) (association only)" in period
    assert "2026-09-20 p5 Ride 'Ride p5' (2:00:00, IF 0.88)" in period
    assert {"Sweatloss", "FluidIntake", "SodiumMg"} <= set(calls[-1][1]["fields"].split(","))
    rides = asyncio.run(get_fueling_analysis(start_date="2026-09-01", end_date="2026-09-30", sport_types="Ride", detail_level="compact"))
    assert rides.splitlines()[1].startswith("cycling: 5 sessions") and "Overall" not in rides
    assert "Sessions:" not in rides and "(moving time)" not in rides and "Spearman" not in rides
    compact_json = json.loads(asyncio.run(get_fueling_analysis(start_date="2026-09-01", end_date="2026-09-30", output_format="json", detail_level="compact")))
    assert "rows" not in compact_json and compact_json["overall"]["sessions"] == 7
    assert asyncio.run(get_fueling_analysis(min_minutes=-1)).startswith("Error")
    assert asyncio.run(get_fueling_analysis(detail_level="huge")).startswith("Error")


def test_get_activity_report_context_and_route_history(monkeypatch):
    """The report shows fueling, weather and W′ (w_bal requested with the streams), provenance notes and,
    on request, the same-route history (two extra requests)."""
    calls = []
    _routes(monkeypatch, calls, **{"/activities": ROUTE_LIST})
    result = asyncio.run(get_activity_report("i50", include_route_history=True))
    assert "Fueling: carbs used ~300 g (Intervals.icu estimate, 150 g/h) | ingested 120 g (60 g/h, 40 % of used)" in result
    assert "Weather 18.0 °C (15.0-21.0, feels 17.0 °C), wind 9 km/h from SW" in result
    assert "time below 75 % 2:30, 50 % 2:30, 25 % 0:50 (2 dip(s) below 50 %); lowest at the end of interval 4 (11:40, 0:50 @ 450 W)" in result
    assert "== Same route\nRoute 77 'Lake loop': 3 earlier activities" in result
    assert "- source: Garmin Connect sync, FIT file, Garmin activity 123456789" in result
    stream_call = next(c for c in calls if "/streams" in c[0])
    assert stream_call[1]["types"].endswith("w_bal")
    route_call = next(c for c in calls if c[0].endswith("/activities"))
    assert route_call[1]["route_id"] == 77 and route_call[1]["limit"] == 16 and "Staminaatend" in route_call[1]["fields"]
    payload = json.loads(asyncio.run(get_activity_report("i50", include_route_history=True, output_format="json")))
    assert payload["api_calls"] == 5 and payload["route_history"]["route_name"] == "Lake loop"
    assert payload["w_prime"]["stream"]["dips_below_50_pct"] == 2 and payload["fueling"]["carbs_used_g"] == 300
    assert payload["weather"]["wind_from"] == "SW" and payload["provenance"]["freshness"]["reanalysed"] is True
    compact = asyncio.run(get_activity_report("i50", detail_level="compact"))
    assert ("Context: carbs used ~150 g/h (estimate) | ingested 60 g/h | sweat 700 ml/h | fluid 500 ml/h | 18 °C, wind 9 km/h SW, "
            "headwind 40 %, rain 0.5 mm/h | W′bal min 20 %, 2:30 below 50 %") in compact
    assert "Fueling:" not in compact and "== Same route" not in compact and "- source:" not in compact
    _routes(monkeypatch, **{"/activity/": dict(FUELING_ACTIVITY, route_id=None)})
    assert "== Same route\nNo Intervals.icu route for this activity (routes need GPS)" in asyncio.run(
        get_activity_report("i50", include_route_history=True, detail_level="compact"))
    _routes(monkeypatch, **{"/activity/": STRAVA_STUB})
    stub = asyncio.run(get_activity_report("i77"))
    assert stub.startswith("== Overview\nMorning Ride (i77, Ride)\n== Data quality\n- Strava import")
    assert json.loads(asyncio.run(get_activity_report("i77", output_format="json")))["strava_stub"] is True


def test_get_activity_details_context(monkeypatch):
    """Details: compact carries fueling and weather/W′ lines and the source; standard a block; JSON the sections."""
    _routes(monkeypatch)
    compact = asyncio.run(get_activity_details("i50", detail_level="compact"))
    lines = compact.splitlines()
    assert len(lines) == 7 and lines[4] == ("Context: carbs used ~150 g/h (estimate) | ingested 60 g/h | sweat 700 ml/h | fluid 500 ml/h | "
                                            "18 °C, wind 9 km/h SW, headwind 40 %, rain 0.5 mm/h | W′bal min 20 %")
    assert lines[6].startswith("Data: Garmin Connect sync, intervals edited, 5 streams (1 custom)")
    standard = asyncio.run(get_activity_details("i50"))
    assert "Avg Wind Speed: 2.5 m/s (9 km/h)" in standard
    assert "\nFueling, weather, W′ and source:\n- Fueling: carbs used ~300 g" in standard
    assert "- Source: Garmin Connect sync, FIT file, Garmin activity 123456789, also on Strava (987); uploaded 2 min after the end" in standard
    payload = json.loads(asyncio.run(get_activity_details("i50", output_format="json")))
    assert payload["fueling"]["sweat_loss"][0]["per_hour"] == 700 and payload["w_prime"]["min_w_bal_pct"] == 20
    assert payload["provenance"]["garmin_activity_id"] == "123456789"
    _routes(monkeypatch, **{"/activity/": STRAVA_STUB})
    assert asyncio.run(get_activity_details("i77", detail_level="compact")).endswith("upload the original file instead")
    assert "- Strava import: the Intervals.icu API returns only an empty stub" in asyncio.run(get_activity_details("i77"))


def test_get_durability_weather_temperature(monkeypatch):
    """temperature_source switches the heat filter to the weather; sessions list both temperatures."""
    base = {"type": "Ride", "moving_time": 4000, "elapsed_time": 4100, "decoupling": 3.0, "icu_efficiency_factor": 1.3,
            "icu_variability_index": 1.05, "average_heartrate": 140, "icu_training_load": 60, "icu_intensity": 70}
    activities = [
        dict(base, id="d1", name="Hot sensor", start_date_local="2026-10-01T10:00:00", average_temp=31.0, average_weather_temp=22.0),
        dict(base, id="d2", name="Cool", start_date_local="2026-10-03T10:00:00", average_temp=18.0, average_weather_temp=16.0),
    ]
    _routes(monkeypatch, **{"/activities": activities})
    device = asyncio.run(get_durability(start_date="2026-09-20", end_date="2026-10-05"))
    assert "average temperature (device sensor) <= 25 °C" in device and "average temperature above the limit 1" in device
    weather = asyncio.run(get_durability(start_date="2026-09-20", end_date="2026-10-05", temperature_source="weather"))
    assert "average temperature (weather) <= 25 °C" in weather and "qualifying 2" in weather
    assert "'Hot sensor' (d1): +3.0 %, 67 min, VI 1.05, EF 1.30, device 31 °C, weather 22 °C" in weather
    activities[0]["average_feels_like"] = 24.0
    feels = asyncio.run(get_durability(start_date="2026-09-20", end_date="2026-10-05", temperature_source="feels_like"))
    assert "average temperature (weather feels-like) <= 25 °C" in feels
    assert "'Hot sensor' (d1): +3.0 %, 67 min, VI 1.05, EF 1.30, device 31 °C, weather 22 °C, feels-like 24 °C" in feels
    assert asyncio.run(get_durability(temperature_source="air")).startswith("Error: temperature_source")
