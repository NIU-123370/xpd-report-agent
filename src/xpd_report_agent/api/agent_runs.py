from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

RUN_ID_PREFIX = "run_"
IDEMPOTENCY_KEY_MAX_LENGTH = 255
RUN_STATUSES = frozenset(
    {"pending", "running", "waiting_input", "succeeded", "failed"}
)
RESUMABLE_STATUSES = frozenset({"pending", "running"})


class AgentRunStoreError(RuntimeError):
    """Base error raised by the durable Agent run store."""


class IdempotencyConflictError(AgentRunStoreError):
    """The same idempotency key was reused with a different request."""


class InvalidRunTransitionError(AgentRunStoreError):
    """A run cannot move to the requested state."""


class RunRetryNotAllowedError(AgentRunStoreError):
    """A failed run cannot be retried in its current state."""


class RunInputNotAllowedError(AgentRunStoreError):
    """A run is not currently waiting for user input."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def default_agent_run_state_path() -> Path:
    configured = os.getenv("XPD_AGENT_RUN_STATE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    hermes_home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
    return hermes_home / "xpd-report-agent" / "agent-runs.json"


def validate_idempotency_key(value: str | None) -> str:
    """Validate an opaque HTTP Idempotency-Key without changing its identity."""

    if not isinstance(value, str) or not value:
        raise ValueError("Idempotency-Key is required.")
    if len(value) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise ValueError(
            f"Idempotency-Key must not exceed {IDEMPOTENCY_KEY_MAX_LENGTH} characters."
        )
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise ValueError("Idempotency-Key must contain visible ASCII characters only.")
    return value


def _validate_identity(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{name} must be a non-empty string of at most 512 characters.")
    if any(character in "\r\n\x00" for character in value):
        raise ValueError(f"Invalid {name}.")
    return value


def _json_clone(value: Any, *, name: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON serializable.") from exc
    return json.loads(encoded)


def _request_digest(request: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(request, Mapping):
        raise ValueError("request must be a mapping.")
    normalized = _json_clone(dict(request), name="request")
    if not isinstance(normalized, dict):  # defensive: dict() above should guarantee this
        raise ValueError("request must be a JSON object.")
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), normalized


def _answer_digest(answer: str) -> tuple[str, str]:
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("answer must be a non-empty string.")
    normalized = answer.strip()
    if len(normalized) > 2000:
        raise ValueError("answer must not exceed 2000 characters.")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest(), normalized


def run_retry_attempt_count(run: Mapping[str, Any]) -> int:
    """Return attempts in the current retry epoch.

    Answering a clarification starts a new logical execution leg. Its Agent
    call still increments the public total attempt count, but it must not
    consume the transport retry budget inherited from the preceding leg.
    Older records have no epoch checkpoint and retain their existing meaning.
    """

    try:
        total = max(0, int(run.get("attempt_count") or 0))
    except (TypeError, ValueError):
        total = 0
    checkpoint = run.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        return total
    try:
        epoch_start = max(0, int(checkpoint.get("retry_epoch_start_attempt") or 0))
    except (TypeError, ValueError):
        epoch_start = 0
    return max(0, total - min(total, epoch_start))


def stable_run_id(owner_scope: str, session_id: str, idempotency_key: str) -> str:
    scope = _validate_identity(owner_scope, name="owner_scope")
    session = _validate_identity(session_id, name="session_id")
    key = validate_idempotency_key(idempotency_key)
    canonical = json.dumps(
        ["xpd-agent-run-v1", scope, session, key],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{RUN_ID_PREFIX}{digest[:32]}"


class AgentRunStore:
    """Durable state for idempotent Agent API executions.

    A process lock plus a POSIX file lock serializes every state transaction.
    Atomic replacement prevents readers from observing partial JSON documents.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_agent_run_state_path()
        self._lock = threading.RLock()

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        """Serialize read-modify-write transactions across workers/processes."""

        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = self.path.with_suffix(self.path.suffix + ".lock")
            try:
                with lock_path.open("a+", encoding="utf-8") as handle:
                    try:
                        os.chmod(lock_path, 0o600)
                    except OSError:
                        pass
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                raise AgentRunStoreError("Agent run state cannot be locked.") from exc

    @contextmanager
    def execution_claim(self, identity: str) -> Iterator[bool]:
        """Try to hold one cross-process execution claim for a run or session."""

        value = _validate_identity(identity, name="execution identity")
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        lock_dir = self.path.parent / ".agent-run-execution-locks"
        try:
            lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = lock_dir / f"{digest}.lock"
            handle = lock_path.open("a+", encoding="utf-8")
        except OSError as exc:
            raise AgentRunStoreError("Agent run execution cannot be claimed.") from exc
        try:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            except OSError as exc:
                raise AgentRunStoreError(
                    "Agent run execution cannot be claimed."
                ) from exc
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @staticmethod
    def empty_state() -> dict[str, Any]:
        return {"version": 1, "runs": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.empty_state()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Fail closed: treating a corrupt store as empty could execute an
            # already accepted POST for a second time.
            raise AgentRunStoreError("Agent run state cannot be read.") from exc
        runs = payload.get("runs") if isinstance(payload, dict) else None
        version = payload.get("version") if isinstance(payload, dict) else None
        if version != 1 or not isinstance(runs, dict):
            raise AgentRunStoreError("Agent run state has an unsupported format.")
        return {"version": 1, "runs": runs}

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass

        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        encoded = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
            # Persist the directory entry as well as the file contents so a
            # power loss cannot roll the rename back to an older checkpoint.
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some filesystems do not support directory fsync. Atomic
                # replacement still prevents partially written JSON reads.
                pass
        except OSError as exc:
            raise AgentRunStoreError("Agent run state cannot be saved.") from exc

    @staticmethod
    def public_run(run: Mapping[str, Any], *, include_result: bool = True) -> dict[str, Any]:
        """Return a caller-safe record without owner scope, request, or checkpoint."""

        public = {
            key: run.get(key)
            for key in (
                "run_id",
                "request_id",
                "idempotency_key",
                "session_id",
                "status",
                "attempt_count",
                "clarification",
                "error",
                "created_at",
                "updated_at",
                "started_at",
                "completed_at",
            )
        }
        if include_result:
            public["result"] = run.get("result")
        return _json_clone(public, name="public run")

    @staticmethod
    def _owned(run: Mapping[str, Any], owner_scope: str) -> bool:
        actual = str(run.get("owner_scope") or "")
        return bool(actual) and hmac.compare_digest(actual, owner_scope)

    def create_or_get(
        self,
        *,
        owner_scope: str,
        session_id: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        request_id: str | None = None,
        checkpoint: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        scope = _validate_identity(owner_scope, name="owner_scope")
        session = _validate_identity(session_id, name="session_id")
        key = validate_idempotency_key(idempotency_key)
        caller_request_id = validate_idempotency_key(request_id or key)
        request_hash, normalized_request = _request_digest(request)
        normalized_checkpoint = _json_clone(dict(checkpoint or {}), name="checkpoint")
        run_id = stable_run_id(scope, session, key)

        with self._state_lock():
            state = self._load()
            existing = state["runs"].get(run_id)
            if isinstance(existing, dict):
                if not hmac.compare_digest(
                    str(existing.get("request_hash") or ""), request_hash
                ):
                    raise IdempotencyConflictError(
                        "Idempotency-Key was already used with a different request."
                    )
                return _json_clone(existing, name="stored run"), False

            timestamp = utc_now()
            run = {
                "run_id": run_id,
                "request_id": caller_request_id,
                "idempotency_key": key,
                "owner_scope": scope,
                "session_id": session,
                "request_hash": request_hash,
                "request": normalized_request,
                "checkpoint": normalized_checkpoint,
                "status": "pending",
                "attempt_count": 0,
                "clarification": None,
                "error": None,
                "result": None,
                "created_at": timestamp,
                "updated_at": timestamp,
                "started_at": None,
                "completed_at": None,
            }
            state["runs"][run_id] = run
            self._save(state)
            return _json_clone(run, name="stored run"), True

    def get_owned(self, run_id: str, owner_scope: str) -> dict[str, Any] | None:
        scope = _validate_identity(owner_scope, name="owner_scope")
        with self._state_lock():
            run = self._load()["runs"].get(run_id)
            if not isinstance(run, dict) or not self._owned(run, scope):
                return None
            return _json_clone(run, name="stored run")

    def get_public_owned(
        self,
        run_id: str,
        owner_scope: str,
        *,
        include_result: bool = True,
    ) -> dict[str, Any] | None:
        run = self.get_owned(run_id, owner_scope)
        return self.public_run(run, include_result=include_result) if run else None

    def _update_owned(
        self,
        run_id: str,
        owner_scope: str,
        update: Any,
    ) -> dict[str, Any] | None:
        scope = _validate_identity(owner_scope, name="owner_scope")
        with self._state_lock():
            state = self._load()
            run = state["runs"].get(run_id)
            if not isinstance(run, dict) or not self._owned(run, scope):
                return None
            update(run)
            run["updated_at"] = utc_now()
            state["runs"][run_id] = run
            self._save(state)
            return _json_clone(run, name="stored run")

    def mark_running(self, run_id: str, owner_scope: str) -> dict[str, Any] | None:
        def update(run: dict[str, Any]) -> None:
            if run.get("status") not in RESUMABLE_STATUSES:
                raise InvalidRunTransitionError("Only pending or running runs can start.")
            timestamp = utc_now()
            run["status"] = "running"
            run["attempt_count"] = int(run.get("attempt_count") or 0) + 1
            run["error"] = None
            run["completed_at"] = None
            run["started_at"] = run.get("started_at") or timestamp

        return self._update_owned(run_id, owner_scope, update)

    def claim_pending(
        self, run_id: str, owner_scope: str
    ) -> tuple[dict[str, Any] | None, bool]:
        """Atomically transition pending to running for exactly one worker."""

        scope = _validate_identity(owner_scope, name="owner_scope")
        with self._state_lock():
            state = self._load()
            run = state["runs"].get(run_id)
            if not isinstance(run, dict) or not self._owned(run, scope):
                return None, False
            if run.get("status") != "pending":
                return _json_clone(run, name="stored run"), False
            timestamp = utc_now()
            run["status"] = "running"
            run["attempt_count"] = int(run.get("attempt_count") or 0) + 1
            run["error"] = None
            run["completed_at"] = None
            run["started_at"] = run.get("started_at") or timestamp
            run["updated_at"] = timestamp
            state["runs"][run_id] = run
            self._save(state)
            return _json_clone(run, name="stored run"), True

    def mark_succeeded(
        self,
        run_id: str,
        owner_scope: str,
        *,
        result: Any = None,
    ) -> dict[str, Any] | None:
        normalized_result = _json_clone(result, name="result")

        def update(run: dict[str, Any]) -> None:
            if run.get("status") == "succeeded":
                return
            if run.get("status") not in RESUMABLE_STATUSES:
                raise InvalidRunTransitionError("This run cannot succeed from its current state.")
            run["status"] = "succeeded"
            run["result"] = normalized_result
            run["clarification"] = None
            run["error"] = None
            run["completed_at"] = utc_now()

        return self._update_owned(run_id, owner_scope, update)

    def mark_failed(
        self,
        run_id: str,
        owner_scope: str,
        *,
        error: Any,
    ) -> dict[str, Any] | None:
        normalized_error = _json_clone(error, name="error")

        def update(run: dict[str, Any]) -> None:
            if run.get("status") == "failed":
                return
            if run.get("status") not in RESUMABLE_STATUSES:
                raise InvalidRunTransitionError("This run cannot fail from its current state.")
            run["status"] = "failed"
            run["error"] = normalized_error
            run["result"] = None
            run["clarification"] = None
            run["completed_at"] = utc_now()

        return self._update_owned(run_id, owner_scope, update)

    def retry(
        self,
        run_id: str,
        owner_scope: str,
        *,
        max_attempts: int,
    ) -> dict[str, Any] | None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")

        def update(run: dict[str, Any]) -> None:
            if run.get("status") != "failed":
                raise RunRetryNotAllowedError("Only failed runs can be retried.")
            if run_retry_attempt_count(run) >= max_attempts:
                raise RunRetryNotAllowedError("The run has reached its retry limit.")
            run["status"] = "pending"
            run["error"] = None
            run["result"] = None
            run["clarification"] = None
            run["completed_at"] = None

        return self._update_owned(run_id, owner_scope, update)

    def mark_waiting_input(
        self,
        run_id: str,
        owner_scope: str,
        *,
        clarification: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        normalized = _json_clone(dict(clarification), name="clarification")
        clarification_id = str(normalized.get("clarification_id") or "").strip()
        question = str(normalized.get("question") or "").strip()
        raw_choices = normalized.get("choices")
        if not clarification_id or not question:
            raise ValueError("clarification_id and question are required.")
        if raw_choices is None:
            choices: list[str] = []
        elif isinstance(raw_choices, list):
            choices = [str(choice).strip() for choice in raw_choices if str(choice).strip()][
                :4
            ]
        else:
            raise ValueError("clarification choices must be a list.")
        normalized = {
            "clarification_id": clarification_id,
            "question": question,
            "choices": choices,
            "requested_at": str(normalized.get("requested_at") or utc_now()),
        }

        def update(run: dict[str, Any]) -> None:
            if run.get("status") == "waiting_input":
                existing = run.get("clarification")
                if isinstance(existing, dict) and hmac.compare_digest(
                    str(existing.get("clarification_id") or ""), clarification_id
                ):
                    return
                raise InvalidRunTransitionError(
                    "This run is already waiting for different input."
                )
            if run.get("status") not in RESUMABLE_STATUSES:
                raise InvalidRunTransitionError(
                    "This run cannot wait for input from its current state."
                )
            checkpoint = run.get("checkpoint")
            if not isinstance(checkpoint, dict):
                checkpoint = {}
            checkpoint["upstream_submission_started"] = False
            checkpoint.pop("pending_input", None)
            run["checkpoint"] = checkpoint
            run["status"] = "waiting_input"
            run["clarification"] = normalized
            run["error"] = None
            run["result"] = None
            run["completed_at"] = None

        return self._update_owned(run_id, owner_scope, update)

    def replay_input(
        self,
        run_id: str,
        owner_scope: str,
        *,
        idempotency_key: str,
        answer: str,
    ) -> dict[str, Any] | None:
        """Return the current run for an already accepted identical input."""

        scope = _validate_identity(owner_scope, name="owner_scope")
        key = validate_idempotency_key(idempotency_key)
        answer_hash, _ = _answer_digest(answer)
        with self._state_lock():
            run = self._load()["runs"].get(run_id)
            if not isinstance(run, dict) or not self._owned(run, scope):
                return None
            checkpoint = run.get("checkpoint")
            receipts = checkpoint.get("input_receipts") if isinstance(checkpoint, dict) else None
            receipt = receipts.get(key) if isinstance(receipts, dict) else None
            if not isinstance(receipt, dict):
                return None
            if not hmac.compare_digest(str(receipt.get("answer_hash") or ""), answer_hash):
                raise IdempotencyConflictError(
                    "Idempotency-Key was already used with a different clarification answer."
                )
            return _json_clone(run, name="stored run")

    def resume_with_input(
        self,
        run_id: str,
        owner_scope: str,
        *,
        idempotency_key: str,
        answer: str,
        baseline_message_count: int,
        artifact_ids: list[str],
    ) -> tuple[dict[str, Any] | None, bool]:
        """Atomically accept one clarification answer and requeue the same run."""

        scope = _validate_identity(owner_scope, name="owner_scope")
        key = validate_idempotency_key(idempotency_key)
        answer_hash, normalized_answer = _answer_digest(answer)
        baseline = max(0, int(baseline_message_count))
        normalized_artifact_ids = _json_clone(artifact_ids, name="artifact_ids")
        if not isinstance(normalized_artifact_ids, list):
            raise ValueError("artifact_ids must be a list.")

        with self._state_lock():
            state = self._load()
            run = state["runs"].get(run_id)
            if not isinstance(run, dict) or not self._owned(run, scope):
                return None, False
            checkpoint = run.get("checkpoint")
            if not isinstance(checkpoint, dict):
                checkpoint = {}
            receipts = checkpoint.get("input_receipts")
            if not isinstance(receipts, dict):
                receipts = {}
            receipt = receipts.get(key)
            if isinstance(receipt, dict):
                if not hmac.compare_digest(
                    str(receipt.get("answer_hash") or ""), answer_hash
                ):
                    raise IdempotencyConflictError(
                        "Idempotency-Key was already used with a different clarification answer."
                    )
                return _json_clone(run, name="stored run"), False
            if run.get("status") != "waiting_input":
                raise RunInputNotAllowedError("The run is not waiting for input.")
            clarification = run.get("clarification")
            if not isinstance(clarification, dict):
                raise InvalidRunTransitionError(
                    "The waiting run has no persisted clarification."
                )
            clarification_id = str(clarification.get("clarification_id") or "")
            question = str(clarification.get("question") or "").strip()
            if not clarification_id or not question:
                raise InvalidRunTransitionError(
                    "The waiting run has an invalid persisted clarification."
                )

            receipts[key] = {
                "clarification_id": clarification_id,
                "answer_hash": answer_hash,
                "accepted_at": utc_now(),
            }
            checkpoint.update(
                {
                    "baseline_message_count": baseline,
                    "artifact_ids": normalized_artifact_ids,
                    "upstream_submission_started": False,
                    "retry_epoch_start_attempt": int(run.get("attempt_count") or 0),
                    "input_receipts": receipts,
                    "pending_input": {
                        "clarification_id": clarification_id,
                        "question": question,
                        "answer": normalized_answer,
                    },
                }
            )
            run["checkpoint"] = checkpoint
            run["status"] = "pending"
            run["clarification"] = None
            run["error"] = None
            run["result"] = None
            run["completed_at"] = None
            run["updated_at"] = utc_now()
            state["runs"][run_id] = run
            self._save(state)
            return _json_clone(run, name="stored run"), True

    def set_checkpoint(
        self,
        run_id: str,
        owner_scope: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        normalized_fields = _json_clone(fields, name="checkpoint")

        def update(run: dict[str, Any]) -> None:
            checkpoint = run.get("checkpoint")
            if not isinstance(checkpoint, dict):
                checkpoint = {}
            checkpoint.update(normalized_fields)
            run["checkpoint"] = checkpoint

        return self._update_owned(run_id, owner_scope, update)

    update_checkpoint = set_checkpoint

    def list_resumable(
        self,
        *,
        max_attempts: int | None = None,
        owner_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        if max_attempts is not None and max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        scope = (
            _validate_identity(owner_scope, name="owner_scope")
            if owner_scope is not None
            else None
        )
        with self._state_lock():
            runs = [
                _json_clone(run, name="stored run")
                for run in self._load()["runs"].values()
                if isinstance(run, dict)
                and run.get("status") in RESUMABLE_STATUSES
                and (
                    run.get("status") == "running"
                    or max_attempts is None
                    or run_retry_attempt_count(run) < max_attempts
                )
                and (scope is None or self._owned(run, scope))
            ]
        runs.sort(key=lambda run: (str(run.get("created_at") or ""), str(run["run_id"])))
        return runs


agent_run_store = AgentRunStore()
