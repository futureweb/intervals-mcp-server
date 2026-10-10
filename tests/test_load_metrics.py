"""
Unit tests for the pure training load, intensity distribution and durability metrics
(utils.load_metrics, utils.intensity, utils.durability) on synthetic data: no data, rest
weeks, single and mixed sports, missing loads, zones and heart rate, small samples.
"""

import math
import os
import pathlib
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.utils import durability as dur  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils import intensity as tid  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils import load_metrics as lm  # pylint: disable=wrong-import-position

END = date(2026, 10, 4)  # a Sunday


def _act(day, sport="Ride", load=100.0, secs=3600, **extra):
    activity = {"id": f"a{day.isoformat()}{sport}", "name": f"{sport} {day}", "type": sport,
                "start_date_local": f"{day.isoformat()}T08:00:00", "icu_training_load": load, "moving_time": secs}
    activity.update(extra)
    return activity


def _power(*secs):
    return [{"id": f"Z{i + 1}", "secs": s} for i, s in enumerate(secs)] + [{"id": "SS", "secs": 999}]


def _four_rides_per_week(weeks=4, end=END, load=100.0):
    """Rides on Monday, Wednesday, Friday and Sunday of the last ``weeks`` weeks up to ``end`` (a Sunday)."""
    start = end - timedelta(days=7 * weeks - 1)
    return [_act(d, load=load) for d in lm.day_range(start, end) if d.weekday() in (0, 2, 4, 6)]


# ------------------------------------------------------------------ load metrics
def test_load_metrics_without_data():
    """No activities: loads are 0, ratio and monotony undefined, small sample flagged."""
    result = lm.load_metrics([], END)
    assert result["acute"]["load"] == 0 and result["chronic"]["load"] == 0
    assert result["acwr"] is None and result["acwr_position"] is None and result["acwr_small_sample"]
    assert result["week"]["monotony"] is None and result["week"]["monotony_note"] == "no load"
    assert result["week"]["strain"] is None and result["week"]["deload_like"] is None


def test_load_metrics_rest_days_count_as_zero():
    """Four rides a week: ratio 1.0, Foster monotony with rest days included, strain, 100 % of the weekly mean."""
    result = lm.load_metrics(_four_rides_per_week(), END)
    assert result["acute"]["load"] == 400 and result["chronic"]["load"] == 1600
    assert result["chronic"]["weekly_mean"] == 400 and result["acwr"] == 1.0 and result["acwr_position"] == "inside"
    week = [100.0, 0, 100.0, 0, 100.0, 0, 100.0]
    expected = (sum(week) / 7) / math.sqrt(sum((v - sum(week) / 7) ** 2 for v in week) / 6)
    assert result["week"]["monotony"] == round(expected, 2) == 1.07
    assert result["week"]["strain"] == round(400 * expected)
    assert result["week"]["vs_chronic_weekly_mean_pct"] == 100 and result["week"]["deload_like"] is False
    assert result["acute"]["zero_load_days"] == 3 and result["chronic"]["days_with_load"] == 16
    assert not result["acwr_small_sample"]


def test_load_metrics_identical_days_and_missing_loads():
    """Identical daily loads make monotony undefined; an activity without load is counted, not added."""
    daily = [_act(END - timedelta(days=i), load=50.0) for i in range(28)]
    strength = _act(END, sport="WeightTraining", load=None)
    result = lm.load_metrics(daily + [strength], END)
    assert result["week"]["monotony"] is None and result["week"]["monotony_note"] == "identical daily loads"
    assert result["acute"]["sessions"] == 8 and result["acute"]["sessions_without_load"] == 1
    assert result["acute"]["load"] == 350 and result["acwr"] == 1.0


def test_load_metrics_custom_windows_and_deload():
    """A light last week is deload-like; the acute window is configurable, monotony stays on 7 days."""
    acts = _four_rides_per_week(weeks=3, end=END - timedelta(days=7)) + _four_rides_per_week(weeks=1, load=40.0)
    result = lm.load_metrics(acts, END, acute_days=14, chronic_days=42)
    assert result["acute"]["days"] == 14 and result["acute"]["load"] == 560
    assert result["week"]["load"] == 160
    # chronic 42 d: 1200 + 160 = 1360 -> weekly mean 226.7; 160 / 226.7 = 71 %
    assert result["week"]["vs_chronic_weekly_mean_pct"] == 71 and result["week"]["deload_like"] is True
    assert result["acwr"] == round((560 / 14) / (1360 / 42), 2)


def test_sport_breakdown_primary_and_cross_training():
    """Per family: the primary sport has the highest 7-day load; a sport with < 3 active days has no monotony."""
    acts = _four_rides_per_week() + [_act(END - timedelta(days=5), sport="Run", load=50.0),
                                     _act(END - timedelta(days=1), sport="WeightTraining", load=None)]
    result = lm.sport_breakdown(acts, END)
    assert list(result["by_sport"]) == ["cycling", "running", "weighttraining"]
    assert result["primary_sport"] == "cycling" and result["multi_sport"]
    assert result["primary_monotony"] == 1.07
    running = result["by_sport"]["running"]["week"]
    assert running["monotony"] is None and running["monotony_note"].startswith("fewer than 3")
    assert result["by_sport"]["weighttraining"]["chronic"]["sessions_without_load"] == 1
    single = lm.sport_breakdown(_four_rides_per_week(), END)
    assert single["primary_sport"] == "cycling" and not single["multi_sport"]
    assert lm.sport_breakdown([], END) == {"by_sport": {}, "primary_sport": None, "primary_monotony": None, "multi_sport": False}


def test_weekly_rows_partial_week_and_deload():
    """ISO weeks: complete weeks get strain and the share of the trailing weekly mean, a partial week does not."""
    acts = _four_rides_per_week(weeks=6, end=END - timedelta(days=7)) + _four_rides_per_week(weeks=1, load=40.0)
    acts.append(_act(END + timedelta(days=1), load=80.0))  # Monday after END
    acts.append(_act(END + timedelta(days=2), sport="WeightTraining", load=None))
    rows = lm.weekly_rows(acts, END + timedelta(days=2), weeks=3)
    assert [r["week"] for r in rows] == ["2026-W39", "2026-W40", "2026-W41"]
    full, light, partial = rows[0], rows[1], rows[2]
    assert full["load"] == 400 and full["vs_chronic_weekly_mean_pct"] == 100 and full["deload_like"] is False
    assert full["rest_days"] == 3 and full["monotony"] == 1.07 and full["strain"] == 428
    # 160 vs (3 x 400 + 160) / 4 = 340 -> 47 %
    assert light["load"] == 160 and light["vs_chronic_weekly_mean_pct"] == 47 and light["deload_like"] is True
    assert partial["days"] == 2 and not partial["complete"] and partial["vs_chronic_weekly_mean_pct"] is None
    assert partial["monotony"] is None and partial["strain"] is None
    assert partial["by_sport"] == {"cycling": 80, "weighttraining": None} and partial["sessions_without_load"] == 1


def _model_wellness(start, days, loads, ctl=50.0, atl=50.0):
    """Wellness records generated with the exponential model (as Intervals.icu stores them)."""
    records = {}
    for offset in range(days):
        day = start + timedelta(days=offset)
        load = loads.get(day, 0.0)
        ctl = lm.model_step(ctl, load, 42)
        atl = lm.model_step(atl, load, 7)
        records[day] = {"id": day.isoformat(), "ctl": ctl, "atl": atl, "rampRate": None, "ctlLoad": load, "atlLoad": load}
    return records


def test_fitness_status_values_fallback_and_recompute():
    """CTL/ATL/form/ramp from the record of the end date, an earlier day, or recomputed without planned load."""
    start = END - timedelta(days=30)
    wellness = _model_wellness(start, 31, {END - timedelta(days=2): 120.0})
    status = lm.fitness_status(wellness, END)
    assert status["available"] and status["date"] == END.isoformat() and status["source"] == "intervals.icu"
    assert status["form"] == round(wellness[END]["ctl"] - wellness[END]["atl"], 1)
    assert status["ramp"] == round(wellness[END]["ctl"] - wellness[END - timedelta(days=7)]["ctl"], 1)
    later = lm.fitness_status(wellness, END + timedelta(days=2))
    assert later["date"] == END.isoformat()
    assert lm.fitness_status({}, END)["available"] is False

    planned = dict(wellness)
    planned[END] = dict(wellness[END], ctl=60.0, atl=70.0, ctlLoad=150.0)
    recomputed = lm.fitness_status(planned, END, completed_load=40.0, today=END)
    previous = wellness[END - timedelta(days=1)]
    assert recomputed["source"] == "recomputed_without_planned"
    assert recomputed["ctl"] == round(lm.model_step(previous["ctl"], 40.0, 42), 1)
    assert recomputed["atl"] == round(lm.model_step(previous["atl"], 40.0, 7), 1)
    assert "150" in recomputed["note"]
    past = lm.fitness_status(planned, END, completed_load=40.0, today=END + timedelta(days=3))
    assert past["source"] == "intervals.icu" and past["ctl"] == 60.0


def test_projection_model_and_check():
    """Steady load keeps CTL/ATL, no load decays them; replaying the model reproduces stored values."""
    flat = lm.project_fitness(50.0, 50.0, END, {END + timedelta(days=i): 50.0 for i in range(1, 8)}, END + timedelta(days=7))
    assert {row["ctl"] for row in flat} == {50.0} and flat[-1]["ramp"] == 0.0
    decay = lm.project_fitness(50.0, 70.0, END, {}, END + timedelta(days=1))
    assert decay[0]["ctl"] == round(50 * math.exp(-1 / 42), 1) and decay[0]["atl"] == round(70 * math.exp(-1 / 7), 1)
    assert decay[0]["ramp"] is None
    start = END - timedelta(days=20)
    wellness = _model_wellness(start, 21, {END - timedelta(days=3): 200.0, END - timedelta(days=9): 90.0})
    check = lm.model_check(wellness, END, 14)
    assert check == {"days": 14, "max_ctl_diff": 0.0, "max_atl_diff": 0.0}
    off = lm.model_check(wellness, END, 14, ctl_days=30)
    assert off["max_ctl_diff"] > 0.5
    assert lm.model_check({}, END) is None


def test_top_sessions_keeps_the_hardest_short_session():
    """Highest loads first; the session with the highest IF replaces the last one when it is missing."""
    acts = [_act(END - timedelta(days=i), load=200.0 - i * 10, icu_intensity=70) for i in range(6)]
    acts.append(_act(END - timedelta(days=9), load=30.0, secs=1500, icu_intensity=105.0))
    top = lm.top_sessions(acts, count=5)
    assert [s["load"] for s in top[:4]] == [200, 190, 180, 170]
    assert top[-1]["intensity_factor"] == 1.05 and top[-1]["minutes"] == 25
    assert lm.top_sessions([]) == []
    assert lm.intensity_factor({"icu_intensity": 90}) == 0.9 and lm.intensity_factor({"icu_intensity": None}) is None
    # ANA-9: icu_intensity is always percent; a tiny value is no IF > 1 and no "hard" session
    assert lm.intensity_factor({"icu_intensity": 2.6}) == pytest.approx(0.026)


# ------------------------------------------------------------------ intensity
def test_zone_seconds_and_three_zone_mapping():
    """Power zones ignore the sweet-spot bucket; the mapping depends on basis and zone count."""
    ride = _act(END, icu_zone_times=_power(10, 20, 30, 40, 50, 60, 70), icu_hr_zone_times=[10, 20, 30, 40, 50, 60, 70])
    assert tid.zone_seconds(ride, "power") == [10, 20, 30, 40, 50, 60, 70]
    # Coggan Z4 (threshold) is middle-zone work by default, high only with threshold_as="high"
    assert tid.three_zones([10, 20, 30, 40, 50, 60, 70], "power") == [30, 70, 180]
    assert tid.three_zones([10, 20, 30, 40, 50, 60, 70], "power", "high") == [30, 30, 220]
    assert tid.three_zones([10, 20, 30, 40, 50, 60], "power") == [30, 70, 110]
    assert tid.three_zones([10, 20, 30, 40, 50, 60, 70], "hr") == [30, 70, 180]
    assert tid.three_zones([10, 20, 30, 40, 50], "hr") == [30, 30, 90]
    assert tid.three_zones([10, 20, 30], "pace") == [10, 20, 30]
    assert tid.three_zones([10, 20, 30, 40], "power") is None
    assert tid.zone_seconds(_act(END, icu_hr_zone_times=[0, 0, 0]), "hr") is None
    # API-18: GAP zone times when Intervals.icu shows them (hilly runs)
    hilly = _act(END, pace_zone_times=[3000, 5, 0, 0, 0, 0, 0], gap_zone_times=[2800, 192, 13, 0, 0, 0, 0], use_gap_zone_times=True)
    assert tid.zone_seconds(hilly, "pace") == [2800, 192, 13, 0, 0, 0, 0]
    assert tid.zone_seconds(dict(hilly, use_gap_zone_times=False), "pace") == [3000, 5, 0, 0, 0, 0, 0]
    assert tid.zone_seconds(_act(END, icu_zone_times="n/a"), "power") is None
    assert tid.mapping_text()["power"]["7"] == "Z1-Z2 | Z3-Z4 | Z5-Z7"
    assert tid.mapping_text("high")["power"]["7"] == "Z1-Z2 | Z3 | Z4-Z7"
    assert tid.mapping_text()["hr"]["7"] == "Z1-Z2 | Z3-Z4 | Z5-Z7"


def test_activity_zones_basis_order_and_fallbacks():
    """Auto: power for cycling, HR then pace then power for other sports; unmapped models are reported."""
    hr7 = [100, 0, 0, 0, 0, 0, 0]
    power7 = _power(0, 0, 0, 100, 0, 0, 0)
    assert tid.activity_zones(_act(END, icu_zone_times=power7, icu_hr_zone_times=hr7))["basis"] == "power"
    assert tid.activity_zones(_act(END, sport="Run", icu_zone_times=power7, icu_hr_zone_times=hr7))["basis"] == "hr"
    run_pace = tid.activity_zones(_act(END, sport="Run", pace_zone_times=[50, 50, 0, 0, 0, 0, 0]))
    assert run_pace["basis"] == "pace" and run_pace["z"] == [100, 0, 0]
    assert tid.activity_zones(_act(END, icu_hr_zone_times=hr7))["basis"] == "hr"
    forced = tid.activity_zones(_act(END, sport="Run", icu_zone_times=power7, icu_hr_zone_times=hr7), "power")
    assert forced["basis"] == "power" and forced["z"] == [0, 100, 0]
    high = tid.activity_zones(_act(END, sport="Run", icu_zone_times=power7, icu_hr_zone_times=hr7), "power", "high")
    assert high["z"] == [0, 0, 100]
    unmapped = tid.activity_zones(_act(END, sport="Run", icu_hr_zone_times=[1, 2, 3, 4]))
    assert unmapped["z"] is None and unmapped["excluded"] == "hr zone model with 4 zones has no three-zone mapping"
    fallback = tid.activity_zones(_act(END, icu_zone_times=_power(1, 2, 3, 4), icu_hr_zone_times=hr7))
    assert fallback["basis"] == "hr"
    assert tid.activity_zones(_act(END, sport="WeightTraining"))["excluded"] == "no zone times"


def test_polarization_index_edge_cases_and_classes():
    """Treff et al. 2019 index with the edge cases of #150 and the class rules."""
    assert tid.polarization_index(0, 0, 0) == (None, "no zone data")
    assert tid.polarization_index(0.9, 0.095, 0.005) == (None, "Z3 below 1 %")
    assert tid.polarization_index(0.0, 0.5, 0.5) == (None, "Z1 is 0")
    pi, note = tid.polarization_index(0.8, 0.0, 0.2)
    assert pi == pytest.approx(math.log10(1600)) and note == "Z2 = 0 replaced by 0.01"
    pi, note = tid.polarization_index(0.8, 0.05, 0.15)
    assert pi == pytest.approx(math.log10(240)) and note is None
    assert tid.classify(0.9, 0.095, 0.005, None) == "Base"
    assert tid.classify(0.8, 0.05, 0.15, math.log10(240)) == "Polarized"
    assert tid.classify(0.7, 0.2, 0.1, 1.5) == "Pyramidal"
    assert tid.classify(0.3, 0.5, 0.2, 1.0) == "Threshold"
    assert tid.classify(0.3, 0.2, 0.5, 2.9) == "HIT"
    weak = tid.polarization_index(0.8, 0.09, 0.11)[0]
    assert weak is not None and weak < 2 and tid.classify(0.8, 0.09, 0.11, weak) == "Pyramidal"
    assert tid.zone_order(0.8, 0.09, 0.11) == "Z1 > Z3 > Z2"
    assert tid.zone_order(0.5, 0.25, 0.25) == "Z1 > Z2 = Z3"


def test_distribution_mixed_basis_and_hard_sessions():
    """Power and HR based time is summed with its share; hard sessions by Z3 time or IF on long sessions."""
    entries = [{"z": [3600.0, 0.0, 400.0], "basis": "power"}, {"z": [1800.0, 200.0, 0.0], "basis": "hr"}, {"z": None}]
    result = tid.distribution(entries)
    assert result["sessions"] == 2 and result["pct"] == [90.0, 3.3, 6.7]
    assert result["basis_pct"] == {"hr": 33.0, "power": 67.0} and result["hours"] == 1.7
    assert result["class"] == "Polarized" and result["order"] == "Z1 > Z3 > Z2"
    assert tid.distribution([])["pct"] is None
    assert tid.hard_session(_act(END), {"z": [0, 0, 600]}) == (True, ["10 min in Z3"])
    assert tid.hard_session(_act(END, secs=1200, icu_intensity=90), {"z": None}) == (True, ["IF 0.90"])
    assert tid.hard_session(_act(END, secs=600, icu_intensity=95), {"z": [0, 0, 100]}) == (False, [])
    assert tid.hard_session(_act(END), {"z": None}) == (None, [])


def test_analyze_period_weeks_drift_and_coverage():
    """Period, sports, ISO weeks and halves; sessions without zones are counted, never treated as easy."""
    start = END - timedelta(days=13)
    acts = []
    for offset in range(14):
        day = start + timedelta(days=offset)
        if offset < 7:
            acts.append(_act(day, icu_zone_times=_power(3000, 0, 0, 0, 0, 0, 0)))
        else:
            acts.append(_act(day, icu_zone_times=_power(1800, 0, 600, 0, 1200, 0, 0)))
    acts.append(_act(END, sport="Run", icu_hr_zone_times=[1800, 0, 0, 0, 0, 0, 0]))
    acts.append(_act(END, sport="WeightTraining"))
    result = tid.analyze_period(acts, start, END)
    assert result["coverage"]["sessions"] == 16 and result["coverage"]["sessions_with_zones"] == 15
    assert result["coverage"]["excluded"] == {"no zone times": 1}
    assert list(result["by_sport"]) == ["cycling", "running", "weighttraining"]
    assert result["by_sport"]["weighttraining"]["pct"] is None
    first, second = result["drift"]["first"], result["drift"]["second"]
    assert first["class"] == "Base" and first["hard_sessions"] == 0
    assert second["hard_sessions"] == 7 and second["hard_days"] == 7
    assert result["drift"]["available"] and result["drift"]["class_changed"]
    assert result["drift"]["delta_pp"][2] > 30
    assert [w["week"] for w in result["weeks"]] == ["2026-W39", "2026-W40"]
    assert result["threshold_as"] == "moderate"
    threshold_week = [_act(start + timedelta(days=i), icu_zone_times=_power(1800, 0, 0, 1200, 0, 0, 0)) for i in range(7)]
    moderate = tid.analyze_period(threshold_week, start, start + timedelta(days=6))
    high = tid.analyze_period(threshold_week, start, start + timedelta(days=6), threshold_as="high")
    assert moderate["total"]["hard_sessions"] == 0 and moderate["total"]["class"] == "Base"
    assert high["total"]["hard_sessions"] == 7
    assert all("day" not in row for row in result["sessions"])
    empty = tid.analyze_period([], start, END)
    assert empty["total"]["pct"] is None and empty["drift"]["available"] is False
    assert tid.drift([], END, END)["available"] is False


# ------------------------------------------------------------------ durability
def _steady(day, sport="Ride", decoupling=3.0, **extra):
    base = {"moving_time": 5400, "elapsed_time": 5500, "average_heartrate": 140, "icu_variability_index": 1.05,
            "decoupling": decoupling, "average_temp": 18.0, "icu_efficiency_factor": 1.45, "gear": {"id": "b1"}}
    base.update(extra)
    return _act(day, sport=sport, **base)


def test_exclusion_reasons():
    """Each quality criterion has its own reason; runs without power pass as pace:HR."""
    assert dur.exclusion_reason(_steady(END)) is None
    assert dur.exclusion_reason(_steady(END, moving_time=3000)) == "short"
    assert dur.exclusion_reason(_steady(END, elapsed_time=9000)) == "pauses"
    assert dur.exclusion_reason(_steady(END, trainer=True), environment="outdoor") == "environment"
    assert dur.exclusion_reason(_steady(END, average_temp=30.0)) == "heat"
    assert dur.exclusion_reason(_steady(END, average_temp=30.0), max_temp_c=None) is None
    assert dur.exclusion_reason(_steady(END, average_temp=None)) is None
    assert dur.exclusion_reason(_steady(END, average_heartrate=None)) == "no_hr"
    assert dur.exclusion_reason(_steady(END, icu_variability_index=None)) == "no_power"
    assert dur.exclusion_reason(_steady(END, sport="Run", icu_variability_index=None)) is None
    assert dur.exclusion_reason(_steady(END, icu_variability_index=1.3)) == "variable"
    assert dur.exclusion_reason(_steady(END, decoupling=None)) == "no_decoupling"
    assert dur.exclusion_reason(_steady(END, icu_efficiency_factor=None), need="efficiency") == "no_efficiency_factor"


def test_decoupling_summary_trend_band_and_small_samples():
    """Median, count above 5 %, recent median vs window with a 1 pp band; small samples flagged."""
    start = END - timedelta(days=27)
    acts = [_steady(start + timedelta(days=i), decoupling=v) for i, v in zip((0, 4, 8, 12, 16), (2.0, 3.0, 4.0, 3.0, 2.0), strict=True)]
    acts += [_steady(END - timedelta(days=3), decoupling=7.0), _steady(END, decoupling=8.0)]
    acts += [_steady(END - timedelta(days=1), sport="Run", icu_variability_index=None, decoupling=1.0)]
    acts += [_steady(END - timedelta(days=2), moving_time=1800), _steady(END - timedelta(days=2), sport="Hike")]
    result = dur.decoupling_summary(acts, start, END)
    cycling, running = result["by_sport"]["cycling"], result["by_sport"]["running"]
    assert cycling["n"] == 7 and cycling["median"] == 3.0 and cycling["above_threshold"] == 2
    assert cycling["recent"]["n"] == 2 and cycling["recent"]["median"] == 7.5
    assert cycling["recent"]["delta_pp"] == 4.5 and cycling["recent"]["direction"] == "higher"
    assert cycling["p25"] == 2.5 and cycling["small_sample"]  # 7 < 8 qualifying sessions (phase 5)
    assert cycling["considered"] == 8 and cycling["qualifying_share_pct"] == 88
    assert running["n"] == 1 and running["small_sample"] and running["basis"] == {"pace:HR": 1}
    assert running["recent"]["direction"] is None
    assert result["considered"] == 9 and result["excluded_by_reason"] == {"short": 1}
    stable = dur.decoupling_summary([_steady(END - timedelta(days=i), decoupling=4.0 + i * 0.1) for i in range(6)], start, END)
    assert stable["by_sport"]["cycling"]["recent"]["direction"] == "within band"
    assert dur.decoupling_summary([], start, END)["by_sport"]["cycling"]["median"] is None


def test_decoupling_sample_share_and_heterogeneity():
    """Phase 5 (G): qualifying share per sport, < 8 sessions flagged, indoor/outdoor, bikes and power meters mixed."""
    start = END - timedelta(days=27)
    acts = [_steady(start + timedelta(days=i), decoupling=3.0, gear={"id": "b1"}, power_meter="Shimano") for i in range(0, 16, 2)]
    uniform = dur.decoupling_summary(acts, start, END)["by_sport"]["cycling"]
    assert uniform["n"] == 8 and not uniform["small_sample"] and uniform["heterogeneity"] == []
    acts += [_steady(END - timedelta(days=1), decoupling=4.0, trainer=True, gear={"id": "b2"}, power_meter="Assioma")]
    acts += [_steady(END, moving_time=1800)]  # considered, excluded (short)
    mixed = dur.decoupling_summary(acts, start, END)["by_sport"]["cycling"]
    assert mixed["considered"] == 10 and mixed["n"] == 9 and mixed["qualifying_share_pct"] == 90
    assert mixed["heterogeneity"] == [
        "indoor and outdoor mixed (1 indoor, 8 outdoor)", "2 different bikes/shoes", "2 different power meters (Assioma, Shimano)",
    ]


def test_efficiency_summary_change_and_gear():
    """EF of steady sessions with power: recent vs window mean with a 2 % band and the number of bikes."""
    start = END - timedelta(days=27)
    acts = [_steady(start + timedelta(days=i), icu_efficiency_factor=1.40, moving_time=1500) for i in (0, 5, 10, 15)]
    acts += [_steady(END - timedelta(days=1), icu_efficiency_factor=1.50, gear={"id": "b2"}),
             _steady(END, icu_efficiency_factor=1.50, elapsed_time=20000)]
    acts += [_steady(END, sport="Run", icu_variability_index=None, icu_efficiency_factor=2.0)]
    result = dur.efficiency_summary(acts, start, END)
    cycling = result["cycling"]
    assert cycling["n"] == 6 and cycling["recent_n"] == 2 and cycling["gear_ids"] == 2
    assert cycling["mean"] == round((4 * 1.4 + 2 * 1.5) / 6, 3) and cycling["direction"] == "higher"
    assert result["running"]["n"] == 0 and result["running"]["direction"] is None
