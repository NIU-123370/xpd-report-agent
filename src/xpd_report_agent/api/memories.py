from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException

from xpd_report_agent.api.session_service import (
    CLIENT_SESSION_KEY_HEADER,
    owner_scope,
    validate_session_key,
)

router = APIRouter(prefix="/api")

MEMORY_FILES = (
    ("agent", "MEMORY.md", "Agent 经验记忆", "XPD_MEMORY_CHAR_LIMIT", 2200),
    ("user", "USER.md", "用户画像记忆", "XPD_USER_CHAR_LIMIT", 1375),
)


def _client_scope(
    raw_key: Annotated[str | None, Header(alias=CLIENT_SESSION_KEY_HEADER)] = None,
) -> str:
    return owner_scope(validate_session_key(raw_key))


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


def memory_file_snapshots() -> list[dict]:
    memory_dir = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser() / "memories"
    watermark = _consolidation_ratio()
    snapshots = []

    for store, filename, label, env_name, default_limit in MEMORY_FILES:
        path = memory_dir / filename
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
                "exists": path.exists(),
                "used_chars": used_chars,
                "limit_chars": limit,
                "usage_ratio": used_chars / limit,
                "watermark_ratio": watermark,
                "at_watermark": used_chars >= int(limit * watermark),
                "modified_at": modified_at,
            }
        )

    return snapshots


@router.get("/memories")
async def get_memory_files(_scope: MemoryScope) -> dict:
    return {
        "ok": True,
        "scope": "local_hermes_profile",
        "data": memory_file_snapshots(),
    }
