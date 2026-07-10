"""wren-mcp: MCP (Model Context Protocol) server for the Wren semantic SQL engine.

Exposes a CLI-prepared Wren project as a streamable-http MCP service that AI
agents (Claude Code, Cursor, Codex, ...) can call via the MCP protocol,
replacing direct CLI shell-out. The tool surface mirrors the wren-langchain /
wren-pydantic SDK tools and the wren CLI, reusing the shared provider trio
in ``wren.providers``.
"""

__version__ = "0.1.0"
