from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from typing import Any

from fastapi import HTTPException

from xpd_report_agent.api.structured_analysis import strip_structured_analysis_block
from xpd_report_agent.memory_paths import (
    IDENTITY_MODE_ENV,
    IDENTITY_MODE_SESSION_KEY,
    IDENTITY_MODE_USER_ID,
    configured_identity_mode,
)

CLIENT_SESSION_KEY_HEADER = "X-XPD-Session-Key"
CLIENT_USER_ID_HEADER = "X-User-Id"
SESSION_ID_PREFIX = "xpd"
SESSION_KEY_MIN_LENGTH = 24
SESSION_KEY_MAX_LENGTH = 256
USER_ID_MAX_LENGTH = 128


def session_signing_secret() -> str:
    secret = os.getenv("XPD_SESSION_SIGNING_SECRET") or os.getenv(
        "HERMES_GATEWAY_API_KEY", ""
    )
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="XPD_SESSION_SIGNING_SECRET or HERMES_GATEWAY_API_KEY must be configured.",
        )
    return secret


def validate_session_key(session_key: str | None) -> str:
    value = (session_key or "").strip()
    if not (SESSION_KEY_MIN_LENGTH <= len(value) <= SESSION_KEY_MAX_LENGTH):
        raise HTTPException(
            status_code=401,
            detail=(
                f"{CLIENT_SESSION_KEY_HEADER} must contain a stable random key "
                f"between {SESSION_KEY_MIN_LENGTH} and {SESSION_KEY_MAX_LENGTH} characters."
            ),
        )
    if re.search(r"[\r\n\x00]", value):
        raise HTTPException(status_code=401, detail=f"Invalid {CLIENT_SESSION_KEY_HEADER}.")
    return value


def identity_mode() -> str:
    mode = configured_identity_mode()
    if mode not in {IDENTITY_MODE_SESSION_KEY, IDENTITY_MODE_USER_ID}:
        raise HTTPException(
            status_code=500,
            detail=(
                f"{IDENTITY_MODE_ENV} must be either "
                f"'{IDENTITY_MODE_SESSION_KEY}' or '{IDENTITY_MODE_USER_ID}'."
            ),
        )
    return mode


def validate_user_id(user_id: str | None) -> str:
    value = (user_id or "").strip()
    if not value or len(value) > USER_ID_MAX_LENGTH:
        raise HTTPException(
            status_code=401,
            detail=(
                f"{CLIENT_USER_ID_HEADER} must contain a stable user identifier "
                f"between 1 and {USER_ID_MAX_LENGTH} characters."
            ),
        )
    if re.search(r"[\s\x00-\x1f\x7f]", value):
        raise HTTPException(status_code=401, detail=f"Invalid {CLIENT_USER_ID_HEADER}.")
    return value


def owner_scope(session_key: str, *, secret: str | None = None) -> str:
    signing_secret = (secret or session_signing_secret()).encode("utf-8")
    return hmac.new(signing_secret, session_key.encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def user_owner_scope(user_id: str, *, secret: str | None = None) -> str:
    # Domain-separate authenticated user IDs from legacy browser session keys.
    # This prevents identical raw values in the two identity modes from sharing
    # owner-scoped resources accidentally.
    return owner_scope(f"user-id:{validate_user_id(user_id)}", secret=secret)


def resolve_owner_scope(
    *, session_key: str | None = None, user_id: str | None = None
) -> str:
    if identity_mode() == IDENTITY_MODE_USER_ID:
        return user_owner_scope(user_id or "")
    return owner_scope(validate_session_key(session_key))


def new_session_id(scope: str) -> str:
    return f"{SESSION_ID_PREFIX}_{scope}_{secrets.token_hex(16)}"


def new_reflection_session_id(scope: str) -> str:
    return f"{SESSION_ID_PREFIX}_{scope}_reflection_{secrets.token_hex(12)}"


def session_belongs_to_scope(session_id: str, scope: str) -> bool:
    expected = f"{SESSION_ID_PREFIX}_{scope}_"
    return hmac.compare_digest(session_id[: len(expected)], expected)


def scope_from_session_id(session_id: str) -> str | None:
    match = re.fullmatch(r"xpd_([0-9a-f]{20})_.+", session_id)
    return match.group(1) if match else None


def require_owned_session(session_id: str, scope: str) -> None:
    if not session_belongs_to_scope(session_id, scope):
        # Deliberately return 404 so callers cannot probe another user's IDs.
        raise HTTPException(status_code=404, detail="Session not found.")


def count_completed_turns(messages: list[dict[str, Any]]) -> int:
    completed = 0
    waiting_for_final = False
    for message in messages:
        role = message.get("role")
        if role == "user":
            waiting_for_final = True
            continue
        if role != "assistant" or not waiting_for_final:
            continue
        content = message.get("content")
        has_content = isinstance(content, str) and bool(content.strip())
        if isinstance(content, list):
            has_content = bool(content)
        if has_content and not message.get("tool_calls"):
            completed += 1
            waiting_for_final = False
    return completed


def normalize_session(
    session: dict[str, Any], *, completed_turn_count: int | None = None
) -> dict[str, Any]:
    started_at = session.get("started_at")
    last_active = session.get("last_active") or started_at
    ended_at = session.get("ended_at")
    message_count = int(session.get("message_count") or 0)
    return {
        "session_id": str(session.get("id") or session.get("session_id") or ""),
        "title": session.get("title") or "新对话",
        "status": "closed" if ended_at else "active",
        "completed_turn_count": (
            completed_turn_count if completed_turn_count is not None else message_count // 2
        ),
        "message_count": message_count,
        "created_at": started_at,
        "last_active_at": last_active,
        "closed_at": ended_at,
        "end_reason": session.get("end_reason"),
        "parent_session_id": session.get("parent_session_id"),
        "preview": session.get("preview") or "",
    }


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_ -]?key|access[_ -]?key(?:[_ -]?id)?|"
        r"access[_ -]?key[_ -]?secret|authorization|cookie|密码|密钥)\b\s*[:=：]\s*[^\s,;]+"
    ),
    re.compile(r"\bLTAI[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)\bmysql(?:\+\w+)?://[^\s]+"),
)


def redact_sensitive_text(value: str) -> str:
    redacted = value
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub("[REDACTED_CREDENTIAL]", redacted)
    return redacted


def client_safe_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    safe_messages: list[dict[str, Any]] = []
    for message in payload.get("data") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        safe_message = {
            key: message.get(key)
            for key in (
                "id",
                "session_id",
                "role",
                "content",
                "tool_name",
                "timestamp",
                "finish_reason",
            )
            if key in message
        }
        if role == "assistant" and isinstance(safe_message.get("content"), str):
            safe_message["content"] = strip_structured_analysis_block(
                str(safe_message["content"])
            )
        if role == "assistant":
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            if isinstance(reasoning, str) and reasoning.strip():
                # Expose one normalized field instead of returning duplicate
                # provider-specific reasoning/reasoning_content values.
                safe_message["reasoning"] = reasoning
        safe_messages.append(safe_message)
    return safe_messages
