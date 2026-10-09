"""
Per-activity context: weather, W′ balance and the history of the same route (pure functions).

- Weather: the values Intervals.icu attaches to an activity with GPS (``average_weather_temp``,
  feels-like, wind speed and gusts in m/s, prevailing wind direction, head- and tailwind share
  of the moving time, clouds, rain in mm/h, snow) next to the device's temperature sensor
  (``average_temp``), which reads body and sun heat as well.
- W′ balance: the activity's W′ (``icu_w_prime``, J), the largest depletion Intervals.icu
  computed (``icu_max_wbal_depletion``) and, when the ``w_bal`` stream was fetched, the time
  spent below 75 / 50 / 25 % of W′; interval ``wbal_end`` values name the interval that ended
  lowest.
- Same route: Intervals.icu groups activities with matching GPS tracks into routes
  (``route_id``); the activity list can be filtered by route. Earlier activities on the route
  are compared on time, power, W/kg, heart rate, weather and start/end field pairs such as
  stamina. Statistics only, no verdict.
"""

import math
import statistics
from typing import Any

from intervals_mcp_server.utils.load_metrics import intensity_factor, num, rnd
from intervals_mcp_server.utils.sports import hms, is_indoor, sport_family

RECORDING_GAP_S = 5  # a jump in the time stream larger than this is a recording pause
WBAL_THRESHOLDS_PCT = (75, 50, 25)
MAX_ROUTE_HISTORY = 15
ROUTE_DISTANCE_TOLERANCE_PCT = 5.0
ROUTE_ELEVATION_TOLERANCE_PCT = 10.0
ROUTE_FIELDS = (
    "id,name,type,start_date_local,start_date,moving_time,elapsed_time,distance,total_elevation_gain,"
    "icu_average_watts,icu_weighted_avg_watts,average_heartrate,max_heartrate,icu_training_load,icu_intensity,"
    "icu_weight,icu_ftp,average_speed,average_weather_temp,average_feels_like,headwind_percent,average_wind_speed,"
    "decoupling,icu_efficiency_factor,trainer,route_id,gear,power_meter"
)
WEATHER_KEYS = (
    "average_weather_temp", "min_weather_temp", "max_weather_temp", "average_feels_like", "min_feels_like",
    "max_feels_like", "average_wind_speed", "average_wind_gust", "prevailing_wind_deg", "headwind_percent",
    "tailwind_percent", "average_clouds", "max_rain", "max_snow",
)
_COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass(degrees: Any) -> str | None:
    """'SW' for 225 degrees (the direction the wind comes from); None when not numeric."""
    value = num(degrees)
    if value is None:
        return None
    return _COMPASS[int((value % 360) / 22.5 + 0.5) % 16]


def _kmh(m_s: float | None) -> float | None:
    return None if m_s is None else rnd(m_s * 3.6, 1)


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


def weather_summary(activity: dict[str, Any]) -> dict[str, Any] | None:
    """Weather and device temperature of an activity; None when neither exists.

    Wind speeds are stored in m/s (km/h added), rain as mm/h, head- and tailwind as % of the
    moving time. ``has_weather`` false with values present happens on re-imported copies.
    """
    weather = {key: num(activity.get(key)) for key in WEATHER_KEYS}
    device = {key: num(activity.get(key)) for key in ("average_temp", "min_temp", "max_temp")}
    if all(v is None for v in weather.values()) and all(v is None for v in device.values()):
        return None
    head, tail = weather["headwind_percent"], weather["tailwind_percent"]
    temp, device_temp = weather["average_weather_temp"], device["average_temp"]
    return {
        "has_weather": activity.get("has_weather"),
        "indoor": is_indoor(activity),
        "temp_c": rnd(temp, 1), "temp_min_c": rnd(weather["min_weather_temp"], 1), "temp_max_c": rnd(weather["max_weather_temp"], 1),
        "feels_like_c": rnd(weather["average_feels_like"], 1), "feels_like_min_c": rnd(weather["min_feels_like"], 1),
        "feels_like_max_c": rnd(weather["max_feels_like"], 1),
        "wind_m_s": rnd(weather["average_wind_speed"], 2), "wind_km_h": _kmh(weather["average_wind_speed"]),
        "gust_m_s": rnd(weather["average_wind_gust"], 2), "gust_km_h": _kmh(weather["average_wind_gust"]),
        "wind_from_deg": weather["prevailing_wind_deg"], "wind_from": compass(weather["prevailing_wind_deg"]),
        "headwind_pct": rnd(head, 0), "tailwind_pct": rnd(tail, 0),
        "crosswind_pct": rnd(100 - head - tail, 0) if head is not None and tail is not None else None,
        "clouds_pct": rnd(weather["average_clouds"], 0), "max_rain_mm_h": rnd(weather["max_rain"], 1),
        "max_snow": rnd(weather["max_snow"], 1),
        "device_temp_c": rnd(device_temp, 1), "device_temp_min_c": rnd(device["min_temp"], 1),
        "device_temp_max_c": rnd(device["max_temp"], 1),
        "device_minus_weather_c": rnd(device_temp - temp, 1) if device_temp is not None and temp is not None else None,
    }


def _temp(value: Any) -> str:
    return "n/a" if value is None else f"{value:.1f} °C"


def weather_line(summary: dict[str, Any] | None) -> str | None:
    """'Weather 14.0 °C (12.6-14.6, feels 13.3 °C), wind 3 km/h from SW, gusts 8 km/h, headwind 54 % / tailwind 20 %, ...'."""
    if not summary:
        return None
    parts: list[str] = []
    if summary["temp_c"] is not None:
        span = f"{summary['temp_min_c']:.1f}-{summary['temp_max_c']:.1f}, " if summary["temp_min_c"] is not None and summary["temp_max_c"] is not None else ""
        feels = f"feels {_temp(summary['feels_like_c'])}" if summary["feels_like_c"] is not None else ""
        inner = (span + feels).rstrip(", ")
        parts.append(f"Weather {_temp(summary['temp_c'])}" + (f" ({inner})" if inner else ""))
    if summary["wind_km_h"] is not None:
        text = f"wind {summary['wind_km_h']:.0f} km/h" + (f" from {summary['wind_from']}" if summary["wind_from"] else "")
        if summary["gust_km_h"] is not None:
            text += f", gusts {summary['gust_km_h']:.0f} km/h"
        parts.append(text)
    if summary["headwind_pct"] is not None:
        parts.append(f"headwind {summary['headwind_pct']:.0f} % / tailwind {summary['tailwind_pct'] or 0:.0f} % of the time")
    if summary["clouds_pct"] is not None:
        parts.append(f"clouds {summary['clouds_pct']:.0f} %")
    rain = summary["max_rain_mm_h"]
    if rain is not None:
        parts.append("no rain" if rain == 0 and not summary["max_snow"] else f"rain up to {rain:g} mm/h")
    if summary["max_snow"]:
        parts.append(f"snow up to {summary['max_snow']:g}")
    if summary["device_temp_c"] is not None:
        parts.append(f"device sensor {_temp(summary['device_temp_c'])}")
    if not parts:
        return None
    text = ", ".join(parts)
    if summary["indoor"]:
        text += " (indoor activity: outdoor weather does not apply)"
    return text[0].upper() + text[1:]


# ---------------------------------------------------------------------------
# W′ balance
# ---------------------------------------------------------------------------


def _sample_durations(time_data: list[Any], length: int) -> list[float]:
    """Seconds each sample stands for; recording pauses (jumps > RECORDING_GAP_S) count one second."""
    durations: list[float] = []
    for index in range(length):
        current = num(time_data[index]) if index < len(time_data) else None
        following = num(time_data[index + 1]) if index + 1 < len(time_data) else None
        if current is None or following is None:
            durations.append(1.0)
            continue
        step = following - current
        durations.append(step if 0 < step <= RECORDING_GAP_S else 1.0)
    return durations


def _wbal_stream_stats(values: list[Any], time_data: list[Any], reference: float) -> dict[str, Any] | None:  # pylint: disable=too-many-locals
    pairs = [(index, num(value)) for index, value in enumerate(values)]
    valid = [(index, value) for index, value in pairs if value is not None]
    if not valid or reference <= 0:
        return None
    durations = _sample_durations(time_data, len(values))
    below: dict[str, float] = {}
    for pct in WBAL_THRESHOLDS_PCT:
        limit = reference * pct / 100
        below[str(pct)] = sum(durations[index] for index, value in valid if value < limit)
    dips, inside = 0, False
    half = reference * 0.5
    for _, value in valid:
        if value < half and not inside:
            dips += 1
        inside = value < half
    low_index, low_value = min(valid, key=lambda item: item[1])
    at = num(time_data[low_index]) if low_index < len(time_data) else None
    return {
        "samples": len(valid), "min_j": rnd(low_value, 0), "min_pct": rnd(low_value / reference * 100, 0), "min_at_s": at,
        "seconds_below_pct": {k: rnd(v, 0) for k, v in below.items()}, "dips_below_50_pct": dips,
    }


def _lowest_interval(intervals: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows: list[tuple[int, dict[str, Any], float]] = []
    for position, interval in enumerate(intervals, start=1):
        end = num(interval.get("wbal_end"))
        if end is not None:
            rows.append((position, interval, end))
    if not rows:
        return None
    position, interval, value = min(rows, key=lambda row: row[2])
    return {
        "interval": position, "type": interval.get("type"), "label": interval.get("label"),
        "start_time_s": interval.get("start_time"), "elapsed_time_s": interval.get("elapsed_time"),
        "average_watts": interval.get("average_watts"), "wbal_start_j": num(interval.get("wbal_start")), "wbal_end_j": value,
    }


def wprime_summary(
    activity: dict[str, Any], intervals: list[dict[str, Any]] | None = None,
    w_bal: list[Any] | None = None, time_data: list[Any] | None = None,
) -> dict[str, Any] | None:
    """W′ figures of an activity; None without W′ data (no power).

    ``w_bal`` / ``time_data`` are the samples of the ``w_bal`` and ``time`` streams (optional):
    they add the time below 75 / 50 / 25 % of W′ (pauses not counted) and the number of
    separate dips below 50 %.
    """
    w_prime = num(activity.get("icu_w_prime"))
    depletion = num(activity.get("icu_max_wbal_depletion"))
    has_stream = bool(w_bal) and any(num(v) is not None for v in w_bal or [])
    if w_prime is None and depletion is None and not has_stream:
        return None
    out: dict[str, Any] = {
        "w_prime_j": w_prime, "model_w_prime_j": num(activity.get("icu_pm_w_prime")),
        "max_depletion_j": depletion,
        "max_depletion_pct": rnd(depletion / w_prime * 100, 0) if depletion is not None and w_prime else None,
        "min_w_bal_j": rnd(w_prime - depletion, 0) if depletion is not None and w_prime is not None else None,
        "min_w_bal_pct": rnd((w_prime - depletion) / w_prime * 100, 0) if depletion is not None and w_prime else None,
        "joules_above_ftp": num(activity.get("icu_joules_above_ftp")),
        "stream": None, "lowest_interval": _lowest_interval(intervals or []),
    }
    if has_stream:
        reference = w_prime or max(v for v in (num(x) for x in w_bal or []) if v is not None)
        out["stream"] = _wbal_stream_stats(list(w_bal or []), list(time_data or []), reference)
    return out


def _kj(value: Any) -> str:
    return "n/a" if value is None else f"{value / 1000:.1f} kJ"


def wprime_line(summary: dict[str, Any] | None) -> str | None:
    """'W′ 18.0 kJ (power model 18.9 kJ): max depletion 15.8 kJ (88 %), lowest W′bal 2.2 kJ (12 %); below 50 % 4:10 ...'."""
    if not summary:
        return None
    text = f"W′ {_kj(summary['w_prime_j'])}"
    if summary["model_w_prime_j"] is not None:
        text += f" (power model {_kj(summary['model_w_prime_j'])})"
    if summary["max_depletion_j"] is not None:
        text += f": max depletion {_kj(summary['max_depletion_j'])}"
        if summary["max_depletion_pct"] is not None:
            text += f" ({summary['max_depletion_pct']:.0f} %), lowest W′bal {_kj(summary['min_w_bal_j'])} ({summary['min_w_bal_pct']:.0f} %)"
    stream = summary.get("stream")
    if stream:
        below = stream["seconds_below_pct"]
        spans = [f"{pct} % {hms(below[str(pct)])}" for pct in WBAL_THRESHOLDS_PCT if below[str(pct)]]
        if spans:
            text += "; time below " + ", ".join(spans) + f" ({stream['dips_below_50_pct']} dip(s) below 50 %)"
        else:
            text += f"; W′bal never below {WBAL_THRESHOLDS_PCT[0]} %"
    lowest = summary.get("lowest_interval")
    if lowest and lowest.get("wbal_end_j") is not None:
        watts = f" @ {lowest['average_watts']} W" if lowest.get("average_watts") is not None else ""
        text += (
            f"; lowest at the end of interval {lowest['interval']} ({hms(lowest['start_time_s'])}, "
            f"{hms(lowest['elapsed_time_s'])}{watts}): {_kj(lowest['wbal_end_j'])}"
        )
    return text


# ---------------------------------------------------------------------------
# Same route history
# ---------------------------------------------------------------------------


def _pair_values(activity: dict[str, Any], pairs: list[tuple[str, str, str]]) -> dict[str, Any]:
    out = {}
    for start_code, end_code, stem in pairs:
        first, last = num(activity.get(start_code)), num(activity.get(end_code))
        if first is not None and last is not None:
            out[stem] = {"start": first, "end": last, "change": rnd(last - first, 1)}
    return out


def route_row(activity: dict[str, Any], pairs: list[tuple[str, str, str]] | None = None) -> dict[str, Any]:
    """Comparable numbers of one activity on a route."""
    watts, weighted, weight = (num(activity.get(k)) for k in ("icu_average_watts", "icu_weighted_avg_watts", "icu_weight"))
    speed = num(activity.get("average_speed"))
    factor = intensity_factor(activity)
    return {
        "id": activity.get("id"), "name": activity.get("name"), "type": activity.get("type"),
        "date": str(activity.get("start_date_local") or "")[:10],
        "moving_time_s": num(activity.get("moving_time")), "elapsed_time_s": num(activity.get("elapsed_time")),
        "distance_km": rnd((num(activity.get("distance")) or 0) / 1000, 2) if num(activity.get("distance")) is not None else None,
        "elevation_gain_m": num(activity.get("total_elevation_gain")),
        "speed_km_h": _kmh(speed), "avg_watts": watts, "np_watts": weighted,
        "w_per_kg": rnd(watts / weight, 2) if watts and weight else None,
        "np_w_per_kg": rnd(weighted / weight, 2) if weighted and weight else None,
        "avg_hr": num(activity.get("average_heartrate")), "max_hr": num(activity.get("max_heartrate")),
        "intensity_factor": rnd(factor, 2), "load": num(activity.get("icu_training_load")),
        "efficiency_factor": rnd(num(activity.get("icu_efficiency_factor")), 2),
        "decoupling_pct": rnd(num(activity.get("decoupling")), 1),
        "weather_temp_c": rnd(num(activity.get("average_weather_temp")), 1),
        "headwind_pct": rnd(num(activity.get("headwind_percent")), 0),
        "pairs": _pair_values(activity, pairs or []),
        "indoor": is_indoor(activity),
    }


def _deviation_pct(value: float | None, reference: float | None) -> float | None:
    if value is None or reference is None or reference == 0:
        return None
    return (value - reference) / reference * 100


def _comparable(row: dict[str, Any], ref: dict[str, Any]) -> list[str]:
    """Reasons a route activity is not directly comparable with the reference (empty = comparable)."""
    reasons = []
    distance = _deviation_pct(row["distance_km"], ref["distance_km"])
    if distance is not None and abs(distance) > ROUTE_DISTANCE_TOLERANCE_PCT:
        reasons.append(f"distance {distance:+.0f} %")
    gain = _deviation_pct(row["elevation_gain_m"], ref["elevation_gain_m"])
    if gain is not None and abs(gain) > ROUTE_ELEVATION_TOLERANCE_PCT and (ref["elevation_gain_m"] or 0) >= 100:
        reasons.append(f"elevation gain {gain:+.0f} %")
    return reasons


def _median(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [row[key] for row in rows if row.get(key) is not None]
    return rnd(statistics.median(values), 2) if values else None


def route_history(  # pylint: disable=too-many-locals
    reference: dict[str, Any], candidates: list[dict[str, Any]], pairs: list[tuple[str, str, str]] | None = None,
    limit: int = MAX_ROUTE_HISTORY,
) -> dict[str, Any]:
    """Earlier activities on the reference's route, compared with the reference.

    Only activities of the same sport family that started before the reference count; other
    sports on the route are counted. Activities whose distance differs by more than 5 % or whose
    elevation gain differs by more than 10 % are listed but left out of the statistics.
    """
    ref = route_row(reference, pairs)
    family = sport_family(reference.get("type"))
    start = str(reference.get("start_date_local") or "")
    earlier = [c for c in candidates if c.get("id") != reference.get("id") and str(c.get("start_date_local") or "") < start]
    same = [c for c in earlier if sport_family(c.get("type")) == family]
    rows = [route_row(c, pairs) for c in sorted(same, key=lambda c: str(c.get("start_date_local") or ""), reverse=True)]
    for row in rows:
        row["not_comparable"] = _comparable(row, ref)
        row["delta_moving_time_s"] = (
            rnd(row["moving_time_s"] - ref["moving_time_s"], 0)
            if row["moving_time_s"] is not None and ref["moving_time_s"] is not None else None
        )
    comparable = [row for row in rows if not row["not_comparable"]]
    stats: dict[str, Any] = {"n": len(comparable)}
    times = sorted(row["moving_time_s"] for row in comparable if row["moving_time_s"] is not None)
    if times and ref["moving_time_s"] is not None:
        stats["rank_by_moving_time"] = 1 + sum(1 for t in times if t < ref["moving_time_s"])
        stats["of"] = len(times) + 1
        stats["fastest_moving_time_s"] = times[0]
    for key in ("moving_time_s", "avg_watts", "np_watts", "w_per_kg", "avg_hr", "speed_km_h", "weather_temp_c", "efficiency_factor"):
        median = _median(comparable, key)
        stats[f"median_{key}"] = median
        if median is not None and ref.get(key) is not None:
            stats[f"reference_minus_median_{key}"] = rnd(ref[key] - median, 2)
    return {
        "route_id": reference.get("route_id"), "reference": ref, "earlier": rows[:limit],
        "earlier_total": len(rows), "other_sports": len(earlier) - len(same), "stats": stats,
        "comparability": (
            f"same sport family ({family}), distance within {ROUTE_DISTANCE_TOLERANCE_PCT:g} % and elevation gain within "
            f"{ROUTE_ELEVATION_TOLERANCE_PCT:g} % of the reference count for the statistics"
        ),
    }


def _value(value: Any, digits: int = 0, suffix: str = "") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value:.{digits}f}{suffix}"


def route_row_text(row: dict[str, Any]) -> str:
    """One line per route activity."""
    parts = [
        f"{row['date']} {row['id']} {row['type']}", f"moving {hms(row['moving_time_s'])}",
        f"{_value(row['distance_km'], 1, ' km')}", f"+{_value(row['elevation_gain_m'], 0, ' m')}",
    ]
    if row["avg_watts"] is not None:
        parts.append(f"avg {_value(row['avg_watts'])} W / NP {_value(row['np_watts'])} W ({_value(row['w_per_kg'], 2)} W/kg)")
    parts.append(f"HR {_value(row['avg_hr'])}")
    if row["weather_temp_c"] is not None:
        parts.append(f"{row['weather_temp_c']:.0f} °C" + (f", headwind {row['headwind_pct']:.0f} %" if row["headwind_pct"] is not None else ""))
    for stem, pair in row["pairs"].items():
        parts.append(f"{stem} {pair['start']:g}→{pair['end']:g}")
    if row.get("not_comparable"):
        parts.append("not comparable: " + ", ".join(row["not_comparable"]))
    return " | ".join(parts)


def route_history_lines(history: dict[str, Any], detail_level: str = "standard") -> list[str]:
    """Text lines of a route history (header, statistics, rows)."""
    stats, ref = history["stats"], history["reference"]
    total = history["earlier_total"]
    if history.get("error"):
        return [f"Route {history['route_id']}: the activity list could not be loaded ({history['error']})."]
    if not total:
        other = f" ({history['other_sports']} of other sports)" if history["other_sports"] else ""
        return [f"Route {history['route_id']}: no earlier activity of the same sport family on this route{other}."]
    lines = [
        f"Route {history['route_id']}" + (f" '{history['route_name']}'" if history.get("route_name") else "")
        + f": {total} earlier activit{'y' if total == 1 else 'ies'} of the same sport family"
        + (f" ({history['other_sports']} of other sports not compared)" if history["other_sports"] else "")
        + f"; {stats['n']} comparable ({history['comparability']})."
    ]
    if stats.get("rank_by_moving_time"):
        delta = stats.get("reference_minus_median_moving_time_s")
        lines.append(
            f"This activity: moving {hms(ref['moving_time_s'])}, rank {stats['rank_by_moving_time']} of {stats['of']} by moving time "
            f"(fastest earlier {hms(stats['fastest_moving_time_s'])}, median {hms(stats['median_moving_time_s'])}"
            + (f", {'+' if delta >= 0 else '-'}{hms(abs(delta))} vs median" if delta is not None else "") + ")"
        )
        deltas = []
        for key, label, digits, suffix in (("avg_watts", "avg power", 0, " W"), ("np_watts", "NP", 0, " W"),
                                           ("w_per_kg", "W/kg", 2, ""), ("avg_hr", "HR", 0, " bpm"),
                                           ("weather_temp_c", "temperature", 1, " °C")):
            diff = stats.get(f"reference_minus_median_{key}")
            if diff is not None:
                deltas.append(f"{label} {diff:+.{digits}f}{suffix} (median {stats[f'median_{key}']:.{digits}f})")
        if deltas:
            lines.append("Vs the median of the comparable earlier activities: " + ", ".join(deltas))
    shown = history["earlier"] if detail_level == "full" else history["earlier"][:8]
    lines.extend(f"  {route_row_text(row)}" for row in shown)
    if len(history["earlier"]) > len(shown) or total > len(history["earlier"]):
        lines.append(f"  ... {total - len(shown)} more (detail_level=full shows up to {MAX_ROUTE_HISTORY})")
    lines.append("Pacing, stops, wind, temperature, surface and fatigue differ between rides; statistics only, no assessment.")
    return lines
