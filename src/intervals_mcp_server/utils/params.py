"""
Annotated parameter types shared by the MCP tools.

Each alias carries the JSON schema description of the parameter and, for constrained values, a
``Literal`` so clients see the allowed values as an ``enum`` in ``tools/list``. Text values are
normalised before validation (surrounding blanks removed, case folded) and an empty string means
"not given" (the parameter's default), as the tools accepted before. Python callers are not
affected: the annotations are only enforced on MCP calls.

A tool can refine a shared description: ``Annotated[DetailLevel, Field(description="...")]``
(the last description wins).
"""

from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field
from pydantic_core import PydanticUseDefault

__all__ = [
    "ActivityId",
    "AllowDuplicate",
    "AthleteId",
    "DetailLevel",
    "DryRun",
    "EndDate",
    "Environment",
    "GearId",
    "OptionalActivityId",
    "OutputFormat",
    "SportTypes",
    "StartDate",
    "ThresholdAs",
    "lower_choice",
    "output_choice",
    "stream_output_choice",
    "upper_choice",
]


def lower_choice(value: Any) -> Any:
    """Strip and lower-case a text choice; an empty text means the default."""
    if isinstance(value, str):
        value = value.strip().lower()
        if not value:
            raise PydanticUseDefault()
    return value


def output_choice(value: Any) -> Any:
    """text or json: json (also application/json) stays json, any other text means text.

    The tools always treated every value other than json as text ("markdown", "table", "plain" ...);
    a client with an old or guessed value keeps getting text instead of a validation error.
    """
    if isinstance(value, str):
        value = value.strip().lower()
        if not value:
            raise PydanticUseDefault()
        return "json" if value in ("json", "application/json") else "text"
    return value


def stream_output_choice(value: Any) -> Any:
    """summary / full / json of get_activity_streams, with the usual synonyms (text = summary, csv = full)."""
    if isinstance(value, str):
        value = value.strip().lower()
        if not value:
            raise PydanticUseDefault()
        aliases = {"text": "summary", "plain": "summary", "markdown": "summary", "md": "summary", "txt": "summary",
                   "csv": "full", "table": "full", "application/json": "json"}
        return aliases.get(value, value)
    return value


def upper_choice(value: Any) -> Any:
    """Strip and upper-case a text choice; an empty text means the default."""
    if isinstance(value, str):
        value = value.strip().upper()
        if not value:
            raise PydanticUseDefault()
    return value


AthleteId = Annotated[
    str | None, Field(description="Default: the configured athlete")
]
ActivityId = Annotated[str, Field(description="Activity id, e.g. i123456789")]
OptionalActivityId = Annotated[str | None, Field(description="Activity id, e.g. i123456789")]
StartDate = Annotated[str | None, Field(description="First day YYYY-MM-DD")]
EndDate = Annotated[str | None, Field(description="Last day YYYY-MM-DD; default today")]
SportTypes = Annotated[
    str | None, Field(description='Comma-separated activity types, e.g. "Ride,GravelRide"')
]
GearId = Annotated[str | None, Field(description="Only activities on this gear id (get_gear_list)")]
# Write tools (see utils/write_safety.py and the write safety section of intervals://guide).
DryRun = Annotated[bool, Field(description="true = check and return the exact request, write nothing")]
AllowDuplicate = Annotated[bool, Field(description="Create even if that day has the same event (name or workout)")]

OutputFormat = Annotated[
    Literal["text", "json"],
    BeforeValidator(output_choice),
    Field(description="json = machine-readable, all fields"),
]
DetailLevel = Annotated[
    Literal["compact", "standard", "full"],
    BeforeValidator(lower_choice),
    Field(description="compact = key numbers first; standard; full = everything"),
]
Environment = Annotated[
    Literal["indoor", "outdoor"] | None,
    BeforeValidator(lower_choice),
    Field(description="Only indoor (trainer, virtual) or outdoor sessions; omit for both"),
]
ThresholdAs = Annotated[
    Literal["moderate", "high"],
    BeforeValidator(lower_choice),
    Field(description="Power zone Z4 (91-105 % FTP) counts as moderate (three-zone Z2) or high (Z3)"),
]
