from __future__ import annotations

import asyncio

from xpd_report_agent.api.memory_consolidation import MemoryConsolidationManager
from xpd_report_agent.memory_governance import memory_policy, personal_memory_states


def _configure_personal_memory(tmp_path, monkeypatch, *, size: int) -> str:
    scope = "a" * 20
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("XPD_IDENTITY_MODE", "user_id")
    monkeypatch.setenv("XPD_MEMORY_CHAR_LIMIT", "1000")
    monkeypatch.setenv("XPD_USER_CHAR_LIMIT", "1000")
    monkeypatch.setenv("XPD_MEMORY_CONSOLIDATION_RATIO", "0.8")
    monkeypatch.setenv("XPD_MEMORY_CRITICAL_RATIO", "0.95")
    monkeypatch.setenv("XPD_MEMORY_CONSOLIDATION_TARGET_RATIO", "0.6")
    personal = tmp_path / "memories" / "users" / scope
    personal.mkdir(parents=True)
    (personal / "MEMORY.md").write_text("x" * size, encoding="utf-8")
    return scope


def test_memory_policy_has_normal_trigger_critical_and_target_zones(
    tmp_path, monkeypatch
):
    scope = _configure_personal_memory(tmp_path, monkeypatch, size=790)
    normal = personal_memory_states(scope)[0]
    assert normal.write_policy == "normal"

    normal.path.write_text("x" * 800, encoding="utf-8")
    trigger = personal_memory_states(scope)[0]
    assert trigger.write_policy == "write_and_consolidate"

    normal.path.write_text("x" * 950, encoding="utf-8")
    critical = personal_memory_states(scope)[0]
    assert critical.write_policy == "consolidate_only"
    assert memory_policy().target_ratio == 0.6


def test_background_consolidation_is_single_flight_and_targets_sixty_percent(
    tmp_path, monkeypatch
):
    scope = _configure_personal_memory(tmp_path, monkeypatch, size=850)
    calls = 0

    async def executor(job):
        nonlocal calls
        calls += 1
        state = personal_memory_states(job["owner_scope"])[0]
        state.path.write_text("x" * 600, encoding="utf-8")
        await asyncio.sleep(0)
        return {"target_met": True}

    async def scenario():
        manager = MemoryConsolidationManager(executor)
        assert manager.schedule_if_needed(scope) is True
        assert manager.schedule_if_needed(scope) is False
        task = manager._tasks[scope]
        await task
        return manager

    manager = asyncio.run(scenario())

    assert calls == 1
    assert personal_memory_states(scope)[0].usage_ratio == 0.6
    assert manager.health()["active_tasks"] == 0
