"""
Guard around every MCP tool call.

``guarded(func)`` wraps a tool function (``@tool`` in mcp_instance applies it) and, for the
outermost tool call only:

- refuses identifier arguments (``*_id``, ``*_ids``) that are not ONE plain path segment, so
  "a/b" or "i123/intervals" cannot pick a sub-endpoint of the Intervals.icu API;
- uses the athlete's time zone for "today" and all default dates (utils.dates);
- limits the number of API requests and the duration of the call (api.client.call_limits)
  and says so in the result when a limit stopped requests;
- caps the size of the result: text is cut at a line boundary with a note on how to get the
  rest, JSON that does not fit is replaced by a JSON error object (never cut into invalid JSON).

Nested calls (a tool calling another tool function) pass straight through.
"""

import functools
import inspect
import json
import os
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from intervals_mcp_server.api.client import call_limits, unsafe_segment_reason
from intervals_mcp_server.utils.dates import activate_athlete_timezone, reset_athlete_timezone

F = TypeVar("F", bound=Callable[..., Any])

# Largest tool result in characters (MCP_MAX_OUTPUT_CHARS). Claude Code's default limit is
# 25k tokens; MCP clients cut or reject larger results without telling the model.
DEFAULT_MAX_OUTPUT_CHARS = 100_000
# Size the paging tools (streams, wellness) aim for, so they page before the hard cap.
OUTPUT_BUDGET_CHARS = 60_000

_IN_TOOL: ContextVar[bool] = ContextVar("intervals_in_tool_call", default=False)


def max_output_chars() -> int:
    """Hard cap of a tool result (MCP_MAX_OUTPUT_CHARS, default 100000)."""
    try:
        value = int(os.environ.get("MCP_MAX_OUTPUT_CHARS", "") or DEFAULT_MAX_OUTPUT_CHARS)
    except ValueError:
        return DEFAULT_MAX_OUTPUT_CHARS
    return max(value, 2_000)


def output_budget() -> int:
    """Characters a paging tool should fill at most (below the hard cap)."""
    return min(OUTPUT_BUDGET_CHARS, int(max_output_chars() * 0.9))


def identifier_error(arguments: dict[str, Any]) -> str | None:
    """Error text for the first id argument that is not one plain path segment."""
    for name, value in arguments.items():
        # None and "" mean "not given" (e.g. event_id="" creates a new event); the tool decides.
        if value is None or value == "" or not isinstance(value, (str, int)) or isinstance(value, bool):
            continue
        if name.endswith("_ids") and isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
        elif name.endswith("_id"):
            parts = [str(value).strip() if isinstance(value, str) else str(value)]
        else:
            continue
        for part in parts:
            reason = unsafe_segment_reason(part)
            if reason:
                return (
                    f"Error: {name} {part!r} is not a valid identifier ({reason}). "
                    "Pass the plain id, e.g. i123456789 for an activity or 123456 for an event."
                )
    return None


_PAGING_HINT = (
    "Get the rest with a narrower request: detail_level=\"standard\" or \"compact\" where the tool has it, "
    "a shorter date range, limit/offset paging, start_index for streams, or the text output."
)


def _lists(value: Any, path: str = "", depth: int = 0) -> list[tuple[str, list[Any]]]:
    """(path, list) of every list with more than one item, up to four levels deep."""
    found: list[tuple[str, list[Any]]] = []
    if depth > 4:
        return found
    if isinstance(value, list):
        if len(value) > 1:
            found.append((path or "$", value))
        for index, item in enumerate(value[:50]):
            found.extend(_lists(item, f"{path}[{index}]", depth + 1))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_lists(item, f"{path}.{key}" if path else str(key), depth + 1))
    return found


_DATE_KEYS = ("start_date_local", "start_date", "date", "start", "group", "week", "day", "id")
_DATE_PREFIX = re.compile(r"\d{4}-\d{2}-\d{2}")


def _item_date(item: Any) -> str | None:
    if isinstance(item, dict):
        for key in _DATE_KEYS:
            value = item.get(key)
            if isinstance(value, str) and _DATE_PREFIX.match(value):
                return value[:19]
    return None


def _newest_first(items: list[Any]) -> bool | None:
    """True for a newest-first list, False for oldest-first, None when not chronological."""
    first, last = _item_date(items[0]), _item_date(items[-1])
    if first and last and first != last:
        return first > last
    return None


def _major_lists(root: Any, total: int) -> list[tuple[str, list[Any]]]:
    """Lists worth cutting: at least a tenth of the result, not inside another such list."""
    major: list[tuple[str, list[Any]]] = []
    for path, items in sorted(_lists(root), key=lambda item: len(item[0])):
        if any(path.startswith(outer + "[") for outer, _ in major):
            continue
        if len(json.dumps(items, ensure_ascii=False)) >= total * 0.1:
            major.append((path, items))
    return major


@dataclass
class _CutPlan:
    """How one list of a too large JSON result is cut."""

    items: list[Any]  # the list inside the result (cut in place)
    original: list[Any]
    tail: bool  # keep the last (newest) items
    paged: bool  # the tool's paged list: next_offset follows the kept items
    entry: dict[str, Any]  # the "truncated" entry


def _shrink_json(payload: Any, limit: int) -> str | None:  # pylint: disable=too-many-locals
    """Valid JSON within limit by cutting the large lists by the same fraction, or None.

    Chronological lists keep their newest items; a paged list (the result has next_offset)
    keeps its first items and gets the next_offset to continue with. "truncated" says which
    lists were cut, how many items were kept and which end.
    """
    root = payload if isinstance(payload, dict) else {"items": payload}
    major = _major_lists(root, len(json.dumps(root, ensure_ascii=False)))
    if not major:
        return None
    paged = isinstance(payload, dict) and "next_offset" in payload
    plans: list[_CutPlan] = []
    for path, items in major:
        top_level = "." not in path and "[" not in path
        newest_first = _newest_first(items)
        keep_tail = newest_first is False and not (paged and top_level)
        end = "last (newest)" if keep_tail else ("first (newest)" if newest_first else "first")
        plans.append(_CutPlan(items, list(items), keep_tail, paged and top_level,
                              {"path": path, "kept": len(items), "total": len(items), "kept_items": end}))
    root["truncated"] = [plan.entry for plan in plans]
    root["truncated_note"] = (
        "Lists were cut to fit the size limit of one tool result (see truncated: how many items were kept and "
        "which end)." + (" Continue with next_offset." if paged else "") + " " + _PAGING_HINT
    )
    raw_offset = root.get("offset")
    offset: int = raw_offset if isinstance(raw_offset, int) and not isinstance(raw_offset, bool) else 0

    original_next = root.get("next_offset")

    def apply(fraction: float) -> int:
        for plan in plans:
            keep = max(1, int(len(plan.original) * fraction))
            plan.items[:] = plan.original[-keep:] if plan.tail else plan.original[:keep]
            plan.entry["kept"] = keep
            if plan.paged:
                root["next_offset"] = offset + keep if keep < len(plan.original) else original_next
        return len(json.dumps(root, ensure_ascii=False))

    low, high = 0.0, 1.0
    for _ in range(18):
        middle = (low + high) / 2
        if apply(middle) <= limit:
            low = middle
        else:
            high = middle
    if apply(low) > limit:
        return None
    root["truncated"] = [plan.entry for plan in plans if plan.entry["kept"] < plan.entry["total"]]
    return json.dumps(root, ensure_ascii=False)


def cap_output(result: Any, limit: int | None = None) -> Any:
    """Keep a tool result below the size limit, never silently.

    Text is cut at a line boundary with a note. JSON stays valid JSON: the largest lists are
    cut (first items kept) and a "truncated" entry says which and how many; only when that
    cannot help is the result replaced by an error object with the paging hints.
    """
    if not isinstance(result, str):
        return result
    limit = limit or max_output_chars()
    if len(result) <= limit:
        return result
    stripped = result.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            payload = json.loads(stripped)
        except ValueError:
            pass
        else:
            shrunk = _shrink_json(payload, limit - 500)
            if shrunk is not None:
                return shrunk
            return json.dumps(
                {
                    "error": "output_too_large",
                    "chars": len(result),
                    "limit": limit,
                    "message": f"The JSON result has {len(result)} characters, more than the limit of {limit}. " + _PAGING_HINT,
                }
            )
    note_room = 400
    cut = result.rfind("\n", 0, limit - note_room)
    if cut < limit // 2:
        cut = limit - note_room
    return (
        result[:cut]
        + f"\n\n[Output truncated: showing the first {cut} of {len(result)} characters (limit {limit}). "
        + _PAGING_HINT
        + "]"
    )


def _limit_note(result: Any, hit: str) -> Any:
    text = (
        f"Note: this tool call reached {hit}; some data was not loaded and the result above is "
        "incomplete. Narrow the request (fewer activities, ids or durations, a shorter date range)."
    )
    if not isinstance(result, str):
        return result
    stripped = result.lstrip()
    if stripped[:1] == "{":
        try:
            payload = json.loads(stripped)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            payload["limit_note"] = text
            return json.dumps(payload, ensure_ascii=False)
    return f"{result}\n\n{text}"


def guarded(func: F) -> F:
    """Wrap a tool coroutine function with the checks described in the module docstring."""
    signature = inspect.signature(func)

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _IN_TOOL.get():
            return await func(*args, **kwargs)
        try:
            bound = signature.bind_partial(*args, **kwargs)
        except TypeError:
            return await func(*args, **kwargs)  # let the function raise its own argument error
        error = identifier_error(dict(bound.arguments))
        if error:
            return error
        from intervals_mcp_server.config import get_config  # pylint: disable=import-outside-toplevel

        athlete = bound.arguments.get("athlete_id") or get_config().athlete_id
        api_key = bound.arguments.get("api_key")
        marker = _IN_TOOL.set(True)
        try:
            with call_limits() as limits:
                zone_token = await activate_athlete_timezone(str(athlete) if athlete else None, api_key)
                try:
                    result = await func(*args, **kwargs)
                finally:
                    reset_athlete_timezone(zone_token)
                if limits.hit:
                    result = _limit_note(result, limits.hit)
        finally:
            _IN_TOOL.reset(marker)
        return cap_output(result)

    return cast(F, wrapper)
