"""
Small per-process caches with a time to live.

The athlete profile, sport settings, custom item definitions and the gear catalog are
cached so that every activity listing does not refetch them. Entries expire after a TTL,
so changes made in the Intervals.icu web app show up without a restart, and the tools
that change the cached data drop the affected entries. Keys combine the athlete id with
a fingerprint of the API key, so a call with another account's key never sees the first
account's data.
"""

import hashlib
import time
from typing import Any, Generic, TypeVar

V = TypeVar("V")

DEFAULT_KEY = "default"


def cache_key(athlete_id: Any, api_key: str | None = None) -> tuple[str, str]:
    """(athlete id, API key fingerprint); the configured key and None share one entry."""
    from intervals_mcp_server.config import get_config  # pylint: disable=import-outside-toplevel

    if not api_key or api_key == get_config().api_key:
        return str(athlete_id), DEFAULT_KEY
    return str(athlete_id), hashlib.sha256(api_key.encode()).hexdigest()[:16]


class TTLCache(Generic[V]):
    """Dict-like cache whose entries expire ``ttl_s`` seconds after they were stored."""

    def __init__(self, ttl_s: float, max_entries: int = 64) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._data: dict[Any, tuple[float, V]] = {}

    def get(self, key: Any) -> V | None:
        """The live value for key, or None (expired entries are dropped)."""
        item = self._data.get(key)
        if item is None:
            return None
        stored, value = item
        if time.monotonic() - stored > self.ttl_s:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: Any, value: V) -> None:
        """Store a value (the oldest entry is evicted when the cache is full)."""
        if key not in self._data and len(self._data) >= self.max_entries:
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        self._data[key] = (time.monotonic(), value)

    def pop(self, key: Any, default: V | None = None) -> V | None:
        """Remove and return an entry."""
        item = self._data.pop(key, None)
        return default if item is None else item[1]

    def drop_athlete(self, athlete_id: Any) -> None:
        """Drop every entry of an athlete (all API keys)."""
        for key in [k for k in self._data if isinstance(k, tuple) and k and k[0] == str(athlete_id)]:
            self._data.pop(key, None)

    def clear(self) -> None:
        """Drop everything."""
        self._data.clear()

    def __contains__(self, key: Any) -> bool:
        return self.get(key) is not None

    def __len__(self) -> int:
        return len(self._data)
