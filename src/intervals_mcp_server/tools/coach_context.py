"""
Compact coaching context MCP tool for Intervals.icu (read-only).

get_coach_context combines, for one end date, the training load (acute/chronic, ratio,
monotony, strain, CTL/ATL/form), the intensity distribution of the last 7 and 28 days, the
recovery markers against the athlete's own 42-day baselines (numbers only), durability, the
most notable sessions and the plan of the next days in about 2-3k characters. It reuses the
metric modules of get_training_load, get_intensity_distribution and get_durability and needs
three API calls. The idea of one pre-computed coaching context comes from the coach report
by morritter in upstream pull request mvilanova/intervals-mcp-server#150.
"""

import json
from datetime import date, timedelta
from typing import Any

from intervals_mcp_server.tools.intensity import dist_text
from intervals_mcp_server.tools.training_load import (
    DURABILITY_FIELDS,
    FITNESS_FIELDS,
    LOAD_FIELDS,
    RACE_CATEGORIES,
    ZONE_FIELDS,
    fetch_activities,
    fetch_events,
    fetch_wellness,
    fitness_text,
    fmt,
    resolve_request,
    load_end_for,
    past_end,
)
from intervals_mcp_server.utils.durability import decoupling_summary, efficiency_summary
from intervals_mcp_server.utils.intensity import analyze_period
from intervals_mcp_server.utils.load_metrics import (
    REFERENCES,
    activity_day,
    activity_load,
    between,
    fitness_status,
    load_metrics,
    num,
    rnd,
    sport_breakdown,
    top_sessions,
    wellness_by_day,
    weekly_rows,
)
from intervals_mcp_server.utils.wellness_stats import compute_metric_trend

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import tool


BASELINE_DAYS = 42
RECOVERY_METRICS = (("hrv", "HRV", "ms"), ("restingHR", "resting HR", "bpm"), ("sleepSecs", "sleep", "h"))
PLAN_DAYS = 7
RACE_LOOKAHEAD_DAYS = 42


def _recovery(entries: list[dict[str, Any]], end: date) -> dict[str, Any]:
    """7-day mean of HRV, resting HR and sleep against the 42-day baseline (numbers only)."""
    out: dict[str, Any] = {}
    for metric, _, _ in RECOVERY_METRICS:
        trend = compute_metric_trend(entries, metric, windows=(7,), baseline_days=BASELINE_DAYS, period_end=end.isoformat())
        recent = trend["rolling"].get(7) or {}
        baseline = trend["baseline"]
        scale = 1 / 3600 if metric == "sleepSecs" else 1.0
        mean7 = recent.get("latest_mean")
        base_mean, base_sd = baseline.get("mean"), baseline.get("stdev")
        diff = mean7 - base_mean if mean7 is not None and base_mean is not None else None
        out[metric] = {
            "mean_7d": rnd(mean7 * scale, 2) if mean7 is not None else None, "n_7d": recent.get("latest_n", 0),
            "baseline_mean": rnd(base_mean * scale, 2) if base_mean is not None else None,
            "baseline_sd": rnd(base_sd * scale, 2) if base_sd is not None else None,
            "baseline_n": baseline.get("n", 0), "small_sample": baseline.get("small_sample", True),
            "diff": rnd(diff * scale, 2) if diff is not None else None,
            "diff_pct": rnd(diff / base_mean * 100, 1) if diff is not None and base_mean else None,
            "z": rnd(diff / base_sd, 2) if diff is not None and base_sd else None,
            "latest": trend.get("latest"),
        }
    return out


def _plan(events: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Planned WORKOUT events of the next 7 days (from tomorrow) and the next race."""
    plan_end = today + timedelta(days=PLAN_DAYS)
    workouts = [
        e for e in events
        if e.get("category") == "WORKOUT" and (day := activity_day(e)) is not None and today < day <= plan_end
    ]
    races = sorted(
        (e for e in events if e.get("category") in RACE_CATEGORIES and (day := activity_day(e)) is not None and day >= today),
        key=lambda e: str(e.get("start_date_local")),
    )
    loads = [activity_load(e) for e in workouts]
    return {
        "from": (today + timedelta(days=1)).isoformat(), "to": plan_end.isoformat(),
        "sessions": len(workouts), "load": rnd(sum(v for v in loads if v is not None), 0),
        "without_load": sum(1 for v in loads if v is None),
        "hours": rnd(sum(float(e.get("moving_time") or 0) for e in workouts) / 3600, 1),
        "next_race": (
            {"date": str(races[0].get("start_date_local"))[:10], "category": races[0].get("category"), "name": races[0].get("name")}
            if races else None
        ),
    }


def _recovery_text(recovery: dict[str, Any]) -> str:
    parts = []
    for metric, label, units in RECOVERY_METRICS:
        r = recovery[metric]
        if r["mean_7d"] is None:
            parts.append(f"{label} n/a")
            continue
        digits = 1 if metric == "sleepSecs" else 0
        compare = (
            f" vs {BASELINE_DAYS}-d {fmt(r['baseline_mean'], digits)} (n {r['baseline_n']}): {fmt(r['diff'], digits + 1, signed=True)} {units}"
            + (f", z {fmt(r['z'], 2, signed=True)}" if r["z"] is not None else "")
            + (" [small sample]" if r["small_sample"] else "")
            if r["baseline_mean"] is not None else ""
        )
        parts.append(f"{label} 7-d mean {fmt(r['mean_7d'], digits)} {units} (n {r['n_7d']}){compare}")
    return "; ".join(parts)


def _durability_text(durability: dict[str, Any], efficiency: dict[str, Any]) -> str:
    parts = []
    for family, entry in durability["by_sport"].items():
        if not entry["n"]:
            continue
        recent = entry["recent"]
        trend = f", last 7 d {fmt(recent['delta_pp'], 1, ' pp', signed=True)} ({recent['direction']})" if recent["direction"] else ""
        ef = efficiency.get(family) or {}
        ef_text = f", EF {fmt(ef['mean'], 2)} (n {ef['n']}{', small sample' if ef['small_sample'] else ''})" if ef.get("n") else ""
        small = ", small sample" if entry["small_sample"] else ""
        parts.append(
            f"{family} decoupling median {fmt(entry['median'], 1)} % (n {entry['n']}{small}, {entry['above_threshold']} > 5 %){trend}{ef_text}"
        )
    excluded = f"; {durability['excluded']} of {durability['considered']} sessions excluded by the quality filter"
    return ("; ".join(parts) or "no qualifying steady sessions") + excluded


def _text(payload: dict[str, Any], detail_level: str) -> str:  # pylint: disable=too-many-locals
    load, sports = payload["load"], payload["sports"]
    week = load["week"]
    intensity = payload["intensity"]
    lines = [
        f"Coach context for athlete {payload['athlete_id']} at {payload['end']} (load windows end {payload['load_end']}; "
        "Intervals.icu load, statistics only, no assessment):",
    ]
    if payload["load_end_note"]:
        lines.append(f"Note: {payload['load_end_note']}.")
    lines += [
        f"Load: 7 d {fmt(load['acute']['load'])} ({load['acute']['sessions']} sessions, {load['acute']['zero_load_days']} days with load 0) | "
        f"28 d {fmt(load['chronic']['load'])} ({fmt(load['chronic']['weekly_mean'])}/week) | ratio {fmt(load['acwr'], 2)} "
        f"({load['acwr_position'] or 'n/a'} 0.8-1.3{', small sample' if load['acwr_small_sample'] else ''}) | monotony "
        f"{fmt(week['monotony'], 2)}, strain {fmt(week['strain'])} | 7 d = {fmt(week['vs_chronic_weekly_mean_pct'])} % of the "
        f"28-d weekly mean{' (deload-like)' if week['deload_like'] else ''}",
        f"Fitness: {fitness_text(payload['fitness'])}",
        f"Intensity 7 d: {dist_text(intensity['last_7'])}",
        f"Intensity 28 d: {dist_text(intensity['last_28'])}",
        f"Recovery markers: {_recovery_text(payload['recovery'])}",
        f"Durability 28 d: {_durability_text(payload['durability'], payload['efficiency'])}",
    ]
    if detail_level != "compact":
        if sports["by_sport"]:
            lines.append("By sport (7 d / 28 d load, ratio): " + "; ".join(
                f"{family} {fmt(m['acute']['load'])}/{fmt(m['chronic']['load'])}, {fmt(m['acwr'], 2)}"
                for family, m in list(sports["by_sport"].items())[:4]
            ) + (f" | primary {sports['primary_sport']} monotony {fmt(sports['primary_monotony'], 2)} (total monotony includes cross-training)"
                 if sports["multi_sport"] else ""))
        drift = intensity["drift"]
        if drift.get("available"):
            lines.append(
                "Intensity drift (first vs second 14 d): " + ", ".join(f"Z{i + 1} {fmt(v, 1, signed=True)} pp" for i, v in enumerate(drift["delta_pp"]))
                + f"; class {drift['first']['class']} -> {drift['second']['class']}"
            )
        if payload["top_sessions"]:
            lines.append("Top sessions 7 d: " + "; ".join(
                f"{s['date'][5:]} {s['type']} {fmt(s['minutes'])} min load {fmt(s['load'])} IF {fmt(s['intensity_factor'], 2)}"
                for s in payload["top_sessions"]
            ))
        plan = payload["plan"]
        if plan:
            race = plan["next_race"]
            lines.append(
                f"Planned {plan['from']} to {plan['to']}: {plan['sessions']} workouts, load {fmt(plan['load'])}"
                + (f" ({plan['without_load']} without planned load)" if plan["without_load"] else "")
                + f", {fmt(plan['hours'], 1)} h" + (f" | next race {race['date']} {race['category']} '{race['name']}'" if race else "")
            )
        cov = payload["coverage"]
        lines.append(
            f"Coverage 28 d: {cov['sessions']} sessions, {cov['sessions_without_load']} without load, "
            f"{fmt(cov['zones_pct'])} % of moving time with zones; wellness days with HRV {cov['hrv_days']}, "
            f"resting HR {cov['rhr_days']}, sleep {cov['sleep_days']} of 28."
        )
    if detail_level == "full":
        lines.append("Weeks: " + " | ".join(
            f"{w['week']} load {fmt(w['load'])}, {w['rest_days']} rest d, monotony {fmt(w['monotony'], 2)}"
            + (f", {fmt(w['vs_chronic_weekly_mean_pct'])} %" if w["vs_chronic_weekly_mean_pct"] is not None else "")
            for w in payload["weeks"]
        ))
        lines.append(f"References: ACWR {REFERENCES['acwr']['source']}; monotony {REFERENCES['monotony']['source']}; "
                     "polarization index Treff et al. 2019, Front Physiol 10:707.")
    lines.append("Details: get_training_load, get_intensity_distribution, get_durability, get_recovery_snapshot, get_load_projection.")
    return "\n".join(lines)


@tool("read")
async def get_coach_context(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments,too-many-return-statements
    end_date: str | None = None,
    athlete_id: str | None = None,
    api_key: str | None = None,
    output_format: str = "text",
    detail_level: str = "standard",
) -> str:
    """Compact coaching context for a weekly review in one call (read-only, about 2-3k characters)

    Combines for the end date (default today): training load (7- and 28-day Intervals.icu
    load, acute:chronic ratio, Foster monotony and strain, the 7-day load as a share of the
    28-day weekly mean, per sport with the primary sport) and CTL/ATL/form/ramp; the
    three-zone intensity distribution of the last 7 and 28 days with polarization index,
    class, hard sessions/days and the drift between the two halves of the 28 days; recovery
    markers as numbers only (7-day mean of HRV, resting HR and sleep against the athlete's
    42-day baseline with n, difference and z-score); durability (median aerobic decoupling
    of steady sessions and efficiency factor over 28 days); the top sessions of the last 7
    days; the planned workouts of the next 7 days and the next race (when the end date is
    today); and the data coverage. Small samples are flagged; missing values are never
    counted as 0. No verdict or diagnosis. The single tools give the details:
    get_training_load, get_intensity_distribution, get_durability, get_recovery_snapshot,
    get_load_projection. Three API calls. After morritter's coach report (upstream PR #150).

    Args:
        end_date: Last day YYYY-MM-DD (optional, default today; not in the future)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        output_format: "text" (default) or "json" (the full structure)
        detail_level: "compact" (load, fitness, intensity, recovery, durability), "standard" (default, plus
            sports, drift, top sessions, plan and coverage) or "full" (plus the last 4 ISO weeks and references)
    """
    athlete_id_to_use, error_msg = resolve_request(athlete_id, detail_level)
    if error_msg:
        return error_msg
    days = past_end(end_date)
    if isinstance(days, str):
        return days
    today, end = days

    activities, error = await fetch_activities(
        athlete_id_to_use, api_key, end - timedelta(days=56), end, f"{LOAD_FIELDS},{ZONE_FIELDS},{DURABILITY_FIELDS}"
    )
    if error:
        return error
    wellness_list, error = await fetch_wellness(
        athlete_id_to_use, api_key, end - timedelta(days=BASELINE_DAYS + 7), end,
        FITNESS_FIELDS + "," + ",".join(m for m, _, _ in RECOVERY_METRICS),
    )
    if error:
        return error
    events: list[dict[str, Any]] = []
    if end == today:
        events, error = await fetch_events(athlete_id_to_use, api_key, today, today + timedelta(days=RACE_LOOKAHEAD_DAYS))
        if error:
            return error

    load_end, note = load_end_for(end_date, end, activities)
    wellness = wellness_by_day(wellness_list)
    window_start = load_end - timedelta(days=27)
    window = between(activities, window_start, load_end)
    intensity_28 = analyze_period(activities, window_start, load_end)
    intensity_7 = analyze_period(activities, load_end - timedelta(days=6), load_end)
    durability = decoupling_summary(activities, window_start, load_end)
    durability.pop("excluded_sessions")
    for entry in durability["by_sport"].values():
        entry.pop("sessions")
    window_days = [load_end - timedelta(days=o) for o in range(28)]
    payload: dict[str, Any] = {
        "athlete_id": athlete_id_to_use, "end": end.isoformat(), "load_end": load_end.isoformat(), "load_end_note": note,
        "load": load_metrics(activities, load_end),
        "sports": sport_breakdown(activities, load_end),
        "fitness": fitness_status(wellness, end, sum(activity_load(a) or 0.0 for a in between(activities, end, end)), today),
        "intensity": {"last_7": intensity_7["total"], "last_28": intensity_28["total"], "drift": intensity_28["drift"],
                      "by_sport_28": intensity_28["by_sport"]},
        "recovery": _recovery(wellness_list, end),
        "durability": durability,
        "efficiency": efficiency_summary(activities, window_start, load_end),
        "top_sessions": top_sessions(between(activities, load_end - timedelta(days=6), load_end)),
        "plan": _plan(events, today) if end == today else None,
        "weeks": weekly_rows(activities, load_end, 4),
        "coverage": {
            "sessions": len(window), "sessions_without_load": sum(1 for a in window if activity_load(a) is None),
            "zones_pct": intensity_28["coverage"]["moving_time_with_zones_pct"],
            "hrv_days": sum(1 for d in window_days if (num(wellness.get(d, {}).get("hrv")) or 0) > 0),
            "rhr_days": sum(1 for d in window_days if (num(wellness.get(d, {}).get("restingHR")) or 0) > 0),
            "sleep_days": sum(1 for d in window_days if (num(wellness.get(d, {}).get("sleepSecs")) or 0) > 0),
        },
    }
    if output_format.strip().lower() == "json":
        return json.dumps(payload, ensure_ascii=False)
    return _text(payload, detail_level)
