"""Sync->async bridge: offload blocking engine calls to a worker thread.

The WrenEngine, every connector (psycopg3 etc.), and the MemoryStore are
blocking and NOT concurrency-safe. MCP tools are async. Every blocking call is
offloaded via ``anyio.to_thread.run_sync`` and serialized by the ServerState
``engine_lock`` so a single cached DB connection is never used from two
threads at once. Correctness over throughput - remote agent call rate is low,
and a per-engine lock is far simpler than a connector pool.

Usage in a tool::

    @mcp.tool()
    async def wren_query(sql: str, limit: int = 100) -> dict:
        return await run_blocked(state, state.query, sql, limit)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

import anyio

if TYPE_CHECKING:
    from wren_mcp._state import ServerState


async def run_blocked(
    state: ServerState, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Run a blocking callable under the engine lock, in a worker thread.

    The lock serializes all engine/connector/memory calls against the single
    cached connection. ``anyio.to_thread.run_sync`` moves the work off the
    event loop so the server stays responsive while a query runs.
    """
    async with state.engine_lock:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
