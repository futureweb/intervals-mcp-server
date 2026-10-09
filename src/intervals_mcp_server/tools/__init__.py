"""
MCP tools registry for Intervals.icu MCP Server.

This module registers all available MCP tools with the FastMCP server instance.
"""

from mcp.server.fastmcp import FastMCP  # pylint: disable=import-error

# Import all tools for re-export
# Note: Tools register themselves via @mcp.tool() decorators when imported
from intervals_mcp_server.tools.activities import (  # noqa: F401
    get_activities,
    get_activity_details,
    get_activity_intervals,
    get_activity_streams,
    list_activity_streams,
    update_activity,
)
from intervals_mcp_server.tools.events import (  # noqa: F401
    add_events_bulk,
    add_or_update_event,
    delete_event,
    delete_events_by_date_range,
    get_event_by_id,
    get_events,
)
from intervals_mcp_server.tools.custom_items import (  # noqa: F401
    create_custom_item,
    delete_custom_item,
    get_custom_item_by_id,
    get_custom_items,
    update_custom_item,
)
from intervals_mcp_server.tools.power_curves import (  # noqa: F401
    get_athlete_power_curves,
)
from intervals_mcp_server.tools.hr_pace_curves import (  # noqa: F401
    get_hr_curves,
    get_pace_curves,
)
from intervals_mcp_server.tools.gear import get_gear_details, get_gear_list  # noqa: F401
from intervals_mcp_server.tools.athlete import (  # noqa: F401
    get_athlete_profile,
    get_sport_settings,
    get_training_zones,
)
from intervals_mcp_server.tools.analysis import (  # noqa: F401
    analyze_workout_execution,
    compare_power_streams,
)
from intervals_mcp_server.tools.climbs import analyze_climbs  # noqa: F401
from intervals_mcp_server.tools.report import get_activity_report  # noqa: F401
from intervals_mcp_server.tools.performance import (  # noqa: F401
    compare_best_efforts,
    compare_workouts,
    find_similar_intervals,
    get_activity_histogram,
    get_best_efforts,
    get_fatigue_resistance,
    get_power_hr_efficiency,
)
from intervals_mcp_server.tools.wellness_insights import (  # noqa: F401
    get_nutrition_summary,
    get_recovery_snapshot,
    get_wellness_trends,
)
from intervals_mcp_server.tools.summary import get_training_summary  # noqa: F401
from intervals_mcp_server.tools.workout_check import preview_workout, validate_workout  # noqa: F401
from intervals_mcp_server.tools.status import get_server_status  # noqa: F401
from intervals_mcp_server.tools.training_review import (  # noqa: F401
    get_plan_compliance,
    get_weekly_summary,
)
from intervals_mcp_server.tools.workout_library import (  # noqa: F401
    add_event_from_library,
    delete_library_workout,
    create_library_workout,
    get_workout_library,
)
from intervals_mcp_server.tools.wellness import get_wellness_data, update_wellness  # noqa: F401


def register_tools(mcp_instance: FastMCP) -> None:
    """
    Register all MCP tools with the FastMCP server instance.

    This function imports all tool modules, which causes their @mcp.tool()
    decorators to register the tools. The tools need access to the mcp instance,
    so they will be imported after the mcp instance is created.

    Args:
        mcp_instance (FastMCP): The FastMCP server instance to register tools with.
    """
    # Tools are registered via decorators when modules are imported above
    # The mcp_instance parameter is kept for future use if needed
    _ = mcp_instance


__all__ = [
    "register_tools",
    "get_activities",
    "get_activity_details",
    "get_activity_intervals",
    "get_activity_streams",
    "list_activity_streams",
    "update_activity",
    "get_events",
    "get_event_by_id",
    "delete_event",
    "delete_events_by_date_range",
    "add_or_update_event",
    "add_events_bulk",
    "get_custom_items",
    "get_custom_item_by_id",
    "create_custom_item",
    "update_custom_item",
    "delete_custom_item",
    "get_athlete_power_curves",
    "get_hr_curves",
    "get_pace_curves",
    "get_gear_list",
    "get_gear_details",
    "get_athlete_profile",
    "get_sport_settings",
    "get_training_zones",
    "get_library_workout",
    "get_training_plan",
    "update_sport_settings",
    "analyze_workout_execution",
    "analyze_climbs",
    "get_activity_report",
    "compare_best_efforts",
    "compare_workouts",
    "find_similar_intervals",
    "get_activity_histogram",
    "get_best_efforts",
    "get_fatigue_resistance",
    "get_power_hr_efficiency",
    "compare_power_streams",
    "get_nutrition_summary",
    "get_recovery_snapshot",
    "get_wellness_trends",
    "get_training_summary",
    "preview_workout",
    "validate_workout",
    "get_server_status",
    "get_wellness_data",
    "get_weekly_summary",
    "get_plan_compliance",
    "get_workout_library",
    "create_library_workout",
    "add_event_from_library",
    "delete_library_workout",
    "update_wellness",
]
