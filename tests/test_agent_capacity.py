from __future__ import annotations

import asyncio

from xpd_report_agent.api.agent_capacity import (
    agent_capacity_health,
    agent_capacity_slot,
    agent_max_concurrency,
)


def test_agent_max_concurrency_defaults_and_stays_in_range(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_NODES", raising=False)
    monkeypatch.delenv("XPD_AGENT_MAX_CONCURRENCY", raising=False)
    assert agent_max_concurrency() == 3

    monkeypatch.setenv("XPD_AGENT_MAX_CONCURRENCY", "invalid")
    assert agent_max_concurrency() == 3
    monkeypatch.setenv("XPD_AGENT_MAX_CONCURRENCY", "0")
    assert agent_max_concurrency() == 1
    monkeypatch.setenv("XPD_AGENT_MAX_CONCURRENCY", "51")
    assert agent_max_concurrency() == 50
    monkeypatch.setenv("XPD_AGENT_MAX_CONCURRENCY", "20")
    assert agent_max_concurrency() == 20
    monkeypatch.setenv("XPD_AGENT_MAX_CONCURRENCY", "4")
    assert agent_max_concurrency() == 4


def test_agent_max_concurrency_derives_multi_node_default(monkeypatch):
    monkeypatch.delenv("XPD_AGENT_MAX_CONCURRENCY", raising=False)

    monkeypatch.setenv(
        "HERMES_GATEWAY_NODES",
        "hermes-1=http://hermes-1:8642,hermes-2=http://hermes-2:8642",
    )
    assert agent_max_concurrency() == 14

    monkeypatch.setenv(
        "HERMES_GATEWAY_NODES",
        (
            "hermes-1=http://hermes-1:8642,"
            "hermes-2=http://hermes-2:8642,"
            "hermes-3=http://hermes-3:8642"
        ),
    )
    assert agent_max_concurrency() == 20

    monkeypatch.setenv("XPD_AGENT_MAX_CONCURRENCY", "invalid")
    assert agent_max_concurrency() == 20


def test_single_node_pool_keeps_legacy_default(monkeypatch):
    monkeypatch.delenv("XPD_AGENT_MAX_CONCURRENCY", raising=False)
    monkeypatch.setenv(
        "HERMES_GATEWAY_NODES",
        "hermes-1=http://hermes-1:8642",
    )

    assert agent_max_concurrency() == 3


def test_agent_capacity_queues_and_reports_health(monkeypatch):
    monkeypatch.setenv("XPD_AGENT_MAX_CONCURRENCY", "1")

    async def scenario() -> None:
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()

        async def first() -> None:
            async with agent_capacity_slot():
                first_entered.set()
                await release_first.wait()

        async def second() -> None:
            async with agent_capacity_slot():
                second_entered.set()

        first_task = asyncio.create_task(first())
        await first_entered.wait()
        second_task = asyncio.create_task(second())
        for _ in range(10):
            if agent_capacity_health()["waiting"] == 1:
                break
            await asyncio.sleep(0)

        assert agent_capacity_health() == {"limit": 1, "active": 1, "waiting": 1}
        assert not second_entered.is_set()

        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert second_entered.is_set()
        assert agent_capacity_health() == {"limit": 1, "active": 0, "waiting": 0}

    asyncio.run(scenario())


def test_cancelled_waiter_is_removed_from_health(monkeypatch):
    monkeypatch.setenv("XPD_AGENT_MAX_CONCURRENCY", "1")

    async def scenario() -> None:
        release_holder = asyncio.Event()
        holder_entered = asyncio.Event()

        async def holder() -> None:
            async with agent_capacity_slot():
                holder_entered.set()
                await release_holder.wait()

        holder_task = asyncio.create_task(holder())
        await holder_entered.wait()
        waiter = asyncio.create_task(_hold_one_slot())
        for _ in range(10):
            if agent_capacity_health()["waiting"] == 1:
                break
            await asyncio.sleep(0)

        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        assert agent_capacity_health() == {"limit": 1, "active": 1, "waiting": 0}

        release_holder.set()
        await holder_task

    asyncio.run(scenario())


def test_agent_capacity_supports_sequential_event_loops(monkeypatch):
    monkeypatch.setenv("XPD_AGENT_MAX_CONCURRENCY", "2")

    asyncio.run(_hold_one_slot())
    asyncio.run(_hold_one_slot())

    assert agent_capacity_health() == {"limit": 2, "active": 0, "waiting": 0}


async def _hold_one_slot() -> None:
    async with agent_capacity_slot():
        health = agent_capacity_health()
        assert health["active"] == 1
