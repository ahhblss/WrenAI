"""Shared exception types for Wren toolkit providers.

These were previously duplicated in wren-langchain and wren-pydantic; they
now live here so wren-mcp (and any future SDK) can reuse them without
depending on a specific agent-framework SDK.
"""


class WrenToolkitInitError(Exception):
    """Raised when ``WrenToolkit.from_project(...)`` cannot validate prerequisites.

    Examples include missing ``wren_project.yml``, missing ``target/mdl.json``,
    or unresolvable profile.
    """


class MemoryNotEnabledError(Exception):
    """Raised when memory operations are called but no memory provider is active.

    Triggered by direct API access to ``toolkit.memory.*`` when the toolkit
    was initialized without memory configured. LLM-facing tools handle this
    case via tool filtering, not by raising.
    """
