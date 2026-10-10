"""
Heart rate and pace curve MCP tools for Intervals.icu.

This module contains tools for retrieving athlete heart rate curves (best average HR
per duration) and pace curves (best time per distance). Together they are useful to
track progress, e.g. the same pace at a lower heart rate.
"""

from typing import Annotated, Any

from pydantic import Field

from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.power_curves import _validate_dates
from intervals_mcp_server.utils.formatting import format_hr_curves, format_pace_curves
from intervals_mcp_server.utils.params import AthleteId
from intervals_mcp_server.utils.validation import resolve_athlete_id

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool

config = get_config()

# 5s, 15s, 30s, 1min, 5min, 10min, 20min, 30min, 60min
DEFAULT_HR_DURATIONS: tuple[int, ...] = (5, 15, 30, 60, 300, 600, 1200, 1800, 3600)

# 400m, 800m, 1km, 3km, 5km, 10km, half marathon, marathon (metres)
DEFAULT_RUN_DISTANCES: tuple[float, ...] = (400, 800, 1000, 3000, 5000, 10000, 21097.5, 42195)

# Default swim distances in metres (reported as not available if the curve lacks them)
DEFAULT_SWIM_DISTANCES: tuple[float, ...] = (50, 100, 200, 300, 400)

DEFAULT_CURVES: tuple[str, ...] = ("90d",)

SWIM_TYPES: tuple[str, ...] = ("Swim", "OpenWaterSwim")

# Tolerance in metres when matching requested distances to curve distances
_DISTANCE_TOLERANCE = 1.0

CurveIds = Annotated[
    list[str] | None,
    Field(
        description='Curve ids as a JSON array of strings, e.g. ["90d", "1y"], "s0" this season, "s1" last season, '
        '"r.2026-01-01.2026-03-01" a range; default ["90d"]'
    ),
]
RangeStart = Annotated[str | None, Field(description="First day YYYY-MM-DD of an extra custom-range curve; needs end_date")]
RangeEnd = Annotated[str | None, Field(description="Last day YYYY-MM-DD of the custom-range curve, after start_date")]


def _build_curve_ids(
    curves: list[str] | None,
    start_date: str | None,
    end_date: str | None,
) -> list[str]:
    """Build the `curves` query parameter from curve ids and an optional date range.

    Args:
        curves: Curve identifiers such as "90d", "1y", "s0" or "r.2026-01-01.2026-03-01".
            If None and no date range is given, the default ("90d") is used.
        start_date: Optional start date (YYYY-MM-DD) of a custom range.
        end_date: Optional end date (YYYY-MM-DD) of a custom range.

    Returns:
        List of curve identifiers for the API request.
    """
    ids = [c.strip() for c in (curves or []) if c and c.strip()]
    if start_date and end_date:
        ids.append(f"r.{start_date}.{end_date}")
    if not ids:
        ids = list(DEFAULT_CURVES)
    return ids


def _curve_meta(curve: dict[str, Any]) -> dict[str, Any]:
    """Extract common metadata from a curve object."""
    return {
        "id": curve.get("id", ""),
        "label": curve.get("label", curve.get("id", "")),
        "start": curve.get("start_date_local", ""),
        "end": curve.get("end_date_local", ""),
    }


def _activity_id_at(activity_ids: list[Any], idx: int) -> str:
    """Return the activity id at an index or an empty string."""
    if idx < len(activity_ids) and activity_ids[idx] is not None:
        return str(activity_ids[idx])
    return ""


def _extract_hr_curve(curve: dict[str, Any], durations: list[int]) -> dict[str, Any]:
    """Extract best average HR (bpm) for the requested durations from a single curve."""
    secs = curve.get("secs", [])
    values = curve.get("values", [])
    activity_ids = curve.get("activity_id", [])
    sec_to_idx: dict[int, int] = {s: i for i, s in enumerate(secs)}

    data_points: list[dict[str, Any]] = []
    missing: list[int] = []
    for dur in durations:
        idx = sec_to_idx.get(dur)
        if idx is None or idx >= len(values) or not values[idx]:
            missing.append(dur)
            continue
        data_points.append(
            {
                "secs": dur,
                "bpm": values[idx],
                "activity_id": _activity_id_at(activity_ids, idx),
            }
        )
    return {**_curve_meta(curve), "data_points": data_points, "missing": missing}


def _extract_pace_curve(curve: dict[str, Any], distances: list[float]) -> dict[str, Any]:
    """Extract best times for the requested distances (metres) from a single curve."""
    curve_distances = curve.get("distance", [])
    values = curve.get("values", [])
    activity_ids = curve.get("activity_id", [])

    data_points: list[dict[str, Any]] = []
    missing: list[float] = []
    for dist in distances:
        idx = next(
            (i for i, d in enumerate(curve_distances) if abs(d - dist) <= _DISTANCE_TOLERANCE),
            None,
        )
        if idx is None or idx >= len(values) or not values[idx]:
            missing.append(dist)
            continue
        data_points.append(
            {
                "distance": dist,
                "secs": values[idx],
                "activity_id": _activity_id_at(activity_ids, idx),
            }
        )
    return {**_curve_meta(curve), "data_points": data_points, "missing": missing}


async def _fetch_curves(
    endpoint: str,
    params: dict[str, Any],
    athlete_id: str | None,
    label: str,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Fetch a curve set and return (curve_list, error_message)."""
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return None, error_msg

    result = await make_intervals_request(
        url=f"/athlete/{seg(athlete_id_to_use)}/{endpoint}",
        params=params,
    )
    if isinstance(result, dict) and "error" in result:
        return None, f"Error fetching {label}: {result.get('message', 'Unknown error')}"

    curve_list: list[Any] = []
    if isinstance(result, dict):
        curve_list = result.get("list", [])
    elif isinstance(result, list):
        curve_list = result
    return [c for c in curve_list if isinstance(c, dict)], None


@tool("read")
async def get_hr_curves(
    activity_type: Annotated[str, Field(description="Activity type of the curves, e.g. Run, Ride, Swim")] = "Run",
    durations: Annotated[
        list[int] | None,
        Field(description="Durations in seconds as a JSON array of integers, e.g. [60, 300, 1200]; default 5 s to 60 min"),
    ] = None,
    curves: CurveIds = None,
    start_date: RangeStart = None,
    end_date: RangeEnd = None,
    athlete_id: AthleteId = None,
) -> str:
    """Use for the highest average heart rate an athlete sustained per duration over one or more periods (e.g. comparing periods; with get_pace_curves the same pace at a lower HR).

    Returns bpm per duration for each curve (default the last 90 days) with the activity that
    set it; durations without data are listed as not available. Read-only, one API call.
    Power: get_athlete_power_curves. Method: intervals://methods/comparisons (get_guide).
    """
    date_error = _validate_dates(start_date, end_date)
    if date_error:
        return date_error

    durations_to_use = list(durations) if durations else list(DEFAULT_HR_DURATIONS)
    params: dict[str, Any] = {
        "curves": _build_curve_ids(curves, start_date, end_date),
        "type": activity_type,
    }

    curve_list, error = await _fetch_curves("hr-curves", params, athlete_id, "HR curves")
    if error:
        return error
    if not curve_list:
        return f"No HR curve data found ({activity_type})."

    extracted = [_extract_hr_curve(c, durations_to_use) for c in curve_list]
    return format_hr_curves(extracted, activity_type)


@tool("read")
async def get_pace_curves(
    activity_type: Annotated[str, Field(description="Activity type of the curves, e.g. Run, TrailRun, Swim")] = "Run",
    distances: Annotated[
        list[float] | None,
        Field(description="Distances in metres as a JSON array of numbers, e.g. [1000, 5000, 21097.5]; default 400 m to marathon, swims 50-400 m"),
    ] = None,
    curves: CurveIds = None,
    start_date: RangeStart = None,
    end_date: RangeEnd = None,
    gap: Annotated[bool, Field(description="Gradient-adjusted pace (GAP) instead of raw pace")] = False,
    athlete_id: AthleteId = None,
) -> str:
    """Use for an athlete's best times per distance over one or more periods: PBs, comparing periods, progress in running or swimming.

    Returns the best time and pace (min/km; min/100 m for Swim and OpenWaterSwim) per
    distance for each curve (default the last 90 days) with the activity that set it;
    optionally gradient-adjusted pace. Distances not on the curve are listed as not
    available. Read-only, one API call. HR side: get_hr_curves. Method:
    intervals://methods/comparisons (get_guide).
    """
    date_error = _validate_dates(start_date, end_date)
    if date_error:
        return date_error

    is_swim = activity_type in SWIM_TYPES
    if distances:
        distances_to_use = [float(d) for d in distances]
    else:
        distances_to_use = list(DEFAULT_SWIM_DISTANCES if is_swim else DEFAULT_RUN_DISTANCES)

    params: dict[str, Any] = {
        "curves": _build_curve_ids(curves, start_date, end_date),
        "type": activity_type,
    }
    if gap:
        params["gap"] = True

    curve_list, error = await _fetch_curves(
        "pace-curves", params, athlete_id, "pace curves"
    )
    if error:
        return error
    if not curve_list:
        return f"No pace curve data found ({activity_type})."

    extracted = [_extract_pace_curve(c, distances_to_use) for c in curve_list]
    return format_pace_curves(extracted, activity_type, gap, per_100m=is_swim)
