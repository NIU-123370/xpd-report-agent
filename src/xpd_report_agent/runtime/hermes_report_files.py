from __future__ import annotations

import hmac
import inspect
import json
import os
import re
import threading
from functools import wraps
from pathlib import Path
from typing import Any

FILE_TOOL_NAMES = frozenset({"read_file", "write_file", "patch", "search_files"})
UNSAFE_FILE_TOOL_NAMES = frozenset({"write_file", "patch", "search_files"})
REPORT_EXPORT_TOOL_NAME = "export_report_file"
SESSION_ID_PATTERN = re.compile(r"xpd_[0-9a-f]{20}_[A-Za-z0-9_]+")
ARTIFACT_FILENAME_PATTERN = re.compile(
    r"art_[0-9a-f]{32}__[^/\\]+\.(?:csv|xlsx|md|pdf|json)", re.I
)

_registry_patch_lock = threading.RLock()


def _storage_root() -> Path | None:
    configured = os.getenv("XPD_FILE_STORAGE_PATH", "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    if not path.is_absolute():
        return None
    return path.resolve()


def _storage_health() -> tuple[Path | None, str | None]:
    root = _storage_root()
    if root is None:
        return None, "XPD_FILE_STORAGE_PATH must be an absolute path."
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
            return root, "Report file storage is not writable."
    except OSError as exc:
        return root, f"Report file storage is unavailable: {exc}"
    return root, None


def _owned_session_id(value: Any) -> str | None:
    session_id = str(value or "")
    if not SESSION_ID_PATTERN.fullmatch(session_id) or "_reflection_" in session_id:
        return None
    return session_id


def _session_exports_dir(session_id: str) -> Path | None:
    root = _storage_root()
    if root is None:
        return None
    session_candidate = root / session_id
    if session_candidate.is_symlink():
        return None
    session_root = session_candidate.resolve()
    if session_root.parent != root:
        return None
    exports_candidate = session_root / "exports"
    if exports_candidate.is_symlink():
        return None
    exports_dir = exports_candidate.resolve()
    if exports_dir.parent != session_root:
        return None
    return exports_dir


def _safe_read_path(raw_path: Any, session_id: str) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    exports_dir = _session_exports_dir(session_id)
    if exports_dir is None:
        return None
    requested = Path(raw_path)
    # The exporter returns a basename. Reject absolute, nested, traversal and
    # symlink paths instead of trying to normalize them into the safe root.
    if requested.is_absolute() or requested.name != raw_path or requested.is_symlink():
        return None
    if exports_dir.is_symlink():
        return None
    candidate = exports_dir / requested
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(exports_dir)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    if not ARTIFACT_FILENAME_PATTERN.fullmatch(resolved.name):
        return None
    return resolved


def _tool_name(tool: Any) -> str | None:
    if not isinstance(tool, dict):
        return None
    function = tool.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return None


def _restrict_agent_tools(agent: Any, session_id: str | None) -> None:
    blocked = set(UNSAFE_FILE_TOOL_NAMES)
    if session_id is None:
        blocked.update({"read_file", REPORT_EXPORT_TOOL_NAME})
    tools = getattr(agent, "tools", None)
    if isinstance(tools, list):
        agent.tools = [tool for tool in tools if _tool_name(tool) not in blocked]
    valid_tool_names = getattr(agent, "valid_tool_names", None)
    if hasattr(valid_tool_names, "discard"):
        for name in blocked:
            valid_tool_names.discard(name)


def _callback_arguments(
    original_create_agent: Any,
    self: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    try:
        bound = inspect.signature(original_create_agent).bind_partial(self, *args, **kwargs)
        return dict(bound.arguments)
    except (TypeError, ValueError):
        return dict(kwargs)


def _owned_session_for_scope(session_id: Any, owner_scope: Any) -> str | None:
    safe_session_id = _owned_session_id(session_id)
    scope = str(owner_scope or "")
    if safe_session_id is None or not re.fullmatch(r"[0-9a-f]{20}", scope):
        return None
    embedded_scope = safe_session_id.split("_", 2)[1]
    if not hmac.compare_digest(embedded_scope, scope):
        return None
    return safe_session_id


def _patch_registry_handlers() -> None:
    """Fail closed even if a hidden native file tool is reached indirectly."""
    from tools.registry import registry

    with _registry_patch_lock:
        for tool_name in FILE_TOOL_NAMES:
            entry = registry.get_entry(tool_name)
            if entry is None or getattr(entry.handler, "_xpd_report_file_guard", False):
                continue
            original_handler = entry.handler

            if tool_name == "read_file":

                @wraps(original_handler)
                def guarded_read(args: dict[str, Any], _original=original_handler, **kwargs: Any):
                    session_id = _owned_session_id(
                        kwargs.get("session_id")
                    ) or _owned_session_id(kwargs.get("task_id"))
                    if session_id is None:
                        return _original(args, **kwargs)
                    resolved = _safe_read_path((args or {}).get("path"), session_id)
                    if resolved is None:
                        return json.dumps(
                            {
                                "error": (
                                    "read_file is restricted to report files generated "
                                    "for the current session."
                                )
                            },
                            ensure_ascii=False,
                        )
                    safe_args = dict(args or {})
                    safe_args["path"] = str(resolved)
                    return _original(safe_args, **kwargs)

                guarded_read._xpd_report_file_guard = True  # type: ignore[attr-defined]
                entry.handler = guarded_read
                continue

            @wraps(original_handler)
            def blocked_write(args: dict[str, Any], _original=original_handler, **kwargs: Any):
                if _owned_session_id(kwargs.get("session_id")) or _owned_session_id(
                    kwargs.get("task_id")
                ):
                    return json.dumps(
                        {
                            "error": (
                                "This API session is read-only for native file tools. "
                                "Use export_report_file for CSV, XLSX, Markdown, PDF, or JSON reports."
                            )
                        },
                        ensure_ascii=False,
                    )
                return _original(args, **kwargs)

            blocked_write._xpd_report_file_guard = True  # type: ignore[attr-defined]
            entry.handler = blocked_write


def install_patch() -> None:
    """Expose only session-scoped native file reads on the Hermes API Server."""
    from gateway.platforms import api_server as api_server_module

    APIServerAdapter = api_server_module.APIServerAdapter
    web = api_server_module.web
    if getattr(APIServerAdapter, "_xpd_report_file_patch", False):
        return

    original_create_agent = APIServerAdapter._create_agent
    original_route_table = APIServerAdapter._http_route_table

    @wraps(original_create_agent)
    def create_agent_with_report_files(self: Any, *args: Any, **kwargs: Any) -> Any:
        agent = original_create_agent(self, *args, **kwargs)
        _patch_registry_handlers()
        arguments = _callback_arguments(original_create_agent, self, args, kwargs)
        raw_session_id = arguments.get("session_id") or getattr(agent, "session_id", None)
        owned_session_id = _owned_session_for_scope(
            raw_session_id,
            arguments.get("gateway_session_key"),
        )
        _restrict_agent_tools(agent, owned_session_id)
        return agent

    async def handle_report_file_health(self: Any, request: Any) -> Any:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        root, storage_error = _storage_health()
        from xpd_report_agent.hermes_plugin.db_query.report_oss import (
            report_oss_health,
        )

        oss_health = report_oss_health()
        return web.json_response(
            {
                "ok": (
                    root is not None
                    and storage_error is None
                    and oss_health.get("ok")
                ),
                "enabled": True,
                "native_read_tool": "read_file",
                "export_tool": REPORT_EXPORT_TOOL_NAME,
                "scope": "current_session_generated_reports_only",
                "storage_configured": root is not None,
                "storage_writable": storage_error is None,
                "oss": oss_health,
                "error": storage_error or oss_health.get("error"),
            }
        )

    @wraps(original_route_table)
    def route_table_with_report_file_health(self: Any):
        routes = list(original_route_table(self))
        paths = {(method, path) for method, path, _ in routes}
        route = ("GET", "/api/report-files/health")
        if route not in paths:
            routes.append((*route, self._xpd_handle_report_file_health))
        return routes

    APIServerAdapter._create_agent = create_agent_with_report_files
    APIServerAdapter._http_route_table = route_table_with_report_file_health
    APIServerAdapter._xpd_handle_report_file_health = handle_report_file_health
    APIServerAdapter._xpd_report_file_patch = True
