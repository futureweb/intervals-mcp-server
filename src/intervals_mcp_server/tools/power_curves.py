"""
Power curve MCP tools for Intervals.icu.

This module contains tools for retrieving athlete power curve data.
"""

import json
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.utils.formatting import format_power_curves
from intervals_mcp_server.utils.params import AthleteId, Environment
from intervals_mcp_server.utils.validation import resolve_athlete_id

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

# 5s, 15s, 30s, 1min, 2min, 5min, 10min, 20min, 60min
DEFAULT_DURATIONS: tuple[int, ...] = (5, 15, 30, 60, 120, 300, 600, 1200, 3600)


def _build_curves_param(
    this_season: bool,
    last_season: bool,
    start_date: str | None,
    end_date: str | None,
) -> list[str]:
    """Build the curves query parameter list based on user selections.

    Args:
        this_season: Whether to include this season's curve.
        last_season: Whether to include last season's curve.
        start_date: Optional start date for a custom date range curve.
        end_date: Optional end date for a custom date range curve.

    Returns:
        List of curve identifiers for the API request.
    """
    curves: list[str] = []
    if this_season:
        curves.append("s0")
    if last_season:
        curves.append("s1")
    if start_date and end_date:
        curves.append(f"r.{start_date}.{end_date}")
    return curves


def _validate_dates(start_date: str | None, end_date: str | None) -> str | None:
    """Validate that start_date and end_date are either both provided or both absent.

    Returns:
        An error message if validation fails, otherwise None.
    """
    if (start_date is None) != (end_date is None):
        return "Error: Both start_date and end_date must be provided together for a custom date range."
    if start_date and end_date:
        try:
            s = datetime.strptime(start_date, "%Y-%m-%d")
            e = datetime.strptime(end_date, "%Y-%m-%d")
            if s >= e:
                return "Error: start_date must be before end_date."
        except ValueError:
            return "Error: Dates must be in YYYY-MM-DD format."
    return None


def _extract_curve_data(
    curve: dict[str, Any],
    durations: list[int],
    include_normalised: bool,
) -> dict[str, Any]:
    """Extract power data for requested durations from a single curve.

    Args:
        curve: A single curve object from the API response.
        durations: List of durations in seconds to extract.
        include_normalised: Whether to include W/kg data.

    Returns:
        Dictionary with curve metadata and extracted data points.
    """
    secs = curve.get("secs", [])
    values = curve.get("values", [])
    activity_ids = curve.get("activity_id", [])
    watts_per_kg = curve.get("watts_per_kg", [])
    wkg_activity_ids = curve.get("wkg_activity_id", [])

    # Build a lookup from seconds to index for efficient access
    sec_to_idx: dict[int, int] = {s: i for i, s in enumerate(secs)}

    data_points: list[dict[str, Any]] = []
    for dur in durations:
        idx = sec_to_idx.get(dur)
        if idx is None or idx >= len(values):
            continue
        point: dict[str, Any] = {
            "secs": dur,
            "watts": values[idx],
            "activity_id": (
                activity_ids[idx]
                if idx < len(activity_ids) and activity_ids[idx] is not None
                else ""
            ),
        }
        if include_normalised and idx < len(watts_per_kg):
            point["watts_per_kg"] = round(watts_per_kg[idx], 2)
            point["wkg_activity_id"] = (
                wkg_activity_ids[idx]
                if idx < len(wkg_activity_ids)
                and wkg_activity_ids[idx] is not None
                else ""
            )
        data_points.append(point)

    return {
        "id": curve.get("id", ""),
        "label": curve.get("label", curve.get("id", "")),
        "start": curve.get("start_date_local", ""),
        "end": curve.get("end_date_local", ""),
        "data_points": data_points,
    }


@tool("read")
async def get_athlete_power_curves(
    activity_type: Annotated[str, Field(description="Activity type of the curves, e.g. Ride, VirtualRide, Run")] = "Ride",
    durations: Annotated[
        list[int] | None,
        Field(description="Durations in seconds as a JSON array of integers, e.g. [5, 60, 300, 1200]; default 5 s to 60 min"),
    ] = None,
    indoor_outdoor: Annotated[Environment, Field(description="Only indoor or outdoor activities; omit for both")] = None,
    start_date: Annotated[str | None, Field(description="First day YYYY-MM-DD of an extra custom-range curve; needs end_date")] = None,
    end_date: Annotated[str | None, Field(description="Last day YYYY-MM-DD of the custom-range curve, after start_date")] = None,
    this_season: Annotated[bool, Field(description="Include this season's curve (s0)")] = True,
    last_season: Annotated[bool, Field(description="Include last season's curve (s1)")] = True,
    include_normalised: Annotated[bool, Field(description="Include W/kg values")] = True,
    athlete_id: AthleteId = None,
) -> str:
    """Use for an athlete's best power per duration over whole periods: season bests, progress between seasons or date ranges.

    Returns watts and W/kg per duration for this season, last season and/or a custom range,
    each with the activity that set it. Defaults: Ride, 5 s to 60 min, both seasons;
    durations that are not curve points are left out. Read-only, one API call. One activity:
    get_best_efforts; HR and pace: get_hr_curves, get_pace_curves. Method:
    intervals://methods/comparisons (get_guide).
    """
    if durations is None:
        durations = list(DEFAULT_DURATIONS)

    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    if indoor_outdoor and indoor_outdoor not in ("indoor", "outdoor"):
        return "Error: indoor_outdoor must be 'indoor', 'outdoor', or omitted."

    date_error = _validate_dates(start_date, end_date)
    if date_error:
        return date_error

    curves = _build_curves_param(this_season, last_season, start_date, end_date)
    if not curves:
        return "Error: At least one curve must be selected (this_season, last_season, or a date range)."

    params: dict[str, Any] = {
        "curves": curves,
        "type": activity_type,
        "includeRanks": False,
    }
    if indoor_outdoor:
        params["filters"] = json.dumps(
            [{"field_id": "indoor", "value": indoor_outdoor, "id": 1}]
        )

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/power-curves",
        params=params,
    )

    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error fetching power curves: {error_message}"

    # Response has a "list" key containing curve objects
    curve_list: list[dict[str, Any]] = []
    if isinstance(result, dict):
        curve_list = result.get("list", [])
    elif isinstance(result, list):
        curve_list = result

    if not curve_list:
        return f"No power curve data found for athlete {athlete_id_to_use} ({activity_type})."

    extracted: list[dict[str, Any]] = []
    for curve in curve_list:
        if isinstance(curve, dict):
            extracted.append(_extract_curve_data(curve, durations, include_normalised))

    if not extracted:
        return f"No power curve data found for athlete {athlete_id_to_use} ({activity_type})."

    return format_power_curves(extracted, activity_type, include_normalised)
