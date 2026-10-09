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
from intervals_mcp_server.auth import auth_status_from_env
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
        "sse_path": os.getenv("FASTMCP_SSE_PATH", "/sse"),
        "auth": auth_status_from_env(),
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
    auth = status.get("auth") or {}
    sse_path = status.get("sse_path", "/sse")
    path_note = "secret path" if sse_path != "/sse" else "default path"
    lines.append(
        f"Transport: {status['transport']} on {status['host']}:{status['port']}, SSE path {path_note}; "
        f"auth mode {auth.get('mode', 'none')}"
        + (
            f" (issuer {auth.get('issuer')}, sign-in {'+'.join(auth.get('login') or [])}, "
            f"Intervals.icu app {auth.get('intervals_app')}, allowed athletes {', '.join(auth.get('allowed_athletes') or []) or 'none'})"
            if auth.get("mode") == "oauth"
            else " (remote transports need a secret path or OAuth plus a TLS reverse proxy)"
        )
    )
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
# Prompts describe which data to gather and how to present it; they never ask for a
# medical or physiological diagnosis and leave the training decision to the coach.
_NO_DIAGNOSIS = (
    " Report numbers with units and say where data is missing or preliminary. Do not state causes "
    "or medical conclusions; statistical associations are not causation. Keep the Intervals.icu load "
    "apart from device loads, and treat device composite scores (readiness, Body Battery) as derived "
    "values, not measurements. The training decision stays with the athlete/coach."
)


@mcp.prompt()
def recovery_check(date_str: str = "") -> str:
    """Daily recovery check from the wellness snapshot, baselines, recent load and the plan of the day."""
    day = f"date_str='{date_str}'" if date_str else "today"
    return (
        f"Act as an endurance coach doing a recovery check for {day}. Call get_recovery_snapshot(days_back=3) and, "
        "when a value is far from its baseline, get_wellness_trends for that metric. Summarise which values are "
        "inside or outside the athlete's own baselines, what the recent training load was, what is planned and "
        "whether the plan is consistent with the data; offer at most one adjustment with its reasoning." + _NO_DIAGNOSIS
    )


@mcp.prompt()
def workout_deep_dive(activity_id: str) -> str:
    """Full analysis of one activity: plan vs execution, intervals, device metrics, climbs, power meters."""
    return (
        f"Analyse activity {activity_id} as an endurance coach. Start with get_activity_report(activity_id) for the "
        "overview, plan-vs-execution, power meter check and climbs. Then go deeper only where needed: "
        "get_activity_intervals(stream_types='custom', detail_level='compact') for per-interval stamina and custom "
        "streams, get_best_efforts for the best efforts, compare_power_streams when two power streams exist, "
        "analyze_climbs for unstructured rides or hikes. Report what was done versus planned, pacing, HR response, "
        "fade and drift, stamina use and anything unusual." + _NO_DIAGNOSIS
    )


@mcp.prompt()
def weekly_training_review(start_date: str = "") -> str:
    """Review the past training week(s): load, compliance, intensity distribution, recovery."""
    week = f"the week starting {start_date}" if start_date else "the last 7 days"
    return (
        f"Review {week} as an endurance coach. Use get_training_summary(group_by='week') for the totals and load "
        "split, get_plan_compliance for planned versus done, get_weekly_summary for the end-of-week CTL/ATL/form, "
        "and get_recovery_snapshot(days_back=6, detail_level='compact') for the recovery trend. Summarise volume, "
        "intensity distribution, compliance, fatigue and the one thing to change next week." + _NO_DIAGNOSIS
    )


@mcp.prompt()
def performance_progression(activity_type: str = "Ride", months: int = 3) -> str:
    """Long-term progression: curves, eFTP, best efforts, efficiency and fatigue resistance."""
    return (
        f"Assess the performance progression for {activity_type} over the last {months} months. Use "
        "get_athlete_power_curves (or get_pace_curves / get_hr_curves), get_wellness_trends(metrics='eftp_" + activity_type + "') "
        "for the eFTP history, compare_best_efforts for a date range, get_power_hr_efficiency for Pw:HR by power "
        "band, get_fatigue_resistance for power after kJ, and compare_workouts for recurring sessions. Describe "
        "what improved, what stagnated and the evidence for each statement." + _NO_DIAGNOSIS
    )


@mcp.prompt()
def long_ride_climbing_analysis(activity_id: str) -> str:
    """Long ride, tour or mountain hike: climbs, descents, pauses, pacing, stamina, fuelling."""
    return (
        f"Analyse the long activity {activity_id} as an endurance coach. Use analyze_climbs for the climb/descent/"
        "pause segmentation with power, HR, VAM and stamina per climb, get_activity_details(detail_level='compact') "
        "for thresholds and device fields, get_best_efforts for the strongest efforts and "
        "get_activity_streams(stream_types='time,watts,heartrate,Stamina', output_format='full', downsample=60) "
        "for the overall pacing curve. Comment on pacing across climbs, HR drift, stamina use, pauses and what "
        "it implies for the next long ride." + _NO_DIAGNOSIS
    )


@mcp.prompt()
def nutrition_weight_trend(weeks: int = 4) -> str:
    """Nutrition, calorie balance and weight over the last weeks in the context of training load."""
    return (
        f"Review nutrition and weight for the last {weeks} weeks. Use get_nutrition_summary(windows='7,14,28') and "
        "get_wellness_trends(metrics='weight,kcalConsumed,GarminTotalCalories'). Relate logged intake, estimated "
        "burn and weight change to the training load; point out days without logged intake and that the burn is "
        "a device estimate and a calorie balance is not a fat change measurement." + _NO_DIAGNOSIS
    )


@mcp.prompt()
def power_meter_comparison(activity_id: str) -> str:
    """Compare two power meters recorded on the same activity."""
    return (
        f"Compare the power meters of activity {activity_id}. Call compare_power_streams(activity_id) and "
        "get_activity_details(detail_level='compact') for the device data. Report the overall offset, the offset by "
        "power band and in stable windows, drift over the ride, lag and best efforts, and name the sensors only "
        "from the device data in the output, never from stream names. State that no calibration was applied and "
        "that a consistent offset between different meters is expected." + _NO_DIAGNOSIS
    )


@mcp.prompt()
def workout_planning_validation(start_date: str = "") -> str:
    """Plan and validate structured workouts before they are written to the calendar."""
    day = start_date or "the coming days"
    return (
        f"Plan structured workouts for {day}. Gather get_sport_settings (thresholds and zones), "
        "get_recovery_snapshot(detail_level='compact'), get_training_summary for the last 4 weeks and get_events for "
        "the week. Draft each workout as a workout document, run validate_workout(workout_doc, workout_type, name, "
        "start_date) and show the preview and the validation result to the athlete. Write to the calendar with "
        "add_or_update_event only after explicit confirmation." + _NO_DIAGNOSIS
    )


# ------------------------------------------------------------------ resources
@mcp.resource("intervals://guide")
def usage_guide() -> str:
    """How to use this server: tool groups, recommended call order and conventions."""
    return (
        "Futureweb Intervals MCP usage guide\n"
        "1. Start with get_server_status to see enabled tool classes and custom item counts.\n"
        "2. One activity: get_activity_report (compact, 3-4 API calls) then get_activity_details, "
        "get_activity_intervals(detail_level='compact'), get_best_efforts, compare_power_streams, analyze_climbs as needed.\n"
        "3. Streams: list_activity_streams first, then get_activity_streams with output_format='full', slicing and downsample.\n"
        "4. Recovery: get_recovery_snapshot, get_wellness_trends, get_nutrition_summary.\n"
        "5. Periods: get_training_summary (week/month/sport/gear), get_weekly_summary, get_plan_compliance, "
        "compare_workouts, get_power_hr_efficiency, get_fatigue_resistance, curves.\n"
        "6. Planning: get_sport_settings / get_training_zones, get_training_plan, get_workout_library, "
        "validate_workout, then (if the write class is enabled) add_or_update_event.\n"
        "Conventions: times are local and UTC with timezone; 'no value' = null/NaN; a 0 in a device-file field may "
        "mean the source field was absent; Intervals.icu load is never mixed with device loads; custom fields come "
        "from the athlete's own definitions; most tools accept output_format='json' and detail_level.\n"
    )


@mcp.resource("intervals://custom-items")
async def custom_items_resource() -> str:
    """The athlete's custom item definitions (codes, names, units, types) as compact JSON."""
    if not config.athlete_id:
        return json.dumps({"error": "ATHLETE_ID is not configured"})
    index = await get_custom_item_index(athlete_id=config.athlete_id)
    return json.dumps(
        {
            item_type: [
                {"code": code, "name": d.get("name"), "units": d.get("units"), "value_type": d.get("value_type"), "source": d.get("fit_source") or ("script" if d.get("has_script") else "manual")}
                for code, d in defs.items()
            ]
            for item_type, defs in index.items()
        },
        ensure_ascii=False,
    )
