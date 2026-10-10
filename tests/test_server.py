"""
Unit tests for the main MCP server tool functions in intervals_mcp_server.server.

These tests use monkeypatching to mock API responses and verify the formatting and output of each tool function:
- get_activities
- get_activity_details
- get_activity_intervals
- get_activity_streams
- get_activity_messages
- add_activity_message
- update_activity
- get_events
- get_event_by_id
- add_or_update_event
- get_wellness_data

The tests ensure that the server's public API returns expected strings and handles data correctly.
"""

import asyncio
import json
import datetime
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.server import (  # pylint: disable=wrong-import-position
    add_activity_message,
    get_activities,
    get_activity_details,
    get_activity_intervals,
    get_activity_messages,
    get_activity_streams,
    add_events_bulk,
    add_or_update_event,
    get_athlete_power_curves,
    get_event_by_id,
    get_events,
    get_gear_list,
    get_plan_compliance,
    get_weekly_summary,
    get_wellness_data,
    update_activity,
    get_custom_items,
    get_custom_item_by_id,
    create_custom_item,
    update_custom_item,
    delete_custom_item,
)
from intervals_mcp_server.tools import gear as gear_module  # pylint: disable=wrong-import-position
from tests.sample_data import INTERVALS_DATA, POWER_CURVES_DATA  # pylint: disable=wrong-import-position


def _reset_gear_cache():
    """Helper to clear the module-level gear cache between tests."""
    gear_module._GEAR_RAW_CACHE.clear()  # pylint: disable=protected-access


def test_get_activities(monkeypatch):
    """
    Test get_activities returns a formatted string containing activity details when given a sample activity.
    """
    sample = {
        "name": "Morning Ride",
        "id": 123,
        "type": "Ride",
        "startTime": "2024-01-01T08:00:00Z",
        "distance": 1000,
        "duration": 3600,
    }

    async def fake_request(*_args, **_kwargs):
        return [sample]

    async def fake_gear_request(*_args, **_kwargs):
        return []

    # Patch in both api.client and tools modules to ensure it works; the gear names come from the
    # gear catalogue (no request may leave the test).
    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    monkeypatch.setattr("intervals_mcp_server.tools.gear.make_intervals_request", fake_gear_request)
    result = asyncio.run(get_activities(athlete_id="1", limit=1, include_unnamed=True))
    assert "Morning Ride" in result
    assert "Activities:" in result


def test_get_activity_details(monkeypatch):
    """
    Test get_activity_details returns a formatted string with the activity name and details.
    """
    sample = {
        "name": "Morning Ride",
        "id": 123,
        "type": "Ride",
        "startTime": "2024-01-01T08:00:00Z",
        "distance": 1000,
        "duration": 3600,
    }

    async def fake_request(*_args, **_kwargs):
        return sample

    # Patch in both api.client and tools modules to ensure it works
    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    result = asyncio.run(get_activity_details(123))
    assert "Activity: Morning Ride" in result


def test_get_events(monkeypatch):
    """
    Test get_events returns a formatted string containing event details when given a sample event.
    """
    event = {
        "date": "2024-01-01",
        "id": "e1",
        "name": "Test Event",
        "description": "desc",
        "race": True,
    }

    async def fake_request(*_args, **_kwargs):
        return [event]

    # Patch in both api.client and tools modules to ensure it works
    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr("intervals_mcp_server.tools.events.make_intervals_request", fake_request)
    result = asyncio.run(get_events(athlete_id="1", start_date="2024-01-01", end_date="2024-01-02"))
    assert "Test Event" in result
    assert "Events:" in result


def test_get_event_by_id(monkeypatch):
    """
    Test get_event_by_id returns a formatted string with event details for a given event ID.
    """
    event = {
        "id": "e1",
        "date": "2024-01-01",
        "name": "Test Event",
        "description": "desc",
        "race": True,
    }

    async def fake_request(*_args, **_kwargs):
        return event

    # Patch in both api.client and tools modules to ensure it works
    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr("intervals_mcp_server.tools.events.make_intervals_request", fake_request)
    result = asyncio.run(get_event_by_id("e1", athlete_id="1"))
    assert "Event Details:" in result
    assert "Test Event" in result


def test_get_wellness_data(monkeypatch):
    """
    Test get_wellness_data returns a formatted string containing wellness data for a given athlete.
    """
    wellness = {
        "2024-01-01": {
            "id": "2024-01-01",
            "ctl": 75,
            "sleepSecs": 28800,
        }
    }

    async def fake_request(*_args, **_kwargs):
        return wellness

    # Patch in both api.client and tools modules to ensure it works
    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr("intervals_mcp_server.tools.wellness.make_intervals_request", fake_request)
    result = asyncio.run(get_wellness_data(athlete_id="1"))
    assert "Wellness Data:" in result
    assert "2024-01-01" in result


def test_get_wellness_data_renders_macros(monkeypatch):
    """
    Integration test: native nutrition macros (carbohydrates, protein,
    fatTotal) flow from the API response through get_wellness_data into the
    formatted output.
    """
    wellness = [
        {
            "id": "2026-04-08",
            "carbohydrates": 310,
            "protein": 145,
            "fatTotal": 72,
        }
    ]

    async def fake_request(*_args, **_kwargs):
        return wellness

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr("intervals_mcp_server.tools.wellness.make_intervals_request", fake_request)
    result = asyncio.run(get_wellness_data(athlete_id="1"))
    assert "Wellness Data:" in result
    assert "2026-04-08" in result
    assert "Nutrition & Hydration:" in result
    assert "- Carbohydrates: 310 g" in result
    assert "- Protein: 145 g" in result
    assert "- Fat: 72 g" in result


def test_get_wellness_data_include_all_fields(monkeypatch):
    """
    Test get_wellness_data with include_all_fields=True returns a formatted string including additional fields.
    """
    wellness = [
        {
            "id": "2024-01-01",
            "ctl": 75,
            "sleepSecs": 28800,
            "customField": "custom_value",
        }
    ]

    async def fake_request(*_args, **_kwargs):
        return wellness

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr("intervals_mcp_server.tools.wellness.make_intervals_request", fake_request)
    result = asyncio.run(get_wellness_data(athlete_id="1", include_all_fields=True))
    assert "Wellness Data:" in result
    assert "2024-01-01" in result
    assert "Fitness (CTL): 75" in result
    assert "Other Fields:" in result
    assert "customField: custom_value" in result


def test_get_activity_intervals(monkeypatch):
    """
    Test get_activity_intervals returns a formatted string with interval analysis for a given activity.
    """

    async def fake_request(*_args, **_kwargs):
        return INTERVALS_DATA

    # Patch in both api.client and tools modules to ensure it works
    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    result = asyncio.run(get_activity_intervals("123"))
    assert "Intervals Analysis:" in result
    assert "Rep 1" in result


def test_get_activity_streams(monkeypatch):
    """
    Test get_activity_streams returns a formatted string with stream data for a given activity.
    """
    sample_streams = [
        {
            "type": "time",
            "name": "time",
            "data": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            "data2": [],
            "valueType": "time_units",
            "valueTypeIsArray": False,
            "anomalies": None,
            "custom": False,
        },
        {
            "type": "watts",
            "name": "watts",
            "data": [150, 155, 160, 165, 170, 175, 180, 185, 190, 195, 200],
            "data2": [],
            "valueType": "power_units",
            "valueTypeIsArray": False,
            "anomalies": None,
            "custom": False,
        },
        {
            "type": "heartrate",
            "name": "heartrate",
            "data": [120, 125, 130, 135, 140, 145, 150, 155, 160, 165, 170],
            "data2": [],
            "valueType": "hr_units",
            "valueTypeIsArray": False,
            "anomalies": None,
            "custom": False,
        },
    ]

    async def fake_request(*_args, **_kwargs):
        return sample_streams

    # Patch in both api.client and tools modules to ensure it works
    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    result = asyncio.run(get_activity_streams("i107537962"))
    assert "Activity Streams" in result
    assert "time" in result
    assert "watts" in result
    assert "heartrate" in result
    assert "Data Points: 11" in result


def test_add_or_update_event(monkeypatch):
    """
    Test add_or_update_event successfully posts an event and returns the response data.
    """
    expected_response = {
        "id": "e123",
        "start_date_local": "2024-01-15T00:00:00",
        "category": "WORKOUT",
        "name": "Test Workout",
        "type": "Ride",
    }

    async def fake_post_request(*_args, **kwargs):
        if kwargs.get("method", "GET") == "GET" and kwargs.get("params"):
            return []  # the day's events (duplicate check): none
        return expected_response

    # Patch in both api.client and tools modules to ensure it works
    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_post_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.events.make_intervals_request", fake_post_request
    )
    result = asyncio.run(
        add_or_update_event(
            athlete_id="i1", start_date="2024-01-15", name="Test Workout", workout_type="Ride"
        )
    )
    assert "Successfully created event id:" in result
    assert "e123" in result
    assert "Read-back: Intervals.icu stored 2024-01-15 WORKOUT Ride 'Test Workout'" in result


def test_get_activity_messages(monkeypatch):
    """Test get_activity_messages returns formatted messages for an activity."""
    sample_messages = [
        {
            "id": 1,
            "name": "Niko",
            "created": "2024-06-15T10:30:00Z",
            "type": "NOTE",
            "content": "Legs felt heavy today",
        },
        {
            "id": 2,
            "name": "Coach",
            "created": "2024-06-15T11:00:00Z",
            "type": "TEXT",
            "content": "Good effort despite that!",
        },
    ]

    async def fake_request(*_args, **_kwargs):
        return sample_messages

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    result = asyncio.run(get_activity_messages(activity_id="i123"))
    assert "Legs felt heavy today" in result
    assert "Good effort despite that!" in result
    assert "Niko" in result
    assert "Coach" in result


def test_get_activity_messages_error(monkeypatch):
    """Test get_activity_messages handles API errors gracefully."""

    async def fake_request(*_args, **_kwargs):
        return {"error": True, "message": "Activity not found"}

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    result = asyncio.run(get_activity_messages(activity_id="i999"))
    assert "Error fetching activity messages" in result
    assert "Activity not found" in result


def test_get_activity_messages_empty(monkeypatch):
    """Test get_activity_messages returns appropriate message when no messages exist."""

    async def fake_request(*_args, **_kwargs):
        return []

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    result = asyncio.run(get_activity_messages(activity_id="i123"))
    assert "No messages found" in result


def test_add_activity_message(monkeypatch):
    """Test add_activity_message posts a message and returns confirmation."""

    async def fake_request(*_args, **kwargs):
        assert kwargs.get("method") == "POST"
        assert kwargs.get("data") == {"content": "Great run!"}
        return {"id": 42, "new_chat": None}

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    result = asyncio.run(add_activity_message(activity_id="i123", content="Great run!"))
    assert "Successfully added message" in result
    assert "42" in result


def test_add_activity_message_missing_id(monkeypatch):
    """Test add_activity_message warns when response has no ID."""

    async def fake_request(*_args, **_kwargs):
        return {"new_chat": None}

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    result = asyncio.run(add_activity_message(activity_id="i123", content="Hello"))
    assert "appears to have been added" in result
    assert "verify manually" in result


def test_add_activity_message_unexpected_response(monkeypatch):
    """Test add_activity_message handles unexpected non-dict response."""

    async def fake_request(*_args, **_kwargs):
        return None

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    result = asyncio.run(add_activity_message(activity_id="i123", content="Hello"))
    assert "Unexpected response" in result


def test_add_activity_message_error(monkeypatch):
    """Test add_activity_message handles API errors."""

    async def fake_request(*_args, **_kwargs):
        return {"error": True, "message": "Not found"}

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    result = asyncio.run(add_activity_message(activity_id="i999", content="Hello"))
    assert "Error adding message" in result


def test_get_athlete_power_curves(monkeypatch):
    """
    Test get_athlete_power_curves returns formatted power curve data with both seasons.
    """

    async def fake_request(*_args, **_kwargs):
        return POWER_CURVES_DATA

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.power_curves.make_intervals_request", fake_request
    )
    result = asyncio.run(
        get_athlete_power_curves(
            activity_type="Ride",
            athlete_id="i1",
        )
    )
    assert "Power Curves (Ride):" in result
    assert "This season" in result
    assert "Last season" in result
    assert "5s:" in result
    assert "W/kg" in result
    assert "i100" in result


def test_get_athlete_power_curves_custom_durations(monkeypatch):
    """
    Test get_athlete_power_curves with custom durations returns only those durations.
    """

    async def fake_request(*_args, **_kwargs):
        return POWER_CURVES_DATA

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.power_curves.make_intervals_request", fake_request
    )
    result = asyncio.run(
        get_athlete_power_curves(
            activity_type="Ride",
            durations=[5, 60],
            athlete_id="i1",
        )
    )
    assert "5s:" in result
    assert "1m:" in result
    # Should not contain durations we didn't request
    assert "15s:" not in result
    assert "10m:" not in result


def test_get_athlete_power_curves_without_normalised(monkeypatch):
    """
    Test get_athlete_power_curves without normalised data excludes W/kg values.
    """

    async def fake_request(*_args, **_kwargs):
        return POWER_CURVES_DATA

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.power_curves.make_intervals_request", fake_request
    )
    result = asyncio.run(
        get_athlete_power_curves(
            activity_type="Ride",
            include_normalised=False,
            athlete_id="i1",
        )
    )
    assert "W/kg" not in result
    assert "780W" in result


def test_get_athlete_power_curves_date_validation(monkeypatch):
    """
    Test get_athlete_power_curves validates date parameters.
    """

    async def fake_request(*_args, **_kwargs):
        return POWER_CURVES_DATA

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.power_curves.make_intervals_request", fake_request
    )
    # Only start_date without end_date should fail
    result = asyncio.run(
        get_athlete_power_curves(
            activity_type="Ride",
            start_date="2026-01-01",
            athlete_id="i1",
        )
    )
    assert "Error" in result
    assert "start_date and end_date must be provided together" in result


def test_get_athlete_power_curves_no_curves_selected(monkeypatch):
    """
    Test get_athlete_power_curves returns error when no curves selected.
    """

    async def fake_request(*_args, **_kwargs):
        return POWER_CURVES_DATA

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.power_curves.make_intervals_request", fake_request
    )
    result = asyncio.run(
        get_athlete_power_curves(
            activity_type="Ride",
            this_season=False,
            last_season=False,
            athlete_id="i1",
        )
    )
    assert "Error" in result
    assert "At least one curve must be selected" in result


def test_get_custom_items(monkeypatch):
    """
    Test get_custom_items returns a formatted string containing custom item details.
    """
    custom_items = [
        {"id": 1, "name": "HR Zones", "type": "ZONES", "description": "Heart rate zones"},
        {"id": 2, "name": "Power Chart", "type": "FITNESS_CHART", "description": None},
    ]

    async def fake_request(*_args, **_kwargs):
        return custom_items

    # Patch in both api.client and tools modules to ensure it works
    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.custom_items.make_intervals_request", fake_request
    )
    result = asyncio.run(get_custom_items(athlete_id="1"))
    assert "Custom Items:" in result
    assert "HR Zones" in result
    assert "ZONES" in result
    assert "Power Chart" in result


def test_get_custom_item_by_id(monkeypatch):
    """
    Test get_custom_item_by_id returns formatted details of a single custom item.
    """
    custom_item = {
        "id": 1,
        "name": "HR Zones",
        "type": "ZONES",
        "description": "Heart rate zones",
        "visibility": "PRIVATE",
        "index": 0,
    }

    async def fake_request(*_args, **_kwargs):
        return custom_item

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.custom_items.make_intervals_request", fake_request
    )
    result = asyncio.run(get_custom_item_by_id(item_id=1, athlete_id="1"))
    assert "Custom Item Details:" in result
    assert "HR Zones" in result
    assert "ZONES" in result
    assert "Heart rate zones" in result
    assert "PRIVATE" in result


def test_create_custom_item(monkeypatch):
    """
    Test create_custom_item returns a success message with formatted item details.
    """
    created_item = {
        "id": 10,
        "name": "New Chart",
        "type": "FITNESS_CHART",
        "description": "A new fitness chart",
        "visibility": "PRIVATE",
    }

    async def fake_request(*_args, **_kwargs):
        return created_item

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.custom_items.make_intervals_request", fake_request
    )
    result = asyncio.run(
        create_custom_item(name="New Chart", item_type="FITNESS_CHART", athlete_id="1")
    )
    assert "Successfully created custom item:" in result
    assert "New Chart" in result
    assert "FITNESS_CHART" in result


def test_create_custom_item_with_string_content(monkeypatch):
    """
    Test create_custom_item correctly parses content when passed as a JSON string.
    """
    captured: dict = {}

    async def fake_request(*_args, **kwargs):
        captured["data"] = kwargs.get("data")
        return {
            "id": 11,
            "name": "Activity Field",
            "type": "ACTIVITY_FIELD",
            "content": {"expression": "icu_training_load"},
        }

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.custom_items.make_intervals_request", fake_request
    )
    result = asyncio.run(
        create_custom_item(
            name="Activity Field",
            item_type="ACTIVITY_FIELD",
            athlete_id="1",
            content='{"expression": "icu_training_load"}',  # type: ignore[arg-type]
        )
    )
    assert "Successfully created custom item:" in result
    # Verify the content was parsed from string to dict before being sent
    assert isinstance(captured["data"]["content"], dict)
    assert captured["data"]["content"]["expression"] == "icu_training_load"


def test_update_custom_item(monkeypatch):
    """
    Test update_custom_item returns a success message with formatted item details.
    """
    updated_item = {
        "id": 1,
        "name": "Updated Chart",
        "type": "FITNESS_CHART",
        "description": "Updated description",
        "visibility": "PUBLIC",
    }

    async def fake_request(*_args, **_kwargs):
        return updated_item

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.custom_items.make_intervals_request", fake_request
    )
    result = asyncio.run(
        update_custom_item(item_id=1, name="Updated Chart", athlete_id="1")
    )
    assert "Successfully updated custom item:" in result
    assert "Updated Chart" in result
    assert "PUBLIC" in result


def test_delete_custom_item(monkeypatch):
    """
    Test delete_custom_item reads the item first and names it in the answer.
    """

    async def fake_request(*_args, **kwargs):
        if kwargs.get("method", "GET") == "GET":
            return {"id": 1, "name": "Old Chart", "type": "FITNESS_CHART"}
        return {}

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.custom_items.make_intervals_request", fake_request
    )
    result = asyncio.run(delete_custom_item(item_id=1, athlete_id="1"))
    assert "Successfully deleted" in result and "'Old Chart' (FITNESS_CHART)" in result


def test_create_custom_item_with_invalid_json_content(monkeypatch):
    """
    Test create_custom_item returns an error message when content is an invalid JSON string.
    """

    async def fake_request(*_args, **_kwargs):
        return {}

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.custom_items.make_intervals_request", fake_request
    )
    result = asyncio.run(
        create_custom_item(
            name="Bad Item",
            item_type="FITNESS_CHART",
            athlete_id="1",
            content="not valid json",  # type: ignore[arg-type]
        )
    )
    assert "Error: content must be valid JSON when passed as a string." in result


# ---------------------------------------------------------------------------
# Gear tools
# ---------------------------------------------------------------------------


def test_get_gear_list(monkeypatch):
    """
    Test get_gear_list returns a formatted catalog with id, type, name and stats.
    """
    _reset_gear_cache()

    sample_gear = [
        {
            "id": "b1",
            "type": "Bike",
            "name": "Litening Air",
            "default_for_type": "Ride",
            "activities": 100,
            "distance": 4_155_700,
            "retired": False,
        },
        {
            "id": "b2",
            "type": "Bike",
            "name": "Retired bike",
            "activities": 50,
            "distance": 2_000_000,
            "retired": True,
        },
    ]

    async def fake_request(*_args, **_kwargs):
        return sample_gear

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.gear.make_intervals_request", fake_request
    )

    result = asyncio.run(get_gear_list(athlete_id="i1"))

    assert "Gear catalog for athlete i1:" in result
    assert "Litening Air" in result
    assert "b1" in result
    assert "Retired bike" in result
    assert "yes" in result  # retired flag rendered
    assert "Ride" in result  # default_for_type rendered


def test_get_gear_list_empty(monkeypatch):
    """
    Test get_gear_list returns an informative message when no gear is configured.
    """
    _reset_gear_cache()

    async def fake_request(*_args, **_kwargs):
        return []

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.gear.make_intervals_request", fake_request
    )

    result = asyncio.run(get_gear_list(athlete_id="i1"))
    assert "No gear found" in result


def test_get_gear_list_cache_and_refresh(monkeypatch):
    """
    Test that get_gear_list caches the catalog and that refresh=True busts the cache.
    """
    _reset_gear_cache()

    call_count = {"n": 0}
    sample_gear = [
        {
            "id": "b1",
            "type": "Bike",
            "name": "Litening Air",
            "activities": 100,
            "distance": 4_155_700,
        }
    ]

    async def fake_request(*_args, **_kwargs):
        call_count["n"] += 1
        return sample_gear

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.gear.make_intervals_request", fake_request
    )

    # First call: cache cold, one API hit expected.
    asyncio.run(get_gear_list(athlete_id="i1"))
    assert call_count["n"] == 1

    # Second call: cache warm, no additional API hit.
    asyncio.run(get_gear_list(athlete_id="i1"))
    assert call_count["n"] == 1

    # refresh=True busts the cache and triggers a fresh fetch.
    asyncio.run(get_gear_list(athlete_id="i1", refresh=True))
    assert call_count["n"] == 2


def test_get_activity_details_resolves_gear_name(monkeypatch):
    """
    Test get_activity_details injects the resolved gear name into the formatted output
    when the activity payload contains a gear_id.
    """
    _reset_gear_cache()

    activity = {
        "name": "Morning Ride",
        "id": 123,
        "type": "Ride",
        "startTime": "2024-01-01T08:00:00Z",
        "distance": 1000,
        "duration": 3600,
        "gear_id": "b1",
    }
    gear_catalog = [{"id": "b1", "type": "Bike", "name": "Litening Air"}]

    async def fake_request(url=None, **_kwargs):
        # The activity endpoint and the gear endpoint share the same fake
        # request; route by URL pattern.
        if url and "/gear" in url:
            return gear_catalog
        return activity

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    monkeypatch.setattr(
        "intervals_mcp_server.tools.gear.make_intervals_request", fake_request
    )
    # get_activity_details does not accept athlete_id; gear resolution falls
    # back to the configured ATHLETE_ID, which is unset under CI. Provide one.
    monkeypatch.setattr(gear_module.config, "athlete_id", "1")

    result = asyncio.run(get_activity_details(123))
    assert "Activity: Morning Ride" in result
    assert "Gear:" in result
    assert "Name: Litening Air" in result
    assert "ID: b1" in result


def test_get_activities_resolves_gear_name(monkeypatch):
    """
    Test get_activities injects resolved gear names for each activity in the list.
    """
    _reset_gear_cache()

    activities = [
        {
            "name": "Ride 1",
            "id": 1,
            "type": "Ride",
            "startTime": "2024-01-01T08:00:00Z",
            "distance": 1000,
            "duration": 3600,
            "gear_id": "b1",
        },
        {
            "name": "Ride 2",
            "id": 2,
            "type": "Ride",
            "startTime": "2024-01-02T08:00:00Z",
            "distance": 2000,
            "duration": 5400,
            "gear_id": "b2",
        },
    ]
    gear_catalog = [
        {"id": "b1", "type": "Bike", "name": "Litening Air"},
        {"id": "b2", "type": "Bike", "name": "S-Works Tarmac SL8"},
    ]

    async def fake_request(url=None, **_kwargs):
        if url and "/gear" in url:
            return gear_catalog
        return activities

    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )
    monkeypatch.setattr(
        "intervals_mcp_server.tools.gear.make_intervals_request", fake_request
    )

    result = asyncio.run(get_activities(athlete_id="1", limit=2, include_unnamed=True))
    assert "Ride 1" in result
    assert "Ride 2" in result
    assert "Name: Litening Air" in result
    assert "Name: S-Works Tarmac SL8" in result


def _patch_training_review(monkeypatch, responses):
    """Patch make_intervals_request in training_review, routing by URL suffix."""

    async def fake_request(*_args, **kwargs):
        url = kwargs.get("url") or _args[0]
        for suffix, payload in responses.items():
            if url.endswith(suffix):
                return payload
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(
        "intervals_mcp_server.tools.training_review.make_intervals_request", fake_request
    )


def test_get_weekly_summary(monkeypatch):
    """Weekly summary shows per-week totals, per-sport lines and end-of-week CTL/ATL/form."""
    weeks = [
        {
            "date": "2026-08-10",
            "count": 3,
            "moving_time": 7200,
            "distance": 50000.0,
            "training_load": 120,
            "fitness": 17.5,
            "fatigue": 16.5,
            "form": 1.0,
            "rampRate": 0.6,
            "timeInZones": [3600, 600, 0, 0, 0, 0, 0, 0],
            "mostRecentWellnessId": "2026-08-16",
            "byCategory": [
                {"category": "Ride", "count": 1, "moving_time": 5400, "distance": 50000.0, "training_load": 118},
                {"category": "Workout", "count": 2, "moving_time": 1800, "distance": 0.0, "training_load": 2},
                {"category": "Run", "count": 0, "moving_time": 0, "distance": 0.0, "training_load": 0},
            ],
        }
    ]
    _patch_training_review(monkeypatch, {"athlete-summary.json": weeks})
    result = asyncio.run(get_weekly_summary("2026-08-10", "2026-08-16", athlete_id="1"))
    assert "ISO 2026-W33" in result
    assert "3 sessions" in result
    assert "CTL/fitness 17.5" in result and "form 1.0" in result
    assert "as of 2026-08-16" in result
    assert "- Workout: 2 sessions" in result
    assert "- Ride: 1 sessions" in result
    assert "Run:" not in result
    assert "Z1 1:00:00" in result


def test_get_weekly_summary_empty_error_and_invalid(monkeypatch):
    """Weekly summary handles empty results, API errors and invalid dates."""
    _patch_training_review(monkeypatch, {"athlete-summary.json": []})
    assert "No weekly summary data" in asyncio.run(
        get_weekly_summary("2026-08-10", "2026-08-16", athlete_id="1")
    )
    _patch_training_review(monkeypatch, {"athlete-summary.json": {"error": True, "message": "boom"}})
    assert "Error fetching weekly summary: boom" in asyncio.run(
        get_weekly_summary("2026-08-10", "2026-08-16", athlete_id="1")
    )
    assert "Error" in asyncio.run(get_weekly_summary("bad", "2026-08-16", athlete_id="1"))


def test_get_plan_compliance_five_planned(monkeypatch):
    """5 planned (3 paired, 1 missed, 1 future) plus 1 unplanned activity."""
    monkeypatch.setattr(
        "intervals_mcp_server.tools.training_review._today", lambda: datetime.date(2026, 10, 2)
    )
    events = [
        # paired via event.paired_activity_id
        {"id": 1, "category": "WORKOUT", "type": "Ride", "name": "Endurance",
         "start_date_local": "2026-09-28T00:00:00", "moving_time": 3600,
         "icu_training_load": 50, "paired_activity_id": "a1"},
        # paired via activity.paired_event_id only
        {"id": 2, "category": "WORKOUT", "type": "Run", "name": "Easy run",
         "start_date_local": "2026-09-29T00:00:00", "moving_time": 2400, "icu_training_load": 40},
        # paired via both
        {"id": 3, "category": "WORKOUT", "type": "WeightTraining", "name": "Strength",
         "start_date_local": "2026-09-30T00:00:00", "moving_time": 3000,
         "icu_training_load": 10, "paired_activity_id": "a3"},
        # missed
        {"id": 4, "category": "WORKOUT", "type": "Run", "name": "Tempo",
         "start_date_local": "2026-10-01T00:00:00", "moving_time": 3000, "icu_training_load": 60},
        # future
        {"id": 5, "category": "WORKOUT", "type": "Ride", "name": "Long ride",
         "start_date_local": "2026-10-04T00:00:00", "moving_time": 7200, "icu_training_load": 90},
        {"id": 6, "category": "NOTE", "name": "Travel", "start_date_local": "2026-10-03T00:00:00",
         "training_availability": "LIMITED", "can_train_sports": ["Run"], "max_training_time": 3600},
        {"id": 7, "category": "NOTE", "name": "Plain note", "start_date_local": "2026-10-03T00:00:00"},
    ]
    activities = [
        {"id": "a1", "type": "Ride", "name": "Morning ride", "start_date_local": "2026-09-28T07:00:00",
         "moving_time": 4320, "icu_training_load": 60},
        {"id": "a2", "type": "Run", "name": "Run", "start_date_local": "2026-09-29T18:00:00",
         "moving_time": 2400, "icu_training_load": 36, "paired_event_id": 2},
        {"id": "a3", "type": "WeightTraining", "name": "Gym", "start_date_local": "2026-09-30T09:00:00",
         "moving_time": 3000, "icu_training_load": 10, "paired_event_id": 3},
        {"id": "a4", "type": "Swim", "name": "Pool swim", "start_date_local": "2026-09-30T12:00:00",
         "moving_time": 1800, "icu_training_load": 20},
    ]
    _patch_training_review(monkeypatch, {"/events": events, "/activities": activities})
    result = asyncio.run(get_plan_compliance("2026-09-28", "2026-10-04", athlete_id="1"))
    assert "Planned workouts: 5 | completed: 3 | missed: 1 | upcoming: 1 | unplanned activities: 1" in result
    assert "Completion: 75% (3 of 4 planned workouts due)" in result
    assert "1:00:00 -> 1:12:00 (+20%)" in result
    assert "50 -> 60 (+20%)" in result
    assert "40 -> 36 (-10%)" in result
    missed_section = result.split("Missed (1):")[1].split("Upcoming")[0]
    assert "Tempo" in missed_section and "Long ride" not in missed_section
    upcoming_section = result.split("Upcoming, not yet due (1):")[1].split("Unplanned")[0]
    assert "Long ride" in upcoming_section
    unplanned_section = result.split("Unplanned activities (1):")[1]
    assert "Pool swim" in unplanned_section and "Morning ride" not in unplanned_section
    assert "availability LIMITED; can train Run; max training time 1:00:00" in result
    assert "Plain note" not in result


def test_get_plan_compliance_empty_and_error(monkeypatch):
    """Plan compliance handles empty results and API errors."""
    _patch_training_review(monkeypatch, {"/events": [], "/activities": []})
    assert "No events or activities" in asyncio.run(
        get_plan_compliance("2026-09-28", "2026-10-04", athlete_id="1")
    )
    _patch_training_review(
        monkeypatch, {"/events": {"error": True, "message": "nope"}, "/activities": []}
    )
    assert "Error fetching events: nope" in asyncio.run(
        get_plan_compliance("2026-09-28", "2026-10-04", athlete_id="1")
    )
    _patch_training_review(
        monkeypatch, {"/events": [], "/activities": {"error": True, "message": "bad"}}
    )
    assert "Error fetching activities: bad" in asyncio.run(
        get_plan_compliance("2026-09-28", "2026-10-04", athlete_id="1")
    )


def _capture_training_review(monkeypatch, payload):
    """Patch make_intervals_request in training_review and record call kwargs."""
    calls = []

    async def fake_request(*_args, **kwargs):
        calls.append(kwargs)
        return payload

    monkeypatch.setattr(
        "intervals_mcp_server.tools.training_review.make_intervals_request", fake_request
    )
    return calls


def test_get_weekly_summary_multi_week_sorted_and_athlete_filter(monkeypatch):
    """Unsorted multi-week input is sorted; rows of other athletes are dropped; params are sent."""
    rows = [
        {"date": "2026-08-17", "athlete_id": "i1", "count": 2, "fitness": 2.0, "fatigue": 1.0, "form": 1.0},
        {"date": "2026-08-10", "athlete_id": "i2", "count": 9, "fitness": 99.0, "fatigue": 1.0, "form": 1.0},
        {"date": "2026-08-10", "athlete_id": "i1", "count": 1, "fitness": 1.0, "fatigue": 1.0, "form": 0.0},
        {"date": "2026-08-24", "count": 3, "fitness": 3.0, "fatigue": 1.0, "form": 2.0},
    ]
    calls = _capture_training_review(monkeypatch, rows)
    result = asyncio.run(get_weekly_summary("2026-08-10", "2026-08-30", athlete_id="i1"))
    assert calls[0]["params"] == {"start": "2026-08-10", "end": "2026-08-30"}
    assert calls[0]["url"] == "/athlete/i1/athlete-summary.json"
    assert "99.0" not in result and "9 sessions" not in result
    positions = [result.index(f"Week {d}") for d in ("2026-08-10", "2026-08-17", "2026-08-24")]
    assert positions == sorted(positions)
    assert "ISO 2026-W33" in result and "ISO 2026-W35" in result


def test_get_weekly_summary_only_other_athletes(monkeypatch):
    """If all rows belong to other athletes, report no data."""
    _capture_training_review(monkeypatch, [{"date": "2026-08-10", "athlete_id": "i2", "count": 1}])
    result = asyncio.run(get_weekly_summary("2026-08-10", "2026-08-16", athlete_id="i1"))
    assert "No weekly summary data" in result


def test_get_plan_compliance_outside_range_and_duplicate_pairing(monkeypatch):
    """Activity paired to an out-of-range event is not unplanned; shared activity counts once."""
    monkeypatch.setattr(
        "intervals_mcp_server.tools.training_review._today", lambda: datetime.date(2026, 10, 2)
    )
    events = [
        {"id": 1, "category": "WORKOUT", "type": "Run", "name": "A", "start_date_local": "2026-09-29T00:00:00",
         "moving_time": 1000, "paired_activity_id": "a1"},
        {"id": 2, "category": "WORKOUT", "type": "Run", "name": "B", "start_date_local": "2026-09-30T00:00:00",
         "moving_time": 1000, "paired_activity_id": "a1"},
    ]
    activities = [
        {"id": "a1", "type": "Run", "name": "Shared run", "start_date_local": "2026-09-29T08:00:00",
         "moving_time": 1000},
        {"id": "a2", "type": "Ride", "name": "Early ride", "start_date_local": "2026-09-28T08:00:00",
         "moving_time": 2000, "paired_event_id": 999},
    ]
    _patch_training_review(monkeypatch, {"/events": events, "/activities": activities})
    result = asyncio.run(get_plan_compliance("2026-09-28", "2026-10-04", athlete_id="1"))
    assert "completed: 1 | missed: 1 | upcoming: 0 | unplanned activities: 0" in result
    assert "Completed, planned outside range (1):" in result
    assert "Early ride" in result.split("planned outside range (1):")[1]
    assert "Unplanned activities (0):" in result


def test_get_plan_compliance_null_load_shows_na(monkeypatch):
    """A planned workout whose load is null is shown as 'load n/a', not 'load None'."""
    monkeypatch.setattr(
        "intervals_mcp_server.tools.training_review._today", lambda: datetime.date(2026, 10, 2)
    )
    events = [
        {"id": 1, "category": "WORKOUT", "type": "WeightTraining", "name": "Strength",
         "start_date_local": "2026-10-03T00:00:00", "moving_time": 3300, "icu_training_load": None},
    ]
    _patch_training_review(monkeypatch, {"/events": events, "/activities": []})
    result = asyncio.run(get_plan_compliance("2026-09-28", "2026-10-04", athlete_id="1"))
    assert "load n/a" in result
    assert "load None" not in result




def _patch_activity_request(monkeypatch, fake_request):
    monkeypatch.setattr("intervals_mcp_server.api.client.make_intervals_request", fake_request)
    monkeypatch.setattr(
        "intervals_mcp_server.tools.activities.make_intervals_request", fake_request
    )


def test_update_activity(monkeypatch):
    """Test update_activity sends only passed fields via PUT and formats the result."""
    calls = []

    async def fake_request(*_args, **kwargs):
        calls.append(kwargs)
        return {"id": "i123", "name": "Renamed run", "icu_rpe": 6, "feel": 2}

    _patch_activity_request(monkeypatch, fake_request)
    result = asyncio.run(update_activity(activity_id="i123", rpe=6, name="Renamed run"))
    assert len(calls) == 1
    assert calls[0]["method"] == "PUT"
    assert calls[0]["url"] == "/activity/i123"
    assert calls[0]["data"] == {"icu_rpe": 6, "name": "Renamed run"}
    assert "Successfully updated activity i123" in result
    assert "Renamed run" in result
    assert "Updated: icu_rpe=6, name='Renamed run'" in result
    assert "6/10" in result


def test_update_activity_reports_api_values_not_stale_rpe(monkeypatch):
    """The confirmation lists API-returned values even if perceived_exertion is stale."""

    async def fake_request(*_args, **_kwargs):
        return {"id": "i123", "name": "Run", "icu_rpe": 7, "feel": 2, "perceived_exertion": 3}

    _patch_activity_request(monkeypatch, fake_request)
    result = asyncio.run(update_activity(activity_id="i123", rpe=7, feel=2))
    assert "Updated: icu_rpe=7, feel=2" in result


def test_update_activity_feel_and_description(monkeypatch):
    """Test update_activity maps feel and description without extra fields."""
    sent = {}

    async def fake_request(*_args, **kwargs):
        sent.update(kwargs["data"])
        return {"id": "i123", "name": "Run", "feel": 4}

    _patch_activity_request(monkeypatch, fake_request)
    result = asyncio.run(update_activity(activity_id="i123", feel=4, description="Tired legs"))
    assert sent == {"feel": 4, "description": "Tired legs"}
    assert "4/5" in result


def test_update_activity_no_fields(monkeypatch):
    """Test update_activity errors and does not call the API when nothing is passed."""

    async def fake_request(*_args, **_kwargs):
        raise AssertionError("API must not be called")

    _patch_activity_request(monkeypatch, fake_request)
    result = asyncio.run(update_activity(activity_id="i123"))
    assert "at least one" in result


def test_update_activity_invalid_ranges(monkeypatch):
    """Test update_activity validates rpe and feel ranges before calling the API."""

    async def fake_request(*_args, **_kwargs):
        raise AssertionError("API must not be called")

    _patch_activity_request(monkeypatch, fake_request)
    assert "rpe must be" in asyncio.run(update_activity(activity_id="i123", rpe=11))
    assert "rpe must be" in asyncio.run(update_activity(activity_id="i123", rpe=0))
    assert "feel must be" in asyncio.run(update_activity(activity_id="i123", feel=6))
    assert "feel must be" in asyncio.run(update_activity(activity_id="i123", feel=0))


def test_update_activity_empty_response(monkeypatch):
    """Test update_activity handles an empty API response."""

    async def fake_request(*_args, **_kwargs):
        return {}

    _patch_activity_request(monkeypatch, fake_request)
    result = asyncio.run(update_activity(activity_id="i123", rpe=5))
    assert "Unexpected response" in result


def test_update_activity_error(monkeypatch):
    """Test update_activity handles API errors."""

    async def fake_request(*_args, **_kwargs):
        return {"error": True, "message": "Activity not found"}

    _patch_activity_request(monkeypatch, fake_request)
    result = asyncio.run(update_activity(activity_id="i999", rpe=5))
    assert "Error updating activity" in result
    assert "Activity not found" in result




def _bulk_capture(monkeypatch, response):
    """Patch the events request function and capture the write calls.

    GETs are not recorded: before the write (duplicate check) the calendar is empty, after it
    (read-back) the events read back are the *response* of the write.
    """
    calls: list[dict] = []

    async def fake_request(*_args, **kwargs):
        if kwargs.get("method", "GET") == "GET":
            return response if calls else []
        calls.append(kwargs)
        return response

    monkeypatch.setattr("intervals_mcp_server.tools.events.make_intervals_request", fake_request)
    return calls


def test_add_events_bulk_happy_path(monkeypatch):
    """Valid entries are sent in one bulk request; ids are matched by order."""
    calls = _bulk_capture(monkeypatch, [{"id": 11}, {"id": 12}])
    result = json.loads(
        asyncio.run(
            add_events_bulk(
                athlete_id="i1",
                events=[
                    {
                        "name": "Easy run",
                        "start_date": "2025-01-06",
                        "workout_type": "Run",
                        "moving_time": 2700,
                        "workout_doc": {"steps": [{"duration": 600, "text": "Warmup"}]},
                    },
                    {
                        "name": "Rest",
                        "start_date": "2025-01-07",
                        "category": "NOTE",
                        "description": "Full rest",
                    },
                ],
            )
        )
    )
    assert len(calls) == 1
    assert calls[0]["url"] == "/athlete/i1/events/bulk"
    assert calls[0]["method"] == "POST"
    assert calls[0]["params"] == {"upsert": False, "upsertOnUid": False, "updatePlanApplied": False}
    body = calls[0]["data"]
    assert body[0]["category"] == "WORKOUT"
    assert body[0]["type"] == "Run"
    assert body[0]["start_date_local"] == "2025-01-06T00:00:00"
    assert body[0]["moving_time"] == 2700
    assert "Warmup" in body[0]["description"]
    assert body[1]["category"] == "NOTE"
    assert body[1]["description"] == "Full rest"
    assert [{k: v for k, v in row.items() if k != "warnings"} for row in result["created"]] == [
        {"index": 0, "status": "created", "id": 11, "date": "2025-01-06", "name": "Easy run"},
        {"index": 1, "status": "created", "id": 12, "date": "2025-01-07", "name": "Rest"},
    ]
    # The mocked read-back returns the events without their parsed steps: the warning says so.
    assert result["created"][0]["warnings"] == ["no workout steps stored (1 sent): Intervals.icu did not read the text as a workout"]
    assert result["errors"] == [] and result["refused"] == []


def test_add_events_bulk_matches_single_event_body(monkeypatch):
    """Bulk workout body is identical to the body add_or_update_event sends."""
    calls = _bulk_capture(monkeypatch, [{"id": 1}])
    asyncio.run(
        add_or_update_event(
            athlete_id="i1", start_date="2025-01-06", name="Easy run", workout_type="Run"
        )
    )
    asyncio.run(
        add_events_bulk(
            athlete_id="i1",
            events=[{"name": "Easy run", "start_date": "2025-01-06", "workout_type": "Run"}],
        )
    )
    assert calls[0]["data"] == calls[1]["data"][0]


def test_add_events_bulk_invalid_entries_not_sent(monkeypatch):
    """If any entry is invalid, nothing is sent and all errors are reported per index."""
    calls = _bulk_capture(monkeypatch, [{"id": 5}])
    result = json.loads(
        asyncio.run(
            add_events_bulk(
                athlete_id="i1",
                events=[
                    {"name": "Bad date", "start_date": "06.01.2025", "workout_type": "Run"},
                    {"name": "Ok", "start_date": "2025-01-06", "workout_type": "Run"},
                    {"start_date": "2025-01-06", "workout_type": "Run"},
                    {"name": "No type", "start_date": "2025-01-06"},
                    {"name": "Note", "start_date": "2025-01-06", "category": "NOTE"},
                    {"name": "Typo", "start_date": "2025-01-06", "workout_type": "Run", "foo": 1},
                    {"name": "Color", "start_date": "2025-01-06", "workout_type": "Run", "color": "red"},
                    {"name": "N", "start_date": "2025-01-06", "category": "NOTE", "description": "d", "distance": 5},
                    {
                        "name": "Both",
                        "start_date": "2025-01-06",
                        "workout_type": "Run",
                        "description": "x",
                        "workout_doc": {"steps": []},
                    },
                    {
                        "name": "BadDoc",
                        "start_date": "2025-01-06",
                        "workout_type": "Run",
                        "workout_doc": {"steps": [42]},
                    },
                ],
            )
        )
    )
    assert calls == []
    assert result["created"] == []
    assert result["created_count"] == 0
    assert [e["index"] for e in result["errors"]] == [0, 2, 3, 4, 5, 6, 7, 8, 9]


def test_add_events_bulk_reports_all_problems_of_an_entry(monkeypatch):
    """Every problem of one entry is reported, not only the first one."""
    calls = _bulk_capture(monkeypatch, [])
    result = json.loads(
        asyncio.run(
            add_events_bulk(
                athlete_id="i1",
                events=[{"name": "Missing date and type", "moving_time": "1h"}],
            )
        )
    )
    assert calls == []
    error = result["errors"][0]["error"]
    assert "'start_date' is required" in error
    assert "'workout_type' is required for category WORKOUT" in error
    assert "'moving_time' must be an integer" in error


def test_add_events_bulk_workout_description(monkeypatch):
    """A WORKOUT description without workout_doc is sent as the workout text."""
    calls = _bulk_capture(monkeypatch, [{"id": 1, "name": "Server name", "start_date_local": "2025-01-06T00:00:00"}])
    result = json.loads(
        asyncio.run(
            add_events_bulk(
                athlete_id="i1",
                events=[
                    {
                        "name": "Mine",
                        "start_date": "2025-01-06",
                        "workout_type": "Run",
                        "description": "- 10m 60%",
                    }
                ],
            )
        )
    )
    assert calls[0]["data"][0]["description"] == "- 10m 60%"
    assert result["created"][0]["name"] == "Server name"
    assert result["created"][0]["date"] == "2025-01-06"


def test_add_events_bulk_all_invalid_makes_no_request(monkeypatch):
    """No API call is made when every entry is invalid."""
    calls = _bulk_capture(monkeypatch, [])
    result = json.loads(asyncio.run(add_events_bulk(athlete_id="i1", events=[{"name": "x"}])))
    assert calls == []
    assert result["created"] == []
    assert result["error_count"] == 1


def test_add_events_bulk_empty_list(monkeypatch):
    """An empty list returns an error without a request."""
    calls = _bulk_capture(monkeypatch, [])
    result = asyncio.run(add_events_bulk(athlete_id="i1", events=[]))
    assert result.startswith("Error")
    assert calls == []


def test_add_events_bulk_api_error(monkeypatch):
    """A failing bulk request is reported as an API error."""
    _bulk_capture(monkeypatch, {"error": True, "message": "boom"})
    result = asyncio.run(
        add_events_bulk(
            athlete_id="i1",
            events=[{"name": "Ok", "start_date": "2025-01-06", "workout_type": "Run"}],
        )
    )
    assert "Error creating events in bulk: boom" in result
    assert "may have been partially or fully created" in result


def test_get_activities_never_returns_activities_outside_the_range(monkeypatch):
    """Upstream mvilanova/intervals-mcp-server#134: with fewer named activities than the limit no
    activities from before start_date are added; every result lies in the requested local dates."""
    calls = []
    window = [
        {"id": "i3", "name": "Ride in range", "type": "Ride", "start_date_local": "2026-10-05T08:00:00"},
        {"id": "i4", "name": "Unnamed", "type": "Walk", "start_date_local": "2026-10-06T08:00:00"},
        {"id": "i5", "name": "Stray before", "type": "Ride", "start_date_local": "2026-09-20T08:00:00"},
    ]

    async def fake_request(url=None, **kwargs):
        calls.append(kwargs.get("params") or {})
        if url and "/gear" in url:
            return []
        params = kwargs.get("params") or {}
        if params.get("newest", "") < "2026-10-01":  # a request for an earlier range
            return [{"id": "i1", "name": "Old ride", "type": "Ride", "start_date_local": "2026-09-01T08:00:00"}]
        return window

    for target in ("intervals_mcp_server.api.client.make_intervals_request",
                   "intervals_mcp_server.tools.activities.make_intervals_request",
                   "intervals_mcp_server.tools.gear.make_intervals_request"):
        monkeypatch.setattr(target, fake_request)
    result = asyncio.run(get_activities(athlete_id="1", start_date="2026-10-01", end_date="2026-10-09", limit=5))
    assert "Ride in range" in result
    assert "Old ride" not in result and "Stray before" not in result
    assert all(c.get("oldest", "2026-10-01") >= "2026-10-01" for c in calls if "oldest" in c)
    assert "Note: 1 named activities between 2026-10-01 and 2026-10-09 (fewer than the limit 5" in result
    assert "1 unnamed hidden" in result
    filtered = asyncio.run(get_activities(athlete_id="1", start_date="2026-10-01", end_date="2026-10-09", detail_level="compact",
                                          include_unnamed=True))
    assert "Stray before" not in filtered and "Ride in range" in filtered and "Unnamed" in filtered
    # When the API stops at the request limit the whole range is fetched again, never an earlier range.
    window[:] = [{"id": f"u{i}", "name": "Unnamed", "type": "Walk", "start_date_local": "2026-10-02T08:00:00"} for i in range(15)]
    window.append({"id": "i9", "name": "Late named", "type": "Ride", "start_date_local": "2026-10-03T08:00:00"})
    calls.clear()
    paged = asyncio.run(get_activities(athlete_id="1", start_date="2026-10-01", end_date="2026-10-09", limit=5))
    assert "Late named" in paged
    activity_calls = [c for c in calls if "oldest" in c]
    assert [c.get("limit") for c in activity_calls] == [15, None] and all(c["oldest"] == "2026-10-01" for c in activity_calls)
