"""
Unit and tool tests for the custom field aggregation policy (utils.field_policy) used by
get_training_summary: percentages, ms / cm running dynamics, scores and estimates are never
summed even when the definition says SUM; energy, volume, time and device loads are.
"""

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.server import get_training_summary  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.custom_fields import index_custom_items  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.field_policy import (  # pylint: disable=wrong-import-position
    aggregate_custom_fields,
    aggregation_policy,
    format_aggregate,
    pair_changes,
    start_end_pairs,
)
from tests.test_coaching_tools import _install_router  # pylint: disable=wrong-import-position


def _item(item_id, name, code, units=None, aggregate=None, value_type="numeric"):
    return {"id": item_id, "type": "ACTIVITY_FIELD", "name": name,
            "content": {"code": code, "type": value_type, "units": units, "aggregate": aggregate}}


ITEMS = [
    _item(1, "Stamina at start", "Staminaatstart", "%", "SUM"),
    _item(2, "Stamina at end", "Staminaatend", "%", "SUM"),
    _item(3, "GCT", "GCT", "ms", "SUM"),
    _item(4, "Vertical Oscillation", "VerticalOscillation", "cm", "SUM"),
    _item(5, "Sweat loss", "Sweatloss", "ml", "SUM"),
    _item(6, "Active Calories", "ActiveCalories", "kcal", "SUM"),
    _item(7, "Training Load", "TrainingLoad", None, "SUM"),
    _item(8, "EPOC", "EPOC", "ml/kg", "SUM"),
    _item(9, "VO2 Max", "VO2MaxGarmin", None, "MAX"),
    _item(10, "Recovery Time", "RecoveryTime", "h", "MAX"),
    _item(11, "Min. Temperature", "Mintemperature", "°C", "MIN"),
    _item(12, "Avg Power", "AvgPower", "W", "SUM"),
    _item(13, "Elapsed Time", "ElapsedTime", "hr", "SUM"),
    _item(14, "Body Weight", "BodyWeight", "kg", "SUM"),
    _item(15, "Mystery", "Mystery", None, "SUM"),
    _item(16, "Training Effect", "TrainingEffectSelect", None, "SUM", value_type="select"),
    _item(17, "GCT Balance Percent", "GCTBalance", "%", "SUM"),
    _item(18, "Aerobic Effect", "AerobicEffect", None, "MAX"),
]
DEFS = index_custom_items(ITEMS)["ACTIVITY_FIELD"]
ACTIVITIES = [
    {"start_date_local": "2026-10-01T08:00:00", "Staminaatstart": 100, "Staminaatend": 70, "GCT": 310.0, "VerticalOscillation": 8.5,
     "Sweatloss": 800, "ActiveCalories": 900, "TrainingLoad": 120.5, "EPOC": 120.5, "VO2MaxGarmin": 50.8, "RecoveryTime": 30.0,
     "Mintemperature": 12, "AvgPower": 210, "ElapsedTime": 1.5, "BodyWeight": 83.5, "Mystery": 3, "GCTBalance": 49.8,
     "AerobicEffect": 3.5},
    {"start_date_local": "2026-10-03T08:00:00", "Staminaatstart": 95, "Staminaatend": 60, "GCT": 320.0, "VerticalOscillation": 8.9,
     "Sweatloss": 650, "ActiveCalories": 700, "TrainingLoad": 90.0, "EPOC": 90.0, "VO2MaxGarmin": 51.3, "RecoveryTime": 20.0,
     "Mintemperature": 8, "AvgPower": 190, "ElapsedTime": 2.0, "BodyWeight": 83.1, "Mystery": float("nan"), "GCTBalance": 50.4,
     "AerobicEffect": 2.9},
    {"start_date_local": "2026-10-05T08:00:00", "Staminaatstart": 98, "Staminaatend": None, "GCT": "NaN", "Sweatloss": 0,
     "ActiveCalories": None, "Mintemperature": 15, "AerobicEffect": 4.1},
]


def test_policy_by_units_and_meaning():
    """Percentages, ms, cm, W, kg and scores are never summed; energy, volume, time and loads are."""
    policy = {code: aggregation_policy(definition)["policy"] for code, definition in DEFS.items()}
    assert policy["Staminaatstart"] == "mean" and policy["GCTBalance"] == "mean"
    assert policy["GCT"] == "mean" and policy["VerticalOscillation"] == "mean"
    assert policy["AvgPower"] == "mean" and policy["BodyWeight"] == "mean"
    assert policy["Sweatloss"] == "sum" and policy["ActiveCalories"] == "sum" and policy["ElapsedTime"] == "sum"
    assert policy["TrainingLoad"] == "device_load_sum" and policy["EPOC"] == "device_load_sum"
    assert policy["VO2MaxGarmin"] == "trend" and policy["RecoveryTime"] == "trend"
    assert policy["Mintemperature"] == "mean" and aggregation_policy(DEFS["Mintemperature"])["primary"] == "min"
    assert policy["AerobicEffect"] == "mean" and aggregation_policy(DEFS["AerobicEffect"])["primary"] == "mean"
    assert policy["Mystery"] == "none" and policy["TrainingEffectSelect"] == "none"
    assert "SUM is not applied" in aggregation_policy(DEFS["Staminaatstart"])["reason"]
    assert aggregation_policy(DEFS["Mystery"], override="sum")["policy"] == "sum"
    assert aggregation_policy(DEFS["GCT"], override="bogus")["policy"] == "mean"  # unknown override ignored


def test_aggregate_values_and_missing():
    """Sums skip missing values and keep a stored 0; means report median/min/max; trends latest and change."""
    aggs = aggregate_custom_fields(ACTIVITIES, DEFS)
    assert aggs["Sweatloss"]["sum"] == 1450 and aggs["Sweatloss"]["n"] == 3  # the stored 0 counts as a value
    assert aggs["ActiveCalories"]["sum"] == 1600 and aggs["ActiveCalories"]["n"] == 2  # None skipped
    assert "sum" not in aggs["Staminaatstart"] and aggs["Staminaatstart"]["mean"] == 97.67
    assert aggs["Staminaatend"]["min"] == 60 and aggs["Staminaatend"]["median"] == 65
    assert aggs["GCT"]["n"] == 2 and aggs["GCT"]["mean"] == 315  # "NaN" skipped, never summed to 630
    assert "sum" not in aggs["VerticalOscillation"]
    assert aggs["VO2MaxGarmin"]["latest"] == 51.3 and aggs["VO2MaxGarmin"]["change"] == 0.5
    assert aggs["Mintemperature"]["min"] == 8
    assert aggs["Mystery"]["policy"] == "none" and "sum" not in aggs["Mystery"] and "mean" not in aggs["Mystery"]
    assert "TrainingEffectSelect" not in aggs
    text = format_aggregate("Staminaatstart", aggs["Staminaatstart"])
    assert text.startswith("Stamina at start [Staminaatstart] mean 97.67 %") and "sum" not in text
    assert "device load sum 210.5" in format_aggregate("TrainingLoad", aggs["TrainingLoad"])
    assert format_aggregate("Mystery", aggs["Mystery"]) == "Mystery [Mystery]: no aggregate (n 1)"
    assert not aggregate_custom_fields([], DEFS)


def test_start_end_pairs():
    """'... at start' / '... at end' fields are paired and their typical change reported."""
    assert start_end_pairs(DEFS) == [("Staminaatstart", "Staminaatend", "stamina")]
    pairs = pair_changes(ACTIVITIES, DEFS)
    assert pairs == [{"stem": "stamina", "start_code": "Staminaatstart", "end_code": "Staminaatend", "n": 2, "units": "%",
                      "median_change": -32.5, "mean_change": -32.5, "largest_drop": -35.0}]


def test_training_summary_never_sums_percentages(monkeypatch):
    """Regression: 'Stamina at start: sum 1454 %', 'GCT sum 630 ms' no longer appear in the summary."""
    activities = [dict(a, id=f"i{n}", type="Run", name="run", moving_time=3600, elapsed_time=3700) for n, a in enumerate(ACTIVITIES)]
    _install_router(monkeypatch, {"/custom-item": ITEMS, "/activities": activities})
    text = asyncio.run(get_training_summary("2026-10-01", "2026-10-09", group_by="total"))
    assert "Stamina at start [Staminaatstart] mean 97.67 %" in text
    assert "GCT [GCT] mean 315 ms" in text
    assert "Sweat loss [Sweatloss] sum 1450 ml (n 3, 483.33 ml/session)" in text
    assert "Training Load [TrainingLoad] device load sum 210.5 (n 2, device scale)" in text
    assert "VO2 Max [VO2MaxGarmin] latest 51.3 (2026-10-03), change +0.5 since 2026-10-01" in text
    assert "sum 295.67" not in text and " % (n" not in text.split("Stamina at start")[1][:20]
    assert "Custom fields without a meaningful aggregate (not summed): Mystery" in text
    assert "stamina start→end: median change -32.5 %" in text
    full = asyncio.run(get_training_summary("2026-10-01", "2026-10-09", group_by="total", detail_level="full"))
    assert "SUM is not applied" in full
    compact = asyncio.run(get_training_summary("2026-10-01", "2026-10-09", group_by="total", detail_level="compact"))
    assert "Device loads (separate scale): " in compact and "Stamina" not in compact
    payload = json.loads(asyncio.run(get_training_summary("2026-10-01", "2026-10-09", group_by="total", output_format="json")))
    assert payload["overall"]["custom_fields"]["GCT"]["policy"] == "mean"
    assert "sum" not in payload["overall"]["custom_fields"]["GCT"]
    assert asyncio.run(get_training_summary("2026-10-01", detail_level="x")).startswith("Error: detail_level")


def test_zeros_and_sport_foreign_values():
    """A stored 0 of an estimate is 'no value'; running dynamics on rides are ignored when the sport lacks the field."""
    activities = [
        {"start_date_local": "2026-10-01", "type": "Run", "GCT": 300.0, "VO2MaxGarmin": 50.0, "AerobicEffect": 0.0},
        {"start_date_local": "2026-10-02", "type": "Ride", "GCT": 0.0, "VO2MaxGarmin": 0.0, "AerobicEffect": 2.0},
        {"start_date_local": "2026-10-03", "type": "Run", "GCT": 310.0, "VO2MaxGarmin": 51.0, "AerobicEffect": 3.0},
    ]
    assigned = {"Run": {"GCT", "VO2MaxGarmin", "AerobicEffect"}, "Ride": {"VO2MaxGarmin", "AerobicEffect"}}
    aggs = aggregate_custom_fields(activities, DEFS, assigned_by_type=assigned)
    assert aggs["GCT"]["mean"] == 305 and aggs["GCT"]["other_sport_values_ignored"] == 1
    assert aggs["VO2MaxGarmin"]["min"] == 50 and aggs["VO2MaxGarmin"]["zeros_excluded"] == 1
    assert aggs["AerobicEffect"]["min"] == 0 and aggs["AerobicEffect"]["zero_values"] == 1  # a real 0 effect is kept
    text = format_aggregate("GCT", aggs["GCT"])
    assert "1 value(s) from sports without this field ignored" in text
    assert "1 stored 0 left out as 'no value'" in format_aggregate("VO2MaxGarmin", aggs["VO2MaxGarmin"])
    unknown = aggregate_custom_fields(activities, DEFS)  # without sport settings every value counts
    assert unknown["GCT"]["n"] == 3
