from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPORT_OSS_ENABLED_ENV = "XPD_REPORT_OSS_ENABLED"
REPORT_OSS_ENDPOINT_ENV = "XPD_REPORT_OSS_ENDPOINT"
REPORT_OSS_REGION_ENV = "XPD_REPORT_OSS_REGION"
REPORT_OSS_BUCKET_ENV = "XPD_REPORT_OSS_BUCKET"
REPORT_OSS_PREFIX_ENV = "XPD_REPORT_OSS_PREFIX"
REPORT_OSS_ACCESS_KEY_ID_ENV = "XPD_REPORT_OSS_ACCESS_KEY_ID"
REPORT_OSS_ACCESS_KEY_SECRET_ENV = "XPD_REPORT_OSS_ACCESS_KEY_SECRET"
REPORT_OSS_SECURITY_TOKEN_ENV = "XPD_REPORT_OSS_SECURITY_TOKEN"
REPORT_OSS_DOWNLOAD_EXPIRES_ENV = "XPD_REPORT_OSS_DOWNLOAD_EXPIRES_SECONDS"

DEFAULT_REPORT_OSS_BUCKET = "starpartner-biz"
DEFAULT_REPORT_OSS_PREFIX = "public/dev/agent-report-files"
DEFAULT_DOWNLOAD_EXPIRES_SECONDS = 3600
MAX_DOWNLOAD_EXPIRES_SECONDS = 7 * 24 * 60 * 60

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_REGION_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
_ENDPOINT_REGION_PATTERN = re.compile(
    r"(?:^|\.)oss-([a-z0-9-]+?)(?:-internal)?\.aliyuncs\.com$"
)
_SESSION_ID_PATTERN = re.compile(r"xpd_[0-9a-f]{20}_[A-Za-z0-9_]+")
_ARTIFACT_ID_PATTERN = re.compile(r"art_[0-9a-f]{32}")
_REPORT_CONTEXT_FILENAME = ".report-oss-context.json"
_DEFAULT_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class ReportOssConfig:
    enabled: bool
    endpoint: str | None
    region: str | None
    bucket: str
    prefix: str
    access_key_id: str | None
    access_key_secret: str | None
    security_token: str | None
    download_expires_seconds: int


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _configured_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean value.")


def _download_expiry() -> int:
    try:
        configured = int(
            os.getenv(
                REPORT_OSS_DOWNLOAD_EXPIRES_ENV,
                str(DEFAULT_DOWNLOAD_EXPIRES_SECONDS),
            )
        )
    except (TypeError, ValueError):
        configured = DEFAULT_DOWNLOAD_EXPIRES_SECONDS
    return max(60, min(MAX_DOWNLOAD_EXPIRES_SECONDS, configured))


def _normalize_endpoint(value: str | None) -> str | None:
    if value is None:
        return None
    endpoint = value.strip().rstrip("/")
    if not endpoint.startswith(("https://", "http://")):
        raise ValueError("Report OSS endpoint must use HTTP or HTTPS.")
    return endpoint


def _region_from_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    hostname = endpoint.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    match = _ENDPOINT_REGION_PATTERN.search(hostname)
    return match.group(1) if match else None


def _normalize_prefix(value: str) -> str:
    prefix = value.strip().strip("/")
    parts = prefix.split("/") if prefix else []
    if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
        raise ValueError("Report OSS prefix is invalid.")
    return "/".join(parts)


def report_oss_config() -> ReportOssConfig:
    endpoint = _normalize_endpoint(
        _first_env(REPORT_OSS_ENDPOINT_ENV, "XPD_OSS_ENDPOINT")
    )
    access_key_id = _first_env(
        REPORT_OSS_ACCESS_KEY_ID_ENV,
        "XPD_OSS_ACCESS_KEY_ID",
        "OSS_ACCESS_KEY_ID",
    )
    access_key_secret = _first_env(
        REPORT_OSS_ACCESS_KEY_SECRET_ENV,
        "XPD_OSS_ACCESS_KEY_SECRET",
        "OSS_ACCESS_KEY_SECRET",
    )
    security_token = _first_env(
        REPORT_OSS_SECURITY_TOKEN_ENV,
        "XPD_OSS_SECURITY_TOKEN",
        "OSS_SESSION_TOKEN",
    )
    auto_enabled = bool(endpoint and access_key_id and access_key_secret)
    enabled = _configured_bool(REPORT_OSS_ENABLED_ENV, default=auto_enabled)

    bucket = os.getenv(REPORT_OSS_BUCKET_ENV, DEFAULT_REPORT_OSS_BUCKET).strip()
    prefix = _normalize_prefix(
        os.getenv(REPORT_OSS_PREFIX_ENV, DEFAULT_REPORT_OSS_PREFIX)
    )
    region = _first_env(REPORT_OSS_REGION_ENV) or _region_from_endpoint(endpoint)

    if enabled:
        if endpoint is None:
            raise ValueError("Report OSS endpoint is required when OSS upload is enabled.")
        if region is None or not _REGION_PATTERN.fullmatch(region):
            raise ValueError(
                "Report OSS region is required or must be derivable from the endpoint."
            )
        if not _BUCKET_PATTERN.fullmatch(bucket):
            raise ValueError("Report OSS bucket is invalid.")
        if not access_key_id or not access_key_secret:
            raise ValueError("Report OSS access credentials are required.")

    return ReportOssConfig(
        enabled=enabled,
        endpoint=endpoint,
        region=region,
        bucket=bucket,
        prefix=prefix,
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        security_token=security_token,
        download_expires_seconds=_download_expiry(),
    )


def _oss_module():
    try:
        import alibabacloud_oss_v2 as oss
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Alibaba Cloud OSS SDK V2 is not installed in this runtime."
        ) from exc
    return oss


def _client(config: ReportOssConfig):
    oss = _oss_module()
    credentials = oss.credentials.StaticCredentialsProvider(
        config.access_key_id or "",
        config.access_key_secret or "",
        config.security_token,
    )
    sdk_config = oss.config.load_default()
    sdk_config.credentials_provider = credentials
    sdk_config.region = config.region
    sdk_config.endpoint = config.endpoint
    sdk_config.connect_timeout = 10
    sdk_config.readwrite_timeout = 30
    sdk_config.retry_max_attempts = 2
    return oss, oss.Client(sdk_config)


def _metadata_path(path: Path, artifact_id: str) -> Path:
    return path.with_name(f".{artifact_id}.oss.json")


def _safe_object_component(value: Any, *, fallback: str, limit: int = 100) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "").strip())
    safe = "".join(
        character
        if character.isalnum() or character in {"-", "_", ".", "@"}
        else "_"
        for character in normalized
    )
    safe = re.sub(r"_+", "_", safe).strip("._-")
    return (safe or fallback)[:limit].rstrip("._-") or fallback


def _report_timezone() -> ZoneInfo:
    name = os.getenv("HERMES_TIMEZONE", _DEFAULT_TIMEZONE).strip() or _DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(_DEFAULT_TIMEZONE)


def _report_context_path(session_id: str) -> Path:
    if not _SESSION_ID_PATTERN.fullmatch(session_id) or "_reflection_" in session_id:
        raise ValueError("Report OSS context requires an owned xpd session.")
    configured = os.getenv("XPD_FILE_STORAGE_PATH", "").strip()
    root = Path(configured).expanduser()
    if not configured or not root.is_absolute():
        raise ValueError("XPD_FILE_STORAGE_PATH must be an absolute path.")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    session_candidate = root / session_id
    if session_candidate.is_symlink():
        raise ValueError("Invalid report OSS context path.")
    session_root = session_candidate.resolve()
    if session_root.parent != root:
        raise ValueError("Invalid report OSS context path.")
    session_root.mkdir(exist_ok=True, mode=0o700)
    os.chmod(session_root, 0o700)
    return session_root / _REPORT_CONTEXT_FILENAME


def write_report_oss_context(
    session_id: str,
    *,
    uid: str,
    trace_id: str,
) -> None:
    """Persist request identity for the separate Hermes export process."""

    path = _report_context_path(session_id)
    payload = {
        "version": 1,
        "session_id": session_id,
        "uid": _safe_object_component(uid, fallback=session_id.split("_", 2)[1]),
        "trace_id": _safe_object_component(trace_id, fallback="trace"),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _report_oss_context(session_id: str, artifact_id: str) -> tuple[str, str]:
    owner_scope = session_id.split("_", 2)[1]
    fallback_trace_id = artifact_id.removeprefix("art_")
    try:
        payload = json.loads(
            _report_context_path(session_id).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return owner_scope, fallback_trace_id
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("session_id") != session_id
    ):
        return owner_scope, fallback_trace_id
    return (
        _safe_object_component(payload.get("uid"), fallback=owner_scope),
        _safe_object_component(payload.get("trace_id"), fallback=fallback_trace_id),
    )


def _object_key(
    config: ReportOssConfig,
    *,
    session_id: str,
    artifact_id: str,
    filename: str,
    now: datetime | None = None,
) -> str:
    uid, trace_id = _report_oss_context(session_id, artifact_id)
    if not _ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
        raise ValueError("Report OSS artifact id is invalid.")
    extension = Path(filename).suffix.lower()
    if not extension or not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
        raise ValueError("Report OSS filename extension is invalid.")
    timezone = _report_timezone()
    timestamp = now or datetime.now(timezone)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone)
    else:
        timestamp = timestamp.astimezone(timezone)
    day_directory = timestamp.strftime("%Y%m%d")
    unix_timestamp_seconds = int(timestamp.timestamp())
    object_filename = f"{uid}-{trace_id}-{unix_timestamp_seconds}{extension}"
    return "/".join((config.prefix, day_directory, object_filename))


def _presigned_download(
    config: ReportOssConfig,
    *,
    bucket: str,
    key: str,
    filename: str,
) -> dict[str, Any]:
    oss, client = _client(config)
    disposition = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
    result = client.presign(
        oss.GetObjectRequest(
            bucket=bucket,
            key=key,
            response_content_disposition=disposition,
        ),
        expires=timedelta(seconds=config.download_expires_seconds),
    )
    expiration = result.expiration
    if isinstance(expiration, datetime):
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=UTC)
        expires_at = expiration.isoformat()
    else:
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=config.download_expires_seconds)
        ).isoformat()
    return {
        "download_url": result.url,
        "download_url_expires_at": expires_at,
    }


def upload_report_artifact(
    path: Path,
    *,
    session_id: str,
    artifact_id: str,
    filename: str,
    media_type: str,
) -> dict[str, Any] | None:
    """Upload one generated report and persist only non-secret OSS metadata."""

    config = report_oss_config()
    if not config.enabled:
        return None
    oss, client = _client(config)
    key = _object_key(
        config,
        session_id=session_id,
        artifact_id=artifact_id,
        filename=filename,
    )
    disposition = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
    result = client.put_object_from_file(
        oss.PutObjectRequest(
            bucket=config.bucket,
            key=key,
            content_type=media_type,
            content_disposition=disposition,
            forbid_overwrite=True,
        ),
        str(path),
    )
    status_code = int(getattr(result, "status_code", 0) or 0)
    if not 200 <= status_code < 300:
        raise RuntimeError(f"Report OSS upload failed with HTTP {status_code}.")

    metadata = {
        "version": 1,
        "artifact_id": artifact_id,
        "session_id": session_id,
        "bucket": config.bucket,
        "key": key,
        "etag": str(getattr(result, "etag", "") or "") or None,
        "uploaded_at": datetime.now(UTC).isoformat(),
    }
    metadata_path = _metadata_path(path, artifact_id)
    temporary = metadata_path.with_suffix(metadata_path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(metadata_path)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "storage": "oss",
        "oss_uri": f"oss://{config.bucket}/{key}",
        "object_key": key,
        **_presigned_download(
            config,
            bucket=config.bucket,
            key=key,
            filename=filename,
        ),
    }


def remote_artifact_payload(
    path: Path,
    *,
    session_id: str,
    artifact_id: str,
    filename: str,
) -> dict[str, Any] | None:
    metadata_path = _metadata_path(path, artifact_id)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(metadata, dict)
        or metadata.get("version") != 1
        or metadata.get("artifact_id") != artifact_id
        or metadata.get("session_id") != session_id
    ):
        return None
    bucket = str(metadata.get("bucket") or "")
    key = str(metadata.get("key") or "")
    if not _BUCKET_PATTERN.fullmatch(bucket) or not key:
        return None
    try:
        config = report_oss_config()
        if not config.enabled:
            return None
        signed = _presigned_download(
            config,
            bucket=bucket,
            key=key,
            filename=filename,
        )
    except Exception:
        return None
    return {
        "storage": "oss",
        "oss_uri": f"oss://{bucket}/{key}",
        "object_key": key,
        **signed,
    }


def report_oss_health() -> dict[str, Any]:
    try:
        config = report_oss_config()
        sdk_available = True
        if config.enabled:
            _oss_module()
    except Exception as exc:
        return {
            "ok": False,
            "enabled": True,
            "configured": False,
            "bucket": DEFAULT_REPORT_OSS_BUCKET,
            "prefix": DEFAULT_REPORT_OSS_PREFIX,
            "sdk_available": False,
            "error": str(exc),
        }
    return {
        "ok": True,
        "enabled": config.enabled,
        "configured": config.enabled,
        "bucket": config.bucket,
        "prefix": config.prefix,
        "sdk_available": sdk_available,
        "error": None,
    }
