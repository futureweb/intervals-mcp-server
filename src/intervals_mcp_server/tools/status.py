"""
Server status / diagnostics tool and MCP prompts.

get_server_status reports the version, enabled permission classes, registered and hidden
tools, the configured athlete, whether the Intervals.icu API answers with the configured
key, how many custom items (fields, streams, wellness fields) the account defines and
which display-unit overrides are active. It never prints the API key.
"""

import json
import os
from importlib import metadata
from typing import Any

from intervals_mcp_server import __version__
from intervals_mcp_server.api import client as api_client
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.utils.custom_fields import ACTIVITY_FIELD, ACTIVITY_STREAM, INPUT_FIELD, INTERVAL_FIELD

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import disabled_tools, mcp, tool, tool_permissions

config = get_config()


def _package_version() -> str:
    for name in ("futureweb-intervals-mcp", "intervals-mcp-server"):
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return __version__


async def server_status(api_key: str | None = None) -> dict[str, Any]:
    """Collect the status as a dict (shared by the tool and the --doctor CLI flag)."""
    permissions = tool_permissions()
    hidden = disabled_tools()
    registered = {name: cls for name, cls in permissions.items() if name not in hidden}
    status: dict[str, Any] = {
        "version": _package_version(),
        "permissions_enabled": sorted(config.permissions),
        "tools_registered": len(registered),
        "tools_by_class": {
            cls: sorted(n for n, c in registered.items() if c == cls) for cls in ("read", "write", "destructive", "admin")
        },
        "tools_hidden": hidden,
        "transport": os.getenv("MCP_TRANSPORT", "stdio"),
        "host": os.getenv("FASTMCP_HOST", "127.0.0.1"),
        "port": os.getenv("FASTMCP_PORT", "8000"),
        "athlete_id_configured": bool(config.athlete_id),
        "athlete_id": config.athlete_id or None,
        "api_key_configured": bool(config.api_key or api_key),
        "api_base_url": config.intervals_api_base_url,
        "units_overrides": config.custom_units_overrides,
        "api": {"ok": False, "detail": "not checked"},
        "custom_items": {},
    }
    if not config.athlete_id:
        status["api"] = {"ok": False, "detail": "ATHLETE_ID is not set"}
        return status
    if not (config.api_key or api_key):
        status["api"] = {"ok": False, "detail": "API_KEY is not set"}
        return status
    result = await api_client.make_intervals_request(
        url=f"/athlete/{config.athlete_id}/sport-settings", api_key=api_key
    )
    if isinstance(result, dict) and "error" in result:
        status["api"] = {"ok": False, "detail": str(result.get("message"))}
        return status
    status["api"] = {"ok": True, "detail": f"sport settings for {len(result) if isinstance(result, list) else '?'} sport group(s) readable"}
    index = await get_custom_item_index(athlete_id=config.athlete_id, api_key=api_key)
    status["custom_items"] = {
        "activity_fields": len(index.get(ACTIVITY_FIELD, {})),
        "activity_streams": len(index.get(ACTIVITY_STREAM, {})),
        "interval_fields": len(index.get(INTERVAL_FIELD, {})),
        "wellness_fields": len(index.get(INPUT_FIELD, {})),
        "activity_stream_codes": sorted(index.get(ACTIVITY_STREAM, {})),
    }
    return status


def format_status(status: dict[str, Any]) -> str:
    """Readable status report."""
    lines = [
        f"Futureweb Intervals MCP {status['version']}",
        f"Permissions enabled: {', '.join(status['permissions_enabled'])} (MCP_PERMISSIONS); "
        f"{status['tools_registered']} tools registered",
    ]
    for cls, names in status["tools_by_class"].items():
        if names:
            lines.append(f"  {cls}: {', '.join(names)}")
    if status["tools_hidden"]:
        lines.append("  hidden (class not enabled): " + ", ".join(f"{n} [{c}]" for n, c in sorted(status["tools_hidden"].items())))
    lines.append(f"Transport: {status['transport']} on {status['host']}:{status['port']} (SSE/HTTP must not be exposed without authentication)")
    lines.append(f"Athlete: {status['athlete_id'] or 'not configured'} | API key: {'configured' if status['api_key_configured'] else 'MISSING'} | base URL {status['api_base_url']}")
    lines.append(f"Intervals.icu API: {'OK' if status['api']['ok'] else 'FAILED'} - {status['api']['detail']}")
    items = status.get("custom_items") or {}
    if items:
        lines.append(
            f"Custom items: {items['activity_fields']} activity fields, {items['activity_streams']} activity streams, "
            f"{items['interval_fields']} interval fields, {items['wellness_fields']} wellness fields"
            + (f" (streams: {', '.join(items['activity_stream_codes'])})" if items["activity_stream_codes"] else "")
        )
    if status["units_overrides"]:
        lines.append("Display unit overrides: " + ", ".join(f"{k}={v}" for k, v in status["units_overrides"].items()))
    return "\n".join(lines)


@tool("read")
async def get_server_status(api_key: str | None = None, output_format: str = "text") -> str:
    """Diagnostics: server version, enabled permission classes, registered tools, API reachability

    Shows which tool classes are enabled (read / write / destructive / admin), which tools
    are hidden because their class is disabled, the transport configuration, whether an
    athlete and API key are configured, whether Intervals.icu answers, and how many custom
    activity fields, streams, interval fields and wellness fields the account defines
    (a sync bridge typically adds streams such as stamina). The API key is never shown.

    Args:
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json"
    """
    status = await server_status(api_key)
    if output_format.strip().lower() == "json":
        return json.dumps(status, ensure_ascii=False)
    return format_status(status)


# ------------------------------------------------------------------- prompts
@mcp.prompt()
def recovery_check(date_str: str = "") -> str:
    """Daily recovery check: gather the data, then give a coach-style reading."""
    day = date_str or "today"
    return (
        f"Act as an endurance coach. Call get_recovery_snapshot(date_str='{day}' if a date was given else today, days_back=3) "
        "and, if useful, get_wellness_trends for hrv, restingHR, avgSleepingHR, sleepScore and readiness. "
        "Summarise the recovery state in a few sentences: which values are inside or outside the athlete's own "
        "baselines, what the recent training load was, what is planned for the day and whether the plan is consistent "
        "with the data. Point out missing or preliminary values. Do not invent causes; statistical associations are not causation. "
        "Keep Intervals.icu load and device training loads apart."
    )


@mcp.prompt()
def workout_analysis(activity_id: str) -> str:
    """Full analysis of one activity: execution vs plan, intervals, device metrics, streams."""
    return (
        f"Analyse activity {activity_id} as an endurance coach. Steps: 1) get_activity_details for the summary, "
        "thresholds used and all custom/device fields; 2) analyze_workout_execution to compare the plan with the "
        "execution (or evaluate the intervals); 3) get_activity_intervals with stream_types='custom' for stamina and "
        "other custom streams per interval; 4) for rides with a second power meter compare_power_streams; 5) for "
        "unstructured rides, hikes or runs analyze_climbs. Report what was done versus planned, pacing, HR response, "
        "fade and drift, stamina use, and anything unusual. Quote values with units and say where data is missing."
    )


@mcp.prompt()
def weekly_planning(start_date: str = "") -> str:
    """Plan the coming week from fitness, recent load, recovery and the calendar."""
    week = start_date or "the coming Monday"
    return (
        f"Plan the training week starting {week}. Gather: get_training_summary for the last 4 weeks grouped by week, "
        "get_weekly_summary for the same range, get_recovery_snapshot for today, get_events for the week, "
        "get_sport_settings for current thresholds and zones. Propose sessions with purpose, duration and target "
        "zones consistent with CTL/ATL/ramp rate and recovery. Before writing anything, show the plan and use "
        "validate_workout for each structured session; write to the calendar only when the athlete confirms."
    )
