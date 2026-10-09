"""
Tests for the long-ride fatigue profile: work accumulation (kJ, kJ above FTP, efforts), threshold
crossings, steady segments per power band and phase, HR drift at matched power, stamina, climbs,
the across-ride statistics and the get_long_ride_fatigue_profile tool. Synthetic data only.
"""

import asyncio
import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.tools.fatigue import get_long_ride_fatigue_profile  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.fatigue_profile import (  # pylint: disable=wrong-import-position
    accumulate_work,
    across_rides,
    build_series,
    default_band,
    phase_changes,
    phase_labels,
    ride_profile,
    stamina_codes,
    steady_segments,
)
from tests.sample_data import (  # pylint: disable=wrong-import-position
    LONG_RIDE_THRESHOLDS,
    long_ride_activity,
    long_ride_streams,
)
from tests.test_coaching_tools import _install_router as _coaching_router  # pylint: disable=wrong-import-position

BAND = [(180.0, 200.0)]


def _profile(streams=None, **kwargs):
    """ride_profile of the synthetic long ride with FTP 250 W, 80 kg, thresholds 400 / 600 kJ."""
    streams = streams if streams is not None else long_ride_streams()
    options = {"ftp": 250.0, "weight": 80.0, "thresholds_kj": list(LONG_RIDE_THRESHOLDS), "bands": BAND,
               "stamina": stamina_codes(streams), "sport": "Ride", "min_climb_gain_m": 50.0, "climb_after_s": 3000.0}
    options.update(kwargs)
    return ride_profile(streams, **options)


def _install_router(monkeypatch, overrides=None, calls=None):
    """The coaching router plus the performance helpers the fatigue tools request through."""
    fake = _coaching_router(monkeypatch, overrides, calls)
    monkeypatch.setattr("intervals_mcp_server.tools.performance.make_intervals_request", fake)
    return fake


# ------------------------------------------------------------------ pure functions
def test_work_matches_intervals_definitions_and_ignores_pauses():
    """kJ is the sum of the samples (pause not counted), kJ above FTP the power above FTP, one effort >= 60 s."""
    profile = _profile()
    assert profile["total_kj"] == 639
    assert profile["total_kj_per_kg"] == 8.0
    assert profile["kj_above_ftp"] == 6.0  # 50 W above FTP for 120 s
    assert profile["secs_above_ftp"] == 120
    assert profile["efforts_above_ftp"] == 1
    assert profile["elapsed_s"] == 3839  # the 300 s pause is elapsed time, not work


def test_threshold_crossings_carry_prior_work_and_stamina():
    """Each crossing reports kJ/kg, work and time above FTP, efforts and stamina at that point."""
    first, second = _profile()["thresholds"]
    assert first["reached"] and second["reached"]
    assert first["kj_per_kg"] == 5.0 and second["kj_per_kg"] == 7.5
    assert first["prior_work"]["kj"] == 400
    assert first["prior_work"]["kj_above_ftp"] == 6.0
    assert first["prior_work"]["above_ftp_share_pct"] == 1.5
    assert first["prior_work"]["efforts_above_ftp"] == 1
    assert first["prior_work"]["secs_above_ftp"] == 120
    assert first["stamina"] == pytest.approx(77.18)
    assert first["potential_stamina"] == pytest.approx(88.59)
    assert second["prior_work"]["elapsed_s"] > first["prior_work"]["elapsed_s"]


def test_threshold_not_reached():
    """An unreached threshold has no crossing data, its phase no segments and no comparable change."""
    profile = _profile(thresholds_kj=[400.0, 5000.0])
    assert profile["thresholds"][1] == {"kj": 5000.0, "kj_per_kg": 62.5, "reached": False}
    phases = profile["bands"][0]["phases"]
    assert phases[2]["segments"] == 0 and phases[2]["hr"] is None
    assert profile["bands"][0]["changes"][1]["comparable"] is False


def test_hr_drift_at_matched_power_per_phase():
    """HR is 130/140/150 bpm in the three phases at 190 W: +10 and +20 bpm, W/bpm falls."""
    band = _profile()["bands"][0]
    assert band["band"] == "180-200 W"
    phases = band["phases"]
    assert [p["segments"] for p in phases] == [2, 1, 1]
    assert [p["hr"] for p in phases] == [130.0, 140.0, 150.0]
    assert phases[0]["label"] == "0-400 kJ" and phases[2]["label"] == ">= 600 kJ"
    assert all(p["small_sample"] for p in phases)
    assert phases[1]["cadence"] == 90.0 and phases[1]["temp_c"] == 20.0
    assert phases[1]["stamina_first"] == pytest.approx(77.18)
    change1, change2 = band["changes"]
    assert change1["hr_bpm"] == 10.0 and change2["hr_bpm"] == 20.0
    assert change2["w_per_bpm_pct"] < change1["w_per_bpm_pct"] < 0
    assert change1["watts_w"] == 1.0  # the reference phase holds a few coasting samples
    assert change1["comparable"] and change1["small_sample"]


def test_segments_exclude_warmup_coasting_and_never_span_a_threshold():
    """Warm-up excluded, segments cut at the crossing, HR measured after the first 60 s."""
    profile = _profile()
    segments = profile["segments"]
    assert segments[0]["start_s"] == 600  # the first 10 min are left out
    assert all(s["duration_s"] >= 120 for s in segments)
    crossing = profile["thresholds"][0]["prior_work"]["elapsed_s"]
    assert any(s["start_s"] == crossing for s in segments)  # a segment is cut at the crossing
    assert all(s["measured_s"] == s["duration_s"] - 60 for s in segments)
    assert segments[1]["prior_work"]["efforts_above_ftp"] == 1


def test_no_segments_outside_the_band_or_with_short_minimum():
    """No segment outside the band; a longer minimum keeps only the long segment."""
    assert not _profile(bands=[(250.0, 280.0)])["segments"]
    longer = _profile(min_segment_secs=1000)
    assert [s["duration_s"] for s in longer["segments"]] == [1052]


def test_coasting_and_missing_hr_reject_segments():
    """Segments with more than 10 % coasting or without heart rate are not used."""
    streams = long_ride_streams()
    watts = next(s for s in streams if s["type"] == "watts")["data"]
    for index in range(700, 1500, 5):  # 20 % zeros keep the 30 s mean in the widened band but fail the coasting rule
        watts[index] = 0.0
    profile = _profile(streams)
    assert all(s["start_s"] > 1500 for s in profile["segments"])
    streams = long_ride_streams()
    heart = next(s for s in streams if s["type"] == "heartrate")["data"]
    heart[:] = [None] * len(heart)
    assert not _profile(streams)["segments"]
    assert _profile(streams)["hr_coverage_pct"] == 0.0


def test_series_errors_and_rolling_reset_at_gaps():
    """Missing time or power is an error; the rolling power restarts after a time gap."""
    assert build_series([]) == "no time stream"
    assert build_series([{"type": "time", "data": [0, 1, 2]}]) == "no power stream"
    assert build_series([{"type": "time", "data": [0, 2, 1]}, {"type": "watts", "data": [1, 1, 1]}]).startswith("time stream")
    assert _profile([{"type": "time", "data": [0, 1]}]) == {"error": "no power stream"}
    series = build_series([{"type": "time", "data": list(range(40)) + list(range(100, 140))},
                           {"type": "watts", "data": [200.0] * 80}])
    assert not isinstance(series, str)
    accumulate_work(series, None)
    assert series.rolling[29] == 200.0 and series.rolling[40] is None and series.rolling[69] == 200.0
    assert series.kj[-1] == pytest.approx(16.0)
    assert not series.efforts  # no FTP: no efforts
    assert not steady_segments(series, (190.0, 210.0), [], None)  # shorter than the warm-up exclusion


def test_stamina_codes_by_type_or_custom_item_name():
    """Stamina streams are recognised by type or by the custom item name."""
    streams = [{"type": "time"}, {"type": "Stamina"}, {"type": "PotentialStamina"}]
    assert stamina_codes(streams) == {"stamina": "Stamina", "potential": "PotentialStamina"}
    named = stamina_codes([{"type": "S1"}, {"type": "S2"}], {"S1": "Garmin Stamina", "S2": "Garmin Potential Stamina"})
    assert named == {"stamina": "S1", "potential": "S2"}
    assert not stamina_codes([{"type": "watts"}, "x"])  # type: ignore[list-item]


def test_without_stamina_or_ftp():
    """Without stamina streams, FTP or body mass the related values are None, not 0."""
    streams = [s for s in long_ride_streams() if "Stamina" not in s["type"]]
    profile = _profile(streams, ftp=None, weight=None)
    assert profile["stamina_streams"] is None
    assert profile["kj_above_ftp"] is None and profile["efforts_above_ftp"] is None
    assert profile["total_kj_per_kg"] is None
    assert profile["bands"][0]["phases"][0]["stamina_first"] is None
    assert profile["thresholds"][0]["kj_per_kg"] is None


def test_climbs_with_stamina_and_prior_work():
    """Climbs carry stamina at start and end, the prior work and the late flag; no altitude, no climbs."""
    climbs = _profile()["climbs"]
    assert len(climbs) == 1
    climb = climbs[0]
    assert climb["gain_m"] == pytest.approx(120, abs=1)
    assert climb["late"] is False and _profile(climb_after_s=1000.0)["climbs"][0]["late"] is True
    assert climb["stamina_start"] > climb["stamina_end"]
    assert climb["potential_start"] > climb["potential_end"]
    assert climb["prior_work"]["kj"] > 400 and climb["prior_work"]["efforts_above_ftp"] == 1
    assert climb["w_per_bpm"] == pytest.approx(190 / climb["avg_hr"], abs=0.01)
    flat = [s for s in long_ride_streams() if s["type"] != "altitude"]
    profile = _profile(flat)
    assert not profile["climbs"] and profile["climb_note"]
    assert not _profile(with_climbs=False)["climbs"]


def test_phase_labels_and_default_band():
    """Phase labels in kJ or kJ/kg and the default band of 75-85 % FTP rounded outward to 5 W."""
    assert phase_labels([750.0, 1500.0]) == ["0-750 kJ", "750-1500 kJ", ">= 1500 kJ"]
    assert phase_labels([10.0], "kJ/kg") == ["0-10 kJ/kg", ">= 10 kJ/kg"]
    assert default_band(234) == (175.0, 200.0)
    assert default_band(225) == (165.0, 195.0)
    assert default_band(None) is None and default_band(4) is None


def _ride(rid, hr_step, share=None):
    """A ride entry for across_rides with a given HR step per phase and share of work above FTP."""
    profile = _profile(long_ride_streams(hr_step=hr_step))
    if share is not None:
        profile["thresholds"][0]["prior_work"]["above_ftp_share_pct"] = share
    return {"id": rid, "profile": profile}


def test_across_rides_median_range_and_split_by_prior_intensity():
    """Each ride counts once: median and range per phase, split by prior work above FTP from 4 rides."""
    labels = phase_labels(list(LONG_RIDE_THRESHOLDS))
    rows = across_rides([_ride("r1", 4, 1.0), _ride("r2", 6, 2.0), _ride("r3", 8, 5.0), _ride("r4", 10, 6.0)], labels)
    first = next(r for r in rows if r["phase"] == 1)
    assert first["rides"] == 4 and first["small_sample"]  # every ride has only small phase samples
    assert first["hr_bpm"] == {"n": 4, "median": 7.0, "min": 4.0, "max": 10.0}
    assert first["rides_with_small_phase_samples"] == 4 and first["rides_with_cadence_shift"] == 0
    split = first["by_prior_intensity"]
    assert split["lower_share"]["rides"] == 2 and split["lower_share"]["hr_bpm"]["median"] == 5.0
    assert split["higher_share"]["hr_bpm"]["median"] == 9.0
    small = across_rides([_ride("r1", 4), _ride("r2", 6)], labels)
    assert small[0]["small_sample"] and small[0]["by_prior_intensity"] is None
    rides = [_ride("r1", 4), _ride("r2", 6), _ride("r3", 8)]
    for ride in rides[:2]:  # two of three rides with large phase samples: the row counts as reliable
        ride["profile"]["bands"][0]["changes"][0]["small_sample"] = False
    rides[2]["profile"]["bands"][0]["changes"][0]["cadence_shift"] = True
    row = across_rides(rides, labels)[0]
    assert not row["small_sample"] and row["rides_with_small_phase_samples"] == 1 and row["rides_with_cadence_shift"] == 1


def test_cadence_shift_between_phases_is_flagged():
    """A phase pair whose cadence differs by more than 15 rpm (climbing vs flat) is flagged."""
    base = {"segments": 3, "small_sample": False, "watts": 190.0, "hr": 130.0, "w_per_bpm": 1.46, "temp_c": 20.0}
    phases = [{**base, "label": "0-750 kJ", "cadence": 90.0}, {**base, "label": "750-1500 kJ", "cadence": 70.0},
              {**base, "label": ">= 1500 kJ", "cadence": 80.0}]
    changes = phase_changes(phases)
    first, second = changes[0], changes[1]
    assert first["cadence_rpm"] == -20.0 and first["cadence_shift"]
    assert second["cadence_rpm"] == -10.0 and not second["cadence_shift"]


# ------------------------------------------------------------------ tool
def test_tool_one_ride_text(monkeypatch):
    """One ride: thresholds with prior work, phases, changes, climbs, method and background in the text."""
    calls = []
    _install_router(monkeypatch, {"/activity/": long_ride_activity("r1", "2026-09-20"), "/streams": long_ride_streams()}, calls)
    result = asyncio.run(get_long_ride_fatigue_profile(activity_ids="r1", power_bands="180-200", work_thresholds="400,600"))
    assert "Long-ride fatigue profile for athlete i1: 1 ride(s) (1 activity id(s))" in result
    assert "band 180-200 W (given), thresholds 400 / 600 kJ" in result
    assert "Method: Steady segments" in result
    assert "FTP 250 W, 80.0 kg (activity): 639 kJ (8.0 kJ/kg)" in result
    assert "400 kJ (5.0 kJ/kg) at 43:02: 6.0 kJ above FTP (1.5 % of the work), 2:00 above FTP, 1 effort >= 60 s above FTP" in result
    assert "400-600 kJ: HR +10.0 bpm" in result and ">= 600 kJ: HR +20.0 bpm" in result
    assert "Climb at 43:55 [before 2 h]: 20:04, 120 m, 2.5 %, 190 W (NP 190), 142 bpm" in result
    assert "stamina 77->65 (potential 88->82); before: 410 kJ (5.1 kJ/kg)" in result
    assert "One ride: no across-ride statistics" in result
    assert "Most phases have few steady segments" in result
    assert "Background: Intervals.icu fatigued power curves" in result
    stream_call = next(c for c in calls if c[0].endswith("/streams"))
    assert set(stream_call[1]["types"].split(",")) >= {"time", "watts", "heartrate", "Stamina", "PotentialStamina"}


def test_tool_period_default_band_json_and_filters(monkeypatch):
    """Period search keeps long rides of the sports, default band from FTP, JSON per detail level."""
    activities = [
        long_ride_activity("r3", "2026-09-30", gear="b2"),
        long_ride_activity("r2", "2026-09-20"),
        long_ride_activity("short", "2026-09-10", icu_joules=300000),
        long_ride_activity("run", "2026-09-05", type="Run"),
        long_ride_activity("r1", "2026-09-01"),
    ]
    _install_router(monkeypatch, {"/activities": activities, "/streams": long_ride_streams()})
    payload = json.loads(asyncio.run(get_long_ride_fatigue_profile(
        start_date="2026-08-01", end_date="2026-10-01", work_thresholds="400,600", output_format="json")))
    assert payload["bands"] == ["185-215 W"]  # 75-85 % of FTP 250, rounded outward
    assert payload["band_source"].startswith("default 75-85 % of FTP 250 W")
    assert [r["id"] for r in payload["rides"]] == ["r3", "r2", "r1"]
    assert payload["thresholds"] == {"values": [400.0, 600.0], "unit": "kJ", "phases": ["0-400 kJ", "400-600 kJ", ">= 600 kJ"]}
    assert "segments" not in payload["rides"][0]["profile"]
    assert len(payload["across_rides"]) == 2 and payload["across_rides"][0]["rides"] == 3
    assert any("2 bikes" in note for note in payload["notes"])
    assert payload["api_calls"] == 4
    full = json.loads(asyncio.run(get_long_ride_fatigue_profile(
        start_date="2026-08-01", end_date="2026-10-01", work_thresholds="400,600", output_format="json",
        detail_level="full", power_bands="180-200", limit=1)))
    assert len(full["rides"]) == 1 and full["rides"][0]["profile"]["segments"]
    assert full["settings"]["rides_beyond_limit"] == 2
    compact = asyncio.run(get_long_ride_fatigue_profile(
        start_date="2026-08-01", end_date="2026-10-01", work_thresholds="400,600", detail_level="compact", power_bands="180-200"))
    assert "Climbs: 1 (0 after 2 h)" in compact and "Method:" not in compact
    assert "Across rides (change vs 0-400 kJ" in compact


def test_tool_kj_per_kg_thresholds_and_weight_fallback(monkeypatch):
    """kJ/kg thresholds use the activity weight, else the profile weight; without weight the ride is skipped."""
    activity = long_ride_activity("r1", "2026-09-20", icu_weight=None)
    calls = []
    _install_router(monkeypatch, {"/activity/": activity, "/streams": long_ride_streams()}, calls)
    result = asyncio.run(get_long_ride_fatigue_profile(
        activity_ids="r1", power_bands="180-200", work_thresholds="5,7.5", threshold_unit="kj_per_kg"))
    assert "83.5 kg (athlete profile)" in result  # ATHLETE_DATA icu_weight 83.48
    assert "thresholds 5 / 7.5 kJ/kg" in result and "0-5 kJ/kg" in result
    assert "417 kJ (5.0 kJ/kg)" in result
    assert any(c[0].rstrip("/").endswith("/athlete/i1") for c in calls)
    _install_router(monkeypatch, {"/activity/": activity, "/streams": long_ride_streams(), "/athlete/i1": {"id": "i1"}})
    missing = asyncio.run(get_long_ride_fatigue_profile(activity_ids="r1", power_bands="180-200", threshold_unit="kj_per_kg"))
    assert missing.startswith("No activities r1 found.") and "no body mass" in missing


def test_tool_skips_rides_without_streams_or_power(monkeypatch):
    """Rides without streams or power are listed as skipped."""
    _install_router(monkeypatch, {"/activity/r1/streams": [], "/activity/r2/streams": [{"type": "time", "data": [0, 1]}],
                                  "/activity/r1": long_ride_activity("r1", "2026-09-20"),
                                  "/activity/r2": long_ride_activity("r2", "2026-09-21")})
    result = asyncio.run(get_long_ride_fatigue_profile(activity_ids="r1,r2", power_bands="180-200"))
    assert "Skipped r1: no streams returned" in result
    assert "Skipped r2: no power stream" in result


def test_tool_validation_and_errors(monkeypatch):
    """Parameter validation and API errors."""
    _install_router(monkeypatch, {"/activities": {"error": True, "message": "boom"}})
    assert asyncio.run(get_long_ride_fatigue_profile()) == "Error fetching activities: boom"
    assert asyncio.run(get_long_ride_fatigue_profile(detail_level="x")).startswith("Error: detail_level")
    assert asyncio.run(get_long_ride_fatigue_profile(threshold_unit="watts")).startswith("Error: threshold_unit")
    assert asyncio.run(get_long_ride_fatigue_profile(work_thresholds="a")).startswith("Error: work_thresholds")
    assert asyncio.run(get_long_ride_fatigue_profile(work_thresholds="1,2,3,4")).startswith("Error: work_thresholds")
    assert asyncio.run(get_long_ride_fatigue_profile(power_bands="200-180")).startswith("Error: power band")
    assert asyncio.run(get_long_ride_fatigue_profile(power_bands="x")).startswith("Error: power_bands")
    assert asyncio.run(get_long_ride_fatigue_profile(min_segment_secs=60)).startswith("Error: min_segment_secs")
    assert asyncio.run(get_long_ride_fatigue_profile(min_climb_gain_m=0)).startswith("Error: climb_after_hours")
    assert asyncio.run(get_long_ride_fatigue_profile(start_date="2026-10-02", end_date="2026-10-01")).startswith("Error: start_date")
    assert asyncio.run(get_long_ride_fatigue_profile(end_date="2026/10/01")).startswith("Error: Invalid date")
    _install_router(monkeypatch, {"/activities": [long_ride_activity("r1", "2026-09-20", icu_ftp=None)], "/streams": long_ride_streams()})
    assert asyncio.run(get_long_ride_fatigue_profile(work_thresholds="400,600")).startswith("Error: the newest ride has no FTP")
    assert asyncio.run(get_long_ride_fatigue_profile()).startswith("No Ride,GravelRide rides between")
    _install_router(monkeypatch, {"/activity/": {"error": True, "message": "nope"}})
    assert asyncio.run(get_long_ride_fatigue_profile(activity_ids="x1")) == "Error fetching activity x1: nope"


def test_tool_caps_and_deduplicates_ids_before_requests(monkeypatch):
    """A long id list is de-duplicated and cut to the limit before any request; dropped ids are named."""
    calls = []
    _install_router(monkeypatch, {"/activity/": long_ride_activity("r1", "2026-09-20"), "/streams": long_ride_streams()}, calls)
    ids = ",".join(["r1", "r1"] + [f"x{i}" for i in range(200)])
    result = asyncio.run(get_long_ride_fatigue_profile(activity_ids=ids, power_bands="180-200", limit=3, detail_level="compact"))
    fetched = [c[0] for c in calls if c[0].startswith("/activity/") and not c[0].endswith("/streams")]
    assert fetched == ["/activity/r1", "/activity/x0", "/activity/x1"]
    assert "3 activity id(s); 198 ids beyond the limit of 3 not fetched (x2, x3, x4, x5, x6, ...); 1 duplicate id ignored" in result
    payload = json.loads(asyncio.run(get_long_ride_fatigue_profile(activity_ids="r1,r2", power_bands="180-200", limit=1,
                                                                  output_format="json")))
    assert payload["settings"]["ids_beyond_limit"] == ["r2"] and payload["settings"]["duplicate_ids_ignored"] == 0
