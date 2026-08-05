from __future__ import annotations

import mimetypes
import os
import re
import shutil
from pathlib import Path
from typing import Any

from xpd_report_agent.hermes_plugin.db_query.report_oss import (
    remote_artifact_payload,
)
from xpd_report_agent.paths import PROJECT_ROOT

ARTIFACT_ID_PATTERN = re.compile(r"art_[0-9a-f]{32}")
SESSION_ID_PATTERN = re.compile(r"xpd_[0-9a-f]{20}_[A-Za-z0-9_]+")
ARTIFACT_FILENAME_SEPARATOR = "__"
SUPPORTED_ARTIFACT_EXTENSIONS = frozenset({".csv", ".xlsx", ".md", ".pdf", ".json"})

MEDIA_TYPES = {
    ".csv": "text/csv; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".md": "text/markdown; charset=utf-8",
    ".pdf": "application/pdf",
    ".json": "application/json; charset=utf-8",
}


def artifact_storage_root() -> Path:
    configured = os.getenv("XPD_FILE_STORAGE_PATH", "").strip()
    path = Path(configured).expanduser() if configured else PROJECT_ROOT / "data" / "report-files"
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _validate_session_id(session_id: str) -> str:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("Invalid report artifact session id.")
    return session_id


def _session_root(session_id: str) -> Path:
    safe_session_id = _validate_session_id(session_id)
    root = artifact_storage_root()
    candidate = root / safe_session_id
    if candidate.is_symlink():
        raise ValueError("Invalid report artifact session path.")
    session_root = candidate.resolve()
    if session_root.parent != root:
        raise ValueError("Invalid report artifact session path.")
    return session_root


def session_exports_dir(session_id: str, *, create: bool = False) -> Path:
    session_root = _session_root(session_id)
    candidate = session_root / "exports"
    if candidate.is_symlink():
        raise ValueError("Invalid report artifact export path.")
    exports_dir = candidate.resolve()
    if exports_dir.parent != session_root:
        raise ValueError("Invalid report artifact export path.")
    if create:
        root = artifact_storage_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        session_root.mkdir(exist_ok=True, mode=0o700)
        os.chmod(session_root, 0o700)
        exports_dir.mkdir(exist_ok=True, mode=0o700)
        os.chmod(exports_dir, 0o700)
    return exports_dir


def _parse_artifact_path(path: Path) -> tuple[str, str] | None:
    if path.is_symlink() or not path.is_file():
        return None
    artifact_id, separator, filename = path.name.partition(ARTIFACT_FILENAME_SEPARATOR)
    if not separator or not ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
        return None
    if not filename or Path(filename).name != filename:
        return None
    if path.suffix.lower() not in SUPPORTED_ARTIFACT_EXTENSIONS:
        return None
    return artifact_id, filename


def artifact_payload(
    session_id: str,
    path: Path,
    *,
    include_remote_download: bool = False,
) -> dict[str, Any] | None:
    parsed = _parse_artifact_path(path)
    if parsed is None:
        return None
    artifact_id, filename = parsed
    stat = path.stat()
    suffix = path.suffix.lower()
    payload = {
        "artifact_id": artifact_id,
        "session_id": session_id,
        "filename": filename,
        "format": suffix.removeprefix("."),
        "media_type": MEDIA_TYPES.get(suffix) or mimetypes.guess_type(filename)[0],
        "size_bytes": stat.st_size,
        "created_at": stat.st_mtime,
        "download_url": (
            f"/api/sessions/{session_id}/artifacts/{artifact_id}/download"
        ),
    }
    remote = remote_artifact_payload(
        path,
        session_id=session_id,
        artifact_id=artifact_id,
        filename=filename,
    )
    if remote:
        # Artifact lists and SSE events must expose a stable, authenticated
        # service URL. OSS signatures are intentionally short-lived and are
        # minted only when that service URL is clicked.
        signed_download_url = remote.pop("download_url", None)
        signed_download_expires_at = remote.pop("download_url_expires_at", None)
        payload.update(remote)
        if include_remote_download and isinstance(signed_download_url, str):
            payload["_remote_download_url"] = signed_download_url
            payload["_remote_download_url_expires_at"] = signed_download_expires_at
    return payload


def list_session_artifacts(session_id: str) -> list[dict[str, Any]]:
    exports_dir = session_exports_dir(session_id)
    if not exports_dir.exists():
        return []
    artifacts = []
    for path in exports_dir.iterdir():
        payload = artifact_payload(session_id, path)
        if payload is not None:
            artifacts.append(payload)
    artifacts.sort(key=lambda item: (item["created_at"], item["artifact_id"]))
    return artifacts


def resolve_session_artifact(session_id: str, artifact_id: str) -> tuple[Path, dict[str, Any]]:
    if not ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
        raise FileNotFoundError("Report artifact not found.")
    exports_dir = session_exports_dir(session_id)
    if not exports_dir.exists():
        raise FileNotFoundError("Report artifact not found.")
    matches = [
        path
        for path in exports_dir.iterdir()
        if path.name.startswith(f"{artifact_id}{ARTIFACT_FILENAME_SEPARATOR}")
    ]
    if len(matches) != 1:
        raise FileNotFoundError("Report artifact not found.")
    path = matches[0]
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(exports_dir.resolve())
    except (OSError, ValueError) as exc:
        raise FileNotFoundError("Report artifact not found.") from exc
    payload = artifact_payload(
        session_id,
        resolved,
        include_remote_download=True,
    )
    if payload is None:
        raise FileNotFoundError("Report artifact not found.")
    return resolved, payload


def delete_session_artifacts(session_id: str) -> None:
    session_root = _session_root(session_id)
    if session_root.exists():
        shutil.rmtree(session_root)
