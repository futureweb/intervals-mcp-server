"""
Unit tests for intervals_mcp_server.utils.wellness_stats.

These use ~60 synthetic daily wellness entries with a known linear HRV trend plus one
outlier, a gap of three missing days, null/NaN values, weight decreasing 0.1 kg per
day, intake logged on only 40 of the 60 days and a constant device burn, and check
the trend, correlation, weight and nutrition statistics against hand-computed values.
"""

import random
from datetime import date, timedelta
from typing import Any

import pytest

from intervals_mcp_server.utils.wellness_stats import (
    CAUSATION_NOTE,
    compute_correlation,
    compute_metric_trend,
    format_correlation,
    format_metric_trend,
    format_nutrition_summary,
    format_weight_trend,
    metric_series,
    metric_units,
    nutrition_summary,
    sort_entries,
    weight_trend,
)

START = date(2026, 8, 8)
DAYS = 60  # 2026-08-08 .. 2026-10-06
GAP_DAYS = {20, 21, 22}  # absent from the list entirely
HRV_MISSING = {10: None, 11: "NaN", 12: float("nan"), 50: None}
OUTLIER_DAY = 30
OUTLIER_HRV = 120.0
INTAKE_UNLOGGED = {offset for offset in range(DAYS) if offset % 4 == 3} | {0, 1}
# Unlogged days alternate between kcalConsumed null and 0 (day 0 null, day 1 zero).
INTAKE_NULL = {offset for offset in INTAKE_UNLOGGED if offset % 8 == 3} | {0}
LOGGED_DAYS = sorted(set(range(DAYS)) - GAP_DAYS - INTAKE_UNLOGGED)
DEVICE_BALANCE_DAY = 58
BURN = 2800.0


def _day(offset: int) -> str:
    """ISO date of the synthetic day with the given offset from START."""
    return (START + timedelta(days=offset)).isoformat()


def _hrv(offset: int) -> float:
    """Known linear HRV trend: 50 ms on day 0, +0.5 ms per day."""
    return 50.0 + 0.5 * offset


def _weight(offset: int) -> float:
    """Known weight: 80 kg on day 0, -0.1 kg per day."""
    return 80.0 - 0.1 * offset


def _kcal(offset: int) -> float:
    """Known intake on logged days: 2000 kcal on day 0, +10 kcal per day."""
    return 2000.0 + 10.0 * offset


def _noise(offset: int) -> float:
    """Deterministic pseudo-random sequence in 0..10 (used for the lag test)."""
    return float((offset * 37) % 11)


def _entry(offset: int) -> dict[str, Any]:
    """One synthetic wellness entry."""
    entry: dict[str, Any] = {
        "id": _day(offset),
        "hrv": OUTLIER_HRV if offset == OUTLIER_DAY else _hrv(offset),
        "weight": _weight(offset),
        "GarminTotalCalories": BURN,
        "GarminActiveCalories": 600,
        "lin_a": 3 * offset + 1,
        "lin_b": 6 * offset + 2,
        "noise": _noise(offset),
        "noise_prev": _noise(offset - 1) if offset > 0 else None,
        "updated": f"{_day(offset)}T06:00:00",
        "locked": False,
    }
    if offset in HRV_MISSING:
        entry["hrv"] = HRV_MISSING[offset]
    if offset in INTAKE_UNLOGGED:
        entry["kcalConsumed"] = None if offset in INTAKE_NULL else 0
    else:
        entry.update(
            kcalConsumed=_kcal(offset), carbohydrates=250, protein=120, fatTotal=70
        )
    if offset == DEVICE_BALANCE_DAY:
        entry["GarminKcalBalance"] = -100
    return entry


def _build_entries() -> list[dict[str, Any]]:
    """The synthetic entries in a deterministic shuffled order (the API order is not guaranteed)."""
    entries = [_entry(offset) for offset in range(DAYS) if offset not in GAP_DAYS]
    random.Random(7).shuffle(entries)
    return entries


ENTRIES = _build_entries()


def test_sort_entries_orders_by_date_and_drops_unparsable_ids() -> None:
    """
    Entries are sorted by their id date; entries without a parsable id are dropped.
    """
    unsorted: list[dict[str, Any]] = [
        {"id": "2026-01-03"}, {"id": "bad"}, {"x": 1}, {"id": "2026-01-01"}, {"id": "2026-01-02"}
    ]
    assert [e["id"] for e in sort_entries(unsorted)] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert len(sort_entries(ENTRIES)) == DAYS - len(GAP_DAYS)
    assert sort_entries(ENTRIES)[0]["id"] == _day(0)
    assert sort_entries(ENTRIES)[-1]["id"] == _day(DAYS - 1)


def test_metric_series_fills_gaps_and_treats_nan_as_missing() -> None:
    """
    The series covers every calendar day of the range; absent days and null/NaN
    values are None, everything else is a float.
    """
    series = metric_series(ENTRIES, "hrv")
    assert len(series) == DAYS
    assert series[0] == (START, 50.0)
    assert series[-1] == (START + timedelta(days=DAYS - 1), 79.5)
    for offset in GAP_DAYS | set(HRV_MISSING):
        assert series[offset] == (START + timedelta(days=offset), None)
    assert series[OUTLIER_DAY][1] == OUTLIER_HRV
    assert not metric_series([], "hrv")


def test_metric_trend_counts_latest_and_stats() -> None:
    """
    Day counts, latest value and whole-range statistics match the synthetic data.
    """
    result = compute_metric_trend(ENTRIES, "hrv")
    assert result["metric"] == "hrv"
    assert (result["start"], result["end"]) == (_day(0), _day(DAYS - 1))
    assert result["days_total"] == DAYS
    assert result["days_with_value"] == DAYS - len(GAP_DAYS) - len(HRV_MISSING)  # 53
    assert result["days_missing"] == 7
    assert result["latest"] == {"date": _day(DAYS - 1), "value": 79.5}
    stats = result["stats"]
    assert stats["min"] == 50.0
    assert stats["max"] == OUTLIER_HRV
    # 53 values: 52 linear ones + the outlier on top; the 27th smallest is day 33.
    assert stats["median"] == pytest.approx(_hrv(33))
    assert stats["p25"] < stats["median"] < stats["p75"]
    assert stats["stdev"] > 0


def test_metric_trend_baseline_rolling_and_week_over_week() -> None:
    """
    Baseline, rolling windows and the 7d-vs-previous-7d comparison use the available
    values of the last N calendar days only and report how many there were.
    """
    result = compute_metric_trend(ENTRIES, "hrv")
    baseline = result["baseline"]
    # Last 42 calendar days are offsets 18..59 minus the gap (20-22) and the None on day 50.
    assert baseline["days"] == 42
    assert baseline["n"] == 38
    assert baseline["median"] == pytest.approx((_hrv(40) + _hrv(41)) / 2)
    assert baseline["stdev"] > 0
    rolling7 = result["rolling"][7]
    assert rolling7["latest_n"] == 7
    assert rolling7["latest_mean"] == pytest.approx(sum(_hrv(o) for o in range(53, 60)) / 7)  # 78.0
    rolling14 = result["rolling"][14]
    assert rolling14["latest_n"] == 13
    expected14 = (sum(_hrv(o) for o in range(46, 60)) - _hrv(50)) / 13
    assert rolling14["latest_mean"] == pytest.approx(expected14)
    assert result["rolling"][42]["latest_n"] == 38
    week = result["trend_7d_vs_prev_7d"]
    assert week["recent_mean"] == pytest.approx(78.0)
    assert week["previous_n"] == 6
    assert week["previous_mean"] == pytest.approx((sum(_hrv(o) for o in range(46, 53)) - _hrv(50)) / 6)
    assert week["diff"] > 0 and week["diff_pct"] > 0
    versus = result["latest_vs_baseline"]
    assert versus["diff"] == pytest.approx(79.5 - baseline["mean"])
    assert versus["z"] == pytest.approx(versus["diff"] / baseline["stdev"])


def test_metric_trend_outliers_and_series_rolling() -> None:
    """
    The single injected outlier is detected by its z-score and the per-day rolling
    means are trailing means restricted to the range (no fabricated values).
    """
    result = compute_metric_trend(ENTRIES, "hrv")
    assert len(result["outliers"]) == 1
    outlier = result["outliers"][0]
    assert outlier["date"] == _day(OUTLIER_DAY)
    assert outlier["value"] == OUTLIER_HRV
    assert outlier["z"] >= 2.5
    series = result["series"]
    assert len(series) == DAYS
    assert series[0] == {"date": _day(0), "value": 50.0, "rolling": {7: 50.0, 14: 50.0, 42: 50.0}}
    assert series[-1]["rolling"][7] == pytest.approx(78.0)
    assert series[12]["value"] is None
    # Trailing 7 days ending on day 12: days 6..9 have values, 10..12 do not.
    assert series[12]["rolling"][7] == pytest.approx(sum(_hrv(o) for o in range(6, 10)) / 4)
    assert series[20]["value"] is None
    assert result["windows"] == [7, 14, 42]
    assert result["outlier_z"] == 2.5
    assert not compute_metric_trend(ENTRIES, "hrv", outlier_z=10.0)["outliers"]


def test_correlation_of_linear_metrics_is_one() -> None:
    """
    Two perfectly linearly related metrics have Pearson r = Spearman rho = 1, HRV and
    weight (one rising, one falling) are strongly negatively correlated.
    """
    result = compute_correlation(ENTRIES, "lin_a", "lin_b")
    assert result["n"] == DAYS - len(GAP_DAYS)
    assert result["pearson_r"] == pytest.approx(1.0)
    assert result["spearman_rho"] == pytest.approx(1.0)
    assert result["lag_days"] == 0
    assert result["note"] == CAUSATION_NOTE
    negative = compute_correlation(ENTRIES, "lin_a", "weight")
    assert negative["n"] == DAYS - len(GAP_DAYS)
    assert negative["pearson_r"] == pytest.approx(-1.0)
    assert negative["spearman_rho"] == pytest.approx(-1.0)
    # HRV rises while weight falls, but the injected outlier weakens the linear fit.
    hrv_weight = compute_correlation(ENTRIES, "hrv", "weight")
    assert hrv_weight["n"] == 53
    assert -0.9 < hrv_weight["pearson_r"] < -0.7
    assert hrv_weight["spearman_rho"] < hrv_weight["pearson_r"]


def test_correlation_lag_and_min_pairs() -> None:
    """
    With lag 1, a(day) is paired with b(day + 1); a metric that is yesterday's noise
    matches perfectly at lag 1 but not at lag 0. Fewer than min_pairs pairs give None.
    """
    lagged = compute_correlation(ENTRIES, "noise", "noise_prev", lag_days=1)
    # Days 0..58 pair with the next day unless either day is in the gap (19..22).
    assert lagged["n"] == 55
    assert lagged["pearson_r"] == pytest.approx(1.0)
    assert lagged["spearman_rho"] == pytest.approx(1.0)
    same_day = compute_correlation(ENTRIES, "noise", "noise_prev")
    assert same_day["n"] == 56
    assert same_day["pearson_r"] < 0.9
    too_few = compute_correlation(ENTRIES, "noise", "noise_prev", lag_days=1, min_pairs=100)
    assert too_few["n"] == 55
    assert too_few["pearson_r"] is None
    assert too_few["spearman_rho"] is None
    constant = compute_correlation(ENTRIES, "GarminTotalCalories", "hrv", min_pairs=2)
    assert constant["pearson_r"] is None


def test_weight_trend_slope_and_change() -> None:
    """
    Weight falls 0.1 kg per day, so every window has an OLS slope of -0.7 kg/week.
    """
    result = weight_trend(ENTRIES)
    assert result["latest"] == {"date": _day(DAYS - 1), "value": pytest.approx(74.1)}
    assert result["days_with_value"] == DAYS - len(GAP_DAYS)
    for window in (7, 14, 28):
        stats = result["windows"][window]
        assert stats["days"] == window
        assert stats["slope_kg_per_week"] == pytest.approx(-0.7)
        assert stats["change_kg"] == pytest.approx(-0.1 * (window - 1))
        assert stats["last"]["date"] == _day(DAYS - 1)
        assert stats["first"]["date"] == _day(DAYS - window)
    seven = result["windows"][7]
    assert seven["n"] == 7
    assert seven["mean"] == pytest.approx(sum(_weight(o) for o in range(53, 60)) / 7)
    # A window that reaches into the gap has fewer points but the same exact slope.
    forty = weight_trend(ENTRIES, windows=(40,))["windows"][40]
    assert forty["n"] == 37
    assert forty["slope_kg_per_week"] == pytest.approx(-0.7)
    two_points = weight_trend([{"id": _day(0), "weight": 80}, {"id": _day(1), "weight": 79.5}])
    assert two_points["windows"][7]["change_kg"] == pytest.approx(-0.5)
    assert two_points["windows"][7]["slope_kg_per_week"] is None


def test_nutrition_summary_excludes_unlogged_days() -> None:
    """
    Over the 60-day window only the 40 logged days feed the intake and balance
    statistics; unlogged days (missing, null or 0 kcal) are counted, never treated as 0.
    """
    result = nutrition_summary(ENTRIES, windows=(60,))
    assert result["days_total"] == DAYS
    window = result["windows"][60]
    assert window["days"] == DAYS
    assert window["days_logged"] == 40 == len(LOGGED_DAYS)
    assert window["days_missing_intake"] == 20
    assert window["days_with_burn"] == DAYS - len(GAP_DAYS)
    expected_total = sum(_kcal(o) for o in LOGGED_DAYS)
    assert window["kcal_consumed_total"] == pytest.approx(expected_total)
    assert window["kcal_consumed_mean_logged_days"] == pytest.approx(expected_total / 40)
    assert window["kcal_consumed_mean_logged_days"] > 2000  # zeros would drag it below
    assert window["carbs_mean_g"] == 250
    assert window["protein_mean_g"] == 120
    assert window["fat_mean_g"] == 70
    assert window["burn_mean_kcal"] == BURN
    assert window["weight_change_kg"] == pytest.approx(_weight(DAYS - 1) - _weight(0))


def test_nutrition_balance_only_on_logged_days() -> None:
    """
    The balance is the device field when present, otherwise intake minus burn, and only
    for logged days; unlogged days have no balance and do not enter the totals.
    """
    result = nutrition_summary(ENTRIES, windows=(60, 7))
    rows = {row["date"]: row for row in result["days"]}
    assert len(rows) == DAYS
    logged_row = rows[_day(57)]
    assert logged_row["logged"] is True
    assert logged_row["kcal_consumed"] == _kcal(57)
    assert logged_row["balance_kcal"] == pytest.approx(_kcal(57) - BURN)
    assert logged_row["active_kcal"] == 600
    device_row = rows[_day(DEVICE_BALANCE_DAY)]
    assert device_row["balance_kcal"] == -100
    zero_row = rows[_day(1)]
    assert zero_row["logged"] is False
    assert zero_row["kcal_consumed"] == 0
    assert zero_row["balance_kcal"] is None
    null_row = rows[_day(0)]
    assert null_row["logged"] is False
    assert null_row["kcal_consumed"] is None
    assert null_row["balance_kcal"] is None
    gap_row = rows[_day(20)]
    assert gap_row["logged"] is False
    assert gap_row["burn_kcal"] is None
    assert gap_row["weight"] is None
    window = result["windows"][60]
    expected_balance = sum(_kcal(o) - BURN for o in LOGGED_DAYS if o != DEVICE_BALANCE_DAY) - 100
    assert window["days_with_balance"] == 40
    assert window["balance_total_kcal_logged_days"] == pytest.approx(expected_balance)
    assert window["balance_mean_kcal_logged_days"] == pytest.approx(expected_balance / 40)
    # Last 7 calendar days: offsets 53..59, unlogged 55 and 59.
    week = result["windows"][7]
    assert week["days"] == 7
    assert week["days_logged"] == 5
    assert week["days_missing_intake"] == 2
    assert any("never treated as 0 kcal" in note for note in result["notes"])
    assert any("device estimate" in note for note in result["notes"])


def test_formatters_contain_key_numbers() -> None:
    """
    The text renderings carry the headline numbers, list outliers with their date and
    keep the per-day table short.
    """
    trend_text = format_metric_trend(compute_metric_trend(ENTRIES, "hrv"), units="ms")
    assert "hrv (ms): period 2026-08-08 to 2026-10-06, 60 days, 53 with values, 7 missing" in trend_text
    assert "Latest: 2026-10-06 = 79.5 ms" in trend_text
    assert "Rolling means at 2026-10-06 (lookback included): 7d 78 ms (n=7)" in trend_text
    assert "Personal baseline (42 days 2026-08-26 to 2026-10-06, n=38)" in trend_text
    assert f"Outliers in the period (|z| >= 2.5): {_day(OUTLIER_DAY)} = 120 ms (z +" in trend_text
    assert "Last 14 days (date, value ms, 7d mean):" in trend_text
    assert "46 earlier days of the period not shown" in trend_text
    assert trend_text.count("\n  2026-") == 14
    assert "n/a" not in trend_text.split("Last 14 days", maxsplit=1)[0]

    corr_text = format_correlation(compute_correlation(ENTRIES, "lin_a", "lin_b"))
    assert "Correlation lin_a vs lin_b (same day): n=57" in corr_text
    assert "Pearson r 1, Spearman rho 1" in corr_text
    assert CAUSATION_NOTE in corr_text
    lag_text = format_correlation(compute_correlation(ENTRIES, "noise", "noise_prev", lag_days=1))
    assert "noise_prev 1 day(s) later" in lag_text

    weight_text = format_weight_trend(weight_trend(ENTRIES))
    assert "latest 2026-10-06 = 74.1 kg" in weight_text
    assert "7d: n=7" in weight_text
    assert "slope -0.7 kg/week" in weight_text
    assert "change -0.6 kg" in weight_text

    nutrition_text = format_nutrition_summary(nutrition_summary(ENTRIES, windows=(60,)))
    assert "60d: logged 40/60 (20 without intake)" in nutrition_text
    assert "burn mean 2800 kcal (57 days)" in nutrition_text
    assert "not logged" in nutrition_text
    assert "53 earlier days not shown" in nutrition_text
    assert "never treated as 0 kcal" in nutrition_text


def test_formatters_show_na_for_missing_statistics() -> None:
    """
    Statistics that cannot be computed render as 'n/a' instead of a fabricated number.
    """
    too_few = compute_correlation(ENTRIES, "noise", "noise_prev", min_pairs=100)
    corr_text = format_correlation(too_few)
    assert "Pearson r n/a, Spearman rho n/a" in corr_text
    assert "fewer than 100 paired days" in corr_text
    short = [{"id": _day(0), "weight": 80.0}, {"id": _day(1), "weight": 79.5}]
    weight_text = format_weight_trend(weight_trend(short))
    assert "slope n/a kg/week" in weight_text
    assert "change -0.5 kg" in weight_text
    nutrition_text = format_nutrition_summary(nutrition_summary(short, windows=(7,)))
    assert "7d: logged 0/2 (2 without intake), intake mean n/a kcal (total n/a)" in nutrition_text
    assert "burn mean n/a kcal (0 days)" in nutrition_text
    trend_text = format_metric_trend(compute_metric_trend(short, "hrv"))
    assert "Latest: n/a" in trend_text
    assert "Period stats: n/a" in trend_text
    assert "Latest vs baseline: n/a" in trend_text
    assert "Last 7d vs previous 7d: n/a" in trend_text


def test_degenerate_inputs() -> None:
    """
    An empty list and entries lacking the metric yield empty/None results without errors.
    """
    empty = compute_metric_trend([], "hrv")
    assert empty["days_total"] == 0
    assert empty["latest"] is None
    assert empty["stats"] is None
    assert empty["baseline"]["n"] == 0
    assert empty["latest_vs_baseline"] is None
    assert empty["rolling"][7] == {"latest_mean": None, "latest_n": 0}
    assert empty["trend_7d_vs_prev_7d"] is None
    assert not empty["outliers"]
    assert not empty["series"]
    assert "n/a" in format_metric_trend(empty)
    assert "Series: no days in range" in format_metric_trend(empty)

    no_metric = compute_metric_trend(ENTRIES, "GarminEnduranceScore")
    assert no_metric["days_total"] == DAYS
    assert no_metric["days_with_value"] == 0
    assert no_metric["days_missing"] == DAYS
    assert no_metric["latest"] is None
    assert no_metric["stats"] is None
    assert all(row["value"] is None for row in no_metric["series"])
    assert all(row["rolling"][7] is None for row in no_metric["series"])

    correlation = compute_correlation([], "hrv", "sleepScore")
    assert correlation["n"] == 0
    assert correlation["pearson_r"] is None
    assert "n/a" in format_correlation(correlation)
    assert compute_correlation(ENTRIES, "hrv", "missing")["n"] == 0

    weight = weight_trend([])
    assert weight["latest"] is None
    assert weight["windows"][7]["n"] == 0
    assert weight["windows"][7]["first"] is None
    assert "latest n/a" in format_weight_trend(weight)

    nutrition = nutrition_summary([])
    assert nutrition["days_total"] == 0
    assert not nutrition["days"]
    assert nutrition["windows"][7]["days"] == 0
    assert nutrition["windows"][7]["kcal_consumed_total"] is None
    assert "Notes:" in format_nutrition_summary(nutrition)
    assert not sort_entries([])


def test_requested_period_lookback_and_baseline_are_separate() -> None:
    """
    Regression: a 42-day request with 42 days of lookback reports 42 days (not 84), takes the
    statistics from the requested period only and the rolling means / baseline with lookback.
    """
    entries = [{"id": (date(2026, 7, 1) + timedelta(days=i)).isoformat(), "hrv": 40.0 if i < 42 else 60.0} for i in range(84)]
    period_start, period_end = entries[42]["id"], entries[-1]["id"]
    result = compute_metric_trend(entries, "hrv", windows=(7, 42), period_start=period_start, period_end=period_end)
    assert (result["start"], result["end"], result["days_total"]) == (period_start, period_end, 42)
    assert result["fetched"] == {"start": "2026-07-01", "end": period_end, "days": 84}
    assert result["stats"]["mean"] == 60 and result["stats"]["min"] == 60  # lookback values not in the period stats
    assert len(result["series"]) == 42 and result["series"][0]["date"] == period_start
    assert result["series"][0]["rolling"][7] == pytest.approx((6 * 40 + 60) / 7)  # rolling window reaches into the lookback
    assert result["baseline"]["n"] == 42 and result["baseline"]["start"] == period_start
    text = format_metric_trend(result, units="ms")
    assert text.startswith(f"hrv (ms): period {period_start} to {period_end}, 42 days, 42 with values, 0 missing")
    assert "84" not in text.split("\n", maxsplit=1)[0]


def test_zeros_are_missing_for_physiology_and_small_samples_are_flagged() -> None:
    """
    A stored 0 HRV / resting HR is 'no value' (never interpolated, never averaged); small baselines
    and correlations are flagged.
    """
    entries = [{"id": (date(2026, 9, 1) + timedelta(days=i)).isoformat(), "hrv": 0 if i % 3 == 0 else 45.0, "steps": 0}
               for i in range(12)]
    result = compute_metric_trend(entries, "hrv")
    assert result["zeros_treated_as_missing"] == 4 and result["days_with_value"] == 8
    assert result["stats"]["min"] == 45
    assert result["baseline"]["small_sample"] is True
    assert "small sample (fewer than 14 values), not reliable" in format_metric_trend(result, units="ms")
    assert "4 stored 0 treated as missing" in format_metric_trend(result, units="ms")
    assert compute_metric_trend(entries, "steps")["days_with_value"] == 12  # 0 steps is a real value
    corr = compute_correlation(ENTRIES, "lin_a", "lin_b", min_pairs=10)
    assert corr["small_sample"] is False
    few = compute_correlation(ENTRIES[:20], "lin_a", "lin_b", min_pairs=10)
    assert few["small_sample"] is True and "small sample" in format_correlation(few)


def test_stored_zero_is_no_value_in_correlation_weight_and_nutrition() -> None:
    """
    ANA-6: the zero rule of the trend statistics also applies to correlations, the weight trend and
    the nutrition weight change (a stored 0 HRV / weight is a placeholder, not a measurement).
    """
    rng = random.Random(1)
    entries = []
    for i in range(40):
        hrv = 60 + rng.gauss(0, 3)
        entries.append({"id": (date(2026, 8, 1) + timedelta(days=i)).isoformat(), "hrv": hrv,
                        "restingHR": 50 - (hrv - 60) * 0.5 + rng.gauss(0, 0.5), "weight": 70 + rng.gauss(0, 0.2),
                        "kcalConsumed": 2500})
    for i in (5, 12, 20, 33):
        entries[i]["hrv"] = 0
    corr = compute_correlation(entries, "hrv", "restingHR")
    assert corr["n"] == 36 and corr["pearson_r"] < -0.9 and corr["spearman_rho"] < -0.9
    assert corr["zeros_treated_as_missing"] == 4 and "4 stored 0 treated as missing" in format_correlation(corr)
    entries[-1]["weight"] = 0
    weight = weight_trend(entries)
    assert weight["latest"]["date"] == "2026-09-08" and weight["latest"]["value"] > 69
    assert abs(weight["windows"][7]["change_kg"]) < 1 and abs(weight["windows"][7]["slope_kg_per_week"]) < 2
    assert weight["zeros_treated_as_missing"] == 1 and "1 stored 0 treated as missing" in format_weight_trend(weight)
    nutrition = nutrition_summary(entries)
    assert nutrition["days"][-1]["weight"] is None and abs(nutrition["windows"][7]["weight_change_kg"]) < 1


def test_metric_units() -> None:
    """Native units, custom definition units and eFTP units."""
    assert metric_units("hrv") == "ms" and metric_units("restingHR") == "bpm" and metric_units("respiration") == "breaths/min"
    assert metric_units("spO2") == "%" and metric_units("readiness") == "/100" and metric_units("weight") == "kg"
    assert metric_units("GarminSkinTempDeviationC", "°C") == "°C" and metric_units("eftp_Ride") == "W"
    assert metric_units("unknownThing") is None
    text = format_metric_trend(compute_metric_trend(ENTRIES, "hrv"), units="/100")
    assert "= 79.5/100" in text
