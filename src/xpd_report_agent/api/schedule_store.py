from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEDULE_TIMEZONE = "Asia/Shanghai"
SCHEDULE_ID_PREFIX = "sched_"
RUN_ID_PREFIX = "run_"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def default_schedule_state_path() -> Path:
    configured = os.getenv("XPD_SCHEDULE_STATE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    hermes_home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
    return hermes_home / "xpd-report-agent" / "schedules.json"


def new_schedule_id() -> str:
    return f"{SCHEDULE_ID_PREFIX}{secrets.token_hex(16)}"


def new_callback_token() -> str:
    return secrets.token_urlsafe(32)


def _run_session_id(owner_scope: str) -> str:
    return f"xpd_{owner_scope}_scheduled_{secrets.token_hex(12)}"


class ScheduleStore:
    """Durable owner-scoped schedule and execution metadata.

    Hermes remains the scheduler of record. This small store only maps a
    native Hermes cron job back to the browser owner and its generated xpd
    report session, which Hermes' global cron store does not model.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_schedule_state_path()
        self._lock = threading.RLock()

    @staticmethod
    def empty_state() -> dict[str, Any]:
        return {"version": 1, "schedules": {}, "runs": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.empty_state()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.empty_state()
        if not isinstance(payload, dict):
            return self.empty_state()
        schedules = payload.get("schedules")
        runs = payload.get("runs")
        if not isinstance(schedules, dict) or not isinstance(runs, dict):
            return self.empty_state()
        return {"version": 1, "schedules": schedules, "runs": runs}

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.path)

    @staticmethod
    def public_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
        return {
            key: schedule.get(key)
            for key in (
                "schedule_id",
                "report_type",
                "report_label",
                "frequency",
                "run_date",
                "weekday",
                "time",
                "timezone",
                "format",
                "enabled",
                "state",
                "next_run_at",
                "last_run_at",
                "last_status",
                "last_error",
                "created_at",
                "updated_at",
            )
        }

    @staticmethod
    def public_run(run: dict[str, Any]) -> dict[str, Any]:
        return {
            key: run.get(key)
            for key in (
                "run_id",
                "schedule_id",
                "session_id",
                "report_type",
                "status",
                "trigger_type",
                "scheduled_for",
                "attempt_count",
                "error_summary",
                "created_at",
                "started_at",
                "completed_at",
            )
        }

    def create(self, schedule: dict[str, Any]) -> dict[str, Any]:
        schedule_id = str(schedule["schedule_id"])
        with self._lock:
            state = self._load()
            if schedule_id in state["schedules"]:
                raise ValueError("Schedule already exists.")
            state["schedules"][schedule_id] = deepcopy(schedule)
            self._save(state)
        return self.public_schedule(schedule)

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        with self._lock:
            schedule = self._load()["schedules"].get(schedule_id)
            return deepcopy(schedule) if isinstance(schedule, dict) else None

    def get_owned(self, schedule_id: str, owner_scope: str) -> dict[str, Any] | None:
        schedule = self.get(schedule_id)
        if not schedule or not hmac.compare_digest(
            str(schedule.get("owner_scope") or ""), owner_scope
        ):
            return None
        return schedule

    def list_owned(self, owner_scope: str) -> list[dict[str, Any]]:
        with self._lock:
            schedules = [
                deepcopy(item)
                for item in self._load()["schedules"].values()
                if isinstance(item, dict)
                and hmac.compare_digest(
                    str(item.get("owner_scope") or ""), owner_scope
                )
            ]
        schedules.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return [self.public_schedule(item) for item in schedules]

    def update(self, schedule_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            state = self._load()
            schedule = state["schedules"].get(schedule_id)
            if not isinstance(schedule, dict):
                return None
            schedule.update(deepcopy(updates))
            schedule["updated_at"] = utc_now()
            state["schedules"][schedule_id] = schedule
            self._save(state)
            return deepcopy(schedule)

    def remove_owned(self, schedule_id: str, owner_scope: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._load()
            schedule = state["schedules"].get(schedule_id)
            if not isinstance(schedule, dict) or not hmac.compare_digest(
                str(schedule.get("owner_scope") or ""), owner_scope
            ):
                return None
            removed = deepcopy(schedule)
            del state["schedules"][schedule_id]
            self._save(state)
            return removed

    def set_manual_trigger(self, schedule_id: str, owner_scope: str) -> str | None:
        with self._lock:
            state = self._load()
            schedule = state["schedules"].get(schedule_id)
            if not isinstance(schedule, dict) or not hmac.compare_digest(
                str(schedule.get("owner_scope") or ""), owner_scope
            ):
                return None
            existing = str(schedule.get("pending_manual_run_id") or "")
            if existing:
                return existing
            marker = secrets.token_hex(16)
            schedule["pending_manual_run_id"] = marker
            schedule["updated_at"] = utc_now()
            self._save(state)
            return marker

    def clear_manual_trigger(self, schedule_id: str, marker: str) -> None:
        with self._lock:
            state = self._load()
            schedule = state["schedules"].get(schedule_id)
            if not isinstance(schedule, dict):
                return
            if hmac.compare_digest(str(schedule.get("pending_manual_run_id") or ""), marker):
                schedule["pending_manual_run_id"] = None
                schedule["updated_at"] = utc_now()
                self._save(state)

    @staticmethod
    def _period_key(schedule: dict[str, Any], local_now: datetime) -> str:
        frequency = schedule.get("frequency")
        if frequency == "once":
            return "once"
        if frequency == "daily":
            return f"day:{local_now.date().isoformat()}"
        monday = local_now.date() - timedelta(days=local_now.weekday())
        return f"week:{monday.isoformat()}"

    def claim_run(
        self,
        schedule_id: str,
        callback_token: str,
        *,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Idempotently claim one cron callback.

        A normal recurring callback is unique per business day/week. An
        explicit "run now" request carries a one-use marker so it can create a
        separate execution within the same reporting period.
        """

        with self._lock:
            state = self._load()
            schedule = state["schedules"].get(schedule_id)
            if not isinstance(schedule, dict):
                return None, False
            expected = str(schedule.get("callback_token") or "")
            if not expected or not hmac.compare_digest(expected, callback_token):
                raise PermissionError("Invalid scheduled report callback token.")
            if not schedule.get("enabled", True):
                return None, False

            local_now = (now or datetime.now(UTC)).astimezone(ZoneInfo(SCHEDULE_TIMEZONE))
            last_callback_at = schedule.get("last_callback_at")
            last_callback_run_id = str(schedule.get("last_callback_run_id") or "")
            if last_callback_at and last_callback_run_id:
                try:
                    callback_age = datetime.now(UTC) - datetime.fromisoformat(
                        str(last_callback_at)
                    ).astimezone(UTC)
                except (TypeError, ValueError):
                    callback_age = timedelta.max
                previous = state["runs"].get(last_callback_run_id)
                if callback_age <= timedelta(seconds=90) and isinstance(previous, dict):
                    return deepcopy(previous), False
            manual_marker = str(schedule.get("pending_manual_run_id") or "")
            if manual_marker:
                trigger_type = "manual"
                run_key = f"manual:{manual_marker}"
                schedule["pending_manual_run_id"] = None
            else:
                trigger_type = "scheduled"
                run_key = self._period_key(schedule, local_now)

            digest = hashlib.sha256(
                f"{schedule_id}:{run_key}".encode("utf-8")
            ).hexdigest()
            run_id = f"{RUN_ID_PREFIX}{digest[:32]}"
            existing = state["runs"].get(run_id)
            if isinstance(existing, dict):
                return deepcopy(existing), False

            run = {
                "run_id": run_id,
                "schedule_id": schedule_id,
                "owner_scope": schedule["owner_scope"],
                "session_id": _run_session_id(str(schedule["owner_scope"])),
                "report_type": schedule["report_type"],
                "report_format": schedule["format"],
                "status": "pending",
                "trigger_type": trigger_type,
                "scheduled_for": local_now.replace(second=0, microsecond=0).isoformat(),
                "attempt_count": 0,
                "submission_started": False,
                "error_summary": None,
                "created_at": utc_now(),
                "started_at": None,
                "completed_at": None,
            }
            state["runs"][run_id] = run
            schedule["last_run_at"] = run["scheduled_for"]
            schedule["last_status"] = "pending"
            schedule["last_error"] = None
            schedule["last_callback_at"] = datetime.now(UTC).isoformat()
            schedule["last_callback_run_id"] = run_id
            schedule["updated_at"] = utc_now()
            state["schedules"][schedule_id] = schedule
            self._save(state)
            return deepcopy(run), True

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._load()["runs"].get(run_id)
            return deepcopy(run) if isinstance(run, dict) else None

    def update_run(self, run_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            state = self._load()
            run = state["runs"].get(run_id)
            if not isinstance(run, dict):
                return None
            run.update(deepcopy(updates))
            state["runs"][run_id] = run
            schedule = state["schedules"].get(str(run.get("schedule_id") or ""))
            if isinstance(schedule, dict):
                status = str(run.get("status") or "")
                schedule["last_status"] = status
                schedule["last_error"] = run.get("error_summary")
                schedule["last_run_at"] = run.get("scheduled_for")
                schedule["updated_at"] = utc_now()
                if schedule.get("frequency") == "once" and status in {
                    "succeeded",
                    "failed",
                }:
                    schedule["enabled"] = False
                    schedule["state"] = "completed" if status == "succeeded" else "failed"
                state["schedules"][schedule["schedule_id"]] = schedule
            self._save(state)
            return deepcopy(run)

    def resumable_run_ids(self, *, max_attempts: int) -> list[str]:
        with self._lock:
            runs = self._load()["runs"]
            return [
                run_id
                for run_id, run in runs.items()
                if isinstance(run, dict)
                and run.get("status") in {"pending", "running"}
                and int(run.get("attempt_count") or 0) < max_attempts
            ]

    def metadata_for_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            runs = self._load()["runs"].values()
            run = next(
                (
                    item
                    for item in runs
                    if isinstance(item, dict) and item.get("session_id") == session_id
                ),
                None,
            )
            if not isinstance(run, dict):
                return None
            return {
                "origin": "scheduled",
                "read_only": True,
                "schedule_id": run.get("schedule_id"),
                "report_kind": run.get("report_type"),
                "run_status": run.get("status"),
                "scheduled_for": run.get("scheduled_for"),
                "error_summary": run.get("error_summary"),
            }

    def delete_run_for_session(self, session_id: str) -> None:
        with self._lock:
            state = self._load()
            kept = {
                run_id: run
                for run_id, run in state["runs"].items()
                if not isinstance(run, dict) or run.get("session_id") != session_id
            }
            if kept != state["runs"]:
                state["runs"] = kept
                self._save(state)


_schedule_store: ScheduleStore | None = None


def schedule_store() -> ScheduleStore:
    global _schedule_store
    expected_path = default_schedule_state_path()
    if _schedule_store is None or _schedule_store.path != expected_path:
        _schedule_store = ScheduleStore(expected_path)
    return _schedule_store
