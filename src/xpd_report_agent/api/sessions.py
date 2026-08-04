from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from xpd_report_agent.api.prompts import (
    FINAL_REFLECTION_SYSTEM_PROMPT,
    REPORT_SYSTEM_PROMPT,
)
from xpd_report_agent.api.reflections import ReflectionQueue
from xpd_report_agent.api.session_service import (
    CLIENT_SESSION_KEY_HEADER,
    client_safe_messages,
    count_completed_turns,
    new_reflection_session_id,
    new_session_id,
    normalize_session,
    owner_scope,
    redact_sensitive_text,
    require_owned_session,
    scope_from_session_id,
    session_belongs_to_scope,
    validate_session_key,
)

router = APIRouter(prefix="/api")


class SessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    close_session_id: str | None = None


class SessionUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class SessionChatRequest(BaseModel):
    message: str = Field(min_length=1)
    stream: bool = True


class SessionCloseRequest(BaseModel):
    reason: Literal["user_close", "new_session", "idle_timeout", "delete"] = "user_close"


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


def memory_capacity_notice() -> str:
    hermes_home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
    memory_dir = hermes_home / "memories"
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
    return f"记忆容量状态（整理水位 {ratio:.0%}）：{', '.join(states)}。{instruction}"


def report_system_prompt() -> str:
    return f"{REPORT_SYSTEM_PROMPT}\n\n{memory_capacity_notice()}"


def _client_scope(
    raw_key: Annotated[str | None, Header(alias=CLIENT_SESSION_KEY_HEADER)] = None,
) -> str:
    return owner_scope(validate_session_key(raw_key))


SessionScope = Annotated[str, Depends(_client_scope)]


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


def _raise_upstream(response: httpx.Response, action: str) -> None:
    if response.is_success:
        return
    status_code = response.status_code if response.status_code in {400, 404, 409} else 502
    try:
        body: Any = response.json()
    except Exception:
        body = response.text[:1000]
    raise HTTPException(
        status_code=status_code,
        detail={
            "message": f"Hermes failed to {action}.",
            "status_code": response.status_code,
            "body": body,
        },
    )


async def _hermes_json(
    method: str,
    path: str,
    *,
    scope: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float | None = 15.0,
    action: str,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.request(
                method,
                f"{hermes_origin()}{path}",
                headers=_hermes_headers(scope),
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": f"Hermes request failed while trying to {action}.", "error": str(exc)},
        ) from exc
    _raise_upstream(response, action)
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
            timeout=None,
            payload={
                "system_message": (
                    f"{FINAL_REFLECTION_SYSTEM_PROMPT}\n\n{memory_capacity_notice()}"
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
        if not scope or "_reflection_" in session_id:
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
        normalized = normalize_session(updated.get("session") or session)
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
    return {"ok": True, "session": normalize_session(raw.get("session") or {})}


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
        return normalize_session(session, completed_turn_count=turns)

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
        "session": normalize_session(session, completed_turn_count=turns),
    }


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str,
    req: SessionUpdateRequest,
    scope: SessionScope,
) -> dict:
    require_owned_session(session_id, scope)
    raw = await _hermes_json(
        "PATCH",
        f"/api/sessions/{session_id}",
        payload={"title": req.title},
        action="rename session",
    )
    return {"ok": True, "session": normalize_session(raw.get("session") or {})}


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


@router.post("/sessions/{session_id}/chat")
async def session_chat(
    session_id: str,
    req: SessionChatRequest,
    scope: SessionScope,
) -> dict:
    session = await _get_session(session_id, scope)
    if session.get("ended_at"):
        raise HTTPException(status_code=409, detail="Closed sessions are read-only.")
    try:
        previous_message_count = max(0, int(session.get("message_count") or 0))
    except (TypeError, ValueError):
        previous_message_count = 0
    await _set_initial_title(session_id, scope, req.message)
    raw = await _hermes_json(
        "POST",
        f"/api/sessions/{session_id}/chat",
        scope=scope,
        timeout=None,
        payload={"message": req.message, "system_message": report_system_prompt()},
        action="continue session chat",
    )
    effective_session_id = str(raw.get("session_id") or session_id)
    require_owned_session(effective_session_id, scope)
    messages_payload = await _session_messages_payload(effective_session_id)
    all_messages = client_safe_messages(messages_payload)
    turn_messages = all_messages[previous_message_count:]
    return {
        "ok": True,
        "session_id": effective_session_id,
        "content": _extract_chat_content(raw),
        "reasoning": _reasoning_from_messages(turn_messages),
        "usage": raw.get("usage") or {},
    }


@router.post("/sessions/{session_id}/chat/stream")
async def session_chat_stream(
    session_id: str,
    req: SessionChatRequest,
    scope: SessionScope,
) -> StreamingResponse:
    session = await _get_session(session_id, scope)
    if session.get("ended_at"):
        raise HTTPException(status_code=409, detail="Closed sessions are read-only.")
    await _set_initial_title(session_id, scope, req.message)

    async def events():
        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                async with client.stream(
                    "POST",
                    f"{hermes_origin()}/api/sessions/{session_id}/chat/stream",
                    headers=_hermes_headers(scope),
                    json={"message": req.message, "system_message": report_system_prompt()},
                ) as response:
                    if not response.is_success:
                        body = await response.aread()
                        yield (
                            "event: error\ndata: "
                            + json.dumps(
                                {
                                    "message": "Hermes session stream failed.",
                                    "status_code": response.status_code,
                                    "body": body.decode("utf-8", errors="replace")[:1000],
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        return
                    async for chunk in response.aiter_text():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as exc:
            yield (
                "event: error\ndata: "
                + json.dumps({"message": "Hermes session stream failed.", "error": str(exc)})
                + "\n\n"
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"X-XPD-Session-Id": session_id, "Cache-Control": "no-cache"},
    )


@router.post("/sessions/{session_id}/close")
async def close_session(
    session_id: str,
    req: SessionCloseRequest,
    scope: SessionScope,
) -> dict:
    session = await _get_session(session_id, scope)
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
        normalized = normalize_session(session, completed_turn_count=turns)
        reflection = reflection_queue().schedule(
            session_id=session_id,
            owner_scope=scope,
            turn_end=normalized["completed_turn_count"],
            end_reason=req.reason,
        )
    return {
        "ok": True,
        "session": normalize_session(session),
        "reflection": reflection,
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, scope: SessionScope) -> dict:
    require_owned_session(session_id, scope)
    raw = await _hermes_json(
        "DELETE", f"/api/sessions/{session_id}", action="delete session"
    )
    reflection_queue().delete_for_session(session_id)
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
