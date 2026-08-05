from __future__ import annotations

import hmac
import inspect
import os
from functools import wraps
from pathlib import Path
from typing import Any

from xpd_report_agent.memory_paths import (
    IDENTITY_MODE_USER_ID,
    configured_identity_mode,
    merchant_memory_path,
    user_memory_dir,
    validate_owner_scope,
)

ENTRY_DELIMITER = "\n§\n"
MERCHANT_MEMORY_HEADER = (
    "MEMORY (your personal notes) — MERCHANT SHARED MEMORY "
    "(read-only operating rules)"
)


def _env_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _callback_arguments(
    original_create_agent: Any,
    self: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    try:
        bound = inspect.signature(original_create_agent).bind_partial(
            self, *args, **kwargs
        )
        return dict(bound.arguments)
    except (TypeError, ValueError):
        return dict(kwargs)


def _owned_scope(session_id: Any, gateway_session_key: Any) -> str | None:
    try:
        scope = validate_owner_scope(str(gateway_session_key or ""))
    except ValueError:
        return None

    value = str(session_id or "")
    if value.startswith("xpd_"):
        parts = value.split("_", 2)
        if len(parts) < 3 or not hmac.compare_digest(parts[1], scope):
            return None
    return scope


def _bounded_entries(entries: list[str], limit: int) -> list[str]:
    bounded: list[str] = []
    used = 0
    for entry in entries:
        separator_size = len(ENTRY_DELIMITER) if bounded else 0
        available = limit - used - separator_size
        if available <= 0:
            break
        if len(entry) > available:
            if not bounded:
                bounded.append(entry[:available])
            break
        bounded.append(entry)
        used += separator_size + len(entry)
    return bounded


def _merchant_snapshot(store: Any, path: Path) -> str:
    if not _env_enabled("XPD_MERCHANT_MEMORY_ENABLED", True):
        return ""
    limit = max(256, _env_int("XPD_MERCHANT_MEMORY_CHAR_LIMIT", 2200))
    entries = list(dict.fromkeys(store._read_file(path)))
    entries = store._sanitize_entries_for_snapshot(entries, "merchant/MEMORY.md")
    entries = _bounded_entries(entries, limit)
    if not entries:
        return ""
    content = ENTRY_DELIMITER.join(entries)
    percentage = min(100, int((len(content) / limit) * 100))
    separator = "═" * 46
    return (
        f"{separator}\n{MERCHANT_MEMORY_HEADER} "
        f"[{percentage}% — {len(content):,}/{limit:,} chars]\n"
        f"{separator}\n{content}"
    )


def _scoped_store_class(base_store_class: type) -> type:
    class UserScopedMemoryStore(base_store_class):
        def __init__(
            self,
            *args: Any,
            xpd_memory_dir: Path,
            xpd_merchant_memory_path: Path,
            **kwargs: Any,
        ) -> None:
            self._xpd_memory_dir = xpd_memory_dir
            self._xpd_merchant_memory_path = xpd_merchant_memory_path
            self._xpd_merchant_memory_snapshot = ""
            super().__init__(*args, **kwargs)

        def _path_for(self, target: str) -> Path:
            filename = "USER.md" if target == "user" else "MEMORY.md"
            return self._xpd_memory_dir / filename

        def load_from_disk(self) -> None:
            self._xpd_memory_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.memory_entries = self._read_file(self._path_for("memory"))
            self.user_entries = self._read_file(self._path_for("user"))
            self.memory_entries = list(dict.fromkeys(self.memory_entries))
            self.user_entries = list(dict.fromkeys(self.user_entries))

            sanitized_memory = self._sanitize_entries_for_snapshot(
                self.memory_entries, "users/<owner_scope>/MEMORY.md"
            )
            sanitized_user = self._sanitize_entries_for_snapshot(
                self.user_entries, "users/<owner_scope>/USER.md"
            )
            self._system_prompt_snapshot = {
                "memory": self._render_block("memory", sanitized_memory),
                "user": self._render_block("user", sanitized_user),
            }
            self._xpd_merchant_memory_snapshot = _merchant_snapshot(
                self, self._xpd_merchant_memory_path
            )

        def save_to_disk(self, target: str) -> None:
            self._xpd_memory_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._write_file(self._path_for(target), self._entries_for(target))

        def format_for_system_prompt(self, target: str) -> str | None:
            personal = super().format_for_system_prompt(target)
            if target != "memory" or not self._xpd_merchant_memory_snapshot:
                return personal
            if personal:
                return f"{self._xpd_merchant_memory_snapshot}\n\n{personal}"
            return self._xpd_merchant_memory_snapshot

    UserScopedMemoryStore.__name__ = "XPDUserScopedMemoryStore"
    return UserScopedMemoryStore


def _replace_agent_store(agent: Any, scope: str, scoped_store_class: type) -> None:
    previous = getattr(agent, "_memory_store", None)
    if previous is None:
        return
    store = scoped_store_class(
        memory_char_limit=getattr(previous, "memory_char_limit", 2200),
        user_char_limit=getattr(previous, "user_char_limit", 1375),
        xpd_memory_dir=user_memory_dir(scope),
        xpd_merchant_memory_path=merchant_memory_path(),
    )
    store.load_from_disk()
    store._xpd_owner_scope = scope
    agent._memory_store = store


def install_patch() -> None:
    """Route Hermes built-in memory files by authenticated owner scope."""
    from gateway.platforms import api_server as api_server_module
    from tools.memory_tool import MemoryStore

    APIServerAdapter = api_server_module.APIServerAdapter
    if getattr(APIServerAdapter, "_xpd_user_memory_patch", False):
        return

    original_create_agent = APIServerAdapter._create_agent
    scoped_store_class = _scoped_store_class(MemoryStore)

    @wraps(original_create_agent)
    def create_agent_with_user_memory(
        self: Any, *args: Any, **kwargs: Any
    ) -> Any:
        mode = configured_identity_mode()
        if mode == "session_key":
            return original_create_agent(self, *args, **kwargs)
        if mode != IDENTITY_MODE_USER_ID:
            raise RuntimeError(
                "XPD_IDENTITY_MODE must be either 'session_key' or 'user_id'"
            )

        arguments = _callback_arguments(original_create_agent, self, args, kwargs)
        session_id = arguments.get("session_id")
        scope = _owned_scope(session_id, arguments.get("gateway_session_key"))
        if scope is None:
            raise PermissionError(
                "user_id memory mode requires a valid owner scope that matches session_id"
            )

        agent = original_create_agent(self, *args, **kwargs)
        _replace_agent_store(agent, scope, scoped_store_class)
        return agent

    APIServerAdapter._create_agent = create_agent_with_user_memory
    APIServerAdapter._xpd_user_memory_patch = True
