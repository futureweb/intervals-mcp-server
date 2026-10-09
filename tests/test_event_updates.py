"""Partial event and note updates, event categories and absolute pace targets.

Regression tests for updates that used to send unset fields as null, always set the category
to WORKOUT and moved the event to today (idea and reproduction from
mvilanova/intervals-mcp-server#150 by morritter).
"""

import asyncio
import os
import pathlib
import sys
from datetime import datetime

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.tools.events import add_or_update_event, add_or_update_note  # pylint: disable=wrong-import-position  # noqa: E402
from intervals_mcp_server.utils.types import Step, Value, ValueUnits, WorkoutDoc  # pylint: disable=wrong-import-position  # noqa: E402


def _capture(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_request(*_args, **kwargs):
        calls.append({"url": kwargs.get("url"), "method": kwargs.get("method"), "data": kwargs.get("data")})
        return {"id": "e123"}

    monkeypatch.setattr("intervals_mcp_server.tools.events.make_intervals_request", fake_request)
    return calls


def test_update_sends_only_the_changed_fields(monkeypatch):
    """Renaming an event must not clear its description or move it to today."""
    calls = _capture(monkeypatch)
    result = asyncio.run(add_or_update_event(event_id="77", name="Threshold 3x10 (moved)"))
    assert calls == [{"url": "/athlete/i1/events/77", "method": "PUT", "data": {"name": "Threshold 3x10 (moved)"}}]
    assert result == "Successfully updated event id: e123"


def test_update_keeps_category_and_type_unless_passed(monkeypatch):
    calls = _capture(monkeypatch)
    asyncio.run(add_or_update_event(event_id="77", moving_time=3900, start_date="2026-10-12"))
    assert calls[0]["data"] == {"moving_time": 3900, "start_date_local": "2026-10-12T00:00:00"}
    asyncio.run(add_or_update_event(event_id="77", workout_type="VirtualRide", category="race_b"))
    assert calls[1]["data"] == {"type": "VirtualRide", "category": "RACE_B"}


def test_update_without_fields_is_refused(monkeypatch):
    calls = _capture(monkeypatch)
    assert asyncio.run(add_or_update_event(event_id="77")).startswith("Error: nothing to update")
    assert not calls


def test_create_defaults_and_validation(monkeypatch):
    calls = _capture(monkeypatch)
    assert asyncio.run(add_or_update_event(workout_type="Ride")) == "Error: name is required when creating an event."
    assert asyncio.run(add_or_update_event(name="Race", category="FINAL")).startswith("Error: category must be one of")
    assert not calls
    asyncio.run(add_or_update_event(name="Easy run 45 min", workout_type="Run"))
    data = calls[0]["data"]
    assert calls[0]["method"] == "POST" and calls[0]["url"] == "/athlete/i1/events"
    assert data["category"] == "WORKOUT" and data["type"] == "Run" and data["name"] == "Easy run 45 min"
    assert data["start_date_local"] == datetime.now().strftime("%Y-%m-%d") + "T00:00:00"
    assert "description" not in data and "moving_time" not in data and "distance" not in data
    asyncio.run(add_or_update_event(name="Gran Fondo", workout_type="Ride", category="RACE_A", start_date="2027-05-02"))
    assert calls[1]["data"]["category"] == "RACE_A"


def test_note_update_is_partial_and_create_has_defaults(monkeypatch):
    calls = _capture(monkeypatch)
    asyncio.run(add_or_update_note(event_id="88", name="Travel day"))
    assert calls[0] == {"url": "/athlete/i1/events/88", "method": "PUT", "data": {"category": "NOTE", "name": "Travel day"}}
    assert asyncio.run(add_or_update_note(description="no title")) == "Error: name is required when creating a note."
    asyncio.run(add_or_update_note(name="Rest", description="Legs heavy", start_date="2026-10-13"))
    assert calls[1]["data"] == {
        "category": "NOTE", "name": "Rest", "description": "Legs heavy",
        "start_date_local": "2026-10-13T00:00:00", "color": "green",
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        (Value(value=335, units=ValueUnits.MINS_KM), "5:35/km Pace"),
        (Value(value=5.5833, units=ValueUnits.MINS_KM), "5:35/km Pace"),
        (Value(value=4.0, units=ValueUnits.MINS_KM), "4:00/km Pace"),
        (Value(value=540, units=ValueUnits.MINS_MILE), "9:00/mi Pace"),
        (Value(value=105, units=ValueUnits.SECS_100M), "1:45/100m Pace"),
        (Value(value=95, units=ValueUnits.SECS_100Y), "1:35/100y Pace"),
        (Value(value=120, units=ValueUnits.SECS_500M), "2:00/500m Pace"),
        (Value(start=330, end=340, units=ValueUnits.MINS_KM), "5:30-5:40/km Pace"),
        (Value(start=5.5, end=5.6667, units=ValueUnits.MINS_KM), "5:30-5:40/km Pace"),
        (Value(value=80, units=ValueUnits.PERCENT_PACE), "80% Pace"),
    ],
)
def test_absolute_pace_targets_use_intervals_syntax(value, expected):
    assert str(value) == expected


def test_absolute_pace_in_event_description(monkeypatch):
    """The workout text sent to Intervals.icu carries the pace in its own syntax."""
    calls = _capture(monkeypatch)
    doc = WorkoutDoc.from_dict({"steps": [{"duration": 2700, "pace": {"value": 335, "units": "MINS_KM"}, "text": "steady"}]})
    asyncio.run(add_or_update_event(name="Tempo run", workout_type="Run", workout_doc=doc, start_date="2026-10-14"))
    assert "- 45m 5:35/km Pace steady" in calls[0]["data"]["description"]
    assert str(Step.from_dict({"duration": 60, "pace": {"start": 100, "end": 110, "units": "SECS_100M"}})).strip() == "- 1m 1:40-1:50/100m Pace"
