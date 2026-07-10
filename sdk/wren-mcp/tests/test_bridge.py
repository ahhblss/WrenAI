"""Unit tests for the run_blocked sync->async bridge (timeout protection)."""

from __future__ import annotations

import asyncio
import time

import pytest

from wren_mcp._bridge import run_blocked, run_memory_blocked


class _FakeState:
    """Minimal stand-in for ServerState: engine_lock + memory_lock + tool_timeout."""

    def __init__(self, *, timeout: float) -> None:
        self.engine_lock = asyncio.Lock()
        self.memory_lock = asyncio.Lock()
        self.tool_timeout = timeout


async def test_run_blocked_returns_result_and_releases_lock() -> None:
    """A fast call returns its value and the lock is released after."""
    state = _FakeState(timeout=5)
    result = await run_blocked(state, lambda x: x * 2, 21)
    assert result == 42
    assert not state.engine_lock.locked()


async def test_run_blocked_timeout_releases_lock_and_raises() -> None:
    """A call exceeding tool_timeout raises TimeoutError and frees the lock.

    Regression: run_blocked previously had no ceiling, so a hung connector or
    embedding call held engine_lock forever and stalled every other tool. The
    timeout must also fire promptly - anyio shields the worker future by
    default (abandon_on_cancel=False), which would make wait_for wait for the
    thread to finish and defeat the protection.
    """
    state = _FakeState(timeout=0.1)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    with pytest.raises(TimeoutError):
        await run_blocked(state, time.sleep, 0.5)
    elapsed = loop.time() - t0
    # Must return near the 0.1s timeout, NOT after the 0.5s sleep finishes.
    assert elapsed < 0.4, f"timeout did not fire promptly (elapsed={elapsed:.2f}s)"
    assert not state.engine_lock.locked()


async def test_run_blocked_timeout_does_not_block_next_call() -> None:
    """After a timeout, the next call can still acquire the lock.

    Core guarantee: one hung call can't stall the whole server. The sleeping
    worker thread from the prior call is still alive (Python can't kill
    threads), but the lock is free and a fresh worker serves the next call.
    """
    state = _FakeState(timeout=0.1)
    with pytest.raises(TimeoutError):
        await run_blocked(state, time.sleep, 0.5)
    result = await run_blocked(state, lambda: "ok")
    assert result == "ok"


async def test_run_blocked_serializes_via_lock() -> None:
    """Two concurrent calls run one at a time (engine_lock serializes)."""
    state = _FakeState(timeout=5)
    order: list[str] = []

    def _work(label: str, sleep: float) -> str:
        order.append(f"{label}-start")
        time.sleep(sleep)
        order.append(f"{label}-end")
        return label

    # Kick both off concurrently; the second must wait for the lock.
    results = await asyncio.gather(
        run_blocked(state, _work, "a", 0.05),
        run_blocked(state, _work, "b", 0.05),
    )
    assert results == ["a", "b"]
    # No interleaving: a fully completes before b starts (or vice versa).
    assert order in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    )


async def test_run_memory_blocked_uses_memory_lock_not_engine_lock() -> None:
    """run_memory_blocked holds memory_lock, leaving engine_lock free.

    A memory call must proceed even while engine_lock is held by another
    caller - the whole point of splitting the locks.
    """
    state = _FakeState(timeout=5)
    async with state.engine_lock:
        result = await run_memory_blocked(state, lambda: "ok")
    assert result == "ok"
    assert not state.memory_lock.locked()


async def test_memory_call_does_not_block_engine_call() -> None:
    """A slow memory call and an engine call overlap (separate locks).

    Regression: before the split both shared engine_lock, so a slow embedding
    call stalled every SQL query. With memory_lock separate they run
    concurrently - the engine call must NOT wait for the 0.2s memory call.
    """
    state = _FakeState(timeout=5)
    loop = asyncio.get_event_loop()
    events: list[str] = []

    def _slow_memory() -> str:
        events.append("mem-start")
        time.sleep(0.3)
        events.append("mem-end")
        return "mem"

    def _engine() -> str:
        events.append("eng-start")
        time.sleep(0.2)
        events.append("eng-end")
        return "eng"

    t0 = loop.time()
    results = await asyncio.gather(
        run_memory_blocked(state, _slow_memory),
        run_blocked(state, _engine),
    )
    elapsed = loop.time() - t0
    assert results == ["mem", "eng"]
    # Separate locks -> the engine call runs DURING the memory call's sleep,
    # so eng-end precedes mem-end. A shared lock would serialize the two
    # (mem-end before eng-start), making this false.
    assert events.index("eng-end") < events.index("mem-end"), (
        f"engine did not overlap memory call: {events}"
    )
    # Wall time is ~max(0.3, 0.2)=0.3s, not the sum 0.5s a shared lock takes.
    assert elapsed < 0.45, f"calls serialized (elapsed={elapsed:.2f}s): {events}"
