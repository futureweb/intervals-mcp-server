"""
Intervals.icu MCP Server

This module implements a Model Context Protocol (MCP) server for connecting
Claude with the Intervals.icu API. It provides tools for retrieving and managing
athlete data, including activities, events, workouts, and wellness metrics.

Main Features:
    - Activity retrieval and detailed analysis
    - Event management (races, workouts, calendar items)
    - Wellness data tracking and visualization
    - Error handling with user-friendly messages
    - Configurable parameters with environment variable support

Usage:
    This server is designed to be run as a standalone script and exposes several MCP tools
    for use with Claude Desktop or other MCP-compatible clients. The server loads configuration
    from environment variables (optionally via a .env file) and communicates with the Intervals.icu API.

    To run the server:
        $ futureweb-intervals-mcp            (console script, see intervals_mcp_server.cli)
        $ python src/intervals_mcp_server/server.py   (same, kept for existing setups)

    MCP tools provided:
        - get_activities
        - get_activity_details
        - get_activity_intervals
        - get_activity_streams
        - list_activity_streams
        - get_activity_messages
        - add_activity_message
        - update_activity
        - get_events
        - get_event_by_id
        - add_or_update_event
        - add_events_bulk
        - delete_event
        - delete_events_by_date_range
        - get_wellness_data
        - update_wellness
        - get_athlete_power_curves
        - get_hr_curves
        - get_pace_curves
        - get_custom_items
        - get_custom_item_by_id
        - create_custom_item
        - update_custom_item
        - delete_custom_item

    See the README for more details on configuration and usage.
"""

if __name__ == "__main__":
    # Run as a script: let the CLI handle the flags and report configuration errors in one
    # line before the server modules (OAuth state, FastMCP settings) are imported.
    from intervals_mcp_server.cli import main as _cli_main

    raise SystemExit(_cli_main())

# pylint: disable=wrong-import-position
import logging  # noqa: E402

# Import API client and configuration
from intervals_mcp_server.api.client import (  # noqa: E402
    httpx_client,  # Re-export for backward compatibility with tests
    make_intervals_request,
)
from intervals_mcp_server.config import get_config  # noqa: E402
from intervals_mcp_server.mcp_instance import mcp, oauth_provider  # noqa: E402

# Import types and validation
from intervals_mcp_server.server_setup import (  # noqa: E402
    NETWORK_TRANSPORTS,
    configure_logging,
    setup_transport,
    start_server,
)
from intervals_mcp_server.utils.validation import validate_athlete_id  # noqa: E402

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("intervals_icu_mcp_server")

# Get configuration instance
config = get_config()

# Import tool modules to register them (tools register themselves via @mcp.tool() decorators)
# Import tool functions for re-export
from intervals_mcp_server.tools.activities import (  # pylint: disable=wrong-import-position  # noqa: E402
    add_activity_message,
    get_activities,
    get_activity_details,
    get_activity_intervals,
    get_activity_messages,
    get_activity_streams,
    list_activity_streams,
    update_activity,
)
from intervals_mcp_server.tools.events import (  # pylint: disable=wrong-import-position  # noqa: E402
    get_training_plan,
    add_events_bulk,
    add_or_update_event,
    delete_event,
    delete_events_by_date_range,
    get_event_by_id,
    get_events,
)
from intervals_mcp_server.tools.gear import get_gear_list  # pylint: disable=wrong-import-position  # noqa: E402
from intervals_mcp_server.tools.workout_library import (  # pylint: disable=wrong-import-position  # noqa: E402
    get_library_workout,
    add_event_from_library,
    delete_library_workout,
    create_library_workout,
    get_workout_library,
)
from intervals_mcp_server.tools.wellness import get_wellness_data, update_wellness  # pylint: disable=wrong-import-position  # noqa: E402
from intervals_mcp_server.tools.power_curves import get_athlete_power_curves  # pylint: disable=wrong-import-position  # noqa: E402
from intervals_mcp_server.tools.hr_pace_curves import get_hr_curves, get_pace_curves  # pylint: disable=wrong-import-position  # noqa: E402
from intervals_mcp_server.tools.training_review import (  # pylint: disable=wrong-import-position  # noqa: E402
    get_plan_compliance,
    get_weekly_summary,
)
from intervals_mcp_server.tools.custom_items import (  # pylint: disable=wrong-import-position  # noqa: E402
    create_custom_item,
    delete_custom_item,
    get_custom_item_by_id,
    get_custom_items,
    update_custom_item,
)
from intervals_mcp_server.tools.athlete import (  # pylint: disable=wrong-import-position  # noqa: E402
    update_sport_settings,
    get_athlete_profile,
    get_sport_settings,
    get_training_zones,
)
from intervals_mcp_server.tools.gear import get_gear_details  # pylint: disable=wrong-import-position  # noqa: E402
from intervals_mcp_server.tools.analysis import (  # pylint: disable=wrong-import-position  # noqa: E402
    analyze_workout_execution,
    compare_power_streams,
)
from intervals_mcp_server.tools.climbs import analyze_climbs  # pylint: disable=wrong-import-position  # noqa: E402
from intervals_mcp_server.tools.report import get_activity_report  # pylint: disable=wrong-import-position  # noqa: E402
from intervals_mcp_server.tools.performance import (  # pylint: disable=wrong-import-position  # noqa: E402
    compare_best_efforts,
    compare_workouts,
    find_similar_intervals,
    get_activity_histogram,
    get_best_efforts,
    get_fatigue_resistance,
    get_power_hr_efficiency,
)
from intervals_mcp_server.tools.wellness_insights import (  # pylint: disable=wrong-import-position  # noqa: E402
    get_nutrition_summary,
    get_recovery_snapshot,
    get_wellness_trends,
)
from intervals_mcp_server.tools.summary import get_training_summary  # pylint: disable=wrong-import-position  # noqa: E402
from intervals_mcp_server.tools.training_load import (  # pylint: disable=wrong-import-position  # noqa: E402
    get_load_projection,
    get_training_load,
)
from intervals_mcp_server.tools.intensity import get_intensity_distribution  # pylint: disable=wrong-import-position  # noqa: E402
from intervals_mcp_server.tools.durability import get_durability  # pylint: disable=wrong-import-position  # noqa: E402
from intervals_mcp_server.tools.fatigue import (  # pylint: disable=wrong-import-position  # noqa: E402
    get_long_ride_fatigue_profile,
    get_submax_test_trends,
)
from intervals_mcp_server.tools.coach_context import get_coach_context  # pylint: disable=wrong-import-position  # noqa: E402
from intervals_mcp_server.tools.workout_check import (  # pylint: disable=wrong-import-position  # noqa: E402
    preview_workout,
    validate_workout,
)
from intervals_mcp_server.tools.status import get_server_status  # pylint: disable=wrong-import-position  # noqa: E402

# Re-export make_intervals_request and httpx_client for backward compatibility
# pylint: disable=duplicate-code  # This __all__ list is intentionally similar to tools/__init__.py
__all__ = [
    "make_intervals_request",
    "httpx_client",  # Re-exported for test compatibility
    "add_activity_message",
    "get_activities",
    "get_activity_details",
    "get_activity_intervals",
    "get_activity_messages",
    "get_activity_streams",
    "list_activity_streams",
    "update_activity",
    "get_events",
    "get_event_by_id",
    "delete_event",
    "delete_events_by_date_range",
    "add_or_update_event",
    "get_gear_list",
    "add_events_bulk",
    "get_wellness_data",
    "update_wellness",
    "get_athlete_power_curves",
    "get_hr_curves",
    "get_pace_curves",
    "get_weekly_summary",
    "get_plan_compliance",
    "get_workout_library",
    "create_library_workout",
    "add_event_from_library",
    "delete_library_workout",
    "get_custom_items",
    "get_custom_item_by_id",
    "create_custom_item",
    "update_custom_item",
    "delete_custom_item",
    "get_athlete_profile",
    "get_sport_settings",
    "get_training_zones",
    "get_library_workout",
    "get_training_plan",
    "update_sport_settings",
    "get_gear_details",
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
    "get_training_load",
    "get_load_projection",
    "get_intensity_distribution",
    "get_durability",
    "get_long_ride_fatigue_profile",
    "get_submax_test_trends",
    "get_coach_context",
    "preview_workout",
    "validate_workout",
    "get_server_status",
]


def run() -> None:
    """Validate the configuration and start the server (flags are handled by :mod:`intervals_mcp_server.cli`)."""
    # Validate ATHLETE_ID when server starts (not at import time to allow tests)
    validate_athlete_id(config.athlete_id)
    configure_logging(mcp.settings.log_level)

    # Setup transport and start server
    selected_transport = setup_transport()
    if selected_transport in NETWORK_TRANSPORTS:
        missing = [name for name, value in (("API_KEY", config.api_key), ("ATHLETE_ID", config.athlete_id)) if not value]
        if missing:
            logger.warning("%s not set: every tool call needs it as an argument and will fail otherwise", " and ".join(missing))
    start_server(mcp, selected_transport, provider=oauth_provider)


def main() -> None:
    """Entry point kept for existing callers: same as the ``futureweb-intervals-mcp`` command."""
    from intervals_mcp_server.cli import main as cli_main  # pylint: disable=import-outside-toplevel

    raise SystemExit(cli_main())
