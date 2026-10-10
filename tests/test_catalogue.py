"""
Size and shape of the tool catalogue (stage 3).

The catalogue is what every MCP client loads into its context: descriptions are short and say
when to use the tool, every parameter is described, constrained parameters carry an enum, no tool
takes an API key, text tools return their text once (no output schema, no structured copy), and
the totals stay within a token budget per permission set and tool set. Token counts here are the
chars/4 approximation of the compact JSON of the ``tools/list`` result.
"""

import asyncio
import inspect
import json
import os
import pathlib
import re
import subprocess
import sys
import typing
from typing import Any

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from mcp.shared.memory import create_connected_server_and_client_session  # pylint: disable=wrong-import-position
from mcp.types import TextContent  # pylint: disable=wrong-import-position

from intervals_mcp_server import server  # noqa: F401  # pylint: disable=wrong-import-position,unused-import
from intervals_mcp_server.guides import GUIDE_TOPICS, SERVER_INSTRUCTIONS, guide_text, guide_uri  # pylint: disable=wrong-import-position
from intervals_mcp_server.mcp_instance import catalogue, compact_schema, mark_outside_toolset, mcp, tool_permissions  # pylint: disable=wrong-import-position
from intervals_mcp_server.tools import guide as guide_module  # pylint: disable=wrong-import-position
from intervals_mcp_server.toolsets import CORE_TOOLS, parse_toolset  # pylint: disable=wrong-import-position

ROOT = pathlib.Path(__file__).resolve().parents[1]
PERMISSION_SETS = {
    "read": frozenset({"read"}),
    "read,write": frozenset({"read", "write"}),
    "all": frozenset({"read", "write", "destructive", "admin"}),
}
# Budgets in chars/4 tokens of the compact tools/list JSON: measured value plus about 10 % headroom
# (cl100k counts are about 8 % lower). Before stage 3: read 35.4k, read,write 43.7k, all 48.0k.
BUDGETS = {
    ("read", "full"): 23_000,
    ("read,write", "full"): 26_500,
    ("all", "full"): 29_000,
    ("read", "core"): 9_000,
    ("read,write", "core"): 11_000,
    ("all", "core"): 11_000,
}
MAX_DESCRIPTION_CHARS = 900
# Parameters whose values come from a fixed set wherever they appear.
CONSTRAINED = {
    "output_format", "detail_level", "group_by", "zone_type", "zone_basis", "threshold_as", "metric",
    "sort_by", "environment", "temperature_source", "threshold_unit", "target", "visibility", "item_type",
    "category", "indoor_outdoor", "topic",
}


def _tools(permissions: str, toolset: str = "full") -> list[dict[str, Any]]:
    tools = asyncio.run(catalogue(PERMISSION_SETS[permissions], toolset))
    return [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]


def _tokens(tools: list[dict[str, Any]]) -> int:
    return len(json.dumps({"tools": tools}, ensure_ascii=False, separators=(",", ":"))) // 4


def _properties(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return schema.get("properties") or {}


def _has_enum(prop: dict[str, Any]) -> bool:
    return "enum" in prop or any("enum" in option for option in prop.get("anyOf") or [])


@pytest.fixture(scope="module", name="full_catalogue")
def _full_catalogue() -> list[dict[str, Any]]:
    """The catalogue of every permission class (full tool set)."""
    return _tools("all")


@pytest.mark.parametrize("permissions,toolset", sorted(BUDGETS))
def test_catalogue_token_budget(permissions, toolset):
    """Every permission set and tool set stays within its token budget."""
    tools = _tools(permissions, toolset)
    assert tools
    assert _tokens(tools) <= BUDGETS[(permissions, toolset)], (permissions, toolset, _tokens(tools))


def test_descriptions_are_short_and_say_when_to_use(full_catalogue):
    """Descriptions: at most 900 characters, first sentence says when to use, no Args block."""
    for item in full_catalogue:
        description = item["description"]
        assert len(description) <= MAX_DESCRIPTION_CHARS, (item["name"], len(description))
        assert description.startswith("Use "), (item["name"], description[:60])
        assert "Args:" not in description and "api_key" not in description, item["name"]
        assert description == description.strip() and "\n    " not in description, item["name"]  # no docstring indentation


def test_no_tool_takes_an_api_key():
    """No input schema and no tool signature has an api_key."""
    for permissions in PERMISSION_SETS:
        for item in _tools(permissions):
            assert "api_key" not in json.dumps(item["inputSchema"]), item["name"]
    for name in tool_permissions():
        func = getattr(server, name, None)
        if func is not None:
            assert "api_key" not in inspect.signature(func).parameters, name


def test_text_tools_have_no_output_schema(full_catalogue):
    """Text tools are unstructured: no output schema in tools/list."""
    for item in full_catalogue:
        assert "outputSchema" not in item, item["name"]


def test_every_parameter_is_described_and_constrained_ones_have_an_enum(full_catalogue):
    """Every parameter has a description; constrained ones an enum; no generated titles."""
    for item in full_catalogue:
        for name, prop in _properties(item["inputSchema"]).items():
            assert prop.get("description"), (item["name"], name)
            if name in CONSTRAINED:
                assert _has_enum(prop), (item["name"], name)
            assert "title" not in prop, (item["name"], name)


def test_schema_compaction_keeps_meaningful_nulls():
    """Titles go, null defaults collapse; a null with another default (no limit) stays."""
    schema = {
        "properties": {
            "a": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "A", "description": "x"},
            "b": {"anyOf": [{"type": "number"}, {"type": "null"}], "default": 25, "title": "B"},
            "title": {"type": "string", "title": "Title"},
        },
        "title": "Args",
        "type": "object",
    }
    assert compact_schema(schema) == {
        "properties": {
            "a": {"type": "string", "description": "x"},
            "b": {"anyOf": [{"type": "number"}, {"type": "null"}], "default": 25},
            "title": {"type": "string"},
        },
        "type": "object",
    }


def test_core_toolset():
    """The core set exists, has 15-24 tools and respects the permission classes."""
    defined = {item["name"] for item in _tools("all")}
    assert CORE_TOOLS <= defined, CORE_TOOLS - defined
    assert 15 <= len(CORE_TOOLS) <= 24
    core = {item["name"] for item in _tools("read,write", "core")}
    assert core == {name for name in CORE_TOOLS if tool_permissions()[name] in ("read", "write")}
    assert {item["name"] for item in _tools("read", "core")} == {n for n in CORE_TOOLS if tool_permissions()[n] == "read"}
    assert parse_toolset("") == "full" and parse_toolset(" Core ") == "core"
    with pytest.raises(ValueError, match="MCP_TOOLSET"):
        parse_toolset("tiny")


def test_core_toolset_marks_tools_outside_the_set():
    """In the core set every mention of a tool outside it is marked; the instructions use core tools only."""
    pattern = re.compile(r"\b(" + "|".join(sorted(tool_permissions(), key=len, reverse=True)) + r")\b( \(full tool set\))?")
    defined = {item["name"] for item in _tools("all")}

    def unmarked(text: str) -> list[str]:
        return [m.group(1) for m in pattern.finditer(text) if m.group(1) in defined and m.group(1) not in CORE_TOOLS and not m.group(2)]

    for item in _tools("read,write", "core"):
        assert not unmarked(item["description"]), (item["name"], unmarked(item["description"]))
    assert "get_training_load (full tool set)" in next(t for t in _tools("read,write", "core") if t["name"] == "get_coach_context")["description"]
    assert not unmarked(SERVER_INSTRUCTIONS)
    for topic in GUIDE_TOPICS:
        assert not unmarked(mark_outside_toolset(guide_text(topic), "core")), topic
    assert mark_outside_toolset("see get_durability", "full") == "see get_durability"
    assert mark_outside_toolset(mark_outside_toolset("get_durability", "core"), "core") == "get_durability (full tool set)"
    original = mcp.toolset
    try:
        mcp.toolset = "core"
        text = asyncio.run(mcp.get_prompt("performance_progression", {})).messages[0].content.text
        assert "get_fatigue_resistance (full tool set)" in text and not unmarked(text)
    finally:
        mcp.toolset = original


def test_core_toolset_registration_and_status_in_a_fresh_process():
    """MCP_TOOLSET=core registers only the core tools; status shows the tool set."""
    code = (
        "import asyncio\n"
        "from intervals_mcp_server import server\n"
        "from intervals_mcp_server.mcp_instance import mcp\n"
        "from intervals_mcp_server.tools.status import format_status, server_status\n"
        "names = sorted(t.name for t in asyncio.run(mcp.list_tools()))\n"
        "print(','.join(names))\n"
        "status = asyncio.run(server_status())\n"
        "print(status['toolset'], status['tools_registered'], len(status['tools_outside_toolset']))\n"
        "print(format_status(status).splitlines()[1])\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MCP_", "FASTMCP_", "OAUTH_"))}
    env.update({"MCP_TOOLSET": "core", "MCP_PERMISSIONS": "read,write", "ATHLETE_ID": "", "PYTHONPATH": str(ROOT / "src")})
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120, check=False, cwd=ROOT / "tests")
    assert result.returncode == 0, result.stderr
    names, counts, status_line = result.stdout.strip().splitlines()[-3:]
    assert set(names.split(",")) == {n for n in CORE_TOOLS if tool_permissions()[n] in ("read", "write")}
    toolset, registered, outside = counts.split()
    assert toolset == "core" and int(registered) == len(names.split(",")) and int(outside) > 30
    assert "tool set core (MCP_TOOLSET)" in status_line


def test_instructions_are_compact_and_name_the_rules():
    """The initialize instructions are short and carry the usage rules."""
    assert mcp.instructions == SERVER_INSTRUCTIONS
    assert len(SERVER_INSTRUCTIONS) <= 1500
    for fragment in ("get_coach_context", "get_activity_report", "compact", "not yet", "explicitly asks",
                     "validate_workout", "dry_run", "intervals://workout-syntax"):
        assert fragment in SERVER_INSTRUCTIONS, fragment


def test_guides_are_complete_and_referenced_topics_exist(full_catalogue):
    """Every guide exists and every intervals:// reference points to one."""
    assert set(typing.get_args(guide_module.GuideTopic)) == set(GUIDE_TOPICS)
    for topic in GUIDE_TOPICS:
        text = guide_text(topic)
        assert len(text) > 300 and "TODO" not in text, topic
    known = {guide_uri(topic) for topic in GUIDE_TOPICS}
    texts = [item["description"] + json.dumps(item["inputSchema"]) for item in full_catalogue]
    texts += [SERVER_INSTRUCTIONS] + [guide_text(topic) for topic in GUIDE_TOPICS]
    for text in texts:
        for uri in re.findall(r"intervals://[a-z/-]+[a-z]", text):
            if uri not in ("intervals://methods", "intervals://custom-items"):
                assert uri in known, uri
    names = {item["name"] for item in full_catalogue}
    for item in full_catalogue:  # write/validate tools point to the workout syntax
        if item["name"] in ("add_or_update_event", "add_events_bulk", "create_library_workout", "validate_workout"):
            assert "intervals://workout-syntax" in item["description"] + json.dumps(item["inputSchema"]), item["name"]
    assert "get_guide" in names


def test_tool_names_in_prompts_and_guides_exist(full_catalogue):
    """Tool calls named in prompts, guides and instructions refer to existing tools."""
    names = {item["name"] for item in full_catalogue}
    prompts = asyncio.run(mcp.list_prompts())
    texts = [guide_text(topic) for topic in GUIDE_TOPICS] + [SERVER_INSTRUCTIONS]
    for prompt in prompts:
        args = {arg.name: ("2026-10-20" if "date" in arg.name else "i1") for arg in prompt.arguments or [] if arg.required}
        result = asyncio.run(mcp.get_prompt(prompt.name, args))
        texts.append(" ".join(m.content.text for m in result.messages if isinstance(m.content, TextContent)))
    pattern = re.compile(r"\b((?:get|add|analyze|compare|find|list|update|delete|create|validate|preview)_[a-z_]+)\(")
    for text in texts:
        for name in pattern.findall(text):
            assert name in names, name


def test_new_prompts():
    """The stage 3 prompts name the tools and parameters they rely on."""
    from intervals_mcp_server.tools.status import (  # pylint: disable=import-outside-toplevel
        coach_handoff, fueling_review, plan_health_check, race_week,
    )

    week = race_week("2026-10-25", "Gravel race")
    for fragment in ("get_load_projection(target_date='2026-10-25'", "get_fueling_analysis", "forecast", "checklist",
                     "Do not state causes"):
        assert fragment in week, fragment
    undated = race_week()
    assert "ask the athlete which race and date is meant" in undated and "do not assume the next A race" in undated
    assert "target_date='<the confirmed race date, YYYY-MM-DD>'" in undated
    assert "get_fueling_analysis(activity_id='i1'" in fueling_review("i1") and "12 weeks" in fueling_review()
    plan = plan_health_check(6)
    assert "get_load_projection(scenario=" in plan and "6 weeks" in plan and "never written" in plan
    handoff = coach_handoff("2026-10-10")
    assert "get_coach_context(detail_level='compact')" in handoff and "end_date='2026-10-10'" in handoff


def test_lenient_values_from_older_or_guessing_clients():
    """R28-1/R28-2/R28-6: output_format synonyms give text, guide topics in URI/path/underscore form work,
    FITNESS_TABLE is a custom item type."""
    from pydantic import TypeAdapter  # pylint: disable=import-outside-toplevel

    from intervals_mcp_server.tools.custom_items import CustomItemType  # pylint: disable=import-outside-toplevel
    from intervals_mcp_server.tools.guide import topic_choice  # pylint: disable=import-outside-toplevel
    from intervals_mcp_server.utils.params import OutputFormat, stream_output_choice  # pylint: disable=import-outside-toplevel

    output = TypeAdapter(OutputFormat)
    for value in ("markdown", "Markdown", "md", "html", "table", "plain", "txt", "text/plain", "summary", "compact", " TEXT "):
        assert output.validate_python(value) == "text", value
    for value in ("json", "JSON", " Json ", "application/json"):
        assert output.validate_python(value) == "json", value
    assert stream_output_choice("text") == "summary" and stream_output_choice("CSV") == "full"
    for value, topic in (("intervals://methods/load", "load"), ("methods/load", "load"), ("workout_syntax", "workout-syntax"),
                         ("Workout Syntax", "workout-syntax"), ("intervals://workout-syntax", "workout-syntax"),
                         ("intervals://guide", "usage"), ("power_meters", "power-meters"), ("syntax", "workout-syntax"),
                         ("guide", "usage"), ("/methods/activity_data/", "activity-data")):
        assert topic_choice(value) == topic, value
    for value in ("intervals://methods/load", "workout_syntax", "Power Meters"):
        result = asyncio.run(mcp.call_tool("get_guide", {"topic": value}))
        assert isinstance(result, list) and result[0].text.startswith("#"), value
    assert "FITNESS_TABLE" in typing.get_args(CustomItemType)


def test_pace_value_format_is_in_the_write_schemas():
    """R28-3: the MINS_KM/MINS_MILE value format is visible where workouts are written."""
    tools = {item["name"]: item for item in _tools("all")}
    for name in ("add_or_update_event", "create_library_workout"):
        value = tools[name]["inputSchema"]["$defs"]["Value"]["description"]
        assert "MINS_KM/MINS_MILE: seconds (335 = 5:35/km) or decimal minutes" in value and "5:21" in value, name


def test_wire_format_round_trip():
    """initialize, tools/list, tools/call, prompts/list and resources/list through a real MCP session."""

    async def run() -> dict[str, Any]:
        async with create_connected_server_and_client_session(mcp) as session:
            listed = await session.list_tools()
            called = await session.call_tool("get_guide", {"topic": "Workout-Syntax"})
            bad = await session.call_tool("get_guide", {"topic": "nope"})
            prompts = await session.list_prompts()
            resources = await session.list_resources()
            read = await session.read_resource(guide_uri("load"))  # type: ignore[arg-type]
            return {"listed": listed, "called": called, "bad": bad, "prompts": prompts, "resources": resources, "read": read}

    out = asyncio.run(run())
    tool = next(t for t in out["listed"].tools if t.name == "get_guide")
    assert tool.outputSchema is None and tool.inputSchema["properties"]["topic"]["enum"][0] == "usage"
    called = out["called"]
    assert called.isError is False and called.structuredContent is None
    assert len(called.content) == 1 and called.content[0].text == guide_text("workout-syntax")
    assert out["bad"].isError is True
    prompt_names = {p.name for p in out["prompts"].prompts}
    assert {"race_week", "fueling_review", "plan_health_check", "coach_handoff", "weekly_training_review"} <= prompt_names
    uris = {str(r.uri) for r in out["resources"].resources}
    assert {guide_uri(topic) for topic in GUIDE_TOPICS} <= uris and "intervals://custom-items" in uris
    assert out["read"].contents[0].text == guide_text("load")
