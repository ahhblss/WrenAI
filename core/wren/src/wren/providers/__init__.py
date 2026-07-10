"""Shared provider trio for Wren toolkit SDKs.

Re-exported by wren-langchain and wren-pydantic for backward compatibility;
imported directly by wren-mcp. The trio resolves, at toolkit construction
time, where the manifest comes from (``ProjectMDLSource``), how to resolve a
connection profile (``ProfileConnectionProvider``), and where the long-lived
memory store lives (``QdrantMemoryProvider`` / ``NoopMemoryProvider``).
"""

from wren.providers.connection import ProfileConnectionProvider
from wren.providers.exceptions import (
    MemoryNotEnabledError,
    WrenToolkitInitError,
)
from wren.providers.mdl_source import ProjectMDLSource
from wren.providers.memory import NoopMemoryProvider, QdrantMemoryProvider

__all__ = [
    "MemoryNotEnabledError",
    "NoopMemoryProvider",
    "ProfileConnectionProvider",
    "ProjectMDLSource",
    "QdrantMemoryProvider",
    "WrenToolkitInitError",
]
