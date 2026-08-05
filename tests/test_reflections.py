from __future__ import annotations

import asyncio

from fastapi import HTTPException

from xpd_report_agent.api import reflections as reflections_api
from xpd_report_agent.api import sessions as sessions_api
from xpd_report_agent.api.reflections import ReflectionQueue


async def _no_sleep(_: float) -> None:
    return None


def test_final_reflection_timeout_is_bounded(monkeypatch):
    monkeypatch.delenv("XPD_FINAL_REFLECTION_TIMEOUT_SECONDS", raising=False)
    assert sessions_api._final_reflection_timeout_seconds() == 180

    monkeypatch.setenv("XPD_FINAL_REFLECTION_TIMEOUT_SECONDS", "5")
    assert sessions_api._final_reflection_timeout_seconds() == 30

    monkeypatch.setenv("XPD_FINAL_REFLECTION_TIMEOUT_SECONDS", "9999")
    assert sessions_api._final_reflection_timeout_seconds() == 600


def _schedule(queue: ReflectionQueue) -> dict:
    job = queue.schedule(
        session_id="xpd_owner_session",
        owner_scope="owner",
        turn_end=3,
        end_reason="user_close",
    )
    assert job is not None
    return job


def test_reflection_retries_only_safe_connection_failure(tmp_path, monkeypatch):
    calls = 0

    async def executor(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "HERMES_UNAVAILABLE",
                    "retryable": True,
                    "outcome_unknown": False,
                },
            )
        return {"session_summary": "done"}

    async def scenario():
        monkeypatch.setattr(reflections_api.asyncio, "sleep", _no_sleep)
        queue = ReflectionQueue(executor, path=tmp_path / "reflections.json")
        job = _schedule(queue)
        await asyncio.gather(*list(queue._tasks))
        return queue.get(job["reflection_id"])

    completed = asyncio.run(scenario())

    assert calls == 2
    assert completed["status"] == "succeeded"
    assert completed["outcome_unknown"] is False


def test_reflection_unknown_outcome_is_never_replayed(tmp_path, monkeypatch):
    calls = 0

    async def executor(_):
        nonlocal calls
        calls += 1
        raise HTTPException(
            status_code=504,
            detail={
                "code": "HERMES_TIMEOUT",
                "retryable": True,
                "outcome_unknown": True,
            },
        )

    async def scenario():
        monkeypatch.setattr(reflections_api.asyncio, "sleep", _no_sleep)
        state_path = tmp_path / "reflections.json"
        queue = ReflectionQueue(executor, path=state_path)
        job = _schedule(queue)
        await asyncio.gather(*list(queue._tasks))
        failed = queue.get(job["reflection_id"])

        manual = queue.retry(job["reflection_id"])
        await asyncio.gather(*list(queue._tasks))

        reloaded = ReflectionQueue(executor, path=state_path)
        await reloaded.resume_pending()
        await asyncio.gather(*list(reloaded._tasks))
        return failed, manual, reloaded.get(job["reflection_id"])

    failed, manual, reloaded = asyncio.run(scenario())

    assert calls == 1
    assert failed["status"] == "failed"
    assert failed["retryable"] is True
    assert failed["outcome_unknown"] is True
    assert manual["status"] == "failed"
    assert reloaded["status"] == "failed"


def test_running_reflection_is_failed_closed_after_process_restart(tmp_path):
    calls = 0

    async def executor(_):
        nonlocal calls
        calls += 1
        return {"session_summary": "must not run"}

    async def scenario():
        state_path = tmp_path / "reflections.json"
        queue = ReflectionQueue(executor, path=state_path)
        job_id = queue.idempotency_key("xpd_owner_session", 3)
        queue._save(
            {
                job_id: {
                    "reflection_id": job_id,
                    "idempotency_key": job_id,
                    "session_id": "xpd_owner_session",
                    "owner_scope": "owner",
                    "status": "running",
                    "attempt_count": 1,
                }
            }
        )

        await queue.resume_pending()
        await asyncio.gather(*list(queue._tasks))
        return queue.get(job_id)

    failed = asyncio.run(scenario())

    assert calls == 0
    assert failed["status"] == "failed"
    assert failed["retryable"] is False
    assert failed["outcome_unknown"] is True
