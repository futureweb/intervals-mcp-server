"""
Custom items MCP tools for Intervals.icu.

This module contains tools for managing athlete custom items (charts, fields, zones, etc.)
and a per-process cache of the custom item definitions. The definitions are needed to
label custom activity fields, interval fields, streams and wellness fields (name, units,
select options), so they are fetched once per athlete (and API key) and reused by the other
tools for CUSTOM_ITEMS_CACHE_TTL_S; creating, changing or deleting an item drops the cache.
"""

import json
from typing import Any

from intervals_mcp_server.api import client as api_client
from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.utils.cache import TTLCache, cache_key
from intervals_mcp_server.utils.custom_fields import (
    CustomItemIndex,
    apply_units_overrides,
    infer_temperature_units,
    index_custom_items,
)
from intervals_mcp_server.utils.formatting import format_custom_item_details
from intervals_mcp_server.utils.validation import resolve_athlete_id

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

CUSTOM_ITEMS_CACHE_TTL_S = 1800
# Module-level cache of the raw custom item list per athlete. Errors are not cached.
_CUSTOM_ITEMS_CACHE: TTLCache[list[dict[str, Any]]] = TTLCache(CUSTOM_ITEMS_CACHE_TTL_S)


async def get_custom_items_raw(
    athlete_id: str,
    *,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Return (and cache) the raw custom item list for an athlete.

    One API call per athlete per CUSTOM_ITEMS_CACHE_TTL_S unless ``refresh=True`` or the
    previous fetch failed. The request is issued through ``api.client`` so that a
    monkeypatched client is honoured even when the caller patched only that module.
    """
    key = cache_key(athlete_id)
    cached = None if refresh else _CUSTOM_ITEMS_CACHE.get(key)
    if cached is not None:
        return cached

    result = await api_client.make_intervals_request(
        url=f"/athlete/{seg(athlete_id)}/custom-item"
    )
    if not isinstance(result, list):
        return []
    items = [item for item in result if isinstance(item, dict)]
    _CUSTOM_ITEMS_CACHE.set(key, items)
    return items


async def get_custom_item_index(
    athlete_id: str,
    *,
    refresh: bool = False,
) -> CustomItemIndex:
    """Custom item definitions grouped by item type and code (see utils.custom_fields).

    Temperature fields without units get the unit the other temperature fields agree on
    (units_source 'inferred'); operator-configured display units (CUSTOM_UNITS_OVERRIDES) are
    applied on top.
    """
    return _index(await get_custom_items_raw(athlete_id, refresh=refresh))


def cached_custom_item_index(athlete_id: str) -> CustomItemIndex | None:
    """The definitions as get_custom_item_index returns them if they are cached, else None (no request)."""
    items = _CUSTOM_ITEMS_CACHE.get(cache_key(athlete_id))
    return None if items is None else _index(items)


def _index(items: list[dict[str, Any]]) -> CustomItemIndex:
    index = index_custom_items(items)
    return apply_units_overrides(infer_temperature_units(index), get_config().custom_units_overrides)


def invalidate_custom_items_cache(athlete_id: str | None = None) -> None:  # pylint: disable=unused-argument
    """Drop the cached definitions after create/update/delete.

    The whole cache is dropped: the same athlete may be cached under an alias ("0") or
    another API key, and the cache is small.
    """
    _CUSTOM_ITEMS_CACHE.clear()


@tool("read")
async def get_custom_items(
    athlete_id: str | None = None,
) -> str:
    """Get custom items (charts, custom fields, zones, etc.) for an athlete from Intervals.icu

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/custom-item"
    )

    if isinstance(result, dict) and "error" in result:
        return f"Error fetching custom items: {result.get('message')}"

    if not result:
        return f"No custom items found for athlete {athlete_id_to_use}."

    if isinstance(result, list):
        _CUSTOM_ITEMS_CACHE.set(
            cache_key(athlete_id_to_use), [item for item in result if isinstance(item, dict)]
        )

    output = "Custom Items:\n\n"
    for item in result:
        if isinstance(item, dict):
            output += f"- ID: {item.get('id')}\n"
            output += f"  Name: {item.get('name', 'N/A')}\n"
            output += f"  Type: {item.get('type', 'N/A')}\n"
            if item.get("description"):
                output += f"  Description: {item['description']}\n"
            output += "\n"
    return output


@tool("read")
async def get_custom_item_by_id(
    item_id: int,
    athlete_id: str | None = None,
) -> str:
    """Get detailed information for a specific custom item from Intervals.icu

    Args:
        item_id: The custom item ID
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/custom-item/{seg(item_id)}"
    )

    if isinstance(result, dict) and "error" in result:
        return f"Error fetching custom item: {result.get('message')}"

    if not result or not isinstance(result, dict):
        return f"No custom item found with ID {item_id}."

    return format_custom_item_details(result)


@tool("admin")
async def create_custom_item(
    name: str,
    item_type: str,
    athlete_id: str | None = None,
    description: str | None = None,
    content: dict[str, Any] | None = None,
    visibility: str | None = None,
) -> str:
    """Create a new custom item for an athlete on Intervals.icu

    Args:
        name: Name of the custom item
        item_type: Type of custom item (e.g. FITNESS_CHART, TRACE_CHART, INPUT_FIELD, ACTIVITY_FIELD, INTERVAL_FIELD, ACTIVITY_STREAM, ACTIVITY_CHART, ACTIVITY_HISTOGRAM, ACTIVITY_HEATMAP, ACTIVITY_MAP, ACTIVITY_PANEL, ZONES)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        description: Description of the custom item (optional)
        content: Configuration content for the custom item as a dict (optional). Important enum values:
            - "type" field for INPUT_FIELD/ACTIVITY_FIELD: must be "numeric", "text", or "select" (NOT "number")
            - "aggregate" field: must be "MIN", "SUM", "MAX", or "AVERAGE" (NOT "AVG")
        visibility: Visibility setting: PRIVATE, FOLLOWERS, or PUBLIC (optional)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    data: dict[str, Any] = {"name": name, "type": item_type}
    if description is not None:
        data["description"] = description
    if content is not None:
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                return "Error: content must be valid JSON when passed as a string."
        data["content"] = content
    if visibility is not None:
        data["visibility"] = visibility

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/custom-item",
        data=data,
        method="POST",
    )

    if isinstance(result, dict) and "error" in result:
        return f"Error creating custom item: {result.get('message')}"

    if not result or not isinstance(result, dict):
        return "Error: Unexpected response when creating custom item."

    invalidate_custom_items_cache(athlete_id_to_use)
    return f"Successfully created custom item:\n\n{format_custom_item_details(result)}"


@tool("admin")
async def update_custom_item(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements,too-many-branches
    item_id: int,
    athlete_id: str | None = None,
    name: str | None = None,
    item_type: str | None = None,
    description: str | None = None,
    content: dict[str, Any] | None = None,
    visibility: str | None = None,
) -> str:
    """Update an existing custom item for an athlete on Intervals.icu

    Only the fields passed are changed. ``content`` is MERGED into the item's current content
    (top-level keys): {"aggregate": "SUM"} changes the aggregate and keeps code, type, options
    and formula. Set a key to null to clear it. A call without any field is refused.

    Args:
        item_id: The custom item ID to update
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        name: New name for the custom item (optional)
        item_type: New type for the custom item (optional)
        description: New description for the custom item (optional)
        content: Configuration keys to change, merged into the current content (optional). Important enum values:
            - "type" field for INPUT_FIELD/ACTIVITY_FIELD: must be "numeric", "text", or "select" (NOT "number")
            - "aggregate" field: must be "MIN", "SUM", "MAX", or "AVERAGE" (NOT "AVG")
        visibility: New visibility setting: PRIVATE, FOLLOWERS, or PUBLIC (optional)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    data: dict[str, Any] = {}
    if name is not None:
        if not name.strip():
            return "Error: name must not be blank."
        data["name"] = name
    if item_type is not None:
        data["type"] = item_type
    if description is not None:
        data["description"] = description
    if content is not None:
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                return "Error: content must be valid JSON when passed as a string."
        if not isinstance(content, dict):
            return "Error: content must be an object."
        if content:
            data["content"] = content
    if visibility is not None:
        data["visibility"] = visibility
    if not data:
        return "Error: nothing to update; pass at least one of name, item_type, description, content or visibility."

    url = f"/athlete/{seg(athlete_id_to_use)}/custom-item/{seg(item_id)}"
    if "content" in data:
        current = await make_intervals_request(url=url)
        if isinstance(current, dict) and "error" in current:
            return f"Error reading custom item {item_id} before the update: {current.get('message')}"
        if not isinstance(current, dict) or not current:
            return f"No custom item found with ID {item_id}."
        existing = current.get("content")
        data["content"] = {**(existing if isinstance(existing, dict) else {}), **data["content"]}

    result = await make_intervals_request(url=url, data=data, method="PUT")

    if isinstance(result, dict) and "error" in result:
        return f"Error updating custom item: {result.get('message')}"

    if not result or not isinstance(result, dict):
        return "Error: Unexpected response when updating custom item."

    invalidate_custom_items_cache(athlete_id_to_use)
    return f"Successfully updated custom item:\n\n{format_custom_item_details(result)}"


@tool("destructive")
async def delete_custom_item(
    item_id: int,
    athlete_id: str | None = None,
) -> str:
    """DELETES from Intervals.icu: permanently remove one custom item (chart, field, stream, zones ...)

    The item is read first, so the answer names what was deleted; a missing item is not deleted.
    Data recorded in a deleted custom field is no longer shown. This cannot be undone.

    Args:
        item_id: The custom item ID to delete
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    url = f"/athlete/{seg(athlete_id_to_use)}/custom-item/{seg(item_id)}"
    item = await make_intervals_request(url=url)
    if isinstance(item, dict) and "error" in item:
        return f"Error reading custom item {item_id}: {item.get('message')}"
    if not isinstance(item, dict) or not item:
        return f"No custom item found with ID {item_id}; nothing was deleted."

    result = await make_intervals_request(url=url, method="DELETE")

    if isinstance(result, dict) and "error" in result:
        return f"Error deleting custom item: {result.get('message')}"

    invalidate_custom_items_cache(athlete_id_to_use)
    return (
        f"Successfully deleted custom item {item_id} '{item.get('name') or 'unnamed'}' "
        f"({item.get('type') or 'unknown type'})."
    )
