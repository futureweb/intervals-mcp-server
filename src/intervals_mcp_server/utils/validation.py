"""
Validation utilities for Intervals.icu MCP Server

This module contains validation functions for input parameters.
"""

import re
from datetime import date

from intervals_mcp_server.utils.dates import parse_date_range


def validate_athlete_id(athlete_id: str) -> None:
    """Validate that an athlete ID is in the correct format.

    Empty strings are allowed (meaning no default athlete ID is set).
    Non-empty athlete IDs must be all digits or start with 'i' followed by digits.

    Args:
        athlete_id: The athlete ID to validate.

    Raises:
        ValueError: If the athlete ID is not in the correct format.
    """
    if athlete_id and not re.fullmatch(r"i?\d+", athlete_id):
        raise ValueError(
            "ATHLETE_ID must be all digits (e.g. 123456) or start with 'i' followed by digits (e.g. i123456)"
        )


def validate_date(date_str: str) -> str:
    """Validate that a date string is in YYYY-MM-DD format.

    Args:
        date_str: The date string to validate.

    Returns:
        The validated date string if valid.

    Raises:
        ValueError: If the date string is not in YYYY-MM-DD format.
    """
    # strptime would also accept "2026-1-5", which then reaches the API unpadded.
    if not isinstance(date_str, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        raise ValueError("Invalid date format. Please use YYYY-MM-DD.")
    try:
        date.fromisoformat(date_str)
    except ValueError as exc:
        raise ValueError("Invalid date format. Please use YYYY-MM-DD.") from exc
    return date_str


def resolve_athlete_id(
    athlete_id: str | None, default_athlete_id: str = ""
) -> tuple[str, str | None]:
    """Resolve athlete ID from parameter or default, with error message if missing.

    In the multi-user mode the default is the athlete of the calling connection, never the
    configured ATHLETE_ID (the API client refuses requests for other athletes anyway).

    Args:
        athlete_id: Optional athlete ID parameter.
        default_athlete_id: Default athlete ID to use if athlete_id is None.

    Returns:
        Tuple of (athlete_id_to_use, error_message).
        athlete_id_to_use will be empty string if not found.
        error_message will be None if athlete_id is resolved successfully.
    """
    from intervals_mcp_server.tenancy import connection_athlete  # pylint: disable=import-outside-toplevel

    athlete_id_to_use = athlete_id if athlete_id is not None else (connection_athlete() or default_athlete_id)
    if not athlete_id_to_use:
        return (
            "",
            "Error: No athlete ID provided and no default ATHLETE_ID found in environment variables.",
        )
    return athlete_id_to_use, None


_TYPE_KEYWORDS = (
    ("Ride", ("bike", "biking", "cycle", "cycling", "ride", "riding")),
    ("Run", ("run", "running", "jog", "jogging", "marathon")),
    ("Swim", ("swim", "swimming", "pool")),
    ("Walk", ("walk", "walking", "hike", "hiking")),
    ("Row", ("row", "rowing")),
)


def infer_activity_type(name: str | None) -> str | None:
    """The sport named in an event name ("Easy run" -> Run), or None when no sport or more than
    one is named. Whole words only, so "Arrow-straight tempo" is not a Row."""
    words = set(re.findall(r"[a-z]+", (name or "").lower()))
    found = [sport for sport, keywords in _TYPE_KEYWORDS if words & set(keywords)]
    return found[0] if len(found) == 1 else None


def resolve_activity_type(name: str | None, activity_type: str | None = None) -> str:
    """Determine the activity type based on the name and provided value.

    If an explicit *activity_type* is given it is returned as-is.  Otherwise the
    *name* is searched for common keywords (whole words) to infer the type, defaulting to
    ``"Ride"`` when no match is found. Event creation uses infer_activity_type instead and
    asks for the type rather than defaulting.

    Args:
        name: An optional activity/event name to infer the type from.
        activity_type: An explicitly provided activity type.

    Returns:
        The resolved activity type string.
    """
    if activity_type:
        return activity_type
    return infer_activity_type(name) or "Ride"


def resolve_date_params(
    start_date: str | None,
    end_date: str | None,
    default_start_days_ago: int = 30,
) -> tuple[str, str]:
    """Resolve start and end date parameters with defaults.

    Args:
        start_date: Optional start date in YYYY-MM-DD format.
        end_date: Optional end date in YYYY-MM-DD format.
        default_start_days_ago: Number of days ago for default start date. Defaults to 30.

    Returns:
        Tuple of (start_date, end_date) as strings in YYYY-MM-DD format.
    """
    return parse_date_range(start_date, end_date, default_start_days_ago)
