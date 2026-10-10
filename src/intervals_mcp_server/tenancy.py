"""Optional multi-user mode (``MCP_TENANCY=multi``): one server, several athletes.

In the default single-user mode every tool call uses the server's ``API_KEY`` and ``ATHLETE_ID``.
In multi-user mode the server keeps, per OAuth grant, the credential of the athlete who
connected, and every Intervals.icu request of a tool call uses the credential of the
*calling* connection:

* the athlete's own Intervals.icu access token (``Authorization: Bearer``) from the
  "Sign in with Intervals.icu" step, stored encrypted in the OAuth state file, or
* the server's ``API_KEY``, but only for the owner (``ATHLETE_ID``) and only when the owner
  signed in (password, API key or Intervals.icu as the owner athlete).

The credential travels in a context variable that :class:`~intervals_mcp_server.mcp_instance.
IntervalsFastMCP` sets from the MCP access token of the request; it is never a tool argument.
This module holds that context variable and everything that is decided per request:

* :func:`athlete_argument` - an ``athlete_id`` argument may only name the connection's own
  athlete (``0`` and ``i0`` are aliases of it); anything else is refused before any request;
* :func:`request_refusal` - the API path must belong to the connection's athlete and the
  connection's Intervals.icu scopes must cover the request;
* :class:`RequestBudgets` - a per-athlete daily soft budget and a shared 15-minute budget for
  all OAuth-token connections (the Intervals.icu app limit is shared by every athlete);
* the Intervals.icu OAuth scopes requested for the permission classes of a connection.

The mode is read from the environment on every check and anything other than ``single``
(or empty) counts as multi-user, so a typo can never fall back to the owner's API key: the
server refuses such a value at startup, and at runtime a request without a connection
credential is refused.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "AREA_ORDER",
    "BUDGETS",
    "CLASS_SCOPES",
    "Credential",
    "RequestBudgets",
    "TENANCY_MODES",
    "athlete_argument",
    "budget_settings",
    "canonical_athlete_id",
    "connection_athlete",
    "current_credential",
    "default_athlete",
    "intervals_scopes_for",
    "multi_user",
    "parse_intervals_scopes",
    "rejected_token_message",
    "request_refusal",
    "required_scope",
    "same_athlete",
    "scope_satisfied",
    "tenancy_from_env",
    "use_credential",
]

TENANCY_MODES = ("single", "multi")

# --------------------------------------------------------------------------- #
# Mode
# --------------------------------------------------------------------------- #


def tenancy_from_env(environ: Mapping[str, str] | None = None) -> str:
    """``single`` (default) or ``multi`` from ``MCP_TENANCY``; raise ValueError for anything else."""
    env = os.environ if environ is None else environ
    raw = env.get("MCP_TENANCY", "").strip().lower() or "single"
    if raw not in TENANCY_MODES:
        raise ValueError(f"MCP_TENANCY must be 'single' or 'multi', got {raw!r}")
    return raw


def multi_user(environ: Mapping[str, str] | None = None) -> bool:
    """True unless ``MCP_TENANCY`` is empty or ``single`` (fail closed: an unknown value is multi-user)."""
    env = os.environ if environ is None else environ
    return env.get("MCP_TENANCY", "").strip().lower() not in ("", "single")


# --------------------------------------------------------------------------- #
# Athlete ids
# --------------------------------------------------------------------------- #

_ALIASES = frozenset({"0", "i0"})


def _plain(value: Any) -> str:
    text = str(value).strip().lower()
    return text[1:] if text.startswith("i") and text[1:].isdigit() else text


def canonical_athlete_id(value: Any) -> str:
    """The form Intervals.icu paths accept: ``219504``, ``I219504`` and ``i219504`` become ``i219504``.

    (``/athlete/219504/...`` answers 404; the ``i`` prefix is required.) Other values are
    returned stripped and unchanged.
    """
    plain = _plain(value)
    return f"i{plain}" if plain.isdigit() else str(value).strip()


def same_athlete(first: Any, second: Any) -> bool:
    """True when both values name the same athlete (``i123`` == ``I123`` == ``123``)."""
    a, b = _plain(first), _plain(second)
    return bool(a) and a == b


# --------------------------------------------------------------------------- #
# The credential of the current connection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Credential:
    """How the requests of one connection are authenticated at Intervals.icu.

    ``kind`` is ``bearer`` (the athlete's own Intervals.icu OAuth token) or ``apikey`` (the
    server's API key, only ever for the owner). ``intervals_scopes`` is None for the API key
    (no OAuth scope limits). The secret is kept out of ``repr()``.
    """

    athlete_id: str
    kind: str
    secret: str = field(repr=False)
    grant_id: str | None = None
    intervals_scopes: frozenset[str] | None = None
    owner: bool = False

    @property
    def partition(self) -> str:
        """Cache partition: the owner's API key shares the single-user partition, every grant has its own."""
        return "default" if self.kind == "apikey" else f"grant:{self.grant_id}"

    @property
    def description(self) -> str:
        """Human readable kind for the status report (never the secret)."""
        return "server API key (owner)" if self.kind == "apikey" else "Intervals.icu sign-in (OAuth token)"


_CREDENTIAL: ContextVar[Credential | None] = ContextVar("intervals_connection_credential", default=None)


def current_credential() -> Credential | None:
    """The credential of the connection whose request is being handled, or None."""
    return _CREDENTIAL.get()


@contextmanager
def use_credential(credential: Credential | None) -> Iterator[None]:
    """Run the block with *credential* as the connection credential (tool calls, resources, --doctor)."""
    token = _CREDENTIAL.set(credential)
    try:
        yield
    finally:
        _CREDENTIAL.reset(token)


def connection_athlete() -> str | None:
    """The athlete of the current connection in multi-user mode, else None (single-user mode)."""
    credential = _CREDENTIAL.get()
    return credential.athlete_id if credential is not None and multi_user() else None


def default_athlete(configured: str | None) -> str:
    """The athlete a tool uses when none is given: the connection's own in multi-user mode, else *configured*."""
    return connection_athlete() or (configured or "")


def athlete_argument(value: Any, credential: Credential) -> tuple[str, str | None]:
    """(athlete id to use, error) for an ``athlete_id`` tool argument in multi-user mode.

    Empty means the connection's own athlete; ``0`` / ``i0`` are aliases of it (Intervals.icu
    resolves them to the token's athlete). Any other athlete is refused before a request is sent.
    """
    own = credential.athlete_id
    if value is None or (isinstance(value, str) and not value.strip()):
        return own, None
    text = str(value).strip()
    if text.lower() in _ALIASES or same_athlete(text, own):
        return own, None
    return own, (
        f"Error: athlete_id {text!r} is not the athlete of this connection. On this shared server a "
        f"connection can only access its own Intervals.icu account ({own}); leave athlete_id empty."
    )


# --------------------------------------------------------------------------- #
# Intervals.icu OAuth scopes
# --------------------------------------------------------------------------- #

# Areas of the Intervals.icu OAuth scopes (each with :READ or :WRITE; WRITE implies READ).
AREA_ORDER = ("ACTIVITY", "WELLNESS", "CALENDAR", "LIBRARY", "SETTINGS", "CHATS")

# What each permission class of this server needs at Intervals.icu. read: every read tool
# (activity comments are CHATS); write: activity name/RPE/feel and comments, wellness,
# calendar entries and library workouts; destructive: deleting events, library workouts and
# custom items; admin: sport settings, custom items and bulk event creation.
CLASS_SCOPES: dict[str, tuple[str, ...]] = {
    "read": ("ACTIVITY:READ", "WELLNESS:READ", "CALENDAR:READ", "LIBRARY:READ", "SETTINGS:READ", "CHATS:READ"),
    "write": ("ACTIVITY:WRITE", "WELLNESS:WRITE", "CALENDAR:WRITE", "LIBRARY:WRITE", "CHATS:WRITE"),
    "destructive": ("CALENDAR:WRITE", "LIBRARY:WRITE", "SETTINGS:WRITE"),
    "admin": ("CALENDAR:WRITE", "SETTINGS:WRITE"),
}

_SCOPE = re.compile(r"^([A-Z]+):(READ|WRITE)$")


def parse_intervals_scopes(text: str | Iterable[str] | None) -> frozenset[str]:
    """Scopes from a comma (or space) separated string such as ``ACTIVITY:READ,WELLNESS:WRITE``."""
    if text is None:
        return frozenset()
    items = re.split(r"[,\s]+", text) if isinstance(text, str) else list(text)
    return frozenset(item.strip().upper() for item in items if _SCOPE.match(item.strip().upper()))


def scope_satisfied(granted: Iterable[str], required: str) -> bool:
    """True when *granted* covers *required* (``X:WRITE`` covers ``X:READ``)."""
    granted_set = set(granted)
    if required in granted_set:
        return True
    area, _, access = required.partition(":")
    return access == "READ" and f"{area}:WRITE" in granted_set


def intervals_scopes_for(classes: Iterable[str], exclude_areas: Iterable[str] = ()) -> str:
    """The Intervals.icu ``scope`` parameter for the granted permission classes.

    ``X:READ`` is dropped where ``X:WRITE`` is requested (WRITE implies READ); areas in
    *exclude_areas* (``INTERVALS_OAUTH_EXCLUDE_AREAS``) are never requested.
    """
    excluded = {area.strip().upper() for area in exclude_areas}
    wanted: set[str] = set()
    for permission in classes:
        wanted.update(CLASS_SCOPES.get(permission, ()))
    result = []
    for area in AREA_ORDER:
        if area in excluded:
            continue
        if f"{area}:WRITE" in wanted:
            result.append(f"{area}:WRITE")
        elif f"{area}:READ" in wanted:
            result.append(f"{area}:READ")
    return ",".join(result)


# Area of the endpoints under /athlete/{id}/<segment> (the segment without an extension).
_ATHLETE_AREAS: dict[str, str] = {
    "": "SETTINGS",
    "profile": "SETTINGS",
    "sport-settings": "SETTINGS",
    "settings": "SETTINGS",
    "custom-item": "SETTINGS",
    "custom-item-indexes": "SETTINGS",
    "gear": "SETTINGS",
    "weather-config": "SETTINGS",
    "connections": "SETTINGS",
    "activities": "ACTIVITY",
    "activities-around": "ACTIVITY",
    "activity-tags": "ACTIVITY",
    "activity-hr-curves": "ACTIVITY",
    "activity-pace-curves": "ACTIVITY",
    "activity-power-curves": "ACTIVITY",
    "power-curves": "ACTIVITY",
    "hr-curves": "ACTIVITY",
    "pace-curves": "ACTIVITY",
    "power-hr-curve": "ACTIVITY",
    "mmp-model": "ACTIVITY",
    "routes": "ACTIVITY",
    "athlete-summary": "ACTIVITY",
    "download-fit-files": "ACTIVITY",
    "events": "CALENDAR",
    "event-tags": "CALENDAR",
    "fitness-model-events": "CALENDAR",
    "duplicate-events": "CALENDAR",
    "training-plan": "CALENDAR",
    "weather-forecast": "CALENDAR",
    "wellness": "WELLNESS",
    "wellness-bulk": "WELLNESS",
    "workouts": "LIBRARY",
    "folders": "LIBRARY",
    "workout-tags": "LIBRARY",
    "download-workout": "LIBRARY",
    "duplicate-workouts": "LIBRARY",
    "apply-plan-changes": "LIBRARY",
    "chats": "CHATS",
    "groups": "CHATS",
}


def required_scope(method: str, path: str) -> str | None:
    """The Intervals.icu scope a request needs, or None when this server does not know it.

    GET needs ``<AREA>:READ``, every other method ``<AREA>:WRITE``. Unknown endpoints are
    left to Intervals.icu (it answers 403 when the token lacks the scope).
    """
    parts = [part for part in path.split("?", 1)[0].split("/") if part]
    access = "READ" if method.upper() == "GET" else "WRITE"
    if len(parts) >= 2 and parts[0] == "activity":
        area = "CHATS" if len(parts) >= 3 and parts[2] == "messages" else "ACTIVITY"
        return f"{area}:{access}"
    if len(parts) >= 2 and parts[0] == "athlete":
        segment = parts[2].split(".", 1)[0] if len(parts) >= 3 else ""
        known = _ATHLETE_AREAS.get(segment)
        return f"{known}:{access}" if known else None
    return None


# Classes that bring each scope, for the "reconnect and allow ..." hint.
def _classes_for(scope: str) -> list[str]:
    return [cls for cls, scopes in CLASS_SCOPES.items() if any(scope_satisfied([s], scope) for s in scopes)]


def request_refusal(method: str, url: str, credential: Credential) -> str | None:
    """Why a request must not be sent for *credential* in multi-user mode, or None.

    Only the connection athlete's own endpoints are reachable: ``/athlete/<own id or 0>/...``
    and ``/activity/<id>/...`` (Intervals.icu checks with the connection's own token or, for
    the owner, the owner's key that the activity is theirs). Everything else (other athletes,
    ``/athletes``, chats, shared events) is refused here. The connection's Intervals.icu
    scopes must cover the request.
    """
    parts = [part for part in url.split("?", 1)[0].split("/") if part]
    if len(parts) < 2 or parts[0] not in ("athlete", "activity"):
        return f"Not sent: the endpoint {'/' + '/'.join(parts[:1])} is not available on this shared server."
    if parts[0] == "athlete":
        athlete = parts[1]
        if athlete.lower() not in _ALIASES and not same_athlete(athlete, credential.athlete_id):
            return (
                "Not sent: this request names another athlete. On this shared server a connection can only "
                f"access its own Intervals.icu account ({credential.athlete_id})."
            )
    if credential.intervals_scopes is not None:
        needed = required_scope(method, url)
        if needed and not scope_satisfied(credential.intervals_scopes, needed):
            classes = _classes_for(needed)
            allow = f" and allow '{classes[0]}'" if classes else ""
            return (
                f"Not sent: this connection's Intervals.icu sign-in does not include the permission {needed}, which "
                f"this request needs. Disconnect and reconnect the server in your MCP client{allow} on the consent "
                "page (your most recent Intervals.icu sign-in decides the permissions of all your connections)."
            )
    return None


def rejected_token_message(status: int) -> str:
    """Message for a 401 / 403 answer to a request made with an athlete's OAuth token."""
    if status == 401:
        return (
            "401 Unauthorized: Intervals.icu no longer accepts this connection's sign-in (the app may have been "
            "disconnected in Intervals.icu). Disconnect and reconnect the server in your MCP client to sign in "
            "with Intervals.icu again."
        )
    return (
        "403 Forbidden: Intervals.icu refused this request for this connection's sign-in: a permission may be "
        "missing or the access was revoked. Disconnect and reconnect the server in your MCP client and allow "
        "the permission on the consent page."
    )


# --------------------------------------------------------------------------- #
# Request budgets
# --------------------------------------------------------------------------- #

DEFAULT_ATHLETE_DAILY_REQUESTS = 1000
DEFAULT_APP_REQUESTS_PER_15MIN = 2000
APP_WINDOW_S = 15 * 60


def _count_setting(env: Mapping[str, str], name: str, default: int) -> tuple[int, str | None]:
    raw = env.get(name, "").strip()
    if not raw:
        return default, None
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        return default, f"{name} must be a whole number >= 0 (0 = no limit), got {raw!r}; the server uses {default}"
    return value, None


def budget_settings(environ: Mapping[str, str] | None = None) -> tuple[int, int, list[str]]:
    """(per-athlete daily requests, shared requests per 15 minutes, errors) as the server applies them."""
    env = os.environ if environ is None else environ
    daily, daily_error = _count_setting(env, "MCP_ATHLETE_DAILY_REQUESTS", DEFAULT_ATHLETE_DAILY_REQUESTS)
    window, window_error = _count_setting(env, "MCP_APP_REQUESTS_PER_15MIN", DEFAULT_APP_REQUESTS_PER_15MIN)
    return daily, window, [error for error in (daily_error, window_error) if error]


class RequestBudgets:
    """Soft request budgets of the OAuth-token connections (in memory; a restart resets them).

    Intervals.icu limits an OAuth app as a whole, so every athlete's requests count against
    one shared limit. Each athlete gets a daily budget (UTC day, ``MCP_ATHLETE_DAILY_REQUESTS``)
    and all of them together a 15-minute budget (``MCP_APP_REQUESTS_PER_15MIN``). The owner's
    API key is not an OAuth token and is not counted. The per-call budget
    (``MCP_TOOL_MAX_REQUESTS``) applies on top.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._daily: dict[str, tuple[str, int]] = {}
        self._window: tuple[int, int] = (-1, 0)

    def _day(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).date().isoformat()

    def used_today(self, athlete_id: str) -> int:
        """Requests of *athlete_id* counted today (UTC)."""
        day, count = self._daily.get(_plain(athlete_id), ("", 0))
        return count if day == self._day() else 0

    def admit(self, credential: Credential, environ: Mapping[str, str] | None = None) -> str | None:
        """Count one request of *credential*, or say why it may not be sent."""
        if credential.kind != "bearer":
            return None
        daily, window, _ = budget_settings(environ)
        key = _plain(credential.athlete_id)
        with self._lock:
            today = self._day()
            day, count = self._daily.get(key, (today, 0))
            if day != today:
                count = 0
            bucket = int(self._clock() // APP_WINDOW_S)
            current, in_window = self._window
            if current != bucket:
                in_window = 0
            if daily and count >= daily:
                return (
                    f"Not sent: your daily budget of {daily} Intervals.icu API requests on this shared server is used "
                    "up; it resets at 00:00 UTC. Narrow the requests (shorter date ranges, fewer activities)."
                )
            if window and in_window >= window:
                return (
                    f"Not sent: this shared server reached its limit of {window} Intervals.icu API requests per 15 "
                    "minutes for all connected athletes together. Try again in a few minutes."
                )
            self._daily = {k: v for k, v in self._daily.items() if v[0] == today}
            self._daily[key] = (today, count + 1)
            self._window = (bucket, in_window + 1)
        return None

    def reset(self) -> None:
        """Forget all counts (tests)."""
        with self._lock:
            self._daily.clear()
            self._window = (-1, 0)


BUDGETS = RequestBudgets()
