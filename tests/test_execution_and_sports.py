"""
Unit tests for utils.execution (plan flattening, target resolution, alignment, metrics)
and utils.sports (durations, pace, zones, start times), plus the permission class
configuration and the API client retry.
"""

import asyncio
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server import server  # pylint: disable=wrong-import-position
from intervals_mcp_server.api import client as api_client  # pylint: disable=wrong-import-position
from intervals_mcp_server.config import parse_permissions, parse_units_overrides  # pylint: disable=wrong-import-position
from intervals_mcp_server.mcp_instance import disabled_tools, tool, tool_permissions  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.execution import (  # pylint: disable=wrong-import-position
    align,
    analyze,
    classify_step,
    flatten_steps,
    interval_metrics,
    plan_steps,
    resolve_target,
)
from intervals_mcp_server.utils.sports import (  # pylint: disable=wrong-import-position
    cadence_spm,
    cadence_text,
    format_local_start,
    format_pace,
    format_start_times,
    format_zone_table,
    hms,
    start_times,
    temperature_text,
    to_utc_iso,
    utc_offset,
    zone_ranges,
)
from tests.sample_data import EVENT_DATA, EXECUTION_INTERVALS, EXECUTION_STREAMS  # pylint: disable=wrong-import-position

CONTEXT = {
    "ftp": 234, "lthr": 165, "max_hr": 188, "threshold_pace": 3.2258,
    "power_zones": [55, 75, 90, 105, 120, 150, 999], "hr_zones": [133, 147, 153, 164, 169, 174, 188],
    "pace_zones": [77.5, 87.7, 94.3, 100.0, 103.4, 111.5, 999.0],
}


def test_flatten_steps_expands_repeats():
    """Repeat blocks are expanded in execution order with rep counters."""
    flat = flatten_steps(EVENT_DATA["workout_doc"]["steps"])
    assert [s.get("duration") for s in flat] == [600, 300, 120, 300, 120, 300]
    assert [s.get("rep") for s in flat] == [None, 1, 1, 2, 2, None]
    assert flat[3]["reps"] == 2


def test_resolve_target_units():
    """Targets in %FTP, watts, HR zones, %LTHR and %pace resolve to absolute values."""
    pct = resolve_target({"power": {"value": 80, "units": "%ftp"}}, CONTEXT)
    assert pct["kind"] == "power" and pct["units"] == "W" and pct["source"] == "resolved from athlete thresholds"
    assert pct["low"] == pytest.approx(187.2) and pct["high"] == pytest.approx(187.2)
    assert resolve_target({"power": {"start": 240, "end": 250, "units": "w"}}, CONTEXT)["high"] == 250
    hr_zone = resolve_target({"hr": {"value": 2, "units": "hr_zone"}}, CONTEXT)
    assert (hr_zone["low"], hr_zone["high"]) == (133.0, 147.0)
    assert resolve_target({"hr": {"value": 90, "units": "%lthr"}}, CONTEXT)["low"] == pytest.approx(148.5)
    pace = resolve_target({"pace": {"value": 100, "units": "%pace"}}, CONTEXT)
    assert pace["kind"] == "pace" and pace["low"] == pytest.approx(3.2258)
    resolved = resolve_target({"power": {"value": 80, "units": "%ftp"}, "_power": {"value": 187, "units": "w"}}, CONTEXT)
    assert resolved["source"] == "resolved by Intervals.icu" and resolved["low"] == 187
    assert resolve_target({"text": "note"}, CONTEXT) is None
    assert resolve_target({"power": {"value": 5, "units": "%mmp"}}, {}) is None


def test_classify_step():
    """Warm-up/cool-down flags, explicit intensity, low targets and text classify steps."""
    assert classify_step({"warmup": True}, None, CONTEXT) == "warmup"
    assert classify_step({"intensity": "rest"}, None, CONTEXT) == "rest"
    low = resolve_target({"power": {"value": 50, "units": "%ftp"}}, CONTEXT)
    assert classify_step({"power": {}}, low, CONTEXT) == "rest"
    high = resolve_target({"power": {"value": 100, "units": "%ftp"}}, CONTEXT)
    assert classify_step({"power": {}}, high, CONTEXT) == "work"
    assert classify_step({"text": "locker rollen"}, None, CONTEXT) == "rest"
    assert classify_step({"duration": 60}, None, CONTEXT) == "work"


def test_align_handles_extra_and_missing_intervals():
    """Alignment keeps order and tolerates an inserted or missing interval."""
    planned = plan_steps(EVENT_DATA["workout_doc"]["steps"], CONTEXT)
    intervals = EXECUTION_INTERVALS["icu_intervals"]
    pairs = align(planned, intervals)
    assert pairs == [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 5)]
    extra = intervals[:3] + [{"elapsed_time": 30, "average_watts": 300}] + intervals[3:]
    pairs = align(planned, extra)
    assert (None, 3) in pairs
    assert sum(1 for p, i in pairs if p is not None and i is not None) == 6
    pairs = align(planned, intervals[:-1])
    assert pairs[-1] == (5, None)


def test_interval_metrics_and_analyze():
    """Per-interval metrics come from the samples; the analysis summary counts matches."""
    planned = plan_steps(EVENT_DATA["workout_doc"]["steps"], CONTEXT)
    work = EXECUTION_INTERVALS["icu_intervals"][1]
    rest = EXECUTION_INTERVALS["icu_intervals"][2]
    metrics = interval_metrics(work, EXECUTION_STREAMS, planned[1]["target"], rest)
    assert metrics["samples_valid"] is True
    assert metrics["time_in_target_pct"] == 100.0
    assert metrics["cadence_nonzero_mean"] == 90
    assert metrics["hr_start"] < metrics["hr_end"]
    assert metrics["hr_recovery_60s_drop"] > 10
    assert metrics["power_fade_pct"] < 0
    assert metrics["pw_hr_drift_pct"] is None  # shorter than 10 minutes
    assert metrics["custom_streams"]["Stamina"]["delta"] == pytest.approx(-5.98, abs=0.05)
    result = analyze(planned, EXECUTION_INTERVALS["icu_intervals"], EXECUTION_STREAMS)
    assert result["summary"]["matched"] == 6
    assert result["summary"]["work_steps_in_range"] == "2/2"
    assert result["rows"][1]["duration_diff_s"] == 0
    assert interval_metrics({"elapsed_time": 10}, EXECUTION_STREAMS, None)["samples_valid"] is False


def test_sports_helpers():
    """Durations, pace, zone tables and start times format as documented."""
    assert hms(3661) == "1:01:01" and hms(59) == "0:59" and hms(None) == "n/a"
    assert format_pace(3.2258065) == "5:10/km"
    assert format_pace(0.8333, "SECS_100M") == "120.0 s/100 m"
    assert format_pace(0) == "n/a"
    rows = zone_ranges("power", [55, 75, 999], ["Z1", "Z2", "Z3"], ftp=200)
    assert rows[1] == {"zone": 2, "name": "Z2", "upper_bound": 75, "lower_bound": 55, "min_watts": 111, "max_watts": 150}
    assert rows[2]["max_watts"] is None
    assert format_zone_table("hr", [133, 147], None) == "Z1 ≤133 bpm, Z2 133-147 bpm"
    assert format_zone_table("pace", [77.5, 999], None, threshold_pace=3.2258) == "Z1 ≤77.5% (slower to 6:40/km), Z2 >77.5% (6:40/km to faster)"
    assert to_utc_iso("2026-10-06T15:36:22Z") == "2026-10-06T15:36:22Z"
    assert to_utc_iso("2026-10-06T17:36:22+02:00") == "2026-10-06T15:36:22Z"
    assert to_utc_iso("nonsense") is None
    assert format_start_times({"start_date_local": "2026-10-06T17:36:22", "start_date": "2026-10-06T15:36:22Z", "timezone": "Europe/Vienna"}) == (
        "2026-10-06T17:36:22 local (Europe/Vienna, UTC+02:00) / 2026-10-06T15:36:22Z UTC"
    )
    assert format_start_times({}) == "Unknown"


def test_times_cadence_and_temperature_helpers():
    """Phase 5 (E): offset from local vs UTC, steps per minute for foot sports, temperatures with unit or n/a."""
    no_zone = {"start_date_local": "2026-10-04T09:03:51", "start_date": "2026-10-04T07:03:51Z"}
    assert utc_offset(no_zone) == "+02:00"
    assert format_start_times(no_zone) == "2026-10-04T09:03:51 local (UTC+02:00) / 2026-10-04T07:03:51Z UTC"
    assert format_local_start(no_zone) == "2026-10-04 09:03 local (UTC+02:00)"
    assert format_local_start({"start_date_local": "2026-01-10T07:00:00"}) == "2026-01-10 07:00 local"
    assert utc_offset({"start_date_local": "2026-01-10T07:00:00", "start_date": "2026-01-10T12:30:00Z"}) == "-05:30"
    assert utc_offset({"start_date_local": "2026-01-10T07:00:00"}) is None
    assert start_times(no_zone)["utc_offset"] == "+02:00"
    assert cadence_text(73.565, "Run") == "147 spm (74 rpm as stored)"
    assert cadence_text(41.6, "Hike", 1) == "83 spm (41.6 rpm as stored)"
    assert cadence_text(88.2, "Ride") == "88 rpm" and cadence_text(None, "Run") == "n/a"
    assert cadence_spm(80, "TrailRun") == 160 and cadence_spm(80, "Ride") is None
    assert temperature_text(16.549) == "16.5 °C" and temperature_text(22) == "22 °C" and temperature_text(None) == "n/a"


def test_permission_configuration():
    """MCP_PERMISSIONS parsing and registration-time gating of tool classes."""
    assert parse_permissions("") == frozenset({"read"})
    assert parse_permissions("write") == frozenset({"read", "write"})
    assert parse_permissions("all") == frozenset({"read", "write", "destructive", "admin"})
    with pytest.raises(ValueError):
        parse_permissions("read,delete")
    assert parse_units_overrides("Stamina=%, RecoveryTime = h,bad") == {"Stamina": "%", "RecoveryTime": "h"}
    hidden = disabled_tools()
    assert hidden["delete_event"] == "destructive"
    assert hidden["add_events_bulk"] == "admin"
    assert tool_permissions()["get_activities"] == "read"
    with pytest.raises(ValueError):
        tool("superuser")

    @tool("destructive")
    async def _dummy_destructive_tool() -> str:
        return "x"

    assert asyncio.run(_dummy_destructive_tool()) == "x"
    assert disabled_tools()["_dummy_destructive_tool"] == "destructive"


class _FlakyResponse:
    """First answer 429 with Retry-After, then 200 with JSON."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.headers = {"Retry-After": "0"} if status_code == 429 else {}
        self.content = b"{}"
        self._payload = payload

    def raise_for_status(self):
        """Behave like httpx for 2xx responses."""
        if self.status_code >= 400:
            raise AssertionError("raise_for_status should not be reached for retried responses")

    def json(self):
        """Return the payload."""
        return self._payload


class _FlakyClient:
    """Async client that fails once with 429 before succeeding."""

    def __init__(self):
        self.calls = 0
        self.is_closed = False

    async def request(self, *_args, **_kwargs):
        """Return 429 on the first call, then 200."""
        self.calls += 1
        if self.calls == 1:
            return _FlakyResponse(429, {})
        return _FlakyResponse(200, {"ok": True})

    async def aclose(self):
        """Close."""
        self.is_closed = True


def test_make_intervals_request_retries_on_429(monkeypatch):
    """A 429 answer is retried after the Retry-After delay and the second answer is returned."""
    client = _FlakyClient()
    monkeypatch.setattr(server, "httpx_client", client)
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(api_client.asyncio, "sleep", fake_sleep)
    result = asyncio.run(server.make_intervals_request("/athlete/i1"))
    assert result == {"ok": True}
    assert client.calls == 2
    assert slept == [0.0]


def test_fastmcp_settings_message_path():
    """FASTMCP_MESSAGE_PATH (and the other FASTMCP_* variables) are passed to FastMCP."""
    from intervals_mcp_server.mcp_instance import fastmcp_settings_from_env  # pylint: disable=import-outside-toplevel

    settings = fastmcp_settings_from_env({"FASTMCP_SSE_PATH": "/mcp-abc/sse", "FASTMCP_MESSAGE_PATH": "/mcp-abc/messages/", "FASTMCP_PORT": "8001"})
    assert settings == {"sse_path": "/mcp-abc/sse", "message_path": "/mcp-abc/messages/", "port": 8001}
