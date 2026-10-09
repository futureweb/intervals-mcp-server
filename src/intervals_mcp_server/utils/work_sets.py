"""
Comparable work intervals of an activity.

Intervals.icu labels intervals WORK or RECOVERY, but a WORK label alone does not make two
efforts comparable: a ride may contain a warm-up or recoveries labelled WORK, three
10-minute threshold intervals and a 50-second sprint. Averaging all of them mixes very
different efforts (3 x 250 W and 1 x 432 W give 296 W).

This module splits the WORK intervals of one activity into

- ``surges``: efforts shorter than ``surge_max_secs`` (sprints, attacks, short surges),
- ``excluded``: intervals below ``work_min_pct`` of FTP (warm-ups / recoveries labelled
  WORK) or outside explicit duration / intensity filters, with the reason,
- ``main``: the largest group (by total time) of intervals of similar duration (within
  ``duration_tol_pct`` of the group median) and intensity (within ``intensity_tol_pts``
  percentage points of FTP), and
- ``other``: the remaining longer intervals,

and summarises a set with time-weighted means (weights = moving time of each interval),
average and maximum HR per interval and the W/bpm ratio of the time-weighted means.
Nothing is interpolated; intervals without power are kept but contribute no power.
"""

import statistics
from typing import Any

from intervals_mcp_server.utils.custom_fields import is_missing

SURGE_MAX_SECS = 120
WORK_MIN_PCT = 70.0
DURATION_TOL_PCT = 25.0
INTENSITY_TOL_PTS = 8.0


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or is_missing(value):
        return None
    return float(value)


def interval_secs(interval: dict[str, Any]) -> float:
    """Moving time of an interval (elapsed time when moving time is missing)."""
    return _num(interval.get("moving_time")) or _num(interval.get("elapsed_time")) or 0.0


def interval_pct(interval: dict[str, Any], ftp: float | None) -> float | None:
    """Average power in % of FTP (the intensity Intervals.icu stores, else computed)."""
    stored = _num(interval.get("intensity"))
    watts = _num(interval.get("average_watts"))
    if ftp and watts is not None:
        return watts / ftp * 100
    return stored


def _cluster(intervals: list[dict[str, Any]], ftp: float | None, duration_tol_pct: float, intensity_tol_pts: float) -> list[list[dict[str, Any]]]:
    """Greedy grouping of intervals with similar duration and intensity (order of the ride kept)."""
    clusters: list[list[dict[str, Any]]] = []
    for interval in intervals:
        secs, pct = interval_secs(interval), interval_pct(interval, ftp)
        placed = False
        for cluster in clusters:
            med_secs = statistics.median(interval_secs(i) for i in cluster)
            pcts = [p for i in cluster if (p := interval_pct(i, ftp)) is not None]
            close_duration = abs(secs - med_secs) <= med_secs * duration_tol_pct / 100
            close_intensity = pct is None or not pcts or abs(pct - statistics.median(pcts)) <= intensity_tol_pts
            if close_duration and close_intensity:
                cluster.append(interval)
                placed = True
                break
        if not placed:
            clusters.append([interval])
    return clusters


def split_work(  # pylint: disable=too-many-arguments,too-many-locals
    intervals: list[dict[str, Any]],
    ftp: float | None,
    *,
    surge_max_secs: float = SURGE_MAX_SECS,
    work_min_pct: float | None = WORK_MIN_PCT,
    min_secs: float | None = None,
    max_secs: float | None = None,
    min_pct: float | None = None,
    max_pct: float | None = None,
    duration_tol_pct: float = DURATION_TOL_PCT,
    intensity_tol_pts: float = INTENSITY_TOL_PTS,
) -> dict[str, Any]:
    """Split the WORK intervals of one activity into main set, other, surges and excluded."""
    work = [i for i in intervals if isinstance(i, dict) and str(i.get("type")) == "WORK" and interval_secs(i) > 0]
    surges: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for interval in work:
        secs, pct = interval_secs(interval), interval_pct(interval, ftp)
        if secs < surge_max_secs and (min_secs is None or secs < min_secs):
            surges.append(interval)
            continue
        reason = None
        if min_secs is not None and secs < min_secs or max_secs is not None and secs > max_secs:
            reason = "outside the requested duration range"
        elif pct is not None and (min_pct is not None and pct < min_pct or max_pct is not None and pct > max_pct):
            reason = "outside the requested intensity range"
        elif pct is not None and work_min_pct is not None and min_pct is None and pct < work_min_pct:
            reason = f"below {work_min_pct:.0f}% of FTP (warm-up / recovery labelled WORK)"
        if reason:
            excluded.append({"interval": interval, "reason": reason})
        else:
            candidates.append(interval)
    clusters = _cluster(candidates, ftp, duration_tol_pct, intensity_tol_pts)
    main: list[dict[str, Any]] = []
    if clusters:
        main = max(clusters, key=lambda c: (sum(interval_secs(i) for i in c), statistics.median(interval_pct(i, ftp) or 0 for i in c)))
    other = [i for i in candidates if not any(i is m for m in main)]
    return {"main": main, "other": other, "surges": surges, "excluded": excluded}


def _tw_mean(intervals: list[dict[str, Any]], key: str) -> float | None:
    pairs = [(v, interval_secs(i)) for i in intervals if (v := _num(i.get(key))) is not None and interval_secs(i) > 0]
    weight = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / weight if weight else None


def _max(intervals: list[dict[str, Any]], key: str) -> float | None:
    values = [v for i in intervals if (v := _num(i.get(key))) is not None]
    return max(values) if values else None


def set_summary(intervals: list[dict[str, Any]], ftp: float | None) -> dict[str, Any]:
    """Time-weighted summary of a set of intervals plus the per-interval values."""
    if not intervals:
        return {"count": 0}
    watts, hr = _tw_mean(intervals, "average_watts"), _tw_mean(intervals, "average_heartrate")
    secs = [interval_secs(i) for i in intervals]
    return {
        "count": len(intervals),
        "secs_median": statistics.median(secs),
        "total_secs": sum(secs),
        "avg_watts": _round(watts, 1),
        "np_watts": _round(_tw_mean(intervals, "weighted_average_watts"), 1),
        "avg_hr": _round(hr, 1),
        "max_hr": _max(intervals, "max_heartrate"),
        "cadence": _round(_tw_mean(intervals, "average_cadence"), 1),
        "pct_ftp": _round(watts / ftp * 100, 1) if watts is not None and ftp else None,
        "w_per_bpm": _round(watts / hr, 3) if watts is not None and hr else None,
        "intervals": [
            {
                "start_time": i.get("start_time"), "secs": interval_secs(i), "avg_watts": _num(i.get("average_watts")),
                "max_watts": _num(i.get("max_watts")), "avg_hr": _num(i.get("average_heartrate")),
                "max_hr": _num(i.get("max_heartrate")), "cadence": _round(_num(i.get("average_cadence")), 1),
                "pct_ftp": _round(interval_pct(i, ftp), 1),
            }
            for i in intervals
        ],
    }


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def pattern_of(summary: dict[str, Any]) -> dict[str, Any] | None:
    """Duration and intensity of a main set, used as the reference pattern for comparisons."""
    if not summary.get("count"):
        return None
    return {"count": summary["count"], "secs": summary["secs_median"], "pct_ftp": summary.get("pct_ftp")}


def matches_pattern(
    summary: dict[str, Any], pattern: dict[str, Any], duration_tol_pct: float = DURATION_TOL_PCT, intensity_tol_pts: float = INTENSITY_TOL_PTS
) -> str | None:
    """None when a main set is comparable with the pattern, else the reason it is not."""
    if not summary.get("count"):
        return "no comparable work intervals"
    secs, ref = summary["secs_median"], pattern["secs"]
    if ref and abs(secs - ref) > ref * duration_tol_pct / 100:
        return f"interval length {secs / 60:.1f} min vs {ref / 60:.1f} min"
    pct, ref_pct = summary.get("pct_ftp"), pattern.get("pct_ftp")
    if pct is not None and ref_pct is not None and abs(pct - ref_pct) > intensity_tol_pts:
        return f"intensity {pct:.0f}% vs {ref_pct:.0f}% of FTP"
    return None
