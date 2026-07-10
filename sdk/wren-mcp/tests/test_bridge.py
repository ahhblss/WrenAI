"""Unit tests for the run_blocked sync->async bridge (timeout protection)."""

from __future__ import annotations

import asyncio
import time

import pytest

from wren_mcp._bridge import run_blocked


class _FakeState:
    """Minimal stand-in for ServerState: only engine_lock + tool_timeout."""

    def __init__(self, *, timeout: float) -> None:
        self.engine_lock = asyncio.Lock()
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
