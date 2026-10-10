"""
Configuration management for Intervals.icu MCP Server.

This module handles loading and validation of configuration from environment variables.
"""

import os
from dataclasses import dataclass, field

from intervals_mcp_server.toolsets import DEFAULT_TOOLSET, parse_toolset
from intervals_mcp_server.utils.validation import validate_athlete_id

# Try to load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv

    _ = load_dotenv()
except ImportError:
    # python-dotenv not installed, proceed without it
    pass


@dataclass
class Config:  # pylint: disable=too-many-instance-attributes
    """Configuration settings for the Intervals.icu MCP Server."""

    api_key: str
    athlete_id: str
    intervals_api_base_url: str
    user_agent: str
    # Display units for custom item codes whose definition has none or unspecific ones,
    # e.g. {"Stamina": "%"}; configured via CUSTOM_UNITS_OVERRIDES="Stamina=%,RecoveryTime=h".
    custom_units_overrides: dict[str, str] = field(default_factory=dict)
    # Aggregation policy per custom field code across activities (sum, device_load_sum, trend,
    # mean, none); configured via CUSTOM_AGGREGATE_OVERRIDES="TrainingLoad=device_load_sum".
    custom_aggregate_overrides: dict[str, str] = field(default_factory=dict)
    # Enabled tool permission classes (MCP_PERMISSIONS="read,write"); default read-only.
    permissions: frozenset[str] = frozenset({"read"})
    # Tool set (MCP_TOOLSET="full" or "core"); see toolsets.py.
    toolset: str = DEFAULT_TOOLSET


_config_instance: Config | None = None  # pylint: disable=invalid-name


def load_config() -> Config:
    """
    Load configuration from environment variables.

    Returns:
        Config: Configuration instance with loaded values.

    Raises:
        ValueError: If athlete_id is invalid (when non-empty), MCP_PERMISSIONS or MCP_TOOLSET unknown.
    """
    api_key = os.getenv("API_KEY", "")
    athlete_id = os.getenv("ATHLETE_ID", "")
    intervals_api_base_url = os.getenv("INTERVALS_API_BASE_URL", "https://intervals.icu/api/v1")
    user_agent = "intervalsicu-mcp-server/1.0"

    # Validate athlete_id if provided (empty string is allowed)
    if athlete_id:
        validate_athlete_id(athlete_id)

    return Config(
        api_key=api_key,
        athlete_id=athlete_id,
        intervals_api_base_url=intervals_api_base_url,
        user_agent=user_agent,
        custom_units_overrides=parse_units_overrides(os.getenv("CUSTOM_UNITS_OVERRIDES", "")),
        custom_aggregate_overrides=parse_units_overrides(os.getenv("CUSTOM_AGGREGATE_OVERRIDES", "")),
        permissions=parse_permissions(os.getenv("MCP_PERMISSIONS", "read")),
        toolset=parse_toolset(os.getenv("MCP_TOOLSET", "")),
    )


PERMISSION_CLASSES: tuple[str, ...] = ("read", "write", "destructive", "admin")


def parse_permissions(raw: str) -> frozenset[str]:
    """Parse MCP_PERMISSIONS ("read,write", "all", "" = read) into the enabled classes.

    Raises:
        ValueError: If an unknown class name is given.
    """
    names = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not names:
        return frozenset({"read"})
    if "all" in names:
        return frozenset(PERMISSION_CLASSES)
    unknown = sorted(names - set(PERMISSION_CLASSES))
    if unknown:
        raise ValueError(
            f"MCP_PERMISSIONS contains unknown class(es) {', '.join(unknown)}; "
            f"use a comma-separated subset of {', '.join(PERMISSION_CLASSES)} or 'all'"
        )
    return frozenset(names | {"read"})


def parse_units_overrides(raw: str) -> dict[str, str]:
    """Parse 'Code=unit,Other=unit' into a dict (blank entries ignored)."""
    overrides: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        code, units = part.split("=", 1)
        if code.strip() and units.strip():
            overrides[code.strip()] = units.strip()
    return overrides


def get_config() -> Config:
    """
    Get the configuration instance (singleton pattern).

    Returns:
        Config: The configuration instance.
    """
    global _config_instance  # pylint: disable=global-statement  # noqa: PLW0603 - singleton pattern
    if _config_instance is None:
        _config_instance = load_config()
    return _config_instance
