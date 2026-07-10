"""Backward-compat shim. Real implementation lives in wren.providers.memory."""

from wren.providers.memory import (  # noqa: F401
    NoopMemoryProvider,
    QdrantMemoryProvider,
)
