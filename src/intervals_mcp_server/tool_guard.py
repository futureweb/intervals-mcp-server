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
from collections.abc import Callable
from contextvars import ContextVar
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


def _shrink_json(payload: Any, limit: int) -> str | None:
    """Valid JSON within limit by cutting the largest lists (with a "truncated" note), or None."""
    root = payload if isinstance(payload, dict) else {"items": payload}
    truncated: list[dict[str, Any]] = []
    root["truncated"] = truncated
    root["truncated_note"] = (
        "Lists were cut to fit the size limit of one tool result (see truncated: kept items are the first ones). "
        + _PAGING_HINT
    )

    def size() -> int:
        return len(json.dumps(root, ensure_ascii=False))

    for _ in range(8):
        if size() <= limit:
            break
        candidates = sorted(_lists(root), key=lambda item: len(json.dumps(item[1], ensure_ascii=False)), reverse=True)
        candidates = [c for c in candidates if c[0] != "truncated"]
        if not candidates:
            break
        path, items = candidates[0]
        original = list(items)
        entry = {"path": path, "kept": len(original), "total": len(original)}
        truncated.append(entry)  # counted in the size while searching
        low, high = 1, len(original) - 1  # keep as many leading items as fit
        while low < high:
            middle = (low + high + 1) // 2
            items[:] = original[:middle]
            if size() <= limit:
                low = middle
            else:
                high = middle - 1
        items[:] = original[:low]
        entry["kept"] = low
    if size() > limit:
        return None
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
