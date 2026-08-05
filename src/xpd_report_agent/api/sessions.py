from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from collections.abc import AsyncIterable, AsyncIterator
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from xpd_report_agent.api.agent_capacity import (
    agent_capacity_health,
    agent_capacity_slot,
)
from xpd_report_agent.api.agent_runs import (
    AgentRunStoreError,
    IdempotencyConflictError,
    InvalidRunTransitionError,
    RunInputNotAllowedError,
    RunRetryNotAllowedError,
    agent_run_store,
    run_retry_attempt_count,
    validate_idempotency_key,
)
from xpd_report_agent.api.artifact_store import (
    delete_session_artifacts,
    list_session_artifacts,
    resolve_session_artifact,
)
from xpd_report_agent.api.db_skill import db_skill_prompt, export_action_prompt
from xpd_report_agent.api.error_contract import (
    REQUEST_ID_HEADER,
    api_error,
    documented_error_responses,
)
from xpd_report_agent.api.prompts import (
    CHINESE_REASONING_REMINDER,
    FINAL_REFLECTION_SYSTEM_PROMPT,
    REPORT_SYSTEM_PROMPT,
)
from xpd_report_agent.api.reflections import ReflectionQueue
from xpd_report_agent.api.schedule_store import schedule_store
from xpd_report_agent.api.service_auth import require_service_auth
from xpd_report_agent.api.session_service import (
    CLIENT_SESSION_KEY_HEADER,
    CLIENT_USER_ID_HEADER,
    client_safe_messages,
    count_completed_turns,
    new_reflection_session_id,
    new_session_id,
    normalize_session,
    redact_sensitive_text,
    require_owned_session,
    resolve_owner_scope,
    scope_from_session_id,
    session_belongs_to_scope,
    validate_user_id,
)
from xpd_report_agent.api.structured_analysis import (
    RUN_CLARIFICATION_INSTRUCTION,
    STRUCTURED_ANALYSIS_INSTRUCTION,
    StructuredAnalysis,
    parse_run_clarification,
    parse_structured_analysis,
)
from xpd_report_agent.hermes_plugin.db_query.report_oss import (
    report_oss_config,
    write_report_oss_context,
)
from xpd_report_agent.memory_paths import (
    IDENTITY_MODE_USER_ID,
    configured_identity_mode,
    local_memory_dir,
    merchant_memory_path,
    user_memory_dir,
)
from xpd_report_agent.runtime.hermes_config import required_memory_tools_from_env

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)

_active_chat_lock = threading.Lock()
_active_chat_sessions: set[str] = set()
_agent_run_tasks: dict[str, asyncio.Task[None]] = {}

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


class SessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    close_session_id: str | None = None


class SessionUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class SessionChatRequest(BaseModel):
    message: str = Field(min_length=1)
    stream: bool = True


class AgentRunCreateRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)


class MiddlePlatformRunRequest(AgentRunCreateRequest):
    session_id: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=120)


class AgentRunArtifactResponse(BaseModel):
    artifact_id: str
    session_id: str
    filename: str
    format: str
    media_type: str | None = None
    size_bytes: int
    created_at: float
    download_url: str
    download_url_expires_at: str | None = None
    storage: Literal["local", "oss"] | None = None
    oss_uri: str | None = None
    object_key: str | None = None


class AgentRunResultResponse(BaseModel):
    session_id: str
    content: str
    analysis: StructuredAnalysis
    reasoning: str
    usage: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[AgentRunArtifactResponse] = Field(default_factory=list)
    recovered: bool

    @model_validator(mode="before")
    @classmethod
    def fill_legacy_analysis(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("analysis") is None:
            content = str(value.get("content") or "")
            return {
                **value,
                "analysis": StructuredAnalysis(
                    structured=False,
                    conclusion=content[:20_000],
                ).model_dump(mode="json"),
            }
        return value


class AgentRunErrorResponse(BaseModel):
    code: str
    message: str
    retryable: bool
    outcome_unknown: bool
    request_id: str
    upstream_status: int | None = None
    attempts: int | None = None
    retry_exhausted: bool | None = None


class AgentRunClarificationResponse(BaseModel):
    clarification_id: str
    question: str
    choices: list[str] = Field(default_factory=list)
    requested_at: str


class AgentRunResponse(BaseModel):
    run_id: str
    request_id: str
    idempotency_key: str
    session_id: str
    status: Literal["pending", "running", "waiting_input", "succeeded", "failed"]
    attempt_count: int
    clarification: AgentRunClarificationResponse | None = None
    error: AgentRunErrorResponse | None
    result: AgentRunResultResponse | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


class AgentRunSubmissionResponse(BaseModel):
    ok: Literal[True]
    run: AgentRunResponse
    status_url: str


class AgentRunStatusResponse(BaseModel):
    ok: Literal[True]
    run: AgentRunResponse


class SessionCloseRequest(BaseModel):
    reason: Literal["user_close", "new_session", "idle_timeout", "delete"] = "user_close"


class ClarificationAnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


def _normalized_session(
    session: dict[str, Any], *, completed_turn_count: int | None = None
) -> dict[str, Any]:
    normalized = normalize_session(
        session,
        completed_turn_count=completed_turn_count,
    )
    metadata = schedule_store().metadata_for_session(normalized["session_id"])
    if metadata:
        normalized.update(metadata)
    return normalized


def _scheduled_session_metadata(session_id: str) -> dict[str, Any] | None:
    metadata = schedule_store().metadata_for_session(session_id)
    if metadata:
        return metadata
    # Scheduled sessions must remain read-only even if the schedule index is
    # temporarily unavailable or has been repaired independently of Hermes.
    if "_scheduled_" in session_id:
        return {"origin": "scheduled", "read_only": True}
    return None


def _env_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def hermes_origin() -> str:
    host = os.getenv("HERMES_GATEWAY_HOST", "127.0.0.1")
    port = os.getenv("HERMES_GATEWAY_PORT", "8642")
    return f"http://{host}:{port}"


def hermes_api_key() -> str:
    key = os.getenv("HERMES_GATEWAY_API_KEY", "")
    if not key:
        raise HTTPException(status_code=500, detail="HERMES_GATEWAY_API_KEY is not configured.")
    return key


def memory_capacity_notice(scope: str | None = None) -> str:
    user_scoped = configured_identity_mode() == IDENTITY_MODE_USER_ID
    memory_dir = user_memory_dir(scope or "") if user_scoped else local_memory_dir()
    try:
        ratio = float(os.getenv("XPD_MEMORY_CONSOLIDATION_RATIO", "0.8"))
    except ValueError:
        ratio = 0.8
    ratio = min(0.95, max(0.5, ratio))
    stores = (
        ("MEMORY.md", max(256, int(os.getenv("XPD_MEMORY_CHAR_LIMIT", "2200")))),
        ("USER.md", max(256, int(os.getenv("XPD_USER_CHAR_LIMIT", "1375")))),
    )
    states = []
    requires_consolidation = False
    for filename, limit in stores:
        path = memory_dir / filename
        try:
            used = len(path.read_text(encoding="utf-8")) if path.exists() else 0
        except OSError:
            used = 0
        at_watermark = used >= int(limit * ratio)
        requires_consolidation = requires_consolidation or at_watermark
        states.append(f"{filename}={used}/{limit}")
    instruction = (
        "已达到整理水位：本轮 memory 写入前必须先整理，若仍无空间则跳过写入。"
        if requires_consolidation
        else "尚未达到整理水位：仍须去重，只保存高价值稳定信息。"
    )
    notice = f"记忆容量状态（整理水位 {ratio:.0%}）：{', '.join(states)}。{instruction}"
    if user_scoped and os.getenv("XPD_MERCHANT_MEMORY_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        shared_path = merchant_memory_path()
        try:
            shared_used = (
                len(shared_path.read_text(encoding="utf-8")) if shared_path.exists() else 0
            )
        except OSError:
            shared_used = 0
        notice += (
            f" 商家公共经营记忆为只读共享层：merchant/MEMORY.md={shared_used} 字符，"
            "不得通过个人反思修改。"
        )
    return notice


def report_system_prompt(
    scope: str | None = None,
    *,
    structured_result: bool = False,
    user_message: str | None = None,
) -> str:
    notices = [memory_capacity_notice(scope)]
    if "session_search" not in required_memory_tools_from_env():
        notices.append(
            "当前为多用户安全模式：不要调用 session_search；历史会话仅由服务端按用户范围展示。"
        )
    if structured_result:
        notices.append(STRUCTURED_ANALYSIS_INSTRUCTION)
    if skill_prompt := db_skill_prompt(user_message):
        notices.append(skill_prompt)
    if export_prompt := export_action_prompt(user_message):
        notices.append(export_prompt)
    if structured_result:
        # This transport-specific instruction is deliberately last: durable
        # non-streaming Runs cannot use Hermes' blocking in-memory clarify tool.
        notices.append(RUN_CLARIFICATION_INSTRUCTION)
    # Keep the language contract adjacent to the model's generation point. The
    # database Skill can be long, so relying only on the reminder near the top
    # of the base prompt makes provider reasoning more likely to drift to English.
    notices.append(CHINESE_REASONING_REMINDER)
    return f"{REPORT_SYSTEM_PROMPT}\n\n" + "\n".join(notices)


def _client_scope(
    raw_key: Annotated[str | None, Header(alias=CLIENT_SESSION_KEY_HEADER)] = None,
    raw_user_id: Annotated[str | None, Header(alias=CLIENT_USER_ID_HEADER)] = None,
) -> str:
    return resolve_owner_scope(session_key=raw_key, user_id=raw_user_id)


SessionScope = Annotated[str, Depends(_client_scope)]


def _report_uid(scope: str, raw_user_id: str | None) -> str:
    if configured_identity_mode() == IDENTITY_MODE_USER_ID:
        return validate_user_id(raw_user_id)
    return scope


def _prepare_report_oss_context(
    session_id: str,
    *,
    scope: str,
    raw_user_id: str | None,
    trace_id: str,
) -> None:
    if not report_oss_config().enabled:
        return
    write_report_oss_context(
        session_id,
        uid=_report_uid(scope, raw_user_id),
        trace_id=trace_id,
    )


def _middle_platform_scope(
    raw_user_id: Annotated[str, Header(alias=CLIENT_USER_ID_HEADER)],
    _service_authenticated: Annotated[None, Depends(require_service_auth)],
) -> str:
    if configured_identity_mode() != IDENTITY_MODE_USER_ID:
        raise api_error(
            503,
            code="MIDDLE_PLATFORM_IDENTITY_DISABLED",
            message="The middle-platform API requires XPD_IDENTITY_MODE=user_id.",
            retryable=False,
        )
    return resolve_owner_scope(user_id=raw_user_id)


MiddlePlatformScope = Annotated[str, Depends(_middle_platform_scope)]


def _hermes_headers(scope: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {hermes_api_key()}",
        "Content-Type": "application/json",
    }
    if scope:
        # Never forward the browser's raw local key. Hermes receives a stable,
        # non-reversible scope identifier for long-term memory isolation.
        headers["X-Hermes-Session-Key"] = scope
    return headers


def _raise_upstream(
    response: httpx.Response,
    action: str,
    *,
    outcome_unknown: bool = False,
) -> None:
    if response.is_success:
        return
    try:
        body: Any = response.json()
    except Exception:
        body = response.text[:1000]

    upstream_status = response.status_code
    if upstream_status in {400, 404, 409, 422}:
        status_code = upstream_status
        code = {
            400: "HERMES_BAD_REQUEST",
            404: "HERMES_NOT_FOUND",
            409: "HERMES_CONFLICT",
            422: "HERMES_VALIDATION_ERROR",
        }[upstream_status]
        retryable = False
    elif upstream_status in {401, 403}:
        status_code = 502
        code = "HERMES_AUTH_ERROR"
        retryable = False
    elif upstream_status == 429:
        status_code = 503
        code = "HERMES_RATE_LIMITED"
        retryable = True
    elif upstream_status == 503:
        status_code = 503
        code = "HERMES_UNAVAILABLE"
        retryable = True
    elif upstream_status == 504:
        status_code = 504
        code = "HERMES_TIMEOUT"
        retryable = True
    else:
        status_code = 502
        code = "HERMES_UPSTREAM_ERROR"
        retryable = upstream_status >= 500

    raise api_error(
        status_code,
        code=code,
        message=f"Hermes failed to {action}.",
        retryable=retryable,
        outcome_unknown=outcome_unknown and retryable,
        upstream_status=upstream_status,
        body=body,
    )


def _hermes_connect_attempts() -> int:
    try:
        return max(1, min(5, int(os.getenv("XPD_HERMES_CONNECT_MAX_ATTEMPTS", "3"))))
    except ValueError:
        return 3


def _hermes_retry_delay(attempt: int) -> float:
    try:
        base = float(os.getenv("XPD_HERMES_RETRY_BASE_SECONDS", "0.2"))
    except ValueError:
        base = 0.2
    base = max(0.0, min(base, 5.0))
    delay = min(base * (2 ** max(0, attempt - 1)), 5.0)
    return delay * random.uniform(0.8, 1.2)


def _safe_http_retry(method: str) -> bool:
    # Only retrievals are replayed without a higher-level idempotency record.
    # Mutation endpoints use read-after-error reconciliation instead.
    return method.upper() in {"GET", "HEAD", "OPTIONS"}


async def _hermes_json(
    method: str,
    path: str,
    *,
    scope: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float | None = 15.0,
    action: str,
) -> dict[str, Any]:
    attempts = _hermes_connect_attempts()
    safe_retry = _safe_http_retry(method)
    response: httpx.Response | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.request(
                    method,
                    f"{hermes_origin()}{path}",
                    headers=_hermes_headers(scope),
                    json=payload,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # No upstream connection was established, so even a POST is safe
            # to retry. Once connected, non-idempotent requests are never
            # blindly replayed by this wrapper.
            if attempt < attempts:
                await asyncio.sleep(_hermes_retry_delay(attempt))
                continue
            raise api_error(
                503,
                code="HERMES_UNAVAILABLE",
                message=f"Hermes is unavailable while trying to {action}.",
                retryable=True,
                outcome_unknown=False,
                attempts=attempt,
            ) from exc
        except httpx.TimeoutException as exc:
            if safe_retry and attempt < attempts:
                await asyncio.sleep(_hermes_retry_delay(attempt))
                continue
            raise api_error(
                504,
                code="HERMES_TIMEOUT",
                message=f"Hermes timed out while trying to {action}.",
                retryable=True,
                outcome_unknown=not safe_retry,
                attempts=attempt,
            ) from exc
        except httpx.HTTPError as exc:
            if safe_retry and attempt < attempts:
                await asyncio.sleep(_hermes_retry_delay(attempt))
                continue
            raise api_error(
                502,
                code="HERMES_CONNECTION_ERROR",
                message=f"Hermes connection failed while trying to {action}.",
                retryable=True,
                outcome_unknown=not safe_retry,
                attempts=attempt,
            ) from exc

        if response.status_code in {429, 502, 503, 504} and safe_retry and attempt < attempts:
            await asyncio.sleep(_hermes_retry_delay(attempt))
            continue
        break

    if response is None:  # pragma: no cover - loop always returns or raises
        raise RuntimeError("Hermes request produced no response.")
    _raise_upstream(response, action, outcome_unknown=not safe_retry)
    if not response.content:
        return {}
    return response.json()


async def _get_session(session_id: str, scope: str) -> dict[str, Any]:
    require_owned_session(session_id, scope)
    payload = await _hermes_json(
        "GET", f"/api/sessions/{session_id}", action="read session"
    )
    return payload.get("session") or {}


async def _session_messages_payload(session_id: str) -> dict[str, Any]:
    return await _hermes_json(
        "GET", f"/api/sessions/{session_id}/messages", action="read session messages"
    )


async def _completed_turn_count(session_id: str) -> int:
    payload = await _session_messages_payload(session_id)
    messages = [message for message in payload.get("data") or [] if isinstance(message, dict)]
    return count_completed_turns(messages)


async def _set_initial_title(session_id: str, scope: str, message: str) -> None:
    try:
        session = await _get_session(session_id, scope)
        if session.get("title"):
            return
        title = " ".join(message.strip().split())[:42]
        if not title:
            return
        response = await _hermes_json(
            "PATCH",
            f"/api/sessions/{session_id}",
            payload={"title": title},
            action="set session title",
        )
        if not response.get("session"):
            return
    except HTTPException:
        # A title collision or metadata error must never block the chat turn.
        return


def _session_turn_path(session_id: str, *, stream: bool = False) -> str:
    suffix = "/chat/stream" if stream else "/chat"
    return f"/api/sessions/{session_id}{suffix}"


def _session_turn_payload(
    *,
    scope: str,
    message: str,
    prompt_message: str | None = None,
    structured_result: bool = False,
) -> dict[str, str]:
    """Build the one Hermes turn contract shared by Session and Run transports."""

    return {
        "message": message,
        "system_message": report_system_prompt(
            scope,
            structured_result=structured_result,
            user_message=prompt_message if prompt_message is not None else message,
        ),
    }


async def _prepare_session_turn(
    session_id: str,
    *,
    scope: str,
    title_message: str,
    raw_user_id: str | None,
    trace_id: str,
) -> None:
    """Apply transport-independent metadata before submitting a Hermes turn."""

    await _set_initial_title(session_id, scope, title_message)
    _prepare_report_oss_context(
        session_id,
        scope=scope,
        raw_user_id=raw_user_id,
        trace_id=trace_id,
    )


async def _submit_session_turn(
    session_id: str,
    *,
    scope: str,
    message: str,
    prompt_message: str | None = None,
    structured_result: bool = False,
    timeout: float | None,
    action: str,
) -> dict[str, Any]:
    """Submit a non-streaming turn through the shared Session/Run core."""

    return await _hermes_json(
        "POST",
        _session_turn_path(session_id),
        scope=scope,
        timeout=timeout,
        payload=_session_turn_payload(
            scope=scope,
            message=message,
            prompt_message=prompt_message,
            structured_result=structured_result,
        ),
        action=action,
    )


def _extract_chat_content(payload: dict[str, Any]) -> str:
    message = payload.get("message") or {}
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _reasoning_from_messages(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        reasoning = message.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            parts.append(reasoning)
    return "\n\n".join(parts)


def _artifact_ids(session_id: str) -> set[str]:
    return {
        str(item["artifact_id"])
        for item in list_session_artifacts(session_id)
        if item.get("artifact_id")
    }


def _artifact_ready_event(artifact: dict[str, Any]) -> str:
    return (
        "event: artifact.ready\ndata: "
        + json.dumps(artifact, ensure_ascii=False)
        + "\n\n"
    )


_SSE_EVENT_BOUNDARY = re.compile(r"\r\n\r\n|\n\n|\r\r")


def _split_complete_sse_events(buffer: str) -> tuple[list[str], str]:
    events: list[str] = []
    while match := _SSE_EVENT_BOUNDARY.search(buffer):
        events.append(buffer[: match.end()])
        buffer = buffer[match.end() :]
    return events, buffer


def _parse_sse_event(frame: str) -> tuple[str, str]:
    event_name = "message"
    data_lines: list[str] = []
    for line in frame.splitlines():
        if not line or line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value or "message"
        elif field == "data":
            data_lines.append(value)
    return event_name, "\n".join(data_lines)


def _is_export_tool_completed(frame: str) -> bool:
    event_name, raw_data = _parse_sse_event(frame)
    if event_name not in {"tool.completed", "hermes.tool.completed"} or not raw_data:
        return False
    try:
        data = json.loads(raw_data)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("tool_name") == "export_report_file"


def _new_artifact_ready_events(
    session_id: str,
    *,
    existing_artifact_ids: set[str],
    emitted_artifact_ids: set[str],
) -> list[str]:
    try:
        artifacts = list_session_artifacts(session_id)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to list report artifacts for %s: %s", session_id, exc)
        return []
    events: list[str] = []
    for artifact in artifacts:
        artifact_id = str(artifact.get("artifact_id") or "")
        if (
            not artifact_id
            or artifact_id in existing_artifact_ids
            or artifact_id in emitted_artifact_ids
        ):
            continue
        emitted_artifact_ids.add(artifact_id)
        events.append(_artifact_ready_event(artifact))
    return events


async def _relay_hermes_sse_with_artifacts(
    chunks: AsyncIterable[str],
    *,
    session_id: str,
    existing_artifact_ids: set[str],
    emitted_artifact_ids: set[str],
) -> AsyncIterator[str]:
    """Forward only complete upstream frames before inserting local events."""

    buffer = ""
    async for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk
        frames, buffer = _split_complete_sse_events(buffer)
        for frame in frames:
            yield frame
            if _is_export_tool_completed(frame):
                for artifact_event in _new_artifact_ready_events(
                    session_id,
                    existing_artifact_ids=existing_artifact_ids,
                    emitted_artifact_ids=emitted_artifact_ids,
                ):
                    yield artifact_event
    if buffer:
        yield buffer
        if _is_export_tool_completed(buffer):
            for artifact_event in _new_artifact_ready_events(
                session_id,
                existing_artifact_ids=existing_artifact_ids,
                emitted_artifact_ids=emitted_artifact_ids,
            ):
                yield artifact_event
    # Upstream versions do not all emit the same tool lifecycle events. A
    # final scan keeps export discovery compatible without delaying the
    # normal export_report_file path above.
    for artifact_event in _new_artifact_ready_events(
        session_id,
        existing_artifact_ids=existing_artifact_ids,
        emitted_artifact_ids=emitted_artifact_ids,
    ):
        yield artifact_event


def _claim_chat_session(session_id: str) -> bool:
    with _active_chat_lock:
        if session_id in _active_chat_sessions:
            return False
        _active_chat_sessions.add(session_id)
        return True


def _release_chat_session(session_id: str) -> None:
    with _active_chat_lock:
        _active_chat_sessions.discard(session_id)


def _agent_run_max_attempts() -> int:
    try:
        return max(1, min(3, int(os.getenv("XPD_AGENT_RUN_MAX_ATTEMPTS", "2"))))
    except ValueError:
        return 2


def _agent_chat_timeout_seconds() -> float:
    try:
        return max(30.0, min(1800.0, float(os.getenv("XPD_AGENT_CHAT_TIMEOUT_SECONDS", "600"))))
    except ValueError:
        return 600.0


def _agent_reconcile_seconds() -> float:
    try:
        return max(0.0, min(60.0, float(os.getenv("XPD_AGENT_RECONCILE_SECONDS", "10"))))
    except ValueError:
        return 10.0


def _agent_outcome_reconcile_seconds() -> float:
    """Bound how long a restarted submitted run is reconciled without replay."""

    try:
        configured = float(
            os.getenv(
                "XPD_AGENT_OUTCOME_RECONCILE_SECONDS",
                str(_agent_chat_timeout_seconds()),
            )
        )
    except ValueError:
        configured = _agent_chat_timeout_seconds()
    return max(0.0, min(1800.0, configured))


def _final_reflection_timeout_seconds() -> float:
    try:
        configured = float(
            os.getenv("XPD_FINAL_REFLECTION_TIMEOUT_SECONDS", "180")
        )
    except (TypeError, ValueError):
        configured = 180.0
    return max(30.0, min(600.0, configured))


def _run_error(exc: Exception, request_id: str) -> dict[str, Any]:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            error = {
                "code": str(detail.get("code") or "AGENT_RUN_FAILED"),
                "message": str(detail.get("message") or "Agent run failed."),
                "retryable": bool(detail.get("retryable", False)),
                "outcome_unknown": bool(detail.get("outcome_unknown", False)),
                "request_id": request_id,
            }
            for key in ("upstream_status", "attempts"):
                if key in detail:
                    error[key] = detail[key]
            return error
        return {
            "code": "AGENT_RUN_FAILED",
            "message": str(detail),
            "retryable": exc.status_code in {429, 502, 503, 504},
            "outcome_unknown": False,
            "request_id": request_id,
        }
    if isinstance(exc, AgentRunStoreError):
        return {
            "code": "AGENT_RUN_STATE_ERROR",
            "message": "Agent run state could not be persisted.",
            "retryable": False,
            "outcome_unknown": True,
            "request_id": request_id,
        }
    return {
        "code": "AGENT_RUN_INTERNAL_ERROR",
        "message": "Agent run failed unexpectedly.",
        "retryable": False,
        "outcome_unknown": True,
        "request_id": request_id,
    }


def _new_run_artifacts(record: dict[str, Any]) -> list[dict[str, Any]]:
    existing = {
        str(artifact_id)
        for artifact_id in (record.get("checkpoint") or {}).get("artifact_ids", [])
    }
    try:
        artifacts = list_session_artifacts(str(record["session_id"]))
    except (OSError, ValueError):
        return []
    return [
        artifact
        for artifact in artifacts
        if str(artifact.get("artifact_id") or "") not in existing
    ]


def _refresh_run_artifacts(run: dict[str, Any]) -> dict[str, Any]:
    """Refresh expiring OSS URLs without changing the durable run result."""

    result = run.get("result")
    if not isinstance(result, dict):
        return run
    session_id = str(result.get("session_id") or run.get("session_id") or "")
    if not session_id:
        return run
    try:
        current = {
            str(artifact.get("artifact_id") or ""): artifact
            for artifact in list_session_artifacts(session_id)
        }
    except (OSError, ValueError):
        return run
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return run
    result["artifacts"] = [
        current.get(str(artifact.get("artifact_id") or ""), artifact)
        if isinstance(artifact, dict)
        else artifact
        for artifact in artifacts
    ]
    return run


def _run_outcome_from_content(
    record: dict[str, Any],
    content: str,
    *,
    reasoning: str,
    usage: dict[str, Any],
    artifacts: list[dict[str, Any]],
    recovered: bool,
) -> dict[str, Any]:
    clarification = parse_run_clarification(content)
    if clarification is not None:
        canonical = json.dumps(
            [
                str(record["run_id"]),
                int(record.get("attempt_count") or 0),
                clarification.question,
                clarification.choices,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        clarification_id = f"clarify_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"
        return {
            "status": "waiting_input",
            "clarification": {
                "clarification_id": clarification_id,
                "question": clarification.question,
                "choices": clarification.choices,
            },
        }

    clean_content, analysis = parse_structured_analysis(content)
    return {
        "status": "succeeded",
        "result": {
            "session_id": str(record["session_id"]),
            "content": clean_content,
            "analysis": analysis.model_dump(mode="json"),
            "reasoning": reasoning,
            "usage": usage,
            "artifacts": artifacts,
            "recovered": recovered,
        },
    }


def _persist_agent_run_outcome(
    run_id: str,
    scope: str,
    outcome: dict[str, Any],
) -> None:
    if outcome.get("status") == "waiting_input":
        agent_run_store.mark_waiting_input(
            run_id,
            scope,
            clarification=outcome["clarification"],
        )
        return
    agent_run_store.mark_succeeded(
        run_id,
        scope,
        result=outcome["result"],
    )


async def _inspect_agent_run(
    record: dict[str, Any],
    *,
    wait_seconds: float = 0.0,
    tolerate_transient_errors: bool = False,
) -> tuple[dict[str, Any] | None, bool]:
    """Recover a completed turn from Hermes without replaying its POST."""

    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        try:
            payload = await _session_messages_payload(str(record["session_id"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            if not tolerate_transient_errors or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
            continue
        raw_messages = [
            message for message in payload.get("data") or [] if isinstance(message, dict)
        ]
        checkpoint = record.get("checkpoint") or {}
        try:
            baseline = max(0, int(checkpoint.get("baseline_message_count") or 0))
        except (TypeError, ValueError):
            baseline = 0
        raw_turn_messages = raw_messages[baseline:]
        turn_messages = client_safe_messages({"data": raw_turn_messages})
        final_messages = [
            message
            for message in raw_turn_messages
            if message.get("role") == "assistant"
            and not message.get("tool_calls")
            and isinstance(message.get("content"), str)
            and bool(str(message.get("content") or "").strip())
        ]
        if final_messages:
            return (
                _run_outcome_from_content(
                    record,
                    str(final_messages[-1]["content"]),
                    reasoning=_reasoning_from_messages(turn_messages),
                    usage={},
                    artifacts=_new_run_artifacts(record),
                    recovered=True,
                ),
                True,
            )

        progressed = bool(raw_turn_messages) or bool(_new_run_artifacts(record))
        if time.monotonic() >= deadline:
            return None, progressed
        await asyncio.sleep(min(1.0, max(0.05, deadline - time.monotonic())))


async def _result_from_hermes_payload(
    record: dict[str, Any], raw: dict[str, Any]
) -> dict[str, Any]:
    try:
        recovered, _ = await _inspect_agent_run(record)
    except HTTPException:
        recovered = None
    if recovered:
        if recovered.get("status") == "succeeded":
            recovered["result"]["usage"] = raw.get("usage") or {}
            recovered["result"]["recovered"] = False
        return recovered
    outcome = _run_outcome_from_content(
        record,
        _extract_chat_content(raw),
        reasoning="",
        usage=raw.get("usage") or {},
        artifacts=_new_run_artifacts(record),
        recovered=False,
    )
    if outcome.get("status") == "succeeded":
        outcome["result"]["session_id"] = str(
            raw.get("session_id") or record["session_id"]
        )
    return outcome


def _agent_run_submission_message(record: dict[str, Any]) -> str:
    checkpoint = record.get("checkpoint")
    pending_input = checkpoint.get("pending_input") if isinstance(checkpoint, dict) else None
    if not isinstance(pending_input, dict):
        return str(record["request"]["message"])
    question = str(pending_input.get("question") or "").strip()
    answer = str(pending_input.get("answer") or "").strip()
    if not question or not answer:
        return str(record["request"]["message"])
    return (
        "这是对上一轮持久化澄清请求的用户回答。\n"
        f"澄清问题：{question}\n"
        f"用户回答：{answer}\n"
        "请使用该回答继续原始分析，不要重复询问已回答的同一问题。"
    )


async def _fail_or_retry_agent_run(
    *,
    run_id: str,
    scope: str,
    record: dict[str, Any],
    error: dict[str, Any],
) -> bool:
    """Persist a failure and return True only when a safe retry was scheduled."""

    attempts = run_retry_attempt_count(record)
    can_retry = bool(error.get("retryable")) and not bool(error.get("outcome_unknown"))
    if can_retry and attempts >= _agent_run_max_attempts():
        error = {
            **error,
            "retryable": False,
            "retry_exhausted": True,
        }
        can_retry = False

    agent_run_store.mark_failed(run_id, scope, error=error)
    if not can_retry:
        return False
    # Clear the submission checkpoint while the record is still failed. If the
    # process exits between these two durable writes, the next identical request
    # can safely repeat the retry transition instead of treating a pending run
    # as an unknown upstream submission.
    agent_run_store.update_checkpoint(
        run_id,
        scope,
        upstream_submission_started=False,
    )
    try:
        agent_run_store.retry(
            run_id,
            scope,
            max_attempts=_agent_run_max_attempts(),
        )
    except RunRetryNotAllowedError:
        return False
    await asyncio.sleep(min(2 ** max(0, attempts - 1), 5))
    return True


async def _execute_agent_run_exclusive(run_id: str, scope: str) -> None:
    record = agent_run_store.get_owned(run_id, scope)
    if not record:
        return
    session_id = str(record["session_id"])
    if not _claim_chat_session(session_id):
        try:
            if record.get("status") in {"pending", "running"}:
                agent_run_store.mark_failed(
                    run_id,
                    scope,
                    error={
                        "code": "SESSION_BUSY",
                        "message": "Another analysis is already running for this session.",
                        "retryable": True,
                        "outcome_unknown": False,
                        "request_id": record["request_id"],
                    },
                )
        except AgentRunStoreError:
            pass
        return

    try:
        while True:
            record = agent_run_store.get_owned(run_id, scope)
            if not record or record.get("status") not in {"pending", "running"}:
                return

            # A process restart can happen after Hermes accepted the POST but
            # before this service persisted the result. Reconcile first; never
            # replay a turn when Hermes already shows partial progress.
            checkpoint = record.get("checkpoint") or {}
            submission_started = bool(checkpoint.get("upstream_submission_started"))
            if run_retry_attempt_count(record) > 0 or submission_started:
                try:
                    recovered, progressed = await _inspect_agent_run(
                        record,
                        wait_seconds=(
                            _agent_outcome_reconcile_seconds()
                            if submission_started
                            else _agent_reconcile_seconds()
                        ),
                        tolerate_transient_errors=submission_started,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = _run_error(exc, str(record["request_id"]))
                    if submission_started:
                        error.update(
                            {
                                "retryable": False,
                                "outcome_unknown": True,
                            }
                        )
                    if await _fail_or_retry_agent_run(
                        run_id=run_id,
                        scope=scope,
                        record=record,
                        error=error,
                    ):
                        continue
                    return
                if recovered:
                    _persist_agent_run_outcome(run_id, scope, recovered)
                    return
                if submission_started:
                    agent_run_store.mark_failed(
                        run_id,
                        scope,
                        error={
                            "code": "AGENT_RUN_OUTCOME_UNKNOWN",
                            "message": (
                                "The prior Hermes submission did not finish within the "
                                "bounded reconciliation window and was not replayed."
                            ),
                            "retryable": False,
                            "outcome_unknown": True,
                            "request_id": record["request_id"],
                        },
                    )
                    return
                if progressed:
                    agent_run_store.mark_failed(
                        run_id,
                        scope,
                        error={
                            "code": "AGENT_RUN_OUTCOME_UNKNOWN",
                            "message": (
                                "Hermes recorded partial progress, so the request was not replayed."
                            ),
                            "retryable": False,
                            "outcome_unknown": True,
                            "request_id": record["request_id"],
                        },
                    )
                    return

                if run_retry_attempt_count(record) >= _agent_run_max_attempts():
                    agent_run_store.mark_failed(
                        run_id,
                        scope,
                        error={
                            "code": "AGENT_RUN_RETRY_EXHAUSTED",
                            "message": "The Agent run reached its retry limit.",
                            "retryable": False,
                            "retry_exhausted": True,
                            "outcome_unknown": False,
                            "request_id": record["request_id"],
                        },
                    )
                    return
                if record.get("status") == "running":
                    if await _fail_or_retry_agent_run(
                        run_id=run_id,
                        scope=scope,
                        record=record,
                        error={
                            "code": "AGENT_RUN_INTERRUPTED_BEFORE_SUBMISSION",
                            "message": (
                                "The prior worker stopped before submitting to Hermes; "
                                "the run was safely requeued."
                            ),
                            "retryable": True,
                            "outcome_unknown": False,
                            "request_id": record["request_id"],
                        },
                    ):
                        continue
                    return

            try:
                # Keep the durable run pending while it waits for one of the
                # two global analysis slots. The submission checkpoint is set
                # only after capacity is acquired, so a restart cannot mistake
                # a queued request for an unknown Hermes outcome.
                async with agent_capacity_slot():
                    record, claimed = agent_run_store.claim_pending(run_id, scope)
                    if not record or not claimed:
                        return
                    session = await _get_session(session_id, scope)
                    if session.get("ended_at"):
                        raise api_error(
                            409,
                            code="SESSION_CLOSED",
                            message="Closed sessions are read-only.",
                        )
                    await _prepare_session_turn(
                        session_id,
                        scope=scope,
                        title_message=str(record["request"]["message"]),
                        raw_user_id=str(
                            (record.get("checkpoint") or {}).get("report_uid")
                            or scope
                        ),
                        trace_id=str(record["request_id"]),
                    )
                    record = agent_run_store.update_checkpoint(
                        run_id,
                        scope,
                        upstream_submission_started=True,
                    ) or record
                    submission_message = _agent_run_submission_message(record)
                    raw = await _submit_session_turn(
                        session_id,
                        scope=scope,
                        message=submission_message,
                        prompt_message=str(record["request"]["message"]),
                        structured_result=True,
                        timeout=_agent_chat_timeout_seconds(),
                        action="run agent analysis",
                    )
                effective_session_id = str(raw.get("session_id") or session_id)
                require_owned_session(effective_session_id, scope)
                outcome = await _result_from_hermes_payload(record, raw)
                if outcome.get("status") == "succeeded":
                    outcome["result"]["session_id"] = effective_session_id
                _persist_agent_run_outcome(run_id, scope, outcome)
                return
            except asyncio.CancelledError:
                # Leave the durable state as running. Startup reconciliation
                # will determine whether Hermes completed before replaying.
                raise
            except Exception as exc:
                try:
                    recovered, progressed = await _inspect_agent_run(
                        record,
                        wait_seconds=(
                            _agent_reconcile_seconds()
                            if isinstance(exc, HTTPException)
                            and isinstance(exc.detail, dict)
                            and bool(exc.detail.get("outcome_unknown"))
                            else 0.0
                        ),
                    )
                except Exception:
                    recovered, progressed = None, False
                if recovered:
                    _persist_agent_run_outcome(run_id, scope, recovered)
                    return

                error = _run_error(exc, str(record["request_id"]))
                if progressed:
                    error.update(
                        {
                            "code": "AGENT_RUN_OUTCOME_UNKNOWN",
                            "message": (
                                "Hermes recorded partial progress, so the request was not replayed."
                            ),
                            "retryable": False,
                            "outcome_unknown": True,
                        }
                    )
                if await _fail_or_retry_agent_run(
                    run_id=run_id,
                    scope=scope,
                    record=record,
                    error=error,
                ):
                    continue
                return
    finally:
        _release_chat_session(session_id)


async def _execute_agent_run(run_id: str, scope: str) -> None:
    """Queue locally until this worker owns both run and session execution."""

    while True:
        record = agent_run_store.get_owned(run_id, scope)
        if not record or record.get("status") not in {"pending", "running"}:
            return
        session_id = str(record["session_id"])
        with agent_run_store.execution_claim(f"run:{run_id}") as run_claimed:
            if run_claimed:
                with agent_run_store.execution_claim(
                    f"session:{session_id}"
                ) as session_claimed:
                    if session_claimed:
                        await _execute_agent_run_exclusive(run_id, scope)
                        return
        await asyncio.sleep(0.25)


def _spawn_agent_run(record: dict[str, Any]) -> None:
    run_id = str(record["run_id"])
    existing = _agent_run_tasks.get(run_id)
    if existing and not existing.done():
        return
    task = asyncio.create_task(
        _execute_agent_run(run_id, str(record["owner_scope"])),
        name=f"xpd-agent-run:{run_id}",
    )
    _agent_run_tasks[run_id] = task

    def remove_finished(done: asyncio.Task[None]) -> None:
        if _agent_run_tasks.get(run_id) is done:
            _agent_run_tasks.pop(run_id, None)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            logger.error("Agent run task crashed run_id=%s: %s", run_id, error)

    task.add_done_callback(remove_finished)


async def resume_agent_runs() -> None:
    for record in agent_run_store.list_resumable(max_attempts=_agent_run_max_attempts()):
        _spawn_agent_run(record)


async def shutdown_agent_runs() -> None:
    tasks = [task for task in _agent_run_tasks.values() if not task.done()]
    if not tasks:
        return
    try:
        grace = max(
            0.0,
            min(40.0, float(os.getenv("XPD_AGENT_RUN_SHUTDOWN_GRACE_SECONDS", "30"))),
        )
    except ValueError:
        grace = 30.0
    _, pending = await asyncio.wait(tasks, timeout=grace)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def agent_run_health() -> dict[str, Any]:
    try:
        resumable = agent_run_store.list_resumable(max_attempts=_agent_run_max_attempts())
    except AgentRunStoreError as exc:
        return {
            "ok": False,
            "state_path": str(agent_run_store.path),
            "active_tasks": len(_agent_run_tasks),
            "capacity": agent_capacity_health(),
            "error": str(exc),
        }
    return {
        "ok": True,
        "state_path": str(agent_run_store.path),
        "active_tasks": len(_agent_run_tasks),
        "resumable_runs": len(resumable),
        "max_attempts": _agent_run_max_attempts(),
        "capacity": agent_capacity_health(),
        "error": None,
    }


async def _execute_final_reflection(job: dict[str, Any]) -> dict[str, Any] | str | None:
    source_session_id = str(job["session_id"])
    scope = str(job["owner_scope"])
    messages_payload = await _hermes_json(
        "GET",
        f"/api/sessions/{source_session_id}/messages",
        action="load reflection source",
    )
    messages = client_safe_messages(messages_payload)
    transcript_lines: list[str] = []
    turn_number = 0
    for message in messages:
        role = message.get("role")
        if role == "user":
            turn_number += 1
        if role not in {"user", "assistant"}:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        transcript_lines.append(
            f"turn={turn_number} role={role}: {redact_sensitive_text(content)}"
        )
    transcript = "\n".join(transcript_lines)
    if len(transcript) > 24_000:
        transcript = "[较早内容已截断]\n" + transcript[-24_000:]

    reflection_session_id = new_reflection_session_id(scope)
    await _hermes_json(
        "POST",
        "/api/sessions",
        payload={"id": reflection_session_id},
        action="create reflection session",
    )
    try:
        reflection_payload = await _hermes_json(
            "POST",
            f"/api/sessions/{reflection_session_id}/chat",
            scope=scope,
            timeout=_final_reflection_timeout_seconds(),
            payload={
                "system_message": (
                    f"{FINAL_REFLECTION_SYSTEM_PROMPT}\n\n{memory_capacity_notice(scope)}"
                ),
                "message": (
                    f"source_session={source_session_id}\n"
                    f"end_reason={job.get('end_reason')}\n\n"
                    f"会话记录：\n{transcript}"
                ),
            },
            action="run final reflection",
        )
    finally:
        try:
            await _hermes_json(
                "DELETE",
                f"/api/sessions/{reflection_session_id}",
                action="delete temporary reflection session",
            )
        except HTTPException:
            pass

    content = redact_sensitive_text(_extract_chat_content(reflection_payload))[:12_000]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"summary": content}


_reflection_queue: ReflectionQueue | None = None


def reflection_queue() -> ReflectionQueue:
    global _reflection_queue
    if _reflection_queue is None:
        _reflection_queue = ReflectionQueue(_execute_final_reflection)
    return _reflection_queue


async def resume_reflection_jobs() -> None:
    if _env_enabled("XPD_FINAL_REFLECTION_ENABLED"):
        await reflection_queue().resume_pending()


async def close_idle_sessions_once() -> int:
    try:
        idle_minutes = max(1, int(os.getenv("XPD_SESSION_IDLE_MINUTES", "30")))
    except ValueError:
        idle_minutes = 30
    raw = await _hermes_json(
        "GET",
        "/api/sessions?limit=1000&offset=0&source=api_server&include_children=true",
        action="scan idle sessions",
    )
    now = time.time()
    closed = 0
    for session in raw.get("data") or []:
        if not isinstance(session, dict) or session.get("ended_at"):
            continue
        session_id = str(session.get("id") or "")
        scope = scope_from_session_id(session_id)
        if not scope or "_reflection_" in session_id or "_scheduled_" in session_id:
            continue
        try:
            last_active = float(session.get("last_active") or session.get("started_at") or now)
        except (TypeError, ValueError):
            continue
        if now - last_active < idle_minutes * 60:
            continue
        updated = await _hermes_json(
            "PATCH",
            f"/api/sessions/{session_id}",
            payload={"end_reason": "idle_timeout"},
            action="close idle session",
        )
        normalized = _normalized_session(updated.get("session") or session)
        if _env_enabled("XPD_FINAL_REFLECTION_ENABLED"):
            reflection_queue().schedule(
                session_id=session_id,
                owner_scope=scope,
                turn_end=normalized["completed_turn_count"],
                end_reason="idle_timeout",
            )
        closed += 1
    return closed


async def idle_session_sweeper() -> None:
    while True:
        try:
            await close_idle_sessions_once()
        except Exception:
            # The health endpoint exposes Hermes availability. Idle cleanup is
            # best-effort and retries on the next bounded interval.
            pass
        await asyncio.sleep(60)


@router.post("/sessions", status_code=201)
async def create_session(req: SessionCreateRequest, scope: SessionScope) -> dict:
    if not _env_enabled("XPD_SESSION_ENABLED"):
        raise HTTPException(status_code=503, detail="Session persistence is disabled.")
    if req.close_session_id:
        require_owned_session(req.close_session_id, scope)
        try:
            await close_session(
                req.close_session_id,
                SessionCloseRequest(reason="new_session"),
                scope,
            )
        except HTTPException as exc:
            if exc.status_code != 404:
                raise

    session_id = new_session_id(scope)
    payload: dict[str, Any] = {"id": session_id}
    if req.title:
        payload["title"] = req.title
    raw = await _hermes_json(
        "POST", "/api/sessions", payload=payload, action="create session"
    )
    return {"ok": True, "session": _normalized_session(raw.get("session") or {})}


@router.get("/sessions")
async def list_sessions(
    scope: SessionScope,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    # Hermes' resource API is profile-scoped. Fetch a bounded window, then
    # enforce this wrapper's owner namespace before returning anything.
    raw = await _hermes_json(
        "GET",
        "/api/sessions?limit=1000&offset=0&source=api_server&include_children=true",
        action="list sessions",
    )
    owned = [
        session
        for session in raw.get("data") or []
        if isinstance(session, dict)
        and session_belongs_to_scope(str(session.get("id") or ""), scope)
        and "_reflection_" not in str(session.get("id") or "")
    ]
    page = owned[offset : offset + limit]
    semaphore = asyncio.Semaphore(10)

    async def normalize_with_turns(session: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            try:
                turns = await _completed_turn_count(str(session.get("id") or ""))
            except HTTPException:
                turns = None
        return _normalized_session(session, completed_turn_count=turns)

    normalized_page = await asyncio.gather(
        *(normalize_with_turns(session) for session in page)
    )
    return {
        "ok": True,
        "data": normalized_page,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(owned),
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, scope: SessionScope) -> dict:
    session = await _get_session(session_id, scope)
    turns = await _completed_turn_count(session_id)
    return {
        "ok": True,
        "session": _normalized_session(session, completed_turn_count=turns),
    }


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str,
    req: SessionUpdateRequest,
    scope: SessionScope,
) -> dict:
    require_owned_session(session_id, scope)
    if _scheduled_session_metadata(session_id):
        raise HTTPException(status_code=409, detail="Scheduled report sessions are read-only.")
    raw = await _hermes_json(
        "PATCH",
        f"/api/sessions/{session_id}",
        payload={"title": req.title},
        action="rename session",
    )
    return {"ok": True, "session": _normalized_session(raw.get("session") or {})}


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, scope: SessionScope) -> dict:
    require_owned_session(session_id, scope)
    raw = await _session_messages_payload(session_id)
    resolved_id = str(raw.get("session_id") or session_id)
    if not session_belongs_to_scope(resolved_id, scope):
        resolved_id = session_id
    return {
        "ok": True,
        "session_id": resolved_id,
        "data": client_safe_messages(raw),
    }


@router.get("/sessions/{session_id}/artifacts")
async def get_session_artifacts(session_id: str, scope: SessionScope) -> dict:
    await _get_session(session_id, scope)
    return {"ok": True, "data": list_session_artifacts(session_id)}


@router.get("/sessions/{session_id}/artifacts/{artifact_id}/download")
async def download_session_artifact(
    session_id: str,
    artifact_id: str,
    request: Request,
    scope: SessionScope,
) -> Response:
    await _get_session(session_id, scope)
    try:
        path, artifact = resolve_session_artifact(session_id, artifact_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="Report file not found.") from None
    download_url = artifact.get("_remote_download_url")
    if artifact.get("storage") == "oss" and isinstance(download_url, str):
        if "application/json" in request.headers.get("accept", "").lower():
            return JSONResponse(
                {
                    "ok": True,
                    "filename": artifact["filename"],
                    "download_url": download_url,
                    "download_url_expires_at": artifact.get(
                        "_remote_download_url_expires_at"
                    ),
                },
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        return RedirectResponse(
            download_url,
            status_code=307,
            headers={"Cache-Control": "private, no-store"},
        )
    return FileResponse(
        path,
        media_type=artifact["media_type"],
        filename=artifact["filename"],
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _idempotent_session_id(scope: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"xpd-middle-platform-session-v1:{scope}:{idempotency_key}".encode()
    ).hexdigest()[:32]
    return f"xpd_{scope}_{digest}"


async def _ensure_middle_platform_session(
    *,
    scope: str,
    idempotency_key: str,
    requested_session_id: str | None,
    title: str | None,
) -> str:
    if requested_session_id:
        await _get_session(requested_session_id, scope)
        return requested_session_id

    session_id = _idempotent_session_id(scope, idempotency_key)
    try:
        await _get_session(session_id, scope)
        return session_id
    except HTTPException as exc:
        if exc.status_code != 404:
            raise

    payload: dict[str, Any] = {"id": session_id}
    if title:
        payload["title"] = title
    try:
        await _hermes_json(
            "POST",
            "/api/sessions",
            payload=payload,
            action="create idempotent middle-platform session",
        )
    except HTTPException as exc:
        # A prior identical submission may have created the deterministic
        # session while its response was lost. Read-after-error reconciles it.
        try:
            await _get_session(session_id, scope)
        except HTTPException:
            raise exc
    return session_id


@router.post(
    "/v1/agent/runs",
    status_code=202,
    response_model=AgentRunSubmissionResponse,
    responses={
        200: {
            "model": AgentRunSubmissionResponse,
            "description": "Existing Agent run",
        },
        **documented_error_responses(400, 401, 404, 409, 422, 502, 503, 504),
    },
)
async def create_middle_platform_agent_run(
    req: MiddlePlatformRunRequest,
    request: Request,
    response: Response,
    scope: MiddlePlatformScope,
    idempotency_key: Annotated[
        str, Header(alias=IDEMPOTENCY_KEY_HEADER)
    ],
    raw_user_id: Annotated[str, Header(alias=CLIENT_USER_ID_HEADER)],
    _request_id: Annotated[str | None, Header(alias=REQUEST_ID_HEADER)] = None,
) -> dict[str, Any]:
    """Stable API entrypoint intended for the live-commerce middle platform."""

    try:
        key = validate_idempotency_key(idempotency_key)
    except ValueError as exc:
        raise api_error(
            400,
            code="INVALID_IDEMPOTENCY_KEY",
            message=str(exc),
        ) from exc
    session_id = await _ensure_middle_platform_session(
        scope=scope,
        idempotency_key=key,
        requested_session_id=req.session_id,
        title=req.title,
    )
    return await create_agent_run(
        session_id,
        AgentRunCreateRequest(message=req.message),
        request,
        response,
        scope,
        key,
        raw_user_id,
    )


@router.post(
    "/sessions/{session_id}/runs",
    status_code=202,
    response_model=AgentRunSubmissionResponse,
    responses={
        200: {
            "model": AgentRunSubmissionResponse,
            "description": "Existing Agent run",
        },
        **documented_error_responses(400, 404, 409, 422, 502, 503, 504),
    },
)
async def create_agent_run(
    session_id: str,
    req: AgentRunCreateRequest,
    request: Request,
    response: Response,
    scope: SessionScope,
    idempotency_key: Annotated[
        str, Header(alias=IDEMPOTENCY_KEY_HEADER)
    ],
    raw_user_id: Annotated[
        str | None, Header(alias=CLIENT_USER_ID_HEADER)
    ] = None,
) -> dict[str, Any]:
    """Submit one durable, idempotent Agent turn for middle-platform use."""

    try:
        key = validate_idempotency_key(idempotency_key)
    except ValueError as exc:
        raise api_error(
            400,
            code="INVALID_IDEMPOTENCY_KEY",
            message=str(exc),
        ) from exc

    session = await _get_session(session_id, scope)
    if _scheduled_session_metadata(session_id):
        raise api_error(
            409,
            code="SCHEDULED_SESSION_READ_ONLY",
            message="Scheduled report sessions are read-only.",
        )
    if session.get("ended_at"):
        raise api_error(
            409,
            code="SESSION_CLOSED",
            message="Closed sessions are read-only.",
        )

    try:
        message_payload = await _session_messages_payload(session_id)
        baseline_message_count = len(
            [
                message
                for message in message_payload.get("data") or []
                if isinstance(message, dict)
            ]
        )
        artifact_ids = sorted(_artifact_ids(session_id))
        record, created = agent_run_store.create_or_get(
            owner_scope=scope,
            session_id=session_id,
            idempotency_key=key,
            request={"message": req.message},
            request_id=str(request.state.request_id),
            checkpoint={
                "baseline_message_count": baseline_message_count,
                "artifact_ids": artifact_ids,
                "upstream_submission_started": False,
                "report_uid": _report_uid(scope, raw_user_id),
            },
        )
    except IdempotencyConflictError as exc:
        raise api_error(
            409,
            code="IDEMPOTENCY_CONFLICT",
            message=str(exc),
        ) from exc
    except AgentRunStoreError as exc:
        raise api_error(
            503,
            code="AGENT_RUN_STATE_UNAVAILABLE",
            message="Agent run state is unavailable.",
            retryable=False,
            outcome_unknown=True,
        ) from exc

    if created or record.get("status") in {"pending", "running"}:
        _spawn_agent_run(record)
    elif record.get("status") == "failed":
        error = record.get("error") or {}
        if (
            isinstance(error, dict)
            and error.get("retryable")
            and not error.get("outcome_unknown")
            and run_retry_attempt_count(record) < _agent_run_max_attempts()
        ):
            try:
                agent_run_store.update_checkpoint(
                    str(record["run_id"]),
                    scope,
                    upstream_submission_started=False,
                )
                record = agent_run_store.retry(
                    str(record["run_id"]),
                    scope,
                    max_attempts=_agent_run_max_attempts(),
                ) or record
            except RunRetryNotAllowedError:
                pass
            else:
                _spawn_agent_run(record)

    public = _refresh_run_artifacts(agent_run_store.public_run(record))
    response.status_code = 202 if public["status"] in {"pending", "running"} else 200
    return {
        "ok": True,
        "run": public,
        "status_url": f"/api/v1/agent/runs/{public['run_id']}",
    }


@router.get(
    "/runs/{run_id}",
    response_model=AgentRunStatusResponse,
    responses=documented_error_responses(404, 422, 503),
)
async def get_agent_run(run_id: str, scope: SessionScope) -> dict[str, Any]:
    try:
        run = agent_run_store.get_public_owned(run_id, scope)
    except AgentRunStoreError as exc:
        raise api_error(
            503,
            code="AGENT_RUN_STATE_UNAVAILABLE",
            message="Agent run state is unavailable.",
            retryable=False,
            outcome_unknown=True,
        ) from exc
    if not run:
        raise api_error(404, code="AGENT_RUN_NOT_FOUND", message="Agent run not found.")
    return {"ok": True, "run": _refresh_run_artifacts(run)}


@router.get(
    "/v1/agent/runs/{run_id}",
    response_model=AgentRunStatusResponse,
    responses=documented_error_responses(401, 404, 422, 503),
)
async def get_middle_platform_agent_run(
    run_id: str,
    scope: MiddlePlatformScope,
    _request_id: Annotated[str | None, Header(alias=REQUEST_ID_HEADER)] = None,
) -> dict[str, Any]:
    return await get_agent_run(run_id, scope)


def _agent_run_submission_payload(
    record: dict[str, Any], response: Response
) -> dict[str, Any]:
    public = _refresh_run_artifacts(agent_run_store.public_run(record))
    response.status_code = 202 if public["status"] in {"pending", "running"} else 200
    return {
        "ok": True,
        "run": public,
        "status_url": f"/api/v1/agent/runs/{public['run_id']}",
    }


async def _submit_agent_run_input(
    run_id: str,
    req: ClarificationAnswerRequest,
    response: Response,
    scope: str,
    idempotency_key: str,
) -> dict[str, Any]:
    try:
        key = validate_idempotency_key(idempotency_key)
    except ValueError as exc:
        raise api_error(
            400,
            code="INVALID_IDEMPOTENCY_KEY",
            message=str(exc),
        ) from exc

    answer = req.answer.strip()
    if not answer:
        raise api_error(
            400,
            code="INVALID_CLARIFICATION_INPUT",
            message="answer must not be blank.",
        )

    try:
        replay = agent_run_store.replay_input(
            run_id,
            scope,
            idempotency_key=key,
            answer=answer,
        )
    except IdempotencyConflictError as exc:
        raise api_error(
            409,
            code="INPUT_IDEMPOTENCY_CONFLICT",
            message=str(exc),
        ) from exc
    except AgentRunStoreError as exc:
        raise api_error(
            503,
            code="AGENT_RUN_STATE_UNAVAILABLE",
            message="Agent run state is unavailable.",
            retryable=False,
            outcome_unknown=True,
        ) from exc
    if replay is not None:
        if replay.get("status") in {"pending", "running"}:
            _spawn_agent_run(replay)
        return _agent_run_submission_payload(replay, response)

    try:
        record = agent_run_store.get_owned(run_id, scope)
    except AgentRunStoreError as exc:
        raise api_error(
            503,
            code="AGENT_RUN_STATE_UNAVAILABLE",
            message="Agent run state is unavailable.",
            retryable=False,
            outcome_unknown=True,
        ) from exc
    if not record:
        raise api_error(404, code="AGENT_RUN_NOT_FOUND", message="Agent run not found.")
    if record.get("status") != "waiting_input":
        raise api_error(
            409,
            code="AGENT_RUN_NOT_WAITING_INPUT",
            message="The Agent run is not waiting for input.",
        )

    session_id = str(record["session_id"])
    session = await _get_session(session_id, scope)
    if session.get("ended_at"):
        raise api_error(
            409,
            code="SESSION_CLOSED",
            message="Closed sessions are read-only.",
        )
    message_payload = await _session_messages_payload(session_id)
    baseline_message_count = len(
        [
            message
            for message in message_payload.get("data") or []
            if isinstance(message, dict)
        ]
    )
    artifact_ids = sorted(_artifact_ids(session_id))
    try:
        resumed, accepted = agent_run_store.resume_with_input(
            run_id,
            scope,
            idempotency_key=key,
            answer=answer,
            baseline_message_count=baseline_message_count,
            artifact_ids=artifact_ids,
        )
    except IdempotencyConflictError as exc:
        raise api_error(
            409,
            code="INPUT_IDEMPOTENCY_CONFLICT",
            message=str(exc),
        ) from exc
    except RunInputNotAllowedError as exc:
        raise api_error(
            409,
            code="AGENT_RUN_NOT_WAITING_INPUT",
            message=str(exc),
        ) from exc
    except InvalidRunTransitionError as exc:
        raise api_error(
            503,
            code="AGENT_RUN_STATE_INVALID",
            message=str(exc),
            retryable=False,
            outcome_unknown=False,
        ) from exc
    except AgentRunStoreError as exc:
        raise api_error(
            503,
            code="AGENT_RUN_STATE_UNAVAILABLE",
            message="Agent run state is unavailable.",
            retryable=False,
            outcome_unknown=True,
        ) from exc
    if not resumed:
        raise api_error(404, code="AGENT_RUN_NOT_FOUND", message="Agent run not found.")
    if accepted or resumed.get("status") in {"pending", "running"}:
        _spawn_agent_run(resumed)
    return _agent_run_submission_payload(resumed, response)


@router.post(
    "/runs/{run_id}/input",
    status_code=202,
    response_model=AgentRunSubmissionResponse,
    responses={
        200: {
            "model": AgentRunSubmissionResponse,
            "description": "Existing clarification input",
        },
        **documented_error_responses(400, 404, 409, 422, 503),
    },
)
async def submit_agent_run_input(
    run_id: str,
    req: ClarificationAnswerRequest,
    response: Response,
    scope: SessionScope,
    idempotency_key: Annotated[str, Header(alias=IDEMPOTENCY_KEY_HEADER)],
) -> dict[str, Any]:
    return await _submit_agent_run_input(
        run_id,
        req,
        response,
        scope,
        idempotency_key,
    )


@router.post(
    "/v1/agent/runs/{run_id}/input",
    status_code=202,
    response_model=AgentRunSubmissionResponse,
    responses={
        200: {
            "model": AgentRunSubmissionResponse,
            "description": "Existing clarification input",
        },
        **documented_error_responses(400, 401, 404, 409, 422, 503),
    },
)
async def submit_middle_platform_agent_run_input(
    run_id: str,
    req: ClarificationAnswerRequest,
    response: Response,
    scope: MiddlePlatformScope,
    idempotency_key: Annotated[str, Header(alias=IDEMPOTENCY_KEY_HEADER)],
    _request_id: Annotated[str | None, Header(alias=REQUEST_ID_HEADER)] = None,
) -> dict[str, Any]:
    return await _submit_agent_run_input(
        run_id,
        req,
        response,
        scope,
        idempotency_key,
    )


@router.post("/sessions/{session_id}/chat", deprecated=True)
async def session_chat(
    session_id: str,
    req: SessionChatRequest,
    request: Request,
    scope: SessionScope,
    raw_user_id: Annotated[
        str | None, Header(alias=CLIENT_USER_ID_HEADER)
    ] = None,
) -> dict:
    session = await _get_session(session_id, scope)
    if _scheduled_session_metadata(session_id):
        raise HTTPException(status_code=409, detail="Scheduled report sessions are read-only.")
    if session.get("ended_at"):
        raise HTTPException(status_code=409, detail="Closed sessions are read-only.")
    if not _claim_chat_session(session_id):
        raise HTTPException(
            status_code=409,
            detail="This session already has an analysis in progress.",
        )
    try:
        try:
            previous_message_count = max(0, int(session.get("message_count") or 0))
        except (TypeError, ValueError):
            previous_message_count = 0
        await _prepare_session_turn(
            session_id,
            scope=scope,
            title_message=req.message,
            raw_user_id=raw_user_id,
            trace_id=str(request.state.request_id),
        )
        existing_artifact_ids = _artifact_ids(session_id)
        async with agent_capacity_slot():
            raw = await _submit_session_turn(
                session_id,
                scope=scope,
                message=req.message,
                timeout=None,
                action="continue session chat",
            )
        effective_session_id = str(raw.get("session_id") or session_id)
        require_owned_session(effective_session_id, scope)
        messages_payload = await _session_messages_payload(effective_session_id)
        all_messages = client_safe_messages(messages_payload)
        turn_messages = all_messages[previous_message_count:]
        artifacts = [
            artifact
            for artifact in list_session_artifacts(session_id)
            if artifact["artifact_id"] not in existing_artifact_ids
        ]
        return {
            "ok": True,
            "session_id": effective_session_id,
            "content": _extract_chat_content(raw),
            "reasoning": _reasoning_from_messages(turn_messages),
            "usage": raw.get("usage") or {},
            "artifacts": artifacts,
        }
    finally:
        _release_chat_session(session_id)


@router.post("/sessions/{session_id}/chat/stream")
async def session_chat_stream(
    session_id: str,
    req: SessionChatRequest,
    request: Request,
    scope: SessionScope,
    raw_user_id: Annotated[
        str | None, Header(alias=CLIENT_USER_ID_HEADER)
    ] = None,
) -> StreamingResponse:
    session = await _get_session(session_id, scope)
    if _scheduled_session_metadata(session_id):
        raise HTTPException(status_code=409, detail="Scheduled report sessions are read-only.")
    if session.get("ended_at"):
        raise HTTPException(status_code=409, detail="Closed sessions are read-only.")
    if not _claim_chat_session(session_id):
        raise HTTPException(
            status_code=409,
            detail="This session already has an analysis in progress.",
        )
    try:
        await _prepare_session_turn(
            session_id,
            scope=scope,
            title_message=req.message,
            raw_user_id=raw_user_id,
            trace_id=str(request.state.request_id),
        )
        existing_artifact_ids = _artifact_ids(session_id)
    except Exception:
        _release_chat_session(session_id)
        raise

    async def events():
        emitted_artifact_ids: set[str] = set()
        try:
            try:
                async with agent_capacity_slot():
                    async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                        async with client.stream(
                            "POST",
                            f"{hermes_origin()}{_session_turn_path(session_id, stream=True)}",
                            headers=_hermes_headers(scope),
                            json=_session_turn_payload(
                                scope=scope,
                                message=req.message,
                            ),
                        ) as response:
                            if not response.is_success:
                                body = await response.aread()
                                yield (
                                    "event: error\ndata: "
                                    + json.dumps(
                                        {
                                            "message": "Hermes session stream failed.",
                                            "status_code": response.status_code,
                                            "body": body.decode(
                                                "utf-8", errors="replace"
                                            )[:1000],
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                )
                            else:
                                async for chunk in _relay_hermes_sse_with_artifacts(
                                    response.aiter_text(),
                                    session_id=session_id,
                                    existing_artifact_ids=existing_artifact_ids,
                                    emitted_artifact_ids=emitted_artifact_ids,
                                ):
                                    yield chunk
            except httpx.HTTPError as exc:
                yield (
                    "event: error\ndata: "
                    + json.dumps(
                        {"message": "Hermes session stream failed.", "error": str(exc)}
                    )
                    + "\n\n"
                )
            for artifact_event in _new_artifact_ready_events(
                session_id,
                existing_artifact_ids=existing_artifact_ids,
                emitted_artifact_ids=emitted_artifact_ids,
            ):
                yield artifact_event
        finally:
            _release_chat_session(session_id)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"X-XPD-Session-Id": session_id, "Cache-Control": "no-cache"},
    )


@router.post(
    "/sessions/{session_id}/clarifications/{clarification_id}/answer"
)
async def answer_clarification(
    session_id: str,
    clarification_id: str,
    req: ClarificationAnswerRequest,
    scope: SessionScope,
) -> dict:
    # Reject cross-owner session IDs before contacting Hermes. The gateway
    # performs the same check against its in-process clarification registry.
    require_owned_session(session_id, scope)
    answer = req.answer.strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer must not be blank.")
    raw = await _hermes_json(
        "POST",
        f"/api/sessions/{session_id}/clarifications/{clarification_id}/answer",
        scope=scope,
        payload={"answer": answer},
        action="answer clarification",
    )
    return raw


@router.post("/sessions/{session_id}/close")
async def close_session(
    session_id: str,
    req: SessionCloseRequest,
    scope: SessionScope,
) -> dict:
    session = await _get_session(session_id, scope)
    if _scheduled_session_metadata(session_id):
        raise HTTPException(status_code=409, detail="Scheduled report sessions are read-only.")
    if not session.get("ended_at"):
        raw = await _hermes_json(
            "PATCH",
            f"/api/sessions/{session_id}",
            payload={"end_reason": req.reason},
            action="close session",
        )
        session = raw.get("session") or session
    reflection = None
    if _env_enabled("XPD_FINAL_REFLECTION_ENABLED"):
        turns = await _completed_turn_count(session_id)
        normalized = _normalized_session(session, completed_turn_count=turns)
        reflection = reflection_queue().schedule(
            session_id=session_id,
            owner_scope=scope,
            turn_end=normalized["completed_turn_count"],
            end_reason=req.reason,
        )
    return {
        "ok": True,
        "session": _normalized_session(session),
        "reflection": reflection,
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, scope: SessionScope) -> dict:
    require_owned_session(session_id, scope)
    raw = await _hermes_json(
        "DELETE", f"/api/sessions/{session_id}", action="delete session"
    )
    reflection_queue().delete_for_session(session_id)
    schedule_store().delete_run_for_session(session_id)
    delete_session_artifacts(session_id)
    return {
        "ok": True,
        "session_id": session_id,
        "deleted": bool(raw.get("deleted")),
    }


@router.get("/reflections/{reflection_id}")
async def get_reflection(reflection_id: str, scope: SessionScope) -> dict:
    job = reflection_queue().get(reflection_id)
    if not job:
        raise HTTPException(status_code=404, detail="Reflection not found.")
    require_owned_session(str(job["session_id"]), scope)
    return {"ok": True, "reflection": job}


@router.post("/reflections/{reflection_id}/retry")
async def retry_reflection(reflection_id: str, scope: SessionScope) -> dict:
    existing = reflection_queue().get(reflection_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Reflection not found.")
    require_owned_session(str(existing["session_id"]), scope)
    job = reflection_queue().retry(reflection_id)
    return {"ok": True, "reflection": job}
