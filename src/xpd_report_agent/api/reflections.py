from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from xpd_report_agent.api.agent_capacity import agent_capacity_slot

ReflectionExecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | str | None]]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def default_reflection_state_path() -> Path:
    configured = os.getenv("XPD_REFLECTION_STATE_PATH")
    if configured:
        return Path(configured).expanduser()
    hermes_home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
    return hermes_home / "xpd-report-agent" / "reflections.json"


def _retry_flags(exc: Exception) -> tuple[bool, bool]:
    """Return the explicit upstream retry contract carried by API errors."""

    detail = getattr(exc, "detail", None)
    if not isinstance(detail, dict):
        return False, False
    return bool(detail.get("retryable")), bool(detail.get("outcome_unknown"))


def _can_auto_run(job: dict[str, Any], *, max_attempts: int) -> bool:
    if int(job.get("attempt_count") or 0) >= max_attempts:
        return False
    if job.get("status") == "pending":
        return True
    return bool(
        job.get("status") == "failed"
        and job.get("retryable")
        and not job.get("outcome_unknown")
    )


class ReflectionQueue:
    """Small durable queue for non-blocking, idempotent final reflections.

    Periodic three-turn reviews remain Hermes-native. This queue only covers the
    explicit session-end event that Hermes' session resource API does not expose
    as a background-review endpoint.
    """

    def __init__(
        self,
        executor: ReflectionExecutor,
        *,
        path: Path | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.executor = executor
        self.path = path or default_reflection_state_path()
        self.max_attempts = max_attempts
        self._lock = threading.Lock()
        self._tasks: set[asyncio.Task] = set()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        jobs = data.get("jobs") if isinstance(data, dict) else None
        return jobs if isinstance(jobs, dict) else {}

    def _save(self, jobs: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps({"version": 1, "jobs": jobs}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.path)

    @staticmethod
    def idempotency_key(session_id: str, turn_end: int) -> str:
        value = f"{session_id}:session_end:1-{turn_end}:v1"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def schedule(
        self,
        *,
        session_id: str,
        owner_scope: str,
        turn_end: int,
        end_reason: str,
    ) -> dict[str, Any] | None:
        if turn_end < 1:
            return None
        job_id = self.idempotency_key(session_id, turn_end)
        with self._lock:
            jobs = self._load()
            job = jobs.get(job_id)
            if not job:
                job = {
                    "reflection_id": job_id,
                    "idempotency_key": job_id,
                    "session_id": session_id,
                    "owner_scope": owner_scope,
                    "trigger_type": "session_end",
                    "turn_start": 1,
                    "turn_end": turn_end,
                    "prompt_version": "v1",
                    "status": "pending",
                    "attempt_count": 0,
                    "end_reason": end_reason,
                    "error_summary": None,
                    "retryable": None,
                    "outcome_unknown": False,
                    "structured_result": None,
                    "created_at": utc_now(),
                    "completed_at": None,
                }
                jobs[job_id] = job
                self._save(jobs)
        if _can_auto_run(job, max_attempts=self.max_attempts):
            self._spawn(job_id)
        return self.public_job(job)

    def _spawn(self, job_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if any(not task.done() and task.get_name() == f"reflection:{job_id}" for task in self._tasks):
            return
        task = loop.create_task(self._run(job_id), name=f"reflection:{job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, job_id: str) -> None:
        while True:
            retry_delay: float | None = None
            # Reflections are low-priority Agent work. They remain pending
            # until one of the same global analysis slots is available.
            async with agent_capacity_slot():
                with self._lock:
                    jobs = self._load()
                    job = jobs.get(job_id)
                    if not job or job.get("status") == "succeeded":
                        return
                    if job.get("status") == "failed" and not _can_auto_run(
                        job, max_attempts=self.max_attempts
                    ):
                        return
                    attempts = int(job.get("attempt_count") or 0)
                    if attempts >= self.max_attempts:
                        return
                    job["status"] = "running"
                    job["attempt_count"] = attempts + 1
                    job["error_summary"] = None
                    job["retryable"] = None
                    job["outcome_unknown"] = False
                    jobs[job_id] = job
                    self._save(jobs)

                try:
                    result = await self.executor(dict(job))
                except Exception as exc:  # best-effort background work
                    retryable, outcome_unknown = _retry_flags(exc)
                    with self._lock:
                        jobs = self._load()
                        current = jobs.get(job_id)
                        if not current:
                            return
                        current["status"] = "failed"
                        current["error_summary"] = str(exc)[:500]
                        current["retryable"] = retryable
                        current["outcome_unknown"] = outcome_unknown
                        jobs[job_id] = current
                        self._save(jobs)
                        attempts = int(current.get("attempt_count") or 0)
                    if (
                        attempts >= self.max_attempts
                        or not retryable
                        or outcome_unknown
                    ):
                        return
                    retry_delay = float(2 ** (attempts - 1))
                else:
                    with self._lock:
                        jobs = self._load()
                        current = jobs.get(job_id)
                        if not current:
                            return
                        current["status"] = "succeeded"
                        current["retryable"] = False
                        current["outcome_unknown"] = False
                        current["structured_result"] = result
                        current["completed_at"] = utc_now()
                        jobs[job_id] = current
                        self._save(jobs)
                    return
            if retry_delay is not None:
                await asyncio.sleep(retry_delay)

    async def resume_pending(self) -> None:
        with self._lock:
            jobs = self._load()
            changed = False
            for job in jobs.values():
                if job.get("status") != "running":
                    continue
                # A process can stop after Hermes accepted the reflection POST.
                # Without an upstream idempotency key, replaying that job could
                # write the same long-term memory twice, so fail it closed.
                job["status"] = "failed"
                job["retryable"] = False
                job["outcome_unknown"] = True
                job["error_summary"] = (
                    "Reflection was interrupted with an unknown upstream outcome; "
                    "it was not replayed."
                )
                changed = True
            if changed:
                self._save(jobs)
            resumable = [
                job_id
                for job_id, job in jobs.items()
                if _can_auto_run(job, max_attempts=self.max_attempts)
            ]
        for job_id in resumable:
            self._spawn(job_id)

    def get(self, reflection_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._load().get(reflection_id)
        return self.public_job(job) if job else None

    def retry(self, reflection_id: str) -> dict[str, Any] | None:
        with self._lock:
            jobs = self._load()
            job = jobs.get(reflection_id)
            if not job:
                return None
            if job.get("status") == "succeeded":
                return self.public_job(job)
            if job.get("outcome_unknown"):
                return self.public_job(job)
            job["status"] = "pending"
            job["attempt_count"] = 0
            job["error_summary"] = None
            job["retryable"] = None
            job["outcome_unknown"] = False
            jobs[reflection_id] = job
            self._save(jobs)
        self._spawn(reflection_id)
        return self.public_job(job)

    def delete_for_session(self, session_id: str) -> None:
        with self._lock:
            jobs = self._load()
            kept = {
                job_id: job
                for job_id, job in jobs.items()
                if job.get("session_id") != session_id
            }
            if kept != jobs:
                self._save(kept)

    @staticmethod
    def public_job(job: dict[str, Any]) -> dict[str, Any]:
        return {
            key: job.get(key)
            for key in (
                "reflection_id",
                "session_id",
                "trigger_type",
                "turn_start",
                "turn_end",
                "status",
                "attempt_count",
                "error_summary",
                "retryable",
                "outcome_unknown",
                "created_at",
                "completed_at",
            )
        }
