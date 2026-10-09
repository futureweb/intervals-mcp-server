"""
Unit tests for intervals_mcp_server.utils.power_compare.

A synthetic 1 Hz ride of 1800 samples with a known structure is compared: the
secondary meter reads ``primary * 1.03 + 2`` W with gaussian noise and lags 2 s
behind the primary, both sides coast (0 W) in three stretches, a few samples are
missing (None / "NaN" / nan), the time stream has one recording gap of 120 s and
three outliers are injected into the secondary. Because of the +2 W offset the
expected relative difference is ``3 % + 200 / mean primary`` (about 3.9 %).
"""

import json
import random
from typing import Any

import pytest

from intervals_mcp_server.utils.power_compare import (
    compare_power_streams,
    comparison_to_json,
    format_power_comparison,
    format_rides_summary,
    ride_summary,
    summarize_rides,
)

# (kind, start index, end index[, level]) of the primary power stream.
SEGMENTS: tuple[tuple[Any, ...], ...] = (
    ("ramp", 0, 120),
    ("steady", 120, 300, 200),
    ("coast", 300, 330),
    ("varied", 330, 600),
    ("steady", 600, 900, 250),
    ("coast", 900, 940),
    ("varied", 940, 1200),
    ("steady", 1200, 1500, 180),
    ("varied", 1500, 1700),
    ("coast", 1700, 1720),
    ("steady", 1720, 1800, 220),
)
LAG_S = 2
OUTLIER_INDEXES = (700, 1000, 1300)
COASTING_SAMPLES = 30 + 40 + 20


def _primary_samples(rng: random.Random) -> list[float]:
    """Primary power following SEGMENTS: ramp, steady, coasting and 10 s block changes."""
    primary = [0.0] * 1800
    level = 0.0
    for segment in SEGMENTS:
        kind, start, end = segment[0], segment[1], segment[2]
        for index in range(start, end):
            if kind == "ramp":
                primary[index] = 100 + 100 * (index - start) / (end - start) + rng.gauss(0, 4)
            elif kind == "steady":
                primary[index] = segment[3] + rng.gauss(0, 4)
            elif kind == "coast":
                primary[index] = 0.0
            else:
                if (index - start) % 10 == 0:
                    level = rng.uniform(210, 260)
                primary[index] = level + rng.gauss(0, 4)
    return primary


def _build_ride() -> tuple[list[Any], list[Any], list[int]]:
    """Primary, lagged secondary (``1.03 * p + 2`` + noise, outliers, holes) and time."""
    rng = random.Random(42)
    primary: list[Any] = _primary_samples(rng)
    secondary: list[Any] = []
    for index in range(len(primary)):
        source = primary[max(0, index - LAG_S)]
        secondary.append(0.0 if source == 0 else 1.03 * source + 2 + rng.gauss(0, 3))
    # Recording gap: 120 s jump after sample 1499.
    time = list(range(0, 1500)) + list(range(1620, 1920))
    secondary[700] = primary[700] * 2.0
    secondary[1000] = primary[1000] * 0.3
    secondary[1300] = primary[1300] * 1.8
    primary[100] = None
    primary[101] = "NaN"
    primary[102] = float("nan")
    secondary[200] = None
    secondary[201] = "NaN"
    return primary, secondary, time


PRIMARY, SECONDARY, TIME = _build_ride()
RESULT = compare_power_streams(PRIMARY, SECONDARY, TIME)


def _expected_pct(mean_primary_w: float) -> float:
    """Relative difference implied by the model: 3 % scale plus the 2 W offset."""
    return 3.0 + 200.0 / mean_primary_w


def test_sample_counts_and_exclusions() -> None:
    """
    Missing samples on either side, coasting (including the lag-shifted coasting edges of
    the secondary) and the injected outliers are counted and excluded.
    """
    assert RESULT["samples"] == 1800
    assert RESULT["excluded"]["missing"] == 5
    # Each of the 3 coasting stretches ends with 2 samples where only the lagged
    # secondary is still at 0 W.
    assert RESULT["excluded"]["coasting_or_zero"] == COASTING_SAMPLES + 3 * LAG_S
    assert RESULT["excluded"]["outliers"] == len(OUTLIER_INDEXES)
    assert RESULT["paired_valid"] == 1800 - 5 - RESULT["excluded"]["coasting_or_zero"]
    assert RESULT["overall"]["n"] == RESULT["paired_valid"] - len(OUTLIER_INDEXES)


def test_overall_difference_and_ratio() -> None:
    """
    The overall difference recovers the 3 % scale (plus the 2 W offset) and the ratio.
    """
    overall = RESULT["overall"]
    expected = _expected_pct(overall["mean_primary_w"])
    assert abs(overall["mean_diff_pct"] - expected) < 0.3
    assert 2.5 < overall["mean_diff_pct"] < 4.5
    assert abs(overall["ratio_secondary_to_primary"] - 1.03) < 0.02
    assert abs(overall["ratio_secondary_to_primary"] - (1 + expected / 100)) < 0.003
    assert (
        abs(overall["mean_diff_w"] - (overall["mean_secondary_w"] - overall["mean_primary_w"]))
        < 1e-9
    )
    assert abs(overall["median_diff_pct"] - expected) < 0.5
    assert 0 < overall["stdev_diff_pct"] < 10


def test_bins_ordered_by_primary() -> None:
    """
    Bins keep the configured order, empty bins are dropped and every listed bin has samples.
    """
    bins = RESULT["bins"]
    assert bins
    assert [entry["range_w"] for entry in bins] == sorted(entry["range_w"] for entry in bins)
    assert all(entry["n"] > 0 for entry in bins)
    assert all(
        entry["range_w"][0] <= entry["mean_primary_w"] < entry["range_w"][1] for entry in bins
    )
    # The steady 250 W segment and the 210-260 W blocks fall into these bins.
    assert [200, 250] in [entry["range_w"] for entry in bins]
    assert [300, 400] not in [entry["range_w"] for entry in bins]
    assert sum(entry["n"] for entry in bins) <= RESULT["overall"]["n"]


def test_stable_windows_recover_the_offset() -> None:
    """
    Steady segments yield stable windows whose mean comparison matches the model.
    """
    for width in (30, 60):
        window = RESULT["stable_windows"][width]
        assert window["windows"] > 5
        assert abs(window["mean_diff_pct"] - _expected_pct(window["mean_primary_w"])) < 0.3
        assert abs(window["median_diff_pct"] - window["mean_diff_pct"]) < 0.5
    assert RESULT["stable_windows"][30]["windows"] > RESULT["stable_windows"][60]["windows"]


def test_lag_detected_with_positive_sign_for_lagging_secondary() -> None:
    """
    The secondary was built from the primary 2 s earlier, so it lags behind by 2 s and
    the convention (positive lag = secondary lags behind) reports +2.
    """
    lag = RESULT["lag"]
    assert lag["best_lag_s"] == LAG_S
    assert lag["correlation_at_best"] > lag["correlation_at_zero"]
    assert lag["correlation_at_best"] > 0.9


def test_lag_sign_convention_both_directions() -> None:
    """
    A secondary that leads the primary gives a negative lag, one that lags a positive one.
    """
    rng = random.Random(7)
    base = [200.0]
    for _ in range(399):
        base.append(min(300.0, max(120.0, base[-1] + rng.uniform(-15, 15))))
    time = list(range(400))
    lagging = [base[max(0, index - 3)] for index in range(400)]
    leading = [base[min(399, index + 3)] for index in range(400)]
    assert compare_power_streams(base, lagging, time)["lag"]["best_lag_s"] == 3
    assert compare_power_streams(base, leading, time)["lag"]["best_lag_s"] == -3
    aligned = compare_power_streams(base, base, time)["lag"]
    assert aligned["best_lag_s"] == 0
    assert abs(aligned["correlation_at_zero"] - 1.0) < 1e-9


def test_drift_quarters() -> None:
    """
    Four quarters by elapsed time, each with enough samples and the modelled difference.
    """
    drift = RESULT["drift"]
    assert [entry["quarter"] for entry in drift] == [1, 2, 3, 4]
    assert drift[0]["from_s"] == 0
    assert drift[3]["to_s"] == 1919
    assert all(entry["from_s"] < entry["to_s"] for entry in drift)
    assert all(entry["n"] >= 30 for entry in drift)
    assert all(2.5 < entry["mean_diff_pct"] < 5.0 for entry in drift)


def test_best_efforts() -> None:
    """
    Best efforts exist for every duration that fits in a contiguous stretch; 3600 s does not.
    """
    by_secs = {entry["secs"]: entry for entry in RESULT["best_efforts"]}
    assert list(by_secs) == [5, 30, 60, 300, 1200, 3600]
    for secs in (5, 30, 60, 300, 1200):
        assert by_secs[secs]["primary_w"] is not None
        assert by_secs[secs]["secondary_w"] is not None
        assert by_secs[secs]["diff_w"] is not None
    assert by_secs[3600]["secs"] == 3600
    assert all(value is None for key, value in by_secs[3600].items() if key != "secs")
    # The 300 s best of the primary is the steady 250 W segment.
    assert 245 < by_secs[300]["primary_w"] < 256
    assert 2.5 < by_secs[300]["diff_pct"] < 6.0
    assert 2.5 < by_secs[300]["same_window_diff_pct"] < 6.0
    # Best efforts get shorter with duration.
    assert by_secs[5]["primary_w"] >= by_secs[30]["primary_w"] >= by_secs[300]["primary_w"]


def test_windows_do_not_cross_recording_gaps() -> None:
    """
    A time jump splits the ride: windows longer than a contiguous stretch are not found
    and stable windows are counted per stretch.
    """
    power = [200.0] * 100
    time = list(range(0, 50)) + list(range(100, 150))
    result = compare_power_streams(power, power, time, best_effort_secs=(30, 60))
    efforts = {entry["secs"]: entry for entry in result["best_efforts"]}
    assert efforts[30]["primary_w"] == 200.0
    assert efforts[60]["primary_w"] is None
    assert result["stable_windows"][30]["windows"] == 2
    assert result["stable_windows"][60]["windows"] == 0


def test_format_contains_key_numbers() -> None:
    """
    The report lists the overall numbers, one line per bin/window/quarter/effort and n/a.
    """
    text = format_power_comparison(RESULT, "watts", "Power2 [secondary_power]")
    overall = RESULT["overall"]
    assert "Power2 [secondary_power] vs watts" in text
    assert f"diff {overall['mean_diff_w']:.1f} W ({overall['mean_diff_pct']:.1f}%)" in text
    assert f"ratio {overall['ratio_secondary_to_primary']:.3f}" in text
    assert "paired valid: 1699" in text
    assert "outliers 3" in text
    assert f"Lag: best {LAG_S} s" in text
    assert "3600 s: n/a vs n/a W" in text
    for entry in RESULT["bins"]:
        assert f"  {entry['range_w'][0]}-{entry['range_w'][1]} W: n {entry['n']}," in text
    assert "  30 s:" in text and "  60 s:" in text
    assert "  Q1 0-480 s:" in text and "  Q4 " in text
    assert "Notes:" in text and "No calibration" in text


def test_json_serialisable() -> None:
    """
    The JSON copy has string keys, no NaN and round-trips through json.dumps.
    """
    payload = comparison_to_json(RESULT)
    text = json.dumps(payload, allow_nan=False)
    assert json.loads(text)["lag"]["best_lag_s"] == LAG_S
    assert list(payload["stable_windows"]) == ["30", "60"]
    # Tuple keys and NaN values are converted too.
    extra = comparison_to_json({(50, 100): float("nan"), "ok": (1, 2)})
    assert extra == {"50-100": None, "ok": [1, 2]}


def test_degenerate_input() -> None:
    """
    Empty, all-missing and all-coasting input yield an empty result without exceptions.
    """
    for primary, secondary, time in (
        ([], [], []),
        ([None] * 10, ["NaN", float("nan")] * 5, list(range(10))),
        ([0] * 100, [0] * 100, list(range(100))),
    ):
        result = compare_power_streams(primary, secondary, time)
        assert result["samples"] == len(primary)
        assert result["overall"]["n"] == 0
        assert result["overall"]["mean_diff_pct"] is None
        assert not result["bins"]
        assert result["lag"]["best_lag_s"] is None
        assert len(result["drift"]) == 4
        assert all(entry["n"] == 0 for entry in result["drift"])
        assert all(entry["diff_pct"] is None for entry in result["best_efforts"])
        text = format_power_comparison(result)
        assert "n/a" in text
        json.dumps(comparison_to_json(result), allow_nan=False)
    missing = compare_power_streams([None] * 10, [None] * 10, list(range(10)))
    assert missing["excluded"]["missing"] == 10
    coasting = compare_power_streams([0] * 100, [0] * 100, list(range(100)))
    assert coasting["excluded"]["coasting_or_zero"] == 100
    assert coasting["best_efforts"][0]["primary_w"] == 0.0


# ------------------------------------------------------------- several rides
def _scaled_ride(scale: float, offset_w: float = 0.0, drift_pct: float = 0.0, samples: int = 1200) -> dict[str, Any]:
    """Comparison of a steady ride whose secondary reads ``primary * scale + offset`` (+ a linear drift)."""
    primary = [150.0 + (index % 300) / 2 for index in range(samples)]  # 150-300 W saw tooth
    secondary = [w * scale * (1 + drift_pct / 100 * index / samples) + offset_w for index, w in enumerate(primary)]
    return compare_power_streams(primary, secondary, list(range(samples)))


def test_ride_summary_key_numbers() -> None:
    """Per-ride summary: mean difference, drift between the first and last quarter, bins, lag, outliers."""
    summary = ride_summary(_scaled_ride(0.95, drift_pct=2.0))
    assert summary["used"] == 1200 and summary["samples"] == 1200
    assert -5.5 < summary["mean_diff_pct"] < -3.5
    assert summary["drift_last_minus_first_pp"] is not None and summary["drift_last_minus_first_pp"] > 1.0
    assert summary["outlier_pct"] == 0.0 and summary["best_lag_s"] == 0
    assert summary["stable_windows"] is not None
    assert set(summary["bins"]) == {"150-200", "200-250", "250-300"}
    empty = ride_summary(compare_power_streams([], [], []))
    assert empty["used"] == 0 and empty["outlier_pct"] is None and empty["drift_last_minus_first_pp"] is None


def _ride(ride_id: str, group: str, scale: float, samples: int = 1200) -> dict[str, Any]:
    """A ride entry for summarize_rides."""
    return {"id": ride_id, "date": "2026-09-01", "group": group, "summary": ride_summary(_scaled_ride(scale, samples=samples))}


def test_summarize_rides_between_ride_spread_groups_and_exclusions() -> None:
    """Between-ride statistics per group, rides far from the median, short rides excluded, no factor."""
    rides = [
        _ride("r1", "Ultimate", 0.95), _ride("r2", "Ultimate", 0.96), _ride("r3", "Ultimate", 0.94),
        _ride("r4", "Ultimate", 0.80),  # far from the others
        _ride("g1", "Grail", 1.02),
        _ride("short", "Ultimate", 0.95, samples=300),
    ]
    summary = summarize_rides(rides)
    assert summary["excluded"] == [{"id": "short", "reason": "only 300 usable pairs (minimum 600)"}]
    ultimate, grail = summary["groups"]
    assert ultimate["group"] == "Ultimate" and ultimate["rides"] == 4 and not ultimate["small_sample"]
    assert ultimate["overall_diff_pct"]["n"] == 4
    assert ultimate["overall_diff_pct"]["median"] == pytest.approx(-5.5, abs=0.1)
    assert ultimate["overall_diff_pct"]["sd"] > 5  # the 0.80 ride widens the between-ride spread
    assert [r["id"] for r in ultimate["far_from_median"]] == ["r4"]
    assert ultimate["lag_s"] == {"counts": {"0": 4}, "median": 0}
    assert {b["rides"] for b in ultimate["bins"]} == {4} and not any(b["small_sample"] for b in ultimate["bins"])
    assert all(b["small_sample"] for b in grail["bins"])
    assert grail["small_sample"] and grail["overall_diff_pct"]["sd"] is None and not grail["far_from_median"]
    assert any("No correction or calibration factor" in note for note in summary["notes"])
    assert any("not pooled" in note for note in summary["notes"])
    text = "\n".join(format_rides_summary(summary))
    assert "Group Ultimate: 4 rides, 4800 usable pairs" in text
    assert "Group Grail: 1 ride, 1200 usable pairs - small sample, not reliable" in text
    assert "Ride r4 lies -14.50 pp from the group median" in text
    assert "Excluded short: only 300 usable pairs" in text
    json.dumps(comparison_to_json(summary), allow_nan=False)
    assert not summarize_rides([])["groups"]
