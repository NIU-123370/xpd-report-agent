from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from xpd_report_agent.api.agent_capacity import agent_capacity_slot
from xpd_report_agent.memory_governance import (
    discover_personal_memory_scopes,
    memory_policy,
    personal_memory_states,
)

logger = logging.getLogger(__name__)

MemoryConsolidationExecutor = Callable[
    [dict[str, Any]], Awaitable[dict[str, Any] | None]
]


def _env_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def memory_consolidation_enabled() -> bool:
    return _env_enabled("XPD_MEMORY_AUTO_CONSOLIDATION_ENABLED", True)


def memory_consolidation_scan_seconds() -> int:
    return _env_int(
        "XPD_MEMORY_CONSOLIDATION_SCAN_SECONDS",
        30,
        minimum=10,
        maximum=3600,
    )


def memory_consolidation_max_attempts() -> int:
    return _env_int(
        "XPD_MEMORY_CONSOLIDATION_MAX_ATTEMPTS",
        3,
        minimum=1,
        maximum=5,
    )


def memory_consolidation_retry_cooldown_seconds() -> int:
    return _env_int(
        "XPD_MEMORY_CONSOLIDATION_RETRY_COOLDOWN_SECONDS",
        600,
        minimum=60,
        maximum=86_400,
    )


class MemoryConsolidationManager:
    """Run one best-effort background consolidation task per owner."""

    def __init__(self, executor: MemoryConsolidationExecutor) -> None:
        self.executor = executor
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._last_results: dict[str, dict[str, Any]] = {}
        self._retry_after: dict[str, float] = {}

    def schedule_if_needed(self, scope: str) -> bool:
        if not memory_consolidation_enabled():
            return False
        targets = [state.public() for state in personal_memory_states(scope) if state.at_trigger]
        if not targets:
            return False
        if time.monotonic() < self._retry_after.get(scope, 0):
            return False
        current = self._tasks.get(scope)
        if current and not current.done():
            return False
        task = asyncio.create_task(
            self._run(scope, targets),
            name=f"memory-consolidation:{scope}",
        )
        self._tasks[scope] = task

        def remove_finished(done: asyncio.Task[None]) -> None:
            if self._tasks.get(scope) is done:
                self._tasks.pop(scope, None)
            if done.cancelled():
                return
            if error := done.exception():
                logger.error("Memory consolidation crashed scope=%s: %s", scope, error)

        task.add_done_callback(remove_finished)
        return True

    async def _run(self, scope: str, targets: list[dict[str, Any]]) -> None:
        attempts = memory_consolidation_max_attempts()
        for attempt in range(1, attempts + 1):
            job = {
                "owner_scope": scope,
                "attempt_count": attempt,
                "targets": targets,
            }
            try:
                async with agent_capacity_slot():
                    result = await self.executor(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = getattr(exc, "detail", None)
                outcome_unknown = bool(
                    isinstance(detail, dict) and detail.get("outcome_unknown")
                )
                terminal = attempt == attempts or outcome_unknown
                self._last_results[scope] = {
                    "status": "failed" if terminal else "retrying",
                    "attempt_count": attempt,
                    "error": str(exc)[:500],
                }
                if terminal:
                    self._retry_after[scope] = (
                        time.monotonic()
                        + memory_consolidation_retry_cooldown_seconds()
                    )
                    logger.warning(
                        "Memory consolidation failed scope=%s attempts=%s: %s",
                        scope,
                        attempt,
                        exc,
                    )
                    return
                await asyncio.sleep(min(2 ** (attempt - 1), 5))
                targets = [
                    state.public()
                    for state in personal_memory_states(scope)
                    if state.at_trigger
                ]
                if not targets:
                    self._last_results[scope] = {
                        "status": "succeeded",
                        "attempt_count": attempt,
                        "result": {"already_below_trigger": True},
                    }
                    return
                continue
            self._last_results[scope] = {
                "status": "succeeded",
                "attempt_count": attempt,
                "result": result,
            }
            self._retry_after.pop(scope, None)
            return

    def scan_and_schedule(self) -> int:
        return sum(
            1
            for scope in discover_personal_memory_scopes()
            if self.schedule_if_needed(scope)
        )

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def health(self) -> dict[str, Any]:
        policy = memory_policy()
        return {
            "ok": True,
            "enabled": memory_consolidation_enabled(),
            "active_tasks": len([task for task in self._tasks.values() if not task.done()]),
            "recent_failures": len(
                [
                    result
                    for result in self._last_results.values()
                    if result.get("status") == "failed"
                ]
            ),
            "trigger_ratio": policy.trigger_ratio,
            "critical_ratio": policy.critical_ratio,
            "target_ratio": policy.target_ratio,
        }
