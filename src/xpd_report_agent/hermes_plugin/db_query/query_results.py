from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from typing import Any

SESSION_ID_PATTERN = re.compile(r"xpd_[0-9a-f]{20}_[A-Za-z0-9_]+")
RESULT_ID_PATTERN = re.compile(r"result_[0-9a-f]{32}")
DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_RESULTS = 500
DEFAULT_MAX_RESULTS_PER_SESSION = 20
DEFAULT_MAX_BYTES_PER_RESULT = 5 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 50 * 1024 * 1024


class QueryResultRegistry:
    """Bounded in-process proof that export data came from db_execute_sql."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._results: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _ttl_seconds() -> int:
        try:
            return min(
                86_400,
                max(60, int(os.getenv("XPD_QUERY_RESULT_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))),
            )
        except (TypeError, ValueError):
            return DEFAULT_TTL_SECONDS

    @staticmethod
    def _max_results() -> int:
        try:
            return min(
                5000,
                max(10, int(os.getenv("XPD_QUERY_RESULT_MAX_ENTRIES", str(DEFAULT_MAX_RESULTS)))),
            )
        except (TypeError, ValueError):
            return DEFAULT_MAX_RESULTS

    @staticmethod
    def _bounded_env(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            return min(maximum, max(minimum, int(os.getenv(name, str(default)))))
        except (TypeError, ValueError):
            return default

    @classmethod
    def _max_results_per_session(cls) -> int:
        return cls._bounded_env(
            "XPD_QUERY_RESULT_MAX_PER_SESSION",
            DEFAULT_MAX_RESULTS_PER_SESSION,
            1,
            1000,
        )

    @classmethod
    def _max_bytes_per_result(cls) -> int:
        return cls._bounded_env(
            "XPD_QUERY_RESULT_MAX_BYTES_PER_RESULT",
            DEFAULT_MAX_BYTES_PER_RESULT,
            64 * 1024,
            50 * 1024 * 1024,
        )

    @classmethod
    def _max_total_bytes(cls) -> int:
        return cls._bounded_env(
            "XPD_QUERY_RESULT_MAX_TOTAL_BYTES",
            DEFAULT_MAX_TOTAL_BYTES,
            1024 * 1024,
            1024 * 1024 * 1024,
        )

    @staticmethod
    def _session_id(value: Any) -> str | None:
        session_id = str(value or "")
        if not SESSION_ID_PATTERN.fullmatch(session_id) or "_reflection_" in session_id:
            return None
        return session_id

    def _prune(self, now: float, *, reserve: int = 0) -> None:
        expired = [
            result_id
            for result_id, item in self._results.items()
            if float(item["expires_at"]) <= now
        ]
        for result_id in expired:
            self._results.pop(result_id, None)
        allowed = max(0, self._max_results() - max(0, reserve))
        overflow = len(self._results) - allowed
        if overflow > 0:
            oldest = sorted(
                self._results.items(), key=lambda pair: float(pair[1]["created_at"])
            )[:overflow]
            for result_id, _ in oldest:
                self._results.pop(result_id, None)

    @staticmethod
    def _payload_size(payload: dict[str, Any]) -> int:
        return len(
            json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode(
                "utf-8"
            )
        )

    def _make_room(self, *, session_id: str, size_bytes: int) -> bool:
        if size_bytes > self._max_bytes_per_result() or size_bytes > self._max_total_bytes():
            return False

        owned = sorted(
            (
                (result_id, item)
                for result_id, item in self._results.items()
                if item["session_id"] == session_id
            ),
            key=lambda pair: float(pair[1]["created_at"]),
        )
        while len(owned) >= self._max_results_per_session():
            result_id, _ = owned.pop(0)
            self._results.pop(result_id, None)

        total_bytes = sum(int(item.get("size_bytes") or 0) for item in self._results.values())
        if total_bytes + size_bytes <= self._max_total_bytes():
            return True
        oldest = sorted(
            self._results.items(), key=lambda pair: float(pair[1]["created_at"])
        )
        for result_id, item in oldest:
            self._results.pop(result_id, None)
            total_bytes -= int(item.get("size_bytes") or 0)
            if total_bytes + size_bytes <= self._max_total_bytes():
                return True
        return total_bytes + size_bytes <= self._max_total_bytes()

    def store(
        self,
        *,
        session_id: Any,
        sql: str,
        columns: list[str],
        rows: list[dict[str, Any]],
        truncated: bool,
    ) -> str | None:
        safe_session_id = self._session_id(session_id)
        if safe_session_id is None:
            return None
        now = time.time()
        result_id = f"result_{uuid.uuid4().hex}"
        stored_payload = {
            "result_id": result_id,
            "session_id": safe_session_id,
            "sql": sql,
            "columns": columns,
            "rows": rows,
            "truncated": bool(truncated),
            "created_at": now,
            "expires_at": now + self._ttl_seconds(),
        }
        size_bytes = self._payload_size(stored_payload)
        if (
            size_bytes > self._max_bytes_per_result()
            or size_bytes > self._max_total_bytes()
        ):
            return None
        with self._lock:
            # Leave one slot for the result being inserted. Read paths pass no
            # reserve so a full-but-valid registry is not pruned accidentally.
            self._prune(now, reserve=1)
            if not self._make_room(session_id=safe_session_id, size_bytes=size_bytes):
                return None
            stored_payload["columns"] = deepcopy(columns)
            stored_payload["rows"] = deepcopy(rows)
            stored_payload["size_bytes"] = size_bytes
            self._results[result_id] = stored_payload
        return result_id

    def get(self, *, result_id: Any, session_id: Any) -> dict[str, Any] | None:
        safe_session_id = self._session_id(session_id)
        candidate_id = str(result_id or "")
        if safe_session_id is None or not RESULT_ID_PATTERN.fullmatch(candidate_id):
            return None
        now = time.time()
        with self._lock:
            self._prune(now)
            item = self._results.get(candidate_id)
            if item is None or item["session_id"] != safe_session_id:
                return None
            return deepcopy(item)


query_result_registry = QueryResultRegistry()
