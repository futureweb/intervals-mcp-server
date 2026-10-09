"""
Gear-related MCP tools for Intervals.icu.

This module provides:
- A module-level cache of the athlete's raw gear catalog (bikes, shoes, etc.)
  to avoid hitting the /athlete/{id}/gear endpoint on every activity lookup.
- A helper to inject the human-readable gear name into an activity dict (under
  `_resolved_gear_name`), which the formatter then displays in the `Gear:` block.
- A user-facing MCP tool `get_gear_list` so the assistant can discover or
  refresh the gear catalog on demand.

Intervals.icu's activity payload includes only the gear ID (e.g. `b16177481`)
but not the gear name. The gear name lives in a separate endpoint
`/athlete/{athlete_id}/gear` that returns the full catalog. To avoid an extra
round-trip per activity, we cache the raw gear catalog per athlete for the
lifetime of the MCP server process and derive the `{id: name}` lookup from it.
Call `get_gear_list(refresh=True)` to bust the cache.
"""

from typing import Any

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.utils.validation import resolve_athlete_id

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import mcp  # noqa: F401

config = get_config()

# Module-level cache of the raw gear catalog per athlete. Single source of
# truth: the id->name map and the rich listing are both derived from this.
_GEAR_RAW_CACHE: dict[str, list[dict[str, Any]]] = {}


def _extract_gear_id(activity: dict[str, Any]) -> str | None:
    """Pull the gear ID out of an activity dict, handling the two known shapes."""
    gear_raw = activity.get("gear")
    if isinstance(gear_raw, dict):
        gear_id = gear_raw.get("id")
        if gear_id:
            return str(gear_id)
    gear_id = activity.get("gear_id")
    if gear_id:
        return str(gear_id)
    return None


def _items_from_response(result: Any) -> list[dict[str, Any]]:
    """Normalize the /athlete/{id}/gear response into a list of gear dicts."""
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        # Some endpoints wrap the list in a container; pull any list value.
        for value in result.values():
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _derive_gear_map(items: list[dict[str, Any]]) -> dict[str, str]:
    """Convert a raw gear list into a {gear_id: gear_name} lookup."""
    gear_map: dict[str, str] = {}
    for item in items:
        gid = item.get("id")
        name = item.get("name") or item.get("display_name")
        if gid and name:
            gear_map[str(gid)] = str(name)
    return gear_map


async def get_gear_raw(
    athlete_id: str | None = None,
    api_key: str | None = None,
    *,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Return (and cache) the raw gear list for an athlete.

    Single source of truth that backs both the id->name map and the rich
    listing produced by `get_gear_list`. One API call per athlete per process
    lifetime unless `refresh=True`.

    Args:
        athlete_id: Athlete to look up. Defaults to ATHLETE_ID env var via config.
        api_key: Override the configured API key.
        refresh: If True, ignore the cache and re-fetch from the API.
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg or not athlete_id_to_use:
        return []

    if not refresh and athlete_id_to_use in _GEAR_RAW_CACHE:
        return _GEAR_RAW_CACHE[athlete_id_to_use]

    result = await make_intervals_request(
        url=f"/athlete/{athlete_id_to_use}/gear", api_key=api_key
    )
    items = _items_from_response(result)
    _GEAR_RAW_CACHE[athlete_id_to_use] = items
    return items


async def get_gear_map(
    athlete_id: str | None = None,
    api_key: str | None = None,
    *,
    refresh: bool = False,
) -> dict[str, str]:
    """Return the {gear_id: gear_name} lookup for an athlete (derived from cache)."""
    items = await get_gear_raw(athlete_id=athlete_id, api_key=api_key, refresh=refresh)
    return _derive_gear_map(items)


async def resolve_gear_for_activity(
    activity: dict[str, Any],
    athlete_id: str | None = None,
    api_key: str | None = None,
) -> None:
    """Inject `_resolved_gear_name` into an activity dict if gear info is present.

    Mutates the activity dict in place. Safe to call when gear is absent (no-op).
    Uses the cached gear map; the first call per athlete triggers a fetch.
    """
    gear_id = _extract_gear_id(activity)
    if not gear_id:
        return

    gear_map = await get_gear_map(athlete_id=athlete_id, api_key=api_key)
    name = gear_map.get(gear_id)
    if name:
        activity["_resolved_gear_name"] = name


async def resolve_gear_for_activities(
    activities: list[dict[str, Any]],
    athlete_id: str | None = None,
    api_key: str | None = None,
) -> None:
    """Inject `_resolved_gear_name` into each activity in a list. In-place."""
    if not activities:
        return
    # Pre-warm the cache once, then iterate.
    _ = await get_gear_map(athlete_id=athlete_id, api_key=api_key)
    for activity in activities:
        if isinstance(activity, dict):
            await resolve_gear_for_activity(
                activity, athlete_id=athlete_id, api_key=api_key
            )


@mcp.tool()
async def get_gear_list(
    athlete_id: str | None = None,
    api_key: str | None = None,
    refresh: bool = False,
) -> str:
    """Get the gear catalog (bikes, shoes, etc.) for an athlete from Intervals.icu.

    Returns one line per gear item with id, type, name, and basic stats.
    The result is cached for the MCP process lifetime; pass refresh=True to
    re-fetch.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        refresh: If True, bypass the cache and re-fetch from the API (default False)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if not athlete_id_to_use:
        return "Error: athlete_id is required (either as argument or via ATHLETE_ID env var)."

    # Single fetch path: get_gear_raw consults the cache and only hits the API
    # on a cold cache or when refresh=True.
    items = await get_gear_raw(
        athlete_id=athlete_id_to_use, api_key=api_key, refresh=refresh
    )

    if not items:
        return f"No gear found for athlete {athlete_id_to_use}."

    output = f"Gear catalog for athlete {athlete_id_to_use}:\n\n"
    output += f"{'ID':<14} {'Type':<8} {'Name':<32} {'Default':<8} {'Acts':<6} {'Dist (km)':<10} {'Retired':<8}\n"
    output += f"{'-' * 14} {'-' * 8} {'-' * 32} {'-' * 8} {'-' * 6} {'-' * 10} {'-' * 8}\n"
    for it in items:
        gid = str(it.get("id", "?"))
        gtype = str(it.get("component_type", it.get("type", "?")))
        name = str(it.get("name", "?"))[:32]
        default_for = it.get("default_for_type") or it.get("default_for") or ""
        acts = str(it.get("activities", it.get("activity_count", "?")))
        dist_m = it.get("distance", 0) or 0
        dist_km = f"{dist_m / 1000:.1f}" if isinstance(dist_m, (int, float)) else "?"
        retired = "yes" if it.get("retired") else ""
        output += f"{gid:<14} {gtype:<8} {name:<32} {str(default_for):<8} {acts:<6} {dist_km:<10} {retired:<8}\n"

    return output


def _gear_stats_line(item: dict[str, Any]) -> str:
    """'12498.4 km, 522.1 h, 215 activities' from a gear item."""
    dist_m = item.get("distance") or 0
    secs = item.get("time") or 0
    dist = f"{dist_m / 1000:.1f} km" if isinstance(dist_m, (int, float)) else "? km"
    hours = f"{secs / 3600:.1f} h" if isinstance(secs, (int, float)) else "? h"
    return f"{dist}, {hours}, {item.get('activities', '?')} activities"


@mcp.tool()
async def get_gear_details(
    gear_id: str,
    athlete_id: str | None = None,
    api_key: str | None = None,
    refresh: bool = False,
) -> str:
    """Get details of one gear item (bike, shoes, component) from Intervals.icu

    Shows distance, time and activity count, purchase date, retirement, notes, the activity
    type filters that assign activities to it automatically, reminders, and for a bike the
    list of its components (frame, power meter, chain, tyres ...) with their own mileage.
    Which power meter recorded a given activity is NOT derived from gear: use
    get_activity_details (power meter and serial come from the device file) for that.
    To list the activities done on this gear use get_activities(gear_id=...).

    Args:
        gear_id: The gear ID, e.g. "b12472159" for a bike or "30303" for a component
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        refresh: Re-fetch the gear catalog instead of using the cache (optional, default False)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    items = await get_gear_raw(athlete_id=athlete_id_to_use, api_key=api_key, refresh=refresh)
    by_id = {str(it.get("id")): it for it in items}
    item = by_id.get(str(gear_id))
    if item is None:
        return f"No gear with id {gear_id} found for athlete {athlete_id_to_use}."

    lines = [
        f"Gear {item.get('id')}: {item.get('name', '?')} ({item.get('type', '?')})",
        f"- Usage: {_gear_stats_line(item)}"
        + (" (elapsed time)" if item.get("use_elapsed_time") else " (moving time)"),
    ]
    if item.get("purchased"):
        lines.append(f"- Purchased: {item['purchased']}")
    if item.get("retired"):
        lines.append(f"- Retired: {item['retired']}")
    if item.get("notes"):
        lines.append(f"- Notes: {item['notes']}")
    if item.get("component"):
        parents = [
            f"{p.get('name')} ({p.get('id')})"
            for p in items
            if str(item.get("id")) in [str(c) for c in (p.get("component_ids") or [])]
        ]
        if parents:
            lines.append(f"- Component of: {', '.join(parents)}")
    filters = item.get("activity_filters")
    if isinstance(filters, list) and filters:
        parts = []
        for flt in filters:
            if isinstance(flt, dict):
                value = flt.get("value")
                value_text = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
                parts.append(f"{'not ' if flt.get('not') else ''}{flt.get('field_id')} in [{value_text}]")
        if parts:
            lines.append("- Auto-assignment filters: " + "; ".join(parts))
    reminders = item.get("reminders")
    if isinstance(reminders, list) and reminders:
        lines.append(f"- Reminders: {len(reminders)}")
    component_ids = item.get("component_ids") or []
    if component_ids:
        lines.append(f"- Components ({len(component_ids)}):")
        for cid in component_ids:
            comp = by_id.get(str(cid))
            if comp is None:
                lines.append(f"  - {cid}: (not in catalog)")
                continue
            retired = f", retired {comp['retired']}" if comp.get("retired") else ""
            lines.append(
                f"  - {comp.get('type', '?')} {comp.get('name', '?')} ({cid}): {_gear_stats_line(comp)}{retired}"
            )
    return "\n".join(lines)
