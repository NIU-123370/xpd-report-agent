from __future__ import annotations

import os
import re
from pathlib import Path

IDENTITY_MODE_ENV = "XPD_IDENTITY_MODE"
IDENTITY_MODE_SESSION_KEY = "session_key"
IDENTITY_MODE_USER_ID = "user_id"
OWNER_SCOPE_PATTERN = re.compile(r"[0-9a-f]{20}")


def configured_identity_mode() -> str:
    return os.getenv(IDENTITY_MODE_ENV, IDENTITY_MODE_SESSION_KEY).strip().lower()


def user_memory_mode_enabled() -> bool:
    return configured_identity_mode() == IDENTITY_MODE_USER_ID


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()


def memories_root() -> Path:
    return hermes_home() / "memories"


def validate_owner_scope(scope: str | None) -> str:
    value = str(scope or "")
    if not OWNER_SCOPE_PATTERN.fullmatch(value):
        raise ValueError("owner scope must contain exactly 20 lowercase hex characters")
    return value


def user_memory_dir(scope: str) -> Path:
    safe_scope = validate_owner_scope(scope)
    return memories_root() / "users" / safe_scope


def merchant_memory_path() -> Path:
    return memories_root() / "merchant" / "MEMORY.md"


def local_memory_dir() -> Path:
    return memories_root()
