"""
Server status / diagnostics tool and MCP prompts.

get_server_status reports the version, enabled permission classes, registered and hidden
tools, the configured athlete, whether the Intervals.icu API answers with the configured
key, how many custom items (fields, streams, wellness fields) the account defines and
which display-unit overrides are active. It never prints the API key. Deployment details
(bind address and port, SSE path, OAuth user name, password source, allowed athletes and
the state file) are only part of ``--doctor`` on the server itself, not of the tool's answer.

In the multi-user mode (``MCP_TENANCY=multi``) the report describes the calling connection
only: its own athlete, how it authenticates (its Intervals.icu sign-in or, for the owner, the
server's API key), its Intervals.icu scopes and its request budget, never other athletes.
"""

import json
import os
from importlib import metadata
from typing import Any

from intervals_mcp_server import __version__
from intervals_mcp_server.api import client as api_client
from intervals_mcp_server.auth import auth_status_from_env
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tenancy import BUDGETS, budget_settings, current_credential, default_athlete, multi_user
from intervals_mcp_server.tools.custom_items import get_custom_item_index
from intervals_mcp_server.utils.custom_fields import ACTIVITY_FIELD, ACTIVITY_STREAM, INPUT_FIELD, INTERVAL_FIELD
from intervals_mcp_server.utils.params import OutputFormat

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import disabled_tools, mcp, tool, tool_permissions, tools_outside_toolset

config = get_config()


def _package_version() -> str:
    for name in ("futureweb-intervals-mcp", "intervals-mcp-server"):
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return __version__


async def server_status(include_private: bool = False) -> dict[str, Any]:
    """Collect the status as a dict (shared by the tool and the --doctor CLI flag).

    *include_private* adds what only the operator needs (``--doctor``): bind address, port,
    SSE path (a secret path is a credential) and the OAuth deployment details.
    """
    permissions = tool_permissions()
    hidden = disabled_tools()
    outside = tools_outside_toolset()
    registered = {name: cls for name, cls in permissions.items() if name not in hidden and name not in outside}
    multi = multi_user()
    credential = current_credential() if multi else None
    athlete = default_athlete(config.athlete_id)
    status: dict[str, Any] = {
        "version": _package_version(),
        "permissions_enabled": sorted(config.permissions),
        "toolset": config.toolset,
        "tools_registered": len(registered),
        "tools_by_class": {
            cls: sorted(n for n, c in registered.items() if c == cls) for cls in ("read", "write", "destructive", "admin")
        },
        "tools_hidden": hidden,
        "tools_outside_toolset": sorted(outside),
        "transport": os.getenv("MCP_TRANSPORT", "stdio"),
        "auth": auth_status_from_env(include_private=include_private),
        "tenancy": "multi" if multi else "single",
        "athlete_id_configured": bool(athlete),
        "athlete_id": athlete or None,
        "api_key_configured": bool(config.api_key) if not multi or include_private or (credential and credential.owner) else None,
        "api_base_url": config.intervals_api_base_url,
        "units_overrides": config.custom_units_overrides,
        "api": {"ok": False, "detail": "not checked"},
        "custom_items": {},
    }
    if include_private:
        status.update(
            {
                "host": os.getenv("FASTMCP_HOST", "127.0.0.1"),
                "port": os.getenv("FASTMCP_PORT", "8000"),
                "sse_path": os.getenv("FASTMCP_SSE_PATH", "/sse"),
            }
        )
    if multi:
        daily, _, _ = budget_settings()
        status["connection"] = (
            {
                "credential": credential.description,
                "intervals_scopes": sorted(credential.intervals_scopes) if credential.intervals_scopes is not None else None,
                "requests_today": BUDGETS.used_today(credential.athlete_id) if credential.kind == "bearer" else None,
                "daily_request_budget": (daily or None) if credential.kind == "bearer" else None,
            }
            if credential is not None
            else None
        )
        if credential is None:
            status["api"] = {"ok": False, "detail": "multi-user mode: checked per connection (no signed-in connection here)"}
            return status
    elif not config.athlete_id:
        status["api"] = {"ok": False, "detail": "ATHLETE_ID is not set"}
        return status
    elif not config.api_key:
        status["api"] = {"ok": False, "detail": "API_KEY is not set"}
        return status
    result = await api_client.make_intervals_request(
        url=f"/athlete/{api_client.seg(athlete)}/sport-settings"
    )
    if isinstance(result, dict) and "error" in result:
        status["api"] = {"ok": False, "detail": str(result.get("message"))}
        return status
    status["api"] = {"ok": True, "detail": f"sport settings for {len(result) if isinstance(result, list) else '?'} sport group(s) readable"}
    index = await get_custom_item_index(athlete_id=athlete)
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
        f"tool set {status.get('toolset', 'full')} (MCP_TOOLSET); {status['tools_registered']} tools registered",
    ]
    for cls, names in status["tools_by_class"].items():
        if names:
            lines.append(f"  {cls}: {', '.join(names)}")
    if status["tools_hidden"]:
        lines.append("  hidden (class not enabled): " + ", ".join(f"{n} [{c}]" for n, c in sorted(status["tools_hidden"].items())))
    if status.get("tools_outside_toolset"):
        lines.append(f"  outside the tool set: {len(status['tools_outside_toolset'])} tools (MCP_TOOLSET=full shows them)")
    auth = status.get("auth") or {}
    where = ""
    if "host" in status:
        path_note = "secret path" if status.get("sse_path", "/sse") != "/sse" else "default path"
        where = f" on {status['host']}:{status['port']}, SSE path {path_note}"
    athletes = (
        f", allowed athletes {', '.join(auth.get('allowed_athletes') or []) or 'none'}" if "allowed_athletes" in auth else ""
    )
    lines.append(
        f"Transport: {status['transport']}{where}; "
        f"auth mode {auth.get('mode', 'none')}"
        + (
            f" (issuer {auth.get('issuer')}, sign-in {'+'.join(auth.get('login') or [])}"
            f"{' + TOTP' if auth.get('second_factor') == 'totp' else ''}, "
            f"Intervals.icu app {auth.get('intervals_app')}{athletes})"
            if auth.get("mode") == "oauth"
            else " (remote transports need a secret path or OAuth plus a TLS reverse proxy)"
        )
    )
    connection = status.get("connection")
    if status.get("tenancy") == "multi":
        lines.append("Tenancy: multi-user (MCP_TENANCY=multi): every connection uses its own Intervals.icu credential")
    if connection:
        scopes = connection.get("intervals_scopes")
        budget = connection.get("daily_request_budget")
        lines.append(
            f"Athlete: {status['athlete_id']} (this connection) | credential: {connection['credential']}"
            + (f" | Intervals.icu scopes: {', '.join(scopes)}" if scopes else "")
            + (f" | requests today: {connection.get('requests_today') or 0} of {budget}" if budget else "")
            + f" | base URL {status['api_base_url']}"
        )
    else:
        key = status["api_key_configured"]
        lines.append(
            f"Athlete: {status['athlete_id'] or 'not configured'} | API key: "
            f"{'configured' if key else 'MISSING' if key is not None else 'not shown'} | base URL {status['api_base_url']}"
        )
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
async def get_server_status(output_format: OutputFormat = "text") -> str:
    """Use to check the connection when tools are missing or fail: server version, enabled permission classes and tool set, registered and hidden tools, whether Intervals.icu answers, and the athlete's custom field and stream counts. The API key is never shown."""
    status = await server_status()
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
        f"Review {week} as an endurance coach. Call get_coach_context first (load, intensity distribution, recovery "
        "markers, durability, plan and the method used, in one call), then get_training_summary(group_by='week') for the "
        "totals and load split, "
        "get_plan_compliance for planned versus done, get_weekly_summary for the end-of-week CTL/ATL/form, and "
        "get_recovery_snapshot(days_back=6, detail_level='compact') for the recovery trend. Summarise volume, "
        "intensity distribution, compliance, fatigue and the one thing to change next week." + _NO_DIAGNOSIS
    )


@mcp.prompt()
def training_load_review(end_date: str = "") -> str:
    """Training load, intensity distribution, durability and the planned load (statistics with sample sizes)."""
    day = f"end_date='{end_date}'" if end_date else "today"
    return (
        f"Review the training load as an endurance coach for {day}. Call get_coach_context first for the overview (it "
        "states its windows, ACWR method and zone rules), then "
        "go deeper only where needed: get_training_load (acute and chronic load, ratio, monotony, strain, deload-like "
        "weeks, per sport), get_intensity_distribution (three-zone distribution, polarization index, hard days, drift), "
        "get_durability (decoupling of steady long sessions, efficiency factor) and, when workouts are planned, "
        "get_load_projection. Report the numbers with their windows, sample sizes and the cited reference ranges as "
        "context; a value outside a commonly cited range is a statistic, not a risk statement. Say which data is "
        "missing (sessions without zones or load, small samples)." + _NO_DIAGNOSIS
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
        "the week. Read the workout format (resource intervals://workout-syntax or get_guide(topic='workout-syntax')), "
        "draft each workout as a workout document, run validate_workout(workout_doc, workout_type, name, "
        "start_date) and show the preview and the validation result to the athlete. Write to the calendar with "
        "add_or_update_event only after explicit confirmation." + _NO_DIAGNOSIS
    )


@mcp.prompt()
def race_week(race_date: str = "", race_name: str = "") -> str:
    """Race-week check: taper and form on race day, remaining sessions, fueling plan from history, weather, logistics."""
    race = race_name or "the race"
    if race_date:
        calendar = (
            f"1) Calendar: check with get_events(categories='RACE_A,RACE_B,RACE_C') that {race} is on {race_date} "
            "(the date must be YYYY-MM-DD; ask if it is unclear) and list the remaining workouts until then. "
        )
        target = f"target_date='{race_date}'"
    else:
        calendar = (
            "1) Race date: none was given. List the races of the coming weeks with get_events(categories="
            "'RACE_A,RACE_B,RACE_C') and ask the athlete which race and date is meant before going on; do not assume "
            "the next A race. Then list the remaining workouts until the race. "
        )
        target = "target_date='<the confirmed race date, YYYY-MM-DD>'"
    return (
        f"Act as an endurance coach preparing race week for {race}. " + calendar
        + f"2) Taper and form: get_load_projection({target}, detail_level='compact') for CTL, ATL and form at the "
        "start of race day (on race day itself target_date is today); add target_form (e.g. '5,15' or '5%,20%') only "
        "if the athlete names a target range, to see how the load of the last taper_days days would have to change. "
        "Nothing is written. "
        "3) Recovery: get_recovery_snapshot(detail_level='compact'); today's wellness may still be incomplete. "
        "4) Fueling plan: get_fueling_analysis in period mode (start_date about 180 days back, sport_types of the "
        "race) for the carbohydrate, fluid and sodium intake per hour the athlete has actually used on long "
        "sessions, with sample sizes; base the plan per hour on that history and say where data is missing. "
        "5) Weather: this server has no forecast; ask the athlete for it or use one they provide. A previous "
        "edition or the same course (get_activity_report(activity_id, include_route_history=true)) shows past "
        "conditions and pacing. "
        "6) Logistics checklist: bike/shoes and their maintenance reminders (get_gear_list, then get_gear_details), "
        "food and bottles, start time and warm-up, travel, sleep, documents. "
        "Answer with form on race day, which remaining sessions to keep or shorten, the fueling plan per hour and "
        "the checklist. Calendar changes only after the athlete asks: validate_workout, then add_or_update_event."
        + _NO_DIAGNOSIS
    )


@mcp.prompt()
def fueling_review(activity_id: str = "", weeks: int = 12) -> str:
    """Fueling review of one activity or of the long sessions of the last weeks (carbs, fluid, sodium per hour)."""
    scope = (
        f"activity {activity_id}: get_fueling_analysis(activity_id='{activity_id}', detail_level='full')"
        if activity_id
        else f"the last {weeks} weeks: get_fueling_analysis with start_date {weeks} weeks back (period mode)"
    )
    return (
        f"Review the fueling of {scope}. Report carbohydrates used (Intervals.icu estimate) and ingested in g and "
        "g/h, the ingested share, fluid, sodium and sweat loss per hour where custom fields exist, grouped by sport, "
        "duration and intensity with sample sizes. Sessions without logged intake are missing data, not 0. Used "
        "versus ingested is no 1:1 energy deficit (body stores contribute). Compare with targets only when the "
        "athlete states them; give at most two concrete changes for the next long session." + _NO_DIAGNOSIS
    )


@mcp.prompt()
def plan_health_check(weeks: int = 4) -> str:
    """Check the planned weeks: load ramp, monotony, rest days, form, races and plan consistency (what-if, nothing written)."""
    return (
        f"Check the training plan of the next {weeks} weeks as an endurance coach. Call get_coach_context("
        "detail_level='compact') for the current state, then get_load_projection(detail_level='standard', end_date "
        f"{weeks} weeks ahead): weekly load and sessions, ramp, monotony, rest days, lowest form, the model check and "
        "the weeks outside the cited ranges, planned workouts without a load. get_training_plan shows phases, weekly "
        "targets and races; get_events the single workouts. For an alternative, simulate it with "
        "get_load_projection(scenario=...) (sessions or weekly blocks; never written to the calendar) and compare "
        "with the calendar plan. Report the problems with their numbers and at most two changes; write only after "
        "the athlete asks (validate_workout, then add_or_update_event)." + _NO_DIAGNOSIS
    )


@mcp.prompt()
def coach_handoff(end_date: str = "") -> str:
    """Compact summary for handing the athlete over to another coach or a new session."""
    day = f"end_date='{end_date}'" if end_date else "today"
    return (
        f"Write a compact handover summary (at most about 300 words) for another coach or a new chat session, as of {day}. "
        "Use get_coach_context(detail_level='compact'), get_sport_settings (thresholds per sport), "
        "get_activities(detail_level='compact', limit=10) for the recent key sessions, "
        "get_recovery_snapshot(detail_level='compact') and get_events for the next 14 days (planned workouts and "
        "races; get_training_plan for phases). Structure: athlete and sports with thresholds; current load, fitness "
        "and form; key sessions of the last two weeks with their activity ids; recovery markers against baselines "
        "with missing values named; upcoming races and the plan; open questions and agreements. Facts with dates and "
        "ids only, so the next coach can look them up." + _NO_DIAGNOSIS
    )


# ------------------------------------------------------------------ resources
@mcp.resource("intervals://custom-items")
async def custom_items_resource() -> str:
    """The athlete's custom item definitions (codes, names, units, types) as compact JSON."""
    athlete = default_athlete(config.athlete_id)
    if not athlete:
        return json.dumps({"error": "ATHLETE_ID is not configured"})
    index = await get_custom_item_index(athlete_id=athlete)
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
