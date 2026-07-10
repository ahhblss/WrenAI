"""Sync->async bridge: offload blocking engine calls to a worker thread.

The WrenEngine, every connector (psycopg3 etc.), and the MemoryStore are
blocking and NOT concurrency-safe. MCP tools are async. Every blocking call is
offloaded via ``anyio.to_thread.run_sync``. Engine/connector calls serialize on
``engine_lock`` (single cached DB connection, not thread-safe); memory calls
serialize on a separate ``memory_lock`` so an embedding call never blocks a SQL
query (and vice versa) - the two share no state. Correctness over throughput - remote agent call rate is low,
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


async def _run_under_lock(
    state: ServerState,
    lock: asyncio.Lock,
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run a blocking callable under *lock*, in a worker thread.

    ``anyio.to_thread.run_sync`` moves the work off the event loop so the
    server stays responsive while a query runs. The whole call is bounded by
    ``state.tool_timeout``; a timeout releases the lock (the worker thread
    can't be killed, but it no longer blocks other calls) and raises
    ``TimeoutError`` for the tool to surface as an error envelope.
    """
    async with lock:
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
                "_run_under_lock: %r exceeded %.1fs timeout; lock released "
                "(worker thread cannot be killed and may still be running)",
                getattr(fn, "__qualname__", fn),
                state.tool_timeout,
            )
            raise TimeoutError(
                f"tool call exceeded {state.tool_timeout:.0f}s timeout - likely "
                "a hung connector/embedding call; the lock has been released "
                "but the worker thread may still be running"
            ) from None


async def run_blocked(
    state: ServerState, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Engine/connector call - serialized by ``state.engine_lock``.

    The WrenEngine and every connector (psycopg3 etc.) are blocking and not
    concurrency-safe against the single cached DB connection, so engine calls
    serialize on ``engine_lock``. Memory calls use :func:`run_memory_blocked`
    instead so an embedding call doesn't block a SQL query (and vice versa).
    """
    return await _run_under_lock(state, state.engine_lock, fn, *args, **kwargs)


async def run_memory_blocked(
    state: ServerState, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Memory (Qdrant/Ark embedding) call - serialized by ``state.memory_lock``.

    Memory calls share no state with engine/connector calls (separate cached
    MemoryStore vs cached DB connection), so they run under a separate lock -
    an embedding call can proceed while a SQL query runs, and vice versa. The
    OpenAI and Qdrant sync clients are themselves thread-safe, so memory calls
    *could* go lock-free in principle; ``memory_lock`` is kept only to
    serialize the lazy MemoryStore init and stay conservative on client
    thread-safety. Still bounded by ``state.tool_timeout``.
    """
    return await _run_under_lock(state, state.memory_lock, fn, *args, **kwargs)
