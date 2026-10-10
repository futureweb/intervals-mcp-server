"""Regression tests for the six bugs noted in PR #28 (fixed in stage 3B).

a. get_durability labelled the recent efficiency-factor mean "last 7 d" for any recent_days.
b. compare_workouts dropped a reference older than the newest `limit` activities and took the
   pattern from the newest activity.
c. compare_workouts with query, end_date and a reference but no start_date listed 90 days while the
   note said 365.
d. compare_best_efforts with activity_ids neither de-duplicated nor capped the ids before fetching.
e. The JSON of get_coach_context and get_training_summary ignored detail_level.
f. get_activities and get_wellness_data with a past end_date and no start_date started after the end.
"""

import asyncio
import json
import os
import pathlib
import sys
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

# pylint: disable=wrong-import-position,missing-function-docstring
from intervals_mcp_server.server import (  # noqa: E402
    compare_best_efforts,
    compare_workouts,
    get_activities,
    get_coach_context,
    get_durability,
    get_training_summary,
    get_wellness_data,
)
from intervals_mcp_server.utils.dates import athlete_today, parse_date_range  # noqa: E402
from tests.test_coaching_tools import _install_router as _coaching_router  # noqa: E402
from tests.test_load_tools import _setup as _load_setup  # noqa: E402
from tests.test_performance_tools import (  # noqa: E402
    THRESHOLD_ACTIVITIES,
    THRESHOLD_INTERVALS,
    _activity,
    _activity_id_of,
    _install_router as _performance_router,
    _iv,
    _threshold_routes,
)


# ------------------------------------------------------------------ a
def test_durability_efficiency_label_follows_recent_days(monkeypatch):
    _load_setup(monkeypatch)
    text = asyncio.run(get_durability(recent_days=14, detail_level="full"))
    ef_line = next(line for line in text.splitlines() if "efficiency factor mean" in line)
    assert "last 14 d" in ef_line and "last 7 d" not in ef_line
    payload = json.loads(asyncio.run(get_durability(recent_days=14, output_format="json")))
    assert payload["efficiency"]["cycling"]["recent_days"] == 14
    default = asyncio.run(get_durability(detail_level="full"))
    assert "last 7 d" in next(line for line in default.splitlines() if "efficiency factor mean" in line)


# ------------------------------------------------------------------ b
def test_compare_workouts_reference_older_than_the_newest_limit_is_the_pattern(monkeypatch):
    calls: list = []
    routes = _threshold_routes()
    routes["/activities"] = THRESHOLD_ACTIVITIES
    _performance_router(monkeypatch, overrides=routes, calls=calls)
    payload = json.loads(asyncio.run(compare_workouts(query="Threshold", reference_activity_id="t1", limit=2, output_format="json")))
    assert {row["id"] for row in payload["activities"]} == {"t1", "t5"}  # the newest other activity and the reference
    assert payload["reference"] == {"activity_id": "t1", "pattern": {"count": 3, "secs": 600, "pct_ftp": 104.3}, "explicit": True}
    assert ("/activity/t1", {}) in calls  # fetched by id
    text = asyncio.run(compare_workouts(activity_ids="t5,t4,t1", reference_activity_id="t1", limit=2))
    assert "Reference pattern: 3 x 10:00 @ 104% FTP from reference activity t1 (2026-08-01)" in text


def test_compare_workouts_reference_without_a_main_set_says_so(monkeypatch):
    routes = _threshold_routes()
    routes["/activities"] = THRESHOLD_ACTIVITIES
    intervals = dict(THRESHOLD_INTERVALS, t1=[_iv("RECOVERY", 3600, 150, 120)])
    routes["/intervals"] = lambda url, _p: {"icu_intervals": intervals.get(_activity_id_of(url), [])}
    _performance_router(monkeypatch, overrides=routes)
    text = asyncio.run(compare_workouts(query="Threshold", reference_activity_id="t1"))
    assert "Reference pattern: n/a (no main set of WORK intervals found) from reference activity t1" in text


# ------------------------------------------------------------------ c
def test_compare_workouts_reference_window_is_the_listed_window(monkeypatch):
    calls: list = []
    old = _activity("t0", "2025-12-01", "Ride", "b1", "Threshold 3x10 winter", icu_ftp=220)
    routes = _threshold_routes()
    routes["/activities"] = THRESHOLD_ACTIVITIES + [old]
    intervals = dict(THRESHOLD_INTERVALS, t0=[_iv("WORK", 600, 230, 155, 162, 90)] * 3)
    routes["/intervals"] = lambda url, _p: {"icu_intervals": intervals.get(_activity_id_of(url), [])}
    _performance_router(monkeypatch, overrides=routes, calls=calls)
    payload = json.loads(asyncio.run(compare_workouts(query="Threshold", end_date="2026-10-09", reference_activity_id="t5",
                                                      output_format="json")))
    listing = next(params for url, params in calls if url.endswith("/activities"))
    assert listing["oldest"] == "2025-10-09" and listing["newest"] == "2026-10-09"  # 365 days before the reference, not 90
    assert payload["window"] == {"start": "2025-10-09", "end": "2026-10-09", "default_lookback_days": 365}
    assert "t0" in {row["id"] for row in payload["activities"]}  # 312 days old: inside the window the note names
    assert "default window: 365 days before the reference activity (2025-10-09 onwards)" in payload["filters"]


# ------------------------------------------------------------------ d
def test_compare_best_efforts_ids_are_deduplicated_and_capped_before_any_request(monkeypatch):
    calls: list = []
    _performance_router(monkeypatch, calls=calls)
    text = asyncio.run(compare_best_efforts(activity_ids="a1, a1,a4,a3,a1", limit=2, durations="60"))
    fetched = [url for url, _ in calls if url.startswith("/activity/") and url.count("/") == 2]
    assert fetched == ["/activity/a1", "/activity/a4"]
    assert "1 id beyond the limit of 2 not fetched (a3); 2 duplicate ids ignored" in text
    assert not any("/activity/a3" in url for url, _ in calls)


# ------------------------------------------------------------------ e
def test_coach_context_json_follows_detail_level(monkeypatch):
    _load_setup(monkeypatch)
    compact = json.loads(asyncio.run(get_coach_context(output_format="json", detail_level="compact")))
    standard = json.loads(asyncio.run(get_coach_context(output_format="json")))
    full = json.loads(asyncio.run(get_coach_context(output_format="json", detail_level="full")))
    assert compact["detail_level"] == "compact" and standard["detail_level"] == "standard"
    for key in ("sports", "top_sessions", "plan", "coverage", "weeks", "references"):
        assert key not in compact, key
    assert "drift" not in compact["intensity"] and set(compact["intensity"]) == {"last_7", "last_28", "by_sport_28"}
    for key in ("load", "fitness", "recovery", "durability", "efficiency", "method", "today_completeness"):
        assert key in compact, key
    assert {"sports", "top_sessions", "plan", "coverage"} <= set(standard) and "weeks" not in standard
    assert "drift" in standard["intensity"] and "references" not in standard
    assert len(full["weeks"]) == 4 and full["references"]["monotony"].startswith("Foster 1998")
    assert len(json.dumps(compact)) < len(json.dumps(standard)) < len(json.dumps(full))


def test_training_summary_json_follows_detail_level(monkeypatch):
    _coaching_router(monkeypatch)
    compact = json.loads(asyncio.run(get_training_summary("2026-10-01", "2026-10-09", group_by="total", output_format="json",
                                                          detail_level="compact")))
    standard = json.loads(asyncio.run(get_training_summary("2026-10-01", "2026-10-09", group_by="total", output_format="json")))
    full = json.loads(asyncio.run(get_training_summary("2026-10-01", "2026-10-09", group_by="total", output_format="json",
                                                       detail_level="full")))
    assert compact["detail_level"] == "compact"
    overall = compact["overall"]
    assert "time_in_power_zones_s" not in overall and "custom_fields" not in overall and "by_gear" not in overall
    assert overall["training_load"] == 250 and set(overall["by_sport"]["Ride"]) == {"sessions", "moving_time_s"}
    assert "device_loads" in overall
    assert "time_in_power_zones_s" in standard["overall"] and "by_gear" in standard["overall"]
    assert all("reason" not in agg for agg in standard["overall"]["custom_fields"].values())
    assert full["overall"]["custom_fields"] and all("reason" in agg for agg in full["overall"]["custom_fields"].values())
    no_gear = json.loads(asyncio.run(get_training_summary("2026-10-01", "2026-10-09", group_by="total", output_format="json",
                                                          include_gear=False)))
    assert "by_gear" not in no_gear["overall"]


# ------------------------------------------------------------------ f
def test_default_start_is_relative_to_a_past_end_date():
    today = athlete_today()
    past = (today - timedelta(days=200)).isoformat()
    assert parse_date_range(None, past) == ((today - timedelta(days=230)).isoformat(), past)
    future = (today + timedelta(days=20)).isoformat()
    assert parse_date_range(None, future) == ((today - timedelta(days=30)).isoformat(), future)
    assert parse_date_range(None, None) == ((today - timedelta(days=30)).isoformat(), today.isoformat())
    assert parse_date_range("2026-01-01", past) == ("2026-01-01", past)
    assert parse_date_range(None, "06.01.2025")[0] == (today - timedelta(days=30)).isoformat()  # invalid: the tool reports it


def test_activities_and_wellness_with_a_past_end_date(monkeypatch):
    calls: list = []
    _coaching_router(monkeypatch, calls=calls)
    asyncio.run(get_activities(end_date="2025-03-31"))
    asyncio.run(get_wellness_data(end_date="2025-03-31"))
    ranges = [(params["oldest"], params["newest"]) for url, params, _ in calls if params and "oldest" in params]
    assert ("2025-03-01", "2025-03-31") in ranges
    assert all(oldest <= newest for oldest, newest in ranges)
    assert any(url.endswith("/wellness") for url, params, _ in calls if params and params.get("oldest") == "2025-03-01")
