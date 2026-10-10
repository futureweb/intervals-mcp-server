"""
Usage and method guides served as MCP resources and by the get_guide tool.

The tool descriptions stay short; the details a client needs only sometimes live here:

- ``intervals://guide`` (topic ``usage``): which tool for which question, call order, conventions;
- ``intervals://workout-syntax``: the structured workout format of the write and validate tools;
- ``intervals://methods/<topic>``: how an analysis works (windows, filters, models, caveats).

Clients that cannot read resources (e.g. a chat connector that only calls tools) get the same
text from ``get_guide(topic)``. The texts are Markdown files next to this module.
"""

from functools import cache
from importlib import resources

__all__ = ["GUIDE_TOPICS", "METHOD_TOPICS", "SERVER_INSTRUCTIONS", "guide_text", "guide_uri"]

# Method guides (guides/methods/<topic>.md), in the order of the usage guide.
METHOD_TOPICS: tuple[str, ...] = (
    "activity-data",
    "execution",
    "climbs",
    "power-meters",
    "load",
    "intensity",
    "durability",
    "summary",
    "comparisons",
    "fatigue",
    "wellness",
    "fueling",
)
GUIDE_TOPICS: tuple[str, ...] = ("usage", "workout-syntax", *METHOD_TOPICS)

# Sent to the client in the initialize result (FastMCP ``instructions``); keep under ~1.5k characters.
SERVER_INSTRUCTIONS = """\
Intervals.icu data of one athlete: activities, wellness, calendar and training load.
- Weekly or general training analysis: start with get_coach_context. One activity: get_activity_report. \
Use the detail tools only where the overview is not enough.
- Ask for detail_level="compact" first; use standard or full only when needed.
- A missing value is not a normal value and never 0. Today's wellness record fills during the day \
(sleep and HRV after the morning sync, other device values later): the tools name what is not yet \
available; do not read it as normal or as a change.
- Numbers, baselines and cited reference ranges are context, not a verdict or diagnosis; the training \
decision stays with the athlete and coach.
- Write tools change the athlete's Intervals.icu account: use them only when the athlete explicitly asks. \
Preview first (validate_workout or preview_workout before add_or_update_event; dry_run=true returns \
the exact request of a write or range delete) and show what will change; report the read-back.
- Before writing a workout read the workout syntax: resource intervals://workout-syntax or \
get_guide(topic="workout-syntax"). Method details: intervals://methods/<topic> or get_guide.
- Dates are YYYY-MM-DD in the athlete's time zone; athlete_id defaults to the configured athlete."""


def guide_uri(topic: str) -> str:
    """Resource URI of a guide topic."""
    if topic == "usage":
        return "intervals://guide"
    if topic == "workout-syntax":
        return "intervals://workout-syntax"
    return f"intervals://methods/{topic}"


@cache
def guide_text(topic: str) -> str:
    """The Markdown text of a guide topic.

    Raises:
        KeyError: If the topic is unknown.
    """
    if topic not in GUIDE_TOPICS:
        raise KeyError(topic)
    folder = resources.files(__name__)
    path = folder / "methods" / f"{topic}.md" if topic in METHOD_TOPICS else folder / f"{topic}.md"
    return path.read_text(encoding="utf-8")
