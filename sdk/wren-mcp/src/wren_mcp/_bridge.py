"""Sync->async bridge: offload blocking engine calls to a worker thread.

The WrenEngine, every connector (psycopg3 etc.), and the MemoryStore are
blocking and NOT concurrency-safe. MCP tools are async. Every blocking call is
offloaded via ``anyio.to_thread.run_sync`` and serialized by the ServerState
``engine_lock`` so a single cached DB connection is never used from two
threads at once. Correctness over throughput - remote agent call rate is low,
and a per-engine lock is far simpler than a connector pool.

A ``state.tool_timeout`` ceiling (default 120s, env ``WREN_MCP_TOOL_TIMEOUT``)
guards against a hung connector or embedding call holding the lock forever.
On timeout the lock is released so the server keeps responding - the worker
thread itself can't be force-stopped in Python, but it no longer blocks other
calls - and ``TimeoutError`` propagates to the tool, which returns an error
envelope.

Usage in a tool::

    @mcp.tool()
    async def wren_query(sql: str, limit: int = 100) -> dict:
        return await run_blocked(state, state.query, sql, limit)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

import anyio

if TYPE_CHECKING:
    from wren_mcp._state import ServerState

logger = logging.getLogger(__name__)


async def run_blocked(
    state: ServerState, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Run a blocking callable under the engine lock, in a worker thread.

    The lock serializes all engine/connector/memory calls against the single
    cached connection. ``anyio.to_thread.run_sync`` moves the work off the
    event loop so the server stays responsive while a query runs. The whole
    call is bounded by ``state.tool_timeout``; a timeout releases the lock
    (the worker thread can't be killed, but it no longer blocks other calls)
    and raises ``TimeoutError`` for the tool to surface as an error envelope.
    """
    async with state.engine_lock:
        try:
            return await asyncio.wait_for(
                # abandon_on_cancel=True is essential: anyio shields the worker
                # future by default (CancelScope(shield=True)), which would make
                # wait_for's timeout wait for the thread to finish - defeating
                # the whole point. With it set, a timeout cancels the await
                # promptly; the thread keeps running to completion in the
                # background but no longer holds the lock.
                anyio.to_thread.run_sync(
                    lambda: fn(*args, **kwargs), abandon_on_cancel=True
                ),
                timeout=state.tool_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "run_blocked: %r exceeded %.1fs timeout; engine_lock released "
                "(worker thread cannot be killed and may still be running)",
                getattr(fn, "__qualname__", fn),
                state.tool_timeout,
            )
            raise TimeoutError(
                f"tool call exceeded {state.tool_timeout:.0f}s timeout - likely "
                "a hung connector/embedding call; the engine lock has been "
                "released but the worker thread may still be running"
            ) from None
