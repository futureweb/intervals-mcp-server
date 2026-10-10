"""
Date utility functions for Intervals.icu MCP Server.

This module provides helper functions for date parsing and default date calculations.

"Today" is the athlete's local day, not the server's: the Docker image runs in UTC, so
between midnight and 02:00 CEST the server clock still shows yesterday. ``athlete_today()``
is the one helper every default date and "today" in the tools goes through. The time zone
comes from (in this order) the tool call (set by the tool wrapper from the athlete profile's
``timezone``), the ``ATHLETE_TIMEZONE`` environment variable, or the server clock.
"""

import logging
import os
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("intervals_icu_mcp_server")

# Values of ATHLETE_TIMEZONE that mean "use the server clock, do not look the zone up".
SERVER_CLOCK = ("server", "local")

_ACTIVE_TIMEZONE: ContextVar[str | None] = ContextVar("athlete_timezone", default=None)

# Looks up the IANA time zone of an athlete: async (athlete_id) -> name or None.
# Registered by tools.athlete (which owns the cached athlete profile) to avoid an import cycle.
TimezoneResolver = Callable[[str], Awaitable[str | None]]
_RESOLVER: TimezoneResolver | None = None  # pylint: disable=invalid-name


def _zone(name: str | None) -> ZoneInfo | None:
    if not name or name.strip().lower() in SERVER_CLOCK:
        return None
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown time zone %r; using the server clock", name)
        return None


def configured_timezone() -> str | None:
    """The ATHLETE_TIMEZONE setting (an IANA name such as Europe/Vienna, or 'server'), if set."""
    value = os.environ.get("ATHLETE_TIMEZONE", "").strip()
    return value or None


def active_timezone() -> str | None:
    """Time zone used for "today" in the current tool call (None = server clock)."""
    for name in (_ACTIVE_TIMEZONE.get(), configured_timezone()):
        if _zone(name) is not None:
            return name
    return None


def athlete_today() -> date:
    """Today's date in the athlete's time zone (server clock when the zone is unknown)."""
    zone = _zone(active_timezone())
    return datetime.now(zone).date() if zone is not None else date.today()


def athlete_local_time(moment: datetime) -> datetime:
    """A point in time in the athlete's time zone (server local time when the zone is unknown).

    A naive datetime is taken as UTC (Intervals.icu timestamps are UTC).
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    zone = _zone(active_timezone())
    return moment.astimezone(zone) if zone is not None else moment.astimezone()


def set_timezone_resolver(resolver: TimezoneResolver | None) -> None:
    """Register the coroutine that looks up an athlete's time zone (tools.athlete does)."""
    global _RESOLVER  # pylint: disable=global-statement  # noqa: PLW0603 - registry set once at import
    _RESOLVER = resolver


async def activate_athlete_timezone(athlete_id: str | None) -> Token[str | None] | None:
    """Look up the athlete's time zone and use it for "today" in the current tool call.

    Nothing is looked up when ATHLETE_TIMEZONE is set (a zone name or 'server'), when there is
    no athlete id or no resolver. Returns the context token to pass to reset_athlete_timezone.
    """
    if configured_timezone() or not athlete_id or _RESOLVER is None:
        return None
    try:
        name = await _RESOLVER(athlete_id)
    except Exception:  # pylint: disable=broad-exception-caught  # a failed lookup must never break the tool
        logger.warning("Could not look up the athlete's time zone; using the server clock", exc_info=True)
        return None
    if _zone(name) is None:
        return None
    return _ACTIVE_TIMEZONE.set(name)


def reset_athlete_timezone(token: Token[str | None] | None) -> None:
    """Undo activate_athlete_timezone."""
    if token is not None:
        _ACTIVE_TIMEZONE.reset(token)


def get_default_start_date(days_ago: int = 30) -> str:
    """
    Get a default start date string in YYYY-MM-DD format.

    Args:
        days_ago: Number of days ago from today (athlete's local day). Defaults to 30.

    Returns:
        Date string in YYYY-MM-DD format.
    """
    return (athlete_today() - timedelta(days=days_ago)).isoformat()


def get_default_end_date() -> str:
    """
    Get today's date string (athlete's local day) in YYYY-MM-DD format.

    Returns:
        Date string in YYYY-MM-DD format.
    """
    return athlete_today().isoformat()


def get_default_future_end_date(days_ahead: int = 30) -> str:
    """
    Get a default future end date string in YYYY-MM-DD format.

    Args:
        days_ahead: Number of days ahead from today (athlete's local day). Defaults to 30.

    Returns:
        Date string in YYYY-MM-DD format.
    """
    return (athlete_today() + timedelta(days=days_ahead)).isoformat()


def parse_date_range(
    start_date: str | None, end_date: str | None, default_start_days_ago: int = 30
) -> tuple[str, str]:
    """
    Parse and validate a date range, providing defaults if needed.

    Args:
        start_date: Start date in YYYY-MM-DD format (optional).
        end_date: End date in YYYY-MM-DD format (optional).
        default_start_days_ago: Number of days ago for default start date. Defaults to 30.

    Returns:
        Tuple of (start_date, end_date) as strings in YYYY-MM-DD format.
    """
    if not start_date:
        start_date = get_default_start_date(default_start_days_ago)
    if not end_date:
        end_date = get_default_end_date()
    return start_date, end_date
