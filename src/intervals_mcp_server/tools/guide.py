"""
Guides as MCP resources and the get_guide tool.

Resources: ``intervals://guide`` (usage), ``intervals://workout-syntax`` and
``intervals://methods/<topic>``. get_guide returns the same text for clients that only call tools.
"""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field

from intervals_mcp_server.config import get_config
from intervals_mcp_server.guides import GUIDE_TOPICS, guide_text, guide_uri
from intervals_mcp_server.mcp_instance import mark_outside_toolset, mcp, tool
from intervals_mcp_server.utils.params import lower_choice

_TOPIC_ALIASES = {
    "guide": "usage", "overview": "usage", "tools": "usage", "help": "usage", "index": "usage",
    "syntax": "workout-syntax", "workout": "workout-syntax", "workouts": "workout-syntax",
    "workout-doc": "workout-syntax", "workout-format": "workout-syntax",
}


def topic_choice(value: Any) -> Any:
    """Accept the forms the descriptions print: intervals://methods/load, methods/load, workout_syntax ..."""
    value = lower_choice(value)
    if not isinstance(value, str):
        return value
    topic = value.removeprefix("intervals://").strip("/ ")
    topic = topic.removeprefix("methods/").removeprefix("method/").removesuffix(".md").strip("/ ")
    topic = "-".join(topic.replace("_", " ").replace("-", " ").split())
    return _TOPIC_ALIASES.get(topic, topic)

# Same names as guides.GUIDE_TOPICS (a test keeps them equal).
GuideTopic = Literal[
    "usage", "workout-syntax", "activity-data", "execution", "climbs", "power-meters", "load",
    "intensity", "durability", "summary", "comparisons", "fatigue", "wellness", "fueling",
]

_RESOURCE_DESCRIPTIONS = {
    "usage": "Which tool for which question, recommended call order and conventions.",
    "workout-syntax": "Structured workout format (workout_doc steps, targets, units, repeats) for the write and validate tools.",
}


def _register_resource(topic: str) -> None:
    def read() -> str:
        return mark_outside_toolset(guide_text(topic), get_config().toolset)

    read.__name__ = f"guide_{topic.replace('-', '_')}"
    description = _RESOURCE_DESCRIPTIONS.get(topic, f"Method guide: {topic.replace('-', ' ')} (how the analysis works, windows, filters, caveats).")
    register: Callable[[Callable[[], str]], Callable[[], str]] = mcp.resource(
        guide_uri(topic), name=read.__name__, description=description, mime_type="text/markdown"
    )
    register(read)


for _topic in GUIDE_TOPICS:
    _register_resource(_topic)


@tool("read")
async def get_guide(
    topic: Annotated[
        GuideTopic,
        BeforeValidator(topic_choice),
        Field(description="usage = tool overview; workout-syntax = workout format; others = method of that analysis"),
    ] = "usage",
) -> str:
    """Use when the short tool descriptions are not enough: read the workout syntax before writing a workout, or how an analysis works (method, windows, filters, caveats).

    Returns the same Markdown as the resources intervals://guide, intervals://workout-syntax and intervals://methods/<topic>. Static text, no athlete data.
    """
    return mark_outside_toolset(guide_text(topic), get_config().toolset)
