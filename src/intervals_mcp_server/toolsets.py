"""
Tool sets: which of the defined tools a client sees (MCP_TOOLSET).

``full`` (default) registers every tool of the enabled permission classes. ``core`` registers a
curated subset for clients with a small tool budget (23 tools, under 12k tokens at
MCP_PERMISSIONS=read,write): the weekly overview, one-activity analysis, recovery, load and
planning tools plus the writes a coach needs most and the reads that go with them (read back an
event, plan phases, preview a workout, wellness trends). MCP_PERMISSIONS still applies inside a
set: the write tools of ``core`` exist only when ``write`` is enabled. The other tools stay
importable and are listed by get_server_status as outside the tool set; descriptions, prompts and
guides mark them "(full tool set)".
"""

TOOLSETS: tuple[str, ...] = ("full", "core")
DEFAULT_TOOLSET = "full"

# The curated set, grouped by purpose (README "Tool sets" lists the same).
CORE_TOOLS: frozenset[str] = frozenset({
    # orientation
    "get_server_status", "get_guide",
    # weekly review, load and plan
    "get_coach_context", "get_training_summary", "get_load_projection", "get_events", "get_event_by_id",
    "get_training_plan",
    # one activity
    "get_activities", "get_activity_report", "get_activity_details", "get_activity_intervals",
    "get_best_efforts", "get_fueling_analysis",
    # recovery
    "get_recovery_snapshot", "get_wellness_data", "get_wellness_trends",
    # planning
    "get_sport_settings", "validate_workout", "preview_workout",
    # writes (only with MCP_PERMISSIONS=write)
    "add_or_update_event", "update_wellness", "update_activity",
})


def parse_toolset(raw: str | None) -> str:
    """Parse MCP_TOOLSET ("" = full).

    Raises:
        ValueError: If the name is not one of TOOLSETS.
    """
    name = (raw or "").strip().lower() or DEFAULT_TOOLSET
    if name not in TOOLSETS:
        raise ValueError(f"MCP_TOOLSET must be one of {', '.join(TOOLSETS)}, got {raw!r}")
    return name


def in_toolset(name: str, toolset: str) -> bool:
    """Whether the tool *name* belongs to *toolset*."""
    return toolset == "full" or name in CORE_TOOLS
