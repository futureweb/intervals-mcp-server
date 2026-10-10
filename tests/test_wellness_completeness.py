"""
Completeness of today's wellness record: the helper (usual fields, stored 0 placeholders, rare
fields, missing record, local update time around midnight) and its use in get_wellness_data,
get_recovery_snapshot and get_coach_context (one line, JSON today_completeness, request counts,
recovery statistics without today's missing values). Every API call goes through the real
client to a mock transport that honours oldest/newest/fields; "now" is fixed.
"""

import asyncio
import json
import os
import pathlib
import statistics
import sys
from datetime import date, datetime

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.api import client as api_client  # pylint: disable=wrong-import-position
from intervals_mcp_server.server import (  # pylint: disable=wrong-import-position
    get_coach_context,
    get_recovery_snapshot,
    get_wellness_data,
)
from intervals_mcp_server.tools import custom_items as custom_items_module  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils import dates as dates_module  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.custom_fields import INPUT_FIELD, index_custom_items  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.wellness_completeness import (  # pylint: disable=wrong-import-position
    completeness_line,
    has_value,
    today_completeness,
)
from tests.sample_data import (  # pylint: disable=wrong-import-position
    COMPLETENESS_ITEMS,
    COMPLETENESS_TODAY_RECORD,
    completeness_day,
    completeness_wellness,
)

TODAY = date(2026, 10, 10)
DEFS = index_custom_items(COMPLETENESS_ITEMS)[INPUT_FIELD]
MORNING_LINE = (
    "Today 2026-10-10 is incomplete (last updated 09:46 local): night/morning values not yet available: sleeping HR, "
    "respiration, SpO2, Garmin Deep Sleep, Garmin Skin Temperature Deviation, Garmin Sleep Stress Avg, Garmin Morning "
    "Training Readiness. Treat them as missing, not as normal; they usually arrive later in the day.\n"
    "Day totals not yet available (normally complete in the evening): Garmin Total Calories, floors climbed."
)


def _now(monkeypatch, utc="2026-10-10T08:05:00+00:00", zone="Europe/Vienna"):
    """Fix the clock of utils.dates and use the athlete time zone *zone*."""
    moment = datetime.fromisoformat(utc)

    class Frozen(datetime):
        """datetime whose now() is the fixed moment."""

        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz is not None else moment.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(dates_module, "datetime", Frozen)
    monkeypatch.setenv("ATHLETE_TIMEZONE", zone)


def _api(monkeypatch, wellness, fail=None):
    """Answer the API like Intervals.icu (oldest/newest/fields honoured); returns the sent requests."""
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        path, params = request.url.path, dict(request.url.params)
        if fail is not None and fail(path, params):
            return httpx.Response(400, text="boom")
        if path.endswith("/wellness"):
            rows = [dict(e) for e in wellness if params["oldest"] <= e["id"] <= params["newest"]]
            if "fields" in params:
                keep = set(params["fields"].split(","))
                rows = [{k: v for k, v in row.items() if k in keep} for row in rows]
            return httpx.Response(200, json=rows)
        if path.endswith("/custom-item"):
            return httpx.Response(200, json=COMPLETENESS_ITEMS)
        if path.endswith(("/activities", "/events")):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))

    async def get_client():
        return client

    monkeypatch.setattr(api_client, "_get_httpx_client", get_client)
    custom_items_module._CUSTOM_ITEMS_CACHE.clear()  # pylint: disable=protected-access
    return sent


def _wellness_requests(sent):
    return [dict(r.url.params) for r in sent if r.url.path.endswith("/wellness")]


def _today(**changes):
    return {**COMPLETENESS_TODAY_RECORD, **changes}


def _codes(info):
    return [item["field"] for item in info["missing_usual_fields"]]


# ------------------------------------------------------------------ helper
def test_incomplete_morning_record(monkeypatch):
    """Usual fields missing this morning are listed, night/morning values first, custom fields by name."""
    _now(monkeypatch)
    info = today_completeness(completeness_wellness(), TODAY, DEFS)
    assert info["exists"] is True and info["date"] == "2026-10-10"
    assert info["updated"] == "2026-10-10T07:46:01.604+00:00" and info["updated_local"] == "2026-10-10T09:46+02:00"
    assert _codes(info) == [
        "avgSleepingHR", "respiration", "spO2", "GarminSleepDeepMinutes", "GarminSkinTempDeviationC",
        "GarminSleepStressAvg", "GarminTrainingReadiness", "GarminTotalCalories", "floorsClimbed",
    ]
    assert info["missing_usual_fields"][3] == {"field": "GarminSleepDeepMinutes", "name": "Garmin Deep Sleep", "group": "night_morning"}
    assert [item["group"] for item in info["missing_usual_fields"][-2:]] == ["day_total", "day_total"]
    assert "not as normal or 0" in info["note"] and info["reference_days"] == 14
    assert completeness_line(info) == MORNING_LINE
    assert completeness_line(info, 3, " (see JSON)") == (
        "Today 2026-10-10 is incomplete (last updated 09:46 local): night/morning values not yet available: sleeping HR, "
        "respiration, SpO2 and 4 more (see JSON). Treat them as missing, not as normal; they usually arrive later in the day.\n"
        "Day totals not yet available (normally complete in the evening): Garmin Total Calories, floors climbed."
    )
    # Only day totals missing (evening case): one line.
    evening = today_completeness(completeness_wellness(dict(completeness_day("2026-10-10", 30), steps=None)), TODAY, DEFS)
    assert completeness_line(evening) == (
        "Today 2026-10-10 is incomplete (last updated 22:21 local): day totals not yet available (normally complete in "
        "the evening): steps. Treat them as missing, not as normal; they usually arrive later in the day."
    )
    # Values of the night come before other night/morning values (scores, predictions), whatever the definition order.
    entries = completeness_wellness()
    for entry in entries[:-1]:
        entry["GarminEnduranceScore"] = 7000
    defs = {"GarminEnduranceScore": {"name": "Garmin Endurance Score"}, **DEFS}
    assert _codes(today_completeness(entries, TODAY, defs))[3:8] == [
        "GarminSleepDeepMinutes", "GarminSkinTempDeviationC", "GarminSleepStressAvg", "GarminTrainingReadiness",
        "GarminEnduranceScore",
    ]
    # Without definitions the custom fields keep their codes.
    assert _codes(today_completeness(completeness_wellness(), TODAY)) == _codes(info)
    assert today_completeness(completeness_wellness(), TODAY)["missing_usual_fields"][3]["name"] == "GarminSleepDeepMinutes"


def test_complete_record_has_no_line(monkeypatch):
    """A record with every usual field gets no line."""
    _now(monkeypatch)
    info = today_completeness(completeness_wellness(completeness_day("2026-10-10", 30)), TODAY, DEFS)
    assert info["missing_usual_fields"] == [] and info["note"] is None and info["usual_fields"] > 10
    assert completeness_line(info) is None


def test_missing_record_lists_every_usual_field(monkeypatch):
    """Without a record for today every usual field is not yet available."""
    _now(monkeypatch)
    info = today_completeness(completeness_wellness(None), TODAY, DEFS)
    assert info["exists"] is False and info["updated"] is None and info["updated_local"] is None
    assert _codes(info) == [
        "restingHR", "hrv", "avgSleepingHR", "sleepSecs", "sleepScore", "respiration", "spO2",
        "GarminSleepDeepMinutes", "GarminSkinTempDeviationC", "GarminSleepStressAvg", "GarminTrainingReadiness",
        "BodyBatteryMax", "GarminTotalCalories", "steps", "floorsClimbed",
    ]
    line = completeness_line(info)
    assert line is not None
    assert line.startswith(
        "Today 2026-10-10 has no wellness record yet: night/morning values not yet available: resting HR, HRV, sleeping HR"
    )


def test_stored_zero_is_a_placeholder(monkeypatch):
    """A stored 0 today is missing; a field stored as 0 on the previous days is not a usual field."""
    _now(monkeypatch)
    info = today_completeness(completeness_wellness(_today(avgSleepingHR=0, GarminSleepStressAvg=0.0, hrv=0)), TODAY, DEFS)
    assert {"avgSleepingHR", "GarminSleepStressAvg", "hrv"} <= set(_codes(info))
    assert "kcalConsumed" not in _codes(info)  # 0 on every previous day: never usual
    assert not has_value(0) and not has_value(0.0) and not has_value(float("nan")) and not has_value("NaN")
    assert not has_value(" ") and not has_value([]) and has_value(-0.1) and has_value("Planned") and has_value(False)
    assert has_value(0, zero_is_value=True) and not has_value(None, zero_is_value=True)


def test_zero_is_a_value_for_signed_fields(monkeypatch):
    """Signed fields (a negative value in the 14 days, or deviation/delta/change) count a stored 0 as a value."""
    _now(monkeypatch)
    entries = completeness_wellness()
    for offset, entry in enumerate(reversed(entries[:-1]), start=1):  # offset 1 = yesterday
        zero = 10 < offset <= 14  # 4 of the 14 days
        if zero:
            entry["GarminSkinTempDeviationC"] = 0.0  # the live case: 0.0 °C on 4 days, negative on others
        entry["GarminSleepAwakeMinutes"] = 0 if zero else 5.0  # unsigned: the zeros are placeholders
        entry["WeightDelta"] = 0 if zero else 0.3  # signed by its code
        entry["ReadinessTrend"] = 0 if zero else 2.0  # signed by its description
    defs = {**DEFS, "ReadinessTrend": {"name": "Readiness Trend", "description": "Change against the day before"}}
    codes = _codes(today_completeness(entries, TODAY, defs))
    assert {"GarminSkinTempDeviationC", "WeightDelta", "ReadinessTrend"} <= set(codes)
    assert "GarminSleepAwakeMinutes" not in codes  # 10 of 14 days with a value
    assert "ReadinessTrend" not in _codes(today_completeness(entries, TODAY, DEFS))  # no description: unsigned
    # A 0.0 deviation today is a value, not a missing field.
    entries[-1] = _today(GarminSkinTempDeviationC=0.0, WeightDelta=0)
    codes = _codes(today_completeness(entries, TODAY, defs))
    assert "GarminSkinTempDeviationC" not in codes and "WeightDelta" not in codes


def test_rarely_present_fields_are_not_listed(monkeypatch):
    """Usual = a value on at least 80 % of the 14 days before today (12 of 14); other days do not count."""
    _now(monkeypatch)
    entries = completeness_wellness()
    for offset, entry in enumerate(reversed(entries[:-1]), start=1):  # offset 1 = yesterday
        entry["Eleven"] = 5 if offset <= 11 else None
        entry["Twelve"] = 5 if offset <= 12 else None
        entry["OldOnly"] = 5 if offset > 14 else None  # only outside the 14 days
    codes = _codes(today_completeness(entries, TODAY, DEFS))
    assert "Twelve" in codes and "Eleven" not in codes and "OldOnly" not in codes
    assert "GarminVO2MaxCycling" not in codes  # every fifth day
    for not_checked in ("ctl", "atl", "rampRate", "ctlLoad", "atlLoad", "sportInfo", "updated", "locked", "tempWeight"):
        assert not_checked not in codes


def test_local_update_time_around_midnight(monkeypatch):
    """At 00:30 in Vienna (22:30 UTC) today is the new local day; times are shown in local time."""
    _now(monkeypatch, utc="2026-10-09T22:30:00+00:00")
    assert dates_module.athlete_today() == TODAY
    early = today_completeness(completeness_wellness(_today(updated="2026-10-09T22:21:00Z")), TODAY, DEFS)
    assert early["updated_local"] == "2026-10-10T00:21+02:00"
    assert "(last updated 00:21 local)" in (completeness_line(early) or "")
    before_midnight = today_completeness(completeness_wellness(_today(updated="2026-10-09T21:50:00+00:00")), TODAY, DEFS)
    assert "(last updated 2026-10-09 23:50 local)" in (completeness_line(before_midnight) or "")
    # A tool's default "today" is the local day: get_wellness_data checks 2026-10-10, not the UTC date.
    sent = _api(monkeypatch, completeness_wellness(None))
    text = asyncio.run(get_wellness_data(start_date="2026-10-10"))
    assert _wellness_requests(sent) == [{"oldest": "2026-09-26", "newest": "2026-10-10"}]
    assert text.startswith("No wellness data found for athlete i1 in the specified date range.\n\n"
                           "Today 2026-10-10 has no wellness record yet: night/morning values not yet available: resting HR")


# ------------------------------------------------------------------ get_wellness_data
def test_get_wellness_data_today_line_one_header_and_one_request(monkeypatch):
    """The line follows the single heading; the one wellness request starts 14 days before today."""
    _now(monkeypatch)
    sent = _api(monkeypatch, completeness_wellness())
    text = asyncio.run(get_wellness_data(start_date="2026-10-09", end_date="2026-10-10"))
    assert text.startswith("Wellness Data:\n\n" + MORNING_LINE + "\n\nDate: 2026-10-09\n")
    assert text.count("Wellness Data:") == 1
    assert [line for line in text.splitlines() if line.startswith("Date:")] == ["Date: 2026-10-09", "Date: 2026-10-10"]
    # One wellness request widened to the 14 days before today, plus the (cached) custom item definitions.
    assert _wellness_requests(sent) == [{"oldest": "2026-09-26", "newest": "2026-10-10"}]
    assert [r.url.path.rsplit("/", 1)[-1] for r in sent] == ["wellness", "custom-item"]
    sent.clear()
    asyncio.run(get_wellness_data(start_date="2026-10-09", end_date="2026-10-10", include_all_fields=True))
    assert len(sent) == 1  # definitions cached


def test_get_wellness_data_without_today_or_complete(monkeypatch):
    """No line and no wider request without today; no line for a complete record."""
    _now(monkeypatch)
    sent = _api(monkeypatch, completeness_wellness())
    past = asyncio.run(get_wellness_data(start_date="2026-10-01", end_date="2026-10-05"))
    assert "Today" not in past and _wellness_requests(sent) == [{"oldest": "2026-10-01", "newest": "2026-10-05"}]
    assert len(sent) == 1  # no definitions needed
    sent.clear()
    default = asyncio.run(get_wellness_data())  # 30 days already cover the 14 days before today
    assert _wellness_requests(sent) == [{"oldest": "2026-09-10", "newest": "2026-10-10"}]
    assert MORNING_LINE in default and default.count("Date: ") == 31
    sent = _api(monkeypatch, completeness_wellness(completeness_day("2026-10-10", 30)))
    complete = asyncio.run(get_wellness_data(start_date="2026-10-10"))
    assert "Today" not in complete and complete.startswith("Wellness Data:\n\nDate: 2026-10-10\n")
    assert len(sent) == 1


# ------------------------------------------------------------------ get_recovery_snapshot
def test_recovery_snapshot_today_line_json_and_requests(monkeypatch):
    """Text line, JSON today_completeness, only the requested days shown, no extra request."""
    _now(monkeypatch)
    sent = _api(monkeypatch, completeness_wellness())
    text = asyncio.run(get_recovery_snapshot())
    lines = text.splitlines()
    assert lines[2:4] == MORNING_LINE.splitlines() and lines[4] == ""
    assert "2026-10-10 (today, preliminary) | updated 2026-10-10T07:46:01.604+00:00" in text
    assert [line.split(" ")[0] for line in lines if line[:4] == "2026" and " | updated" in line] == [
        "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10"]
    # Same number of requests as before: the first wellness request starts 14 days before today.
    assert _wellness_requests(sent)[0] == {"oldest": "2026-09-26", "newest": "2026-10-10"}
    assert len(sent) == 5 and len(_wellness_requests(sent)) == 2
    payload = json.loads(asyncio.run(get_recovery_snapshot(output_format="json")))
    completeness = payload["today_completeness"]
    assert completeness["updated_local"] == "2026-10-10T09:46+02:00" and completeness["exists"] is True
    assert completeness["missing_usual_fields"][0] == {"field": "avgSleepingHR", "name": "sleeping HR", "group": "night_morning"}
    assert [day["date"] for day in payload["days"]] == ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10"]
    assert [day["preliminary"] for day in payload["days"]] == [False, False, False, True]
    past = json.loads(asyncio.run(get_recovery_snapshot(date_str="2026-10-09", output_format="json")))
    assert past["today_completeness"] is None and not any(day["preliminary"] for day in past["days"])
    assert _wellness_requests(sent)[-2] == {"oldest": "2026-10-06", "newest": "2026-10-09"}


def test_recovery_snapshot_labels_only_the_athletes_today(monkeypatch):
    """A past date_str is not "(today, preliminary)"; the athlete's today is, also around midnight."""
    _now(monkeypatch, utc="2026-10-09T22:30:00+00:00")  # 00:30 on 2026-10-10 in Vienna
    _api(monkeypatch, completeness_wellness())
    past = asyncio.run(get_recovery_snapshot(date_str="2026-10-09", days_back=1))
    assert "(today, preliminary)" not in past and "\n2026-10-09 | updated 2026-10-09T20:21:13.526+00:00 | locked no" in past
    assert "Today 2026-10-10" not in past
    today = asyncio.run(get_recovery_snapshot(days_back=1))
    assert "\n2026-10-10 (today, preliminary) | updated" in today and "\n2026-10-09 | updated" in today


def test_recovery_snapshot_caps_names_by_detail_level(monkeypatch):
    """compact lists 8 names per group, full all of them."""
    _now(monkeypatch)
    _api(monkeypatch, completeness_wellness(None))  # 15 usual fields missing
    compact = asyncio.run(get_recovery_snapshot(detail_level="compact"))
    assert (
        "Today 2026-10-10 has no wellness record yet: night/morning values not yet available: resting HR, HRV, sleeping HR, "
        "sleep duration, sleep score, respiration, SpO2, Garmin Deep Sleep and 4 more (all with detail_level=full). Treat them"
    ) in compact
    assert "\nDay totals not yet available (normally complete in the evening): Garmin Total Calories, steps, floors climbed.\n" in compact
    full = asyncio.run(get_recovery_snapshot(detail_level="full"))
    assert "Garmin Morning Training Readiness, Body Battery Max. Treat them as missing" in full


def test_recovery_baselines_leave_out_todays_missing_values(monkeypatch):
    """Today's missing (or 0) values are not in the baselines: latest stays yesterday's, n and mean unchanged."""
    _now(monkeypatch)
    _api(monkeypatch, completeness_wellness(_today(hrv=0)))
    payload = json.loads(asyncio.run(get_recovery_snapshot(output_format="json")))
    baselines = {b["metric"]: b for b in payload["baselines"]}
    for metric in ("avgSleepingHR", "hrv", "respiration", "spO2"):
        assert baselines[metric]["latest"]["date"] == "2026-10-09", metric
        assert baselines[metric]["days_with_value"] == 30, metric
    assert baselines["avgSleepingHR"]["baseline"]["mean"] == 52.0 and baselines["spO2"]["baseline"]["mean"] == 95.0
    assert baselines["restingHR"]["latest"] == {"date": "2026-10-10", "value": 49.0}
    text = asyncio.run(get_recovery_snapshot())
    assert "avgSleepingHR: latest 52 (2026-10-09)" in text and "hrv: latest 44 (2026-10-09)" in text


# ------------------------------------------------------------------ get_coach_context
def test_coach_context_today_line_and_one_extra_request(monkeypatch):
    """The line follows the recovery markers; one extra request (last 14 days, all fields) for today only."""
    _now(monkeypatch)
    sent = _api(monkeypatch, completeness_wellness())
    text = asyncio.run(get_coach_context())
    line = next(line for line in text.splitlines() if line.startswith("Today "))
    # No definitions cached: custom fields keep their codes (the context makes no request for names).
    assert line == (
        "Today 2026-10-10 is incomplete (last updated 09:46 local): night/morning values not yet available: sleeping HR, "
        "respiration, SpO2, GarminSleepDeepMinutes, GarminSkinTempDeviationC, GarminSleepStressAvg and 1 more (see "
        "get_recovery_snapshot). Treat them as missing, not as normal; they usually arrive later in the day."
    )
    rows = text.splitlines()
    assert rows[rows.index(line) - 1].startswith("Recovery markers")
    assert rows[rows.index(line) + 1] == (
        "Day totals not yet available (normally complete in the evening): GarminTotalCalories, floors climbed."
    )
    assert rows[rows.index(line) + 2].startswith("Durability 28 d")
    assert [r.url.path.rsplit("/", 1)[-1] for r in sent] == ["activities", "wellness", "events", "wellness"]
    assert _wellness_requests(sent)[1] == {"oldest": "2026-09-26", "newest": "2026-10-10"}  # all fields
    assert "fields" in _wellness_requests(sent)[0]
    asyncio.run(get_recovery_snapshot())  # caches the definitions
    sent.clear()
    payload = json.loads(asyncio.run(get_coach_context(output_format="json")))
    assert payload["today_completeness"]["missing_usual_fields"][3] == {
        "field": "GarminSleepDeepMinutes", "name": "Garmin Deep Sleep", "group": "night_morning"}
    assert len(sent) == 4
    sent.clear()
    past = json.loads(asyncio.run(get_coach_context(end_date="2026-10-09", output_format="json")))
    assert past["today_completeness"] is None and len(sent) == 2


def test_coach_context_recovery_leaves_out_todays_missing_values(monkeypatch):
    """The 7-day means use the six days with values when today's HRV is missing or a stored 0."""
    _now(monkeypatch)
    six = [40.0 + i % 5 for i in range(24, 30)]  # HRV of 2026-10-04 .. 2026-10-09
    for today in (_today(hrv=None), _today(hrv=0)):
        _api(monkeypatch, completeness_wellness(today))
        hrv = json.loads(asyncio.run(get_coach_context(output_format="json")))["recovery"]["hrv"]
        assert hrv["n_7d"] == 6 and hrv["mean_7d"] == round(statistics.fmean(six), 2)
        assert hrv["latest"]["date"] == "2026-10-09"
    _api(monkeypatch, completeness_wellness())
    hrv = json.loads(asyncio.run(get_coach_context(output_format="json")))["recovery"]["hrv"]
    assert hrv["n_7d"] == 7 and hrv["mean_7d"] == round(statistics.fmean(six + [38.0]), 2)


def test_coach_context_check_respects_errors_and_the_request_budget(monkeypatch):
    """A failed or refused completeness request is reported; the rest of the context stays."""
    _now(monkeypatch)
    _api(monkeypatch, completeness_wellness(), fail=lambda path, params: path.endswith("/wellness") and "fields" not in params)
    text = asyncio.run(get_coach_context())
    assert "Today 2026-10-10: completeness not checked (Error fetching wellness data: " in text
    assert "Recovery markers" in text and "Durability 28 d" in text
    monkeypatch.setenv("MCP_TOOL_MAX_REQUESTS", "3")
    sent = _api(monkeypatch, completeness_wellness())
    limited = asyncio.run(get_coach_context())
    assert len(sent) == 3
    assert "Today 2026-10-10: completeness not checked (Error fetching wellness data: Not sent: this tool call reached its limit of 3" in limited
    assert "Note: this tool call reached its limit of 3 Intervals.icu API requests" in limited
