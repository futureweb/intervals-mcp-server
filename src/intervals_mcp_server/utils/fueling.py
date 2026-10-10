"""
Fueling figures of activities (pure functions, no API access).

Intervals.icu stores two native carbohydrate values per activity: ``carbs_used`` (its estimate
of the carbohydrate burnt, not a measurement) and ``carbs_ingested`` (what the athlete logged,
one total, no timestamps). Energy comes as ``calories`` (kcal, device or Intervals.icu) and
``icu_joules`` (mechanical work from power). Fluid intake, sodium and sweat loss exist only as
custom fields; they are found generically by units and words of the definition (a volume field
named "sweat ..." is a sweat loss, a volume field named "fluid / drink / bottle / water /
intake ..." an intake, a mg field named "sodium / salt" a sodium intake), nothing is tied to a
vendor. Null means not logged. A 0 in a custom field filled from the device file (FIT source
or script, e.g. a Garmin sweat loss) is a zero placeholder: Intervals.icu stores 0 when the file
lacks the source, so it is reported as "0 stored" and left out of totals, differences and
statistics; a 0 in a manual field and a stored ``carbs_ingested`` of 0 stay 0 (the latter counted
separately). Rates are per moving hour. Correlations are computed per sport family (intensity
comes from power on rides and from HR or pace elsewhere) and only from MIN_CORRELATION_N
sessions on. No targets or prescriptions.
"""

import math
import statistics
from typing import Any

from intervals_mcp_server.utils.custom_fields import CustomFieldDefs, is_device_file_field
from intervals_mcp_server.utils.field_policy import field_words
from intervals_mcp_server.utils.load_metrics import intensity_factor, num, rnd
from intervals_mcp_server.utils.sports import hms, sport_family

CARBS_NOTE = (
    "carbs_used is Intervals.icu's estimate of the carbohydrate burnt, not a measurement; used minus ingested is not "
    "an energy deficit 1:1, because muscle and liver glycogen, fat oxidation and food eaten before the session "
    "contribute. No intake targets are implied."
)
TIMING_NOTE = (
    "Intervals.icu stores intake as one total per activity without timestamps, so its distribution over the session "
    "cannot be shown."
)
SWEAT_WORDS = {"sweat", "perspiration"}
LOSS_WORDS = {"loss", "lost", "deficit"}
INTAKE_WORDS = {"intake", "ingested", "consumed", "drank", "drink", "drinks", "drunk", "fluid", "fluids", "bottle",
                "bottles", "water", "hydration", "beverage"}
SODIUM_WORDS = {"sodium", "salt", "electrolyte", "electrolytes"}
CARB_WORDS = {"carb", "carbs", "carbohydrate", "carbohydrates", "cho", "gel", "gels"}
_VOLUME_TO_ML = {"ml": 1.0, "milliliter": 1.0, "millilitre": 1.0, "l": 1000.0, "liter": 1000.0, "litre": 1000.0,
                 "liters": 1000.0, "litres": 1000.0, "fl oz": 29.5735, "oz": 29.5735, "cl": 10.0, "dl": 100.0}
_MASS_TO_MG = {"mg": 1.0, "g": 1000.0}
DURATION_BUCKETS_H = ((0.0, 2.0, "< 2 h"), (2.0, 3.0, "2-3 h"), (3.0, 4.0, "3-4 h"), (4.0, math.inf, ">= 4 h"))
MIN_CORRELATION_N = 8
INTENSITY_BUCKETS = ((0.0, 0.65, "IF < 0.65"), (0.65, 0.75, "IF 0.65-0.75"), (0.75, 0.85, "IF 0.75-0.85"), (0.85, math.inf, "IF >= 0.85"))
FUELING_LIST_FIELDS = (
    "id,name,type,start_date_local,moving_time,elapsed_time,carbs_used,carbs_ingested,calories,icu_joules,"
    "icu_intensity,icu_training_load,average_weather_temp,average_temp,trainer"
)


def _units(definition: dict[str, Any]) -> str:
    units = definition.get("units")
    return units.strip().lower() if isinstance(units, str) else ""


def fueling_fields(defs: CustomFieldDefs) -> dict[str, list[str]]:
    """Custom activity fields that hold fluid intake, sodium, sweat loss or carbohydrate intake (codes by role)."""
    roles: dict[str, list[str]] = {"fluid_intake": [], "sodium": [], "sweat_loss": [], "carbs": []}
    for code, definition in defs.items():
        if definition.get("value_type") not in (None, "numeric"):
            continue
        words, units = field_words(definition), _units(definition)
        volume = units in _VOLUME_TO_ML
        loss = bool(words & SWEAT_WORDS) or bool(words & LOSS_WORDS and words & {"fluid", "hydration", "water"})
        if volume and loss:
            roles["sweat_loss"].append(code)
        elif words & SODIUM_WORDS and units in _MASS_TO_MG:
            roles["sodium"].append(code)
        elif volume and words & INTAKE_WORDS:
            roles["fluid_intake"].append(code)
        elif words & CARB_WORDS and units == "g":
            roles["carbs"].append(code)
    return roles


def _amount(definition: dict[str, Any], value: Any, factors: dict[str, float]) -> float | None:
    number = num(value)
    factor = factors.get(_units(definition))
    return None if number is None or factor is None else number * factor


def _per_hour(value: float | None, hours: float | None) -> float | None:
    return rnd(value / hours, 0) if value is not None and hours else None


def _custom_values(activity: dict[str, Any], defs: CustomFieldDefs, codes: list[str], factors: dict[str, float],
                   hours: float | None) -> list[dict[str, Any]]:
    rows = []
    for code in codes:
        if code not in activity:
            continue
        amount = _amount(defs[code], activity.get(code), factors)
        status = "not logged" if amount is None else ("zero" if amount == 0 else "value")
        stored = activity.get(code) if amount is not None else None
        if status == "zero" and is_device_file_field(defs[code]):
            status, amount = "zero_placeholder", None  # 0 = source missing from the file, or a real 0
        rows.append({
            "code": code, "name": defs[code].get("name"), "stored": stored,
            "units": defs[code].get("units"), "amount": rnd(amount, 0), "per_hour": _per_hour(amount, hours),
            "status": status,
        })
    return rows


def activity_fueling(activity: dict[str, Any], defs: CustomFieldDefs | None = None) -> dict[str, Any]:
    """Fueling figures of one activity: carbohydrate used (estimate) and ingested, energy, fluids, sodium, sweat loss."""
    defs = defs or {}
    moving = num(activity.get("moving_time")) or num(activity.get("elapsed_time"))
    hours = moving / 3600 if moving else None
    used, ingested = num(activity.get("carbs_used")), num(activity.get("carbs_ingested"))
    joules = num(activity.get("icu_joules"))
    roles = fueling_fields(defs)
    fluids = _custom_values(activity, defs, roles["fluid_intake"], _VOLUME_TO_ML, hours)
    sweat = _custom_values(activity, defs, roles["sweat_loss"], _VOLUME_TO_ML, hours)
    sodium = _custom_values(activity, defs, roles["sodium"], _MASS_TO_MG, hours)
    fluid_total = sum(f["amount"] for f in fluids if f["amount"] is not None) if any(f["amount"] is not None for f in fluids) else None
    sweat_total = sum(s["amount"] for s in sweat if s["amount"] is not None) if any(s["amount"] is not None for s in sweat) else None
    factor = intensity_factor(activity)
    return {
        "id": activity.get("id"), "name": activity.get("name"), "type": activity.get("type"),
        "date": str(activity.get("start_date_local") or "")[:10],
        "moving_time_s": moving, "hours": rnd(hours, 2), "intensity_factor": rnd(factor, 2),
        "carbs_used_g": used, "carbs_used_g_per_h": _per_hour(used, hours),
        "carbs_ingested_g": ingested,
        "carbs_ingested_status": "not logged" if ingested is None else ("zero" if ingested == 0 else "value"),
        "carbs_ingested_g_per_h": _per_hour(ingested, hours),
        "ingested_pct_of_used": rnd(ingested / used * 100, 0) if ingested is not None and used else None,
        "calories_kcal": num(activity.get("calories")), "work_kj": rnd(joules / 1000, 0) if joules is not None else None,
        "fluid_intake": fluids, "sweat_loss": sweat, "sodium": sodium,
        "fluid_minus_sweat_ml": rnd(fluid_total - sweat_total, 0) if fluid_total is not None and sweat_total is not None else None,
        "weather_temp_c": rnd(num(activity.get("average_weather_temp")), 1),
        "custom_carb_fields": [c for c in roles["carbs"] if c in activity],
    }


def has_fueling_data(figures: dict[str, Any]) -> bool:
    """True when any fueling value is stored."""
    return any(figures.get(k) is not None for k in ("carbs_used_g", "carbs_ingested_g")) or any(
        row["amount"] is not None for key in ("fluid_intake", "sweat_loss", "sodium") for row in figures.get(key) or []
    )


def _g(value: Any) -> str:
    return "n/a" if value is None else f"{value:.0f} g"


def fueling_line(figures: dict[str, Any], prefix: str = "Fueling: ") -> str | None:  # pylint: disable=too-many-branches
    """'Fueling: carbs used ~258 g (est., 178 g/h) | ingested 50 g (34 g/h, 19 % of used) | sweat loss 827 ml (568 ml/h) | 1142 kcal, 1014 kJ'."""
    if not has_fueling_data(figures) and figures.get("calories_kcal") is None:
        return None
    parts = []
    if figures["carbs_used_g"] is not None:
        rate = f", {figures['carbs_used_g_per_h']:.0f} g/h" if figures["carbs_used_g_per_h"] is not None else ""
        parts.append(f"carbs used ~{_g(figures['carbs_used_g'])} (Intervals.icu estimate{rate})")
    status = figures["carbs_ingested_status"]
    if status == "not logged":
        parts.append("ingested not logged")
    else:
        text = f"ingested {_g(figures['carbs_ingested_g'])}"
        extra = [f"{figures['carbs_ingested_g_per_h']:.0f} g/h"] if figures["carbs_ingested_g_per_h"] is not None else []
        if figures["ingested_pct_of_used"] is not None:
            extra.append(f"{figures['ingested_pct_of_used']:.0f} % of used")
        parts.append(text + (f" ({', '.join(extra)})" if extra else "") + (" (a stored 0)" if status == "zero" else ""))
    for key, label, unit in (("fluid_intake", "fluid", "ml"), ("sweat_loss", "sweat loss", "ml"), ("sodium", "sodium", "mg")):
        for row in figures[key]:
            if row["status"] == "zero_placeholder":
                parts.append(f"{label} 0 stored ({row['name']}: placeholder or real 0, not counted)")
            if row["amount"] is None:
                continue
            rate = f", {row['per_hour']:.0f} {unit}/h" if row["per_hour"] is not None else ""
            parts.append(f"{label} {row['amount']:.0f} {unit} ({row['name']}{rate})")
    if figures["fluid_minus_sweat_ml"] is not None:
        parts.append(f"fluid minus sweat loss {figures['fluid_minus_sweat_ml']:+.0f} ml")
    energy = []
    if figures["calories_kcal"] is not None:
        energy.append(f"{figures['calories_kcal']:.0f} kcal")
    if figures["work_kj"] is not None:
        energy.append(f"{figures['work_kj']:.0f} kJ work")
    if energy:
        parts.append(", ".join(energy))
    return prefix + " | ".join(parts)


def intake_distribution(time_data: list[Any], values: list[Any]) -> dict[str, Any] | None:
    """Intake per hour from an intake stream: cumulative (never decreasing) or per-event values; None without data."""
    pairs = [(num(t), num(v)) for t, v in zip(time_data, values, strict=False)]
    valid = [(t, v) for t, v in pairs if t is not None and v is not None]
    if not valid:
        return None
    cumulative = all(b[1] >= a[1] for a, b in zip(valid, valid[1:], strict=False))
    per_hour: dict[int, float] = {}
    previous = 0.0
    for t, value in valid:
        amount = value - previous if cumulative else value
        previous = value if cumulative else previous
        if amount > 0:
            per_hour[int(t // 3600)] = per_hour.get(int(t // 3600), 0.0) + amount
    return {"mode": "cumulative" if cumulative else "events",
            "per_hour": [{"hour": h + 1, "amount": rnd(per_hour[h], 1)} for h in sorted(per_hour)]}


# ---------------------------------------------------------------------------
# Several activities
# ---------------------------------------------------------------------------


def _describe(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    out: dict[str, Any] = {"n": len(values), "median": rnd(statistics.median(values), 0),
                           "min": rnd(min(values), 0), "max": rnd(max(values), 0)}
    if len(values) >= 4:
        p25, _, p75 = statistics.quantiles(values, n=4, method="inclusive")
        out.update(p25=rnd(p25, 0), p75=rnd(p75, 0))
    return out


def _bucket(value: float | None, buckets: tuple[tuple[float, float, str], ...]) -> str | None:
    if value is None:
        return None
    return next((label for low, high, label in buckets if low <= value < high), None)


def _group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ingested = [r["carbs_ingested_g_per_h"] for r in rows if r["carbs_ingested_status"] == "value" and r["carbs_ingested_g_per_h"] is not None]
    used = [r["carbs_used_g_per_h"] for r in rows if r["carbs_used_g_per_h"] is not None]
    share = [r["ingested_pct_of_used"] for r in rows if r["carbs_ingested_status"] == "value" and r["ingested_pct_of_used"] is not None]
    sweat = [s["per_hour"] for r in rows for s in r["sweat_loss"] if s["per_hour"] is not None]
    fluid = [f["per_hour"] for r in rows for f in r["fluid_intake"] if f["per_hour"] is not None]
    return {
        "sessions": len(rows),
        "ingested_logged": sum(1 for r in rows if r["carbs_ingested_status"] == "value"),
        "ingested_zero": sum(1 for r in rows if r["carbs_ingested_status"] == "zero"),
        "ingested_not_logged": sum(1 for r in rows if r["carbs_ingested_status"] == "not logged"),
        "with_carbs_used": len(used),
        "zero_placeholders": sum(1 for r in rows for key in ("sweat_loss", "fluid_intake", "sodium")
                                 for item in r[key] if item["status"] == "zero_placeholder"),
        "ingested_g_per_h": _describe(ingested), "used_g_per_h": _describe(used),
        "ingested_pct_of_used": _describe(share), "sweat_loss_ml_per_h": _describe(sweat), "fluid_ml_per_h": _describe(fluid),
    }


def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < MIN_CORRELATION_N:
        return None

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        position = 0
        while position < len(order):
            end = position
            while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
                end += 1
            for k in range(position, end + 1):
                out[order[k]] = (position + end) / 2 + 1
            position = end + 1
        return out

    xs, ys = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    try:
        return rnd(statistics.correlation(xs, ys), 2)
    except statistics.StatisticsError:
        return None


def _correlations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Spearman rank correlation of ingested g/h with duration and with IF within one sport family."""
    logged = [r for r in rows if r["carbs_ingested_status"] == "value" and r["carbs_ingested_g_per_h"] is not None]
    duration = [(r["hours"], r["carbs_ingested_g_per_h"]) for r in logged if r["hours"]]
    intensity = [(r["intensity_factor"], r["carbs_ingested_g_per_h"]) for r in logged if r["intensity_factor"] is not None]
    return {
        "min_n": MIN_CORRELATION_N,
        "ingested_vs_duration": {"n": len(duration), "rho": _spearman(duration)},
        "ingested_vs_intensity": {"n": len(intensity), "rho": _spearman(intensity)},
    }


def _family_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        **_group(rows),
        "by_duration": {label: _group([r for r in rows if _bucket(r["hours"], DURATION_BUCKETS_H) == label])
                        for _, _, label in DURATION_BUCKETS_H},
        "by_intensity": {label: _group([r for r in rows if _bucket(r["intensity_factor"], INTENSITY_BUCKETS) == label])
                         for _, _, label in INTENSITY_BUCKETS},
        "correlations": _correlations(rows),
    }


def period_fueling(activities: list[dict[str, Any]], defs: CustomFieldDefs | None, min_moving_s: float) -> dict[str, Any]:
    """Fueling rows of the sessions of at least ``min_moving_s``, statistics overall and, per sport
    family, by duration bucket, by intensity bucket and Spearman correlations (sample sizes, logging
    coverage and the minimum n included). Buckets and correlations never mix sport families."""
    rows = [activity_fueling(a, defs) for a in activities if (num(a.get("moving_time")) or 0) >= min_moving_s]
    rows.sort(key=lambda r: r["date"], reverse=True)
    by_family: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_family.setdefault(sport_family(row["type"]), []).append(row)
    return {
        "rows": rows,
        "overall": _group(rows),
        "by_sport_family": {family: _family_block(group) for family, group in sorted(by_family.items())},
        "min_correlation_n": MIN_CORRELATION_N,
        "rates": "per moving hour",
    }


def correlation_text(correlations: dict[str, Any]) -> str:
    """'Spearman ingested g/h vs duration 0.28 (n 15), vs IF 0.63 (n 15)' or why it is not computed."""
    parts = []
    for key, label in (("ingested_vs_duration", "vs duration"), ("ingested_vs_intensity", "vs IF")):
        item = correlations[key]
        parts.append(f"{label} {item['rho']:.2f} (n {item['n']})" if item["rho"] is not None
                     else f"{label} not computed (n {item['n']} < {correlations['min_n']})")
    return "Spearman ingested g/h " + ", ".join(parts) + " (association only)"


def _stat(desc: dict[str, Any] | None, unit: str) -> str:
    if not desc:
        return "n/a"
    spread = f", IQR {desc['p25']:.0f}-{desc['p75']:.0f}" if "p25" in desc else ""
    return f"median {desc['median']:.0f} {unit} (n {desc['n']}, range {desc['min']:.0f}-{desc['max']:.0f}{spread})"


def group_text(label: str, group: dict[str, Any]) -> str:
    """One line per group of sessions."""
    if not group["sessions"]:
        return f"{label}: no sessions"
    text = (
        f"{label}: {group['sessions']} session{'s' if group['sessions'] != 1 else ''}, intake logged on {group['ingested_logged']} "
        f"(0 g stored on {group['ingested_zero']}, not logged on {group['ingested_not_logged']}); "
        f"ingested {_stat(group['ingested_g_per_h'], 'g/h')}; used (estimate) {_stat(group['used_g_per_h'], 'g/h')}"
    )
    if group["ingested_pct_of_used"]:
        text += f"; ingested/used {_stat(group['ingested_pct_of_used'], '%')}"
    if group["sweat_loss_ml_per_h"]:
        text += f"; sweat loss {_stat(group['sweat_loss_ml_per_h'], 'ml/h')}"
    if group["fluid_ml_per_h"]:
        text += f"; fluid {_stat(group['fluid_ml_per_h'], 'ml/h')}"
    if group.get("zero_placeholders"):
        text += f"; {group['zero_placeholders']} device-file 0 value(s) left out as placeholders"
    return text


def row_text(row: dict[str, Any]) -> str:
    """One line per session of a period listing."""
    line = fueling_line(row, prefix="") or "no fueling data"
    factor = f", IF {row['intensity_factor']:.2f}" if row["intensity_factor"] is not None else ""
    temp = f", {row['weather_temp_c']:.0f} °C" if row["weather_temp_c"] is not None else ""
    return f"{row['date']} {row['id']} {row['type']} '{row['name']}' ({hms(row['moving_time_s'])}{factor}{temp}): {line}"
