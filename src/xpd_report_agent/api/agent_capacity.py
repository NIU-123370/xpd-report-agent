from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

DEFAULT_AGENT_MAX_CONCURRENCY = 3
MIN_AGENT_MAX_CONCURRENCY = 1
MAX_AGENT_MAX_CONCURRENCY = 10


def agent_max_concurrency() -> int:
    """Return the configured process-wide Agent analysis concurrency."""

    try:
        configured = int(
            os.getenv(
                "XPD_AGENT_MAX_CONCURRENCY",
                str(DEFAULT_AGENT_MAX_CONCURRENCY),
            )
        )
    except (TypeError, ValueError):
        configured = DEFAULT_AGENT_MAX_CONCURRENCY
    return max(
        MIN_AGENT_MAX_CONCURRENCY,
        min(MAX_AGENT_MAX_CONCURRENCY, configured),
    )


@dataclass
class _LoopState:
    loop: asyncio.AbstractEventLoop
    limit: int
    semaphore: asyncio.Semaphore
    active: int = 0
    waiting: int = 0


class _AgentCapacity:
    """One process-wide limiter that can be reused by sequential event loops."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: _LoopState | None = None

    def _state_for_running_loop(self) -> _LoopState:
        loop = asyncio.get_running_loop()
        with self._lock:
            state = self._state
            if state is not None and state.loop is loop:
                return state
            if state is not None and not state.loop.is_closed():
                raise RuntimeError(
                    "Agent capacity cannot be shared by concurrent event loops."
                )
            if state is not None and (state.active or state.waiting):
                raise RuntimeError(
                    "Closed Agent capacity loop still has active or waiting work."
                )
            limit = agent_max_concurrency()
            state = _LoopState(
                loop=loop,
                limit=limit,
                semaphore=asyncio.Semaphore(limit),
            )
            self._state = state
            return state

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        state = self._state_for_running_loop()
        acquired = False
        with self._lock:
            state.waiting += 1
        try:
            await state.semaphore.acquire()
            acquired = True
            with self._lock:
                state.waiting -= 1
                state.active += 1
            yield
        except BaseException:
            if not acquired:
                with self._lock:
                    state.waiting -= 1
            raise
        finally:
            if acquired:
                with self._lock:
                    state.active -= 1
                state.semaphore.release()

    def health(self) -> dict[str, int]:
        with self._lock:
            state = self._state
            if state is None or (
                state.loop.is_closed() and not state.active and not state.waiting
            ):
                return {
                    "limit": agent_max_concurrency(),
                    "active": 0,
                    "waiting": 0,
                }
            return {
                "limit": state.limit,
                "active": state.active,
                "waiting": state.waiting,
            }


_agent_capacity = _AgentCapacity()


@asynccontextmanager
async def agent_capacity_slot() -> AsyncIterator[None]:
    """Wait for and hold one global Agent analysis slot."""

    async with _agent_capacity.slot():
        yield


def agent_capacity_health() -> dict[str, int]:
    """Return the effective limit and current in-process queue counts."""

    return _agent_capacity.health()
