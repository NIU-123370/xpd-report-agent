from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException

from xpd_report_agent.api.session_service import (
    CLIENT_SESSION_KEY_HEADER,
    CLIENT_USER_ID_HEADER,
    IDENTITY_MODE_USER_ID,
    identity_mode,
    resolve_owner_scope,
)
from xpd_report_agent.memory_governance import memory_policy
from xpd_report_agent.memory_paths import (
    local_memory_dir,
    merchant_memory_path,
    user_memory_dir,
)

router = APIRouter(prefix="/api")

MEMORY_FILES = (
    ("agent", "MEMORY.md", "Agent 经验记忆", "XPD_MEMORY_CHAR_LIMIT", 2200),
    ("user", "USER.md", "用户画像记忆", "XPD_USER_CHAR_LIMIT", 1375),
)


def _client_scope(
    raw_key: Annotated[str | None, Header(alias=CLIENT_SESSION_KEY_HEADER)] = None,
    raw_user_id: Annotated[str | None, Header(alias=CLIENT_USER_ID_HEADER)] = None,
) -> str:
    return resolve_owner_scope(session_key=raw_key, user_id=raw_user_id)


MemoryScope = Annotated[str, Depends(_client_scope)]


def _configured_limit(env_name: str, default: int) -> int:
    try:
        return max(256, int(os.getenv(env_name, str(default))))
    except ValueError:
        return default


def _consolidation_ratio() -> float:
    try:
        ratio = float(os.getenv("XPD_MEMORY_CONSOLIDATION_RATIO", "0.8"))
    except ValueError:
        ratio = 0.8
    return min(0.95, max(0.5, ratio))


def _memory_specs(scope: str | None) -> list[tuple[str, Path, str, str, str, int, bool]]:
    if identity_mode() == IDENTITY_MODE_USER_ID:
        personal_dir = user_memory_dir(scope or "")
        specs = [
            (
                "agent",
                personal_dir / "MEMORY.md",
                "personal/MEMORY.md",
                "个人反思记忆",
                "XPD_MEMORY_CHAR_LIMIT",
                2200,
                False,
            ),
            (
                "user",
                personal_dir / "USER.md",
                "personal/USER.md",
                "个人画像记忆",
                "XPD_USER_CHAR_LIMIT",
                1375,
                False,
            ),
        ]
        merchant_enabled = os.getenv(
            "XPD_MERCHANT_MEMORY_ENABLED", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not merchant_enabled:
            return specs
        return [
            (
                "merchant",
                merchant_memory_path(),
                "merchant/MEMORY.md",
                "商家公共经营记忆",
                "XPD_MERCHANT_MEMORY_CHAR_LIMIT",
                2200,
                True,
            ),
            *specs,
        ]

    memory_dir = local_memory_dir()
    return [
        (store, memory_dir / filename, filename, label, env_name, default, False)
        for store, filename, label, env_name, default in MEMORY_FILES
    ]


def memory_file_snapshots(scope: str | None = None) -> list[dict]:
    policy = memory_policy()
    snapshots = []

    for store, path, filename, label, env_name, default_limit, read_only in _memory_specs(
        scope
    ):
        try:
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            modified_at = path.stat().st_mtime if path.exists() else None
        except (OSError, UnicodeError) as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Unable to read Hermes memory file {filename}: {exc}",
            ) from exc

        used_chars = len(content)
        limit = _configured_limit(env_name, default_limit)
        snapshots.append(
            {
                "store": store,
                "filename": filename,
                "label": label,
                "content": content,
                "read_only": read_only,
                "exists": path.exists(),
                "used_chars": used_chars,
                "limit_chars": limit,
                "usage_ratio": used_chars / limit,
                "watermark_ratio": policy.trigger_ratio,
                "at_watermark": used_chars >= int(limit * policy.trigger_ratio),
                "critical_ratio": policy.critical_ratio,
                "at_critical": used_chars >= int(limit * policy.critical_ratio),
                "target_ratio": policy.target_ratio,
                "write_policy": (
                    "consolidate_only"
                    if used_chars >= int(limit * policy.critical_ratio)
                    else "write_and_consolidate"
                    if used_chars >= int(limit * policy.trigger_ratio)
                    else "normal"
                ),
                "modified_at": modified_at,
            }
        )

    return snapshots


@router.get("/memories")
async def get_memory_files(scope: MemoryScope) -> dict:
    mode = identity_mode()
    return {
        "ok": True,
        "scope": (
            "authenticated_user"
            if mode == IDENTITY_MODE_USER_ID
            else "local_hermes_profile"
        ),
        "identity_mode": mode,
        "data": memory_file_snapshots(scope),
    }
