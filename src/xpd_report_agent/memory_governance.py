from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from xpd_report_agent.memory_paths import (
    OWNER_SCOPE_PATTERN,
    configured_identity_mode,
    local_memory_dir,
    memories_root,
    user_memory_dir,
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class MemoryPolicy:
    trigger_ratio: float
    critical_ratio: float
    target_ratio: float


@dataclass(frozen=True)
class MemoryState:
    target: str
    filename: str
    path: Path
    used_chars: int
    limit_chars: int
    usage_ratio: float
    at_trigger: bool
    at_critical: bool
    write_policy: str

    def public(self) -> dict[str, object]:
        result = asdict(self)
        result["path"] = str(self.path)
        return result


def memory_policy() -> MemoryPolicy:
    trigger = min(
        0.94,
        max(0.5, _env_float("XPD_MEMORY_CONSOLIDATION_RATIO", 0.8)),
    )
    critical = min(
        1.0,
        max(trigger + 0.01, _env_float("XPD_MEMORY_CRITICAL_RATIO", 0.95)),
    )
    target = min(
        trigger - 0.05,
        max(0.2, _env_float("XPD_MEMORY_CONSOLIDATION_TARGET_RATIO", 0.6)),
    )
    return MemoryPolicy(
        trigger_ratio=trigger,
        critical_ratio=critical,
        target_ratio=target,
    )


def personal_memory_dir(scope: str) -> Path:
    if configured_identity_mode() == "user_id":
        return user_memory_dir(scope)
    return local_memory_dir()


def personal_memory_states(scope: str) -> list[MemoryState]:
    directory = personal_memory_dir(scope)
    policy = memory_policy()
    specs = (
        (
            "memory",
            "MEMORY.md",
            max(256, _env_int("XPD_MEMORY_CHAR_LIMIT", 2200)),
        ),
        (
            "user",
            "USER.md",
            max(256, _env_int("XPD_USER_CHAR_LIMIT", 1375)),
        ),
    )
    states: list[MemoryState] = []
    for target, filename, limit in specs:
        path = directory / filename
        try:
            used = len(path.read_text(encoding="utf-8")) if path.exists() else 0
        except (OSError, UnicodeError):
            used = 0
        ratio = used / limit
        at_critical = ratio >= policy.critical_ratio
        at_trigger = ratio >= policy.trigger_ratio
        states.append(
            MemoryState(
                target=target,
                filename=filename,
                path=path,
                used_chars=used,
                limit_chars=limit,
                usage_ratio=ratio,
                at_trigger=at_trigger,
                at_critical=at_critical,
                write_policy=(
                    "consolidate_only"
                    if at_critical
                    else "write_and_consolidate"
                    if at_trigger
                    else "normal"
                ),
            )
        )
    return states


def discover_personal_memory_scopes() -> list[str]:
    if configured_identity_mode() != "user_id":
        return []
    users_dir = memories_root() / "users"
    try:
        children = list(users_dir.iterdir()) if users_dir.exists() else []
    except OSError:
        return []
    return sorted(
        child.name
        for child in children
        if child.is_dir() and OWNER_SCOPE_PATTERN.fullmatch(child.name)
    )


def backup_personal_memory(scope: str, *, keep: int = 5) -> list[str]:
    """Snapshot current personal memory files before semantic consolidation."""

    backup_paths: list[str] = []
    timestamp = time.time_ns()
    for state in personal_memory_states(scope):
        path = state.path
        if not path.exists():
            continue
        backup = path.with_name(f"{path.name}.consolidation-{timestamp}.bak")
        shutil.copy2(path, backup)
        backup_paths.append(str(backup))
        backups = sorted(
            path.parent.glob(f"{path.name}.consolidation-*.bak"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale in backups[max(1, keep) :]:
            try:
                stale.unlink()
            except OSError:
                pass
    return backup_paths


def memory_add_is_paused(store: object, target: str) -> tuple[bool, int, int]:
    """Return the hard write policy using the store's live on-disk file."""

    try:
        path = store._path_for(target)  # type: ignore[attr-defined]
        limit = int(store._char_limit(target))  # type: ignore[attr-defined]
        used = len(path.read_text(encoding="utf-8")) if path.exists() else 0
    except (AttributeError, OSError, TypeError, ValueError, UnicodeError):
        return False, 0, 0
    return used >= int(limit * memory_policy().critical_ratio), used, limit
