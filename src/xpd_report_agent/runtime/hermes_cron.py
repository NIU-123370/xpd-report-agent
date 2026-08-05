from __future__ import annotations

import inspect
import os
import re
import time
from functools import wraps
from pathlib import Path
from typing import Any

SCHEDULE_ID_PATTERN = re.compile(r"sched_[0-9a-f]{32}")
SCRIPT_NAME_PATTERN = re.compile(r"xpd_scheduled_report_sched_[0-9a-f]{32}\.py")


def _scripts_dir() -> Path:
    from hermes_constants import get_hermes_home

    return (get_hermes_home() / "scripts").resolve()


def _ticker_health() -> dict[str, Any]:
    from cron.jobs import TICKER_HEARTBEAT_FILE, TICKER_INTERVAL_SECONDS

    heartbeat_age = None
    alive = False
    try:
        heartbeat_age = max(0.0, time.time() - TICKER_HEARTBEAT_FILE.stat().st_mtime)
        alive = heartbeat_age <= max(180, TICKER_INTERVAL_SECONDS * 3)
    except OSError:
        pass
    return {
        "ticker_alive": alive,
        "ticker_interval_seconds": TICKER_INTERVAL_SECONDS,
        "ticker_heartbeat_age_seconds": heartbeat_age,
    }


def install_patch() -> None:
    """Add a narrow native-cron creation bridge to Hermes API Server.

    Hermes' shipped ``/api/jobs`` endpoint intentionally does not accept
    ``script``/``no_agent``. Scheduled reports need no-agent jobs so the cron
    ticker only invokes the tenant-safe FastAPI callback and never analyzes in
    a global ``cron_*`` session. This route exposes exactly that one use case.
    """

    from gateway.platforms import api_server as api_server_module

    APIServerAdapter = api_server_module.APIServerAdapter
    web = api_server_module.web
    if getattr(APIServerAdapter, "_xpd_cron_patch", False):
        return

    original_route_table = APIServerAdapter._http_route_table

    async def handle_xpd_cron_health(self: Any, request: Any) -> Any:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            from cron.jobs import list_jobs

            native_jobs = list_jobs(include_disabled=True)
            xpd_jobs = [
                job
                for job in native_jobs
                if str(job.get("name") or "").startswith("xpd-report:")
            ]
            return web.json_response(
                {
                    "ok": True,
                    "enabled": True,
                    "native": True,
                    "timezone": os.getenv("HERMES_TIMEZONE", "Asia/Shanghai"),
                    "xpd_job_count": len(xpd_jobs),
                    **_ticker_health(),
                }
            )
        except Exception as exc:
            return web.json_response(
                {"ok": False, "enabled": True, "error": str(exc)[:500]},
                status=500,
            )

    async def handle_create_xpd_cron_job(self: Any, request: Any) -> Any:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body."}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "Invalid JSON body."}, status=400)

        schedule_id = str(body.get("schedule_id") or "")
        name = str(body.get("name") or "").strip()
        schedule = str(body.get("schedule") or "").strip()
        script_name = str(body.get("script") or "").strip()
        if not SCHEDULE_ID_PATTERN.fullmatch(schedule_id):
            return web.json_response({"error": "Invalid schedule_id."}, status=400)
        if not name.startswith("xpd-report:") or len(name) > 200:
            return web.json_response({"error": "Invalid XPD cron job name."}, status=400)
        if not schedule or len(schedule) > 100:
            return web.json_response({"error": "Invalid cron schedule."}, status=400)
        expected_script = f"xpd_scheduled_report_{schedule_id}.py"
        if (
            script_name != expected_script
            or not SCRIPT_NAME_PATTERN.fullmatch(script_name)
        ):
            return web.json_response({"error": "Invalid XPD callback script."}, status=400)

        scripts_dir = _scripts_dir()
        candidate = scripts_dir / script_name
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(scripts_dir)
        except (OSError, ValueError):
            return web.json_response({"error": "Callback script was not found."}, status=400)
        if not resolved.is_file() or candidate.is_symlink():
            return web.json_response({"error": "Invalid callback script file."}, status=400)

        try:
            from cron.jobs import create_job

            job = create_job(
                prompt="",
                schedule=schedule,
                name=name,
                deliver="local",
                script=script_name,
                no_agent=True,
            )
            try:
                from cron.scheduler import _notify_provider_jobs_changed

                _notify_provider_jobs_changed()
            except Exception:
                pass
            return web.json_response({"job": job}, status=201)
        except Exception as exc:
            return web.json_response({"error": str(exc)[:500]}, status=400)

    @wraps(original_route_table)
    def route_table_with_xpd_cron(self: Any):
        routes = list(original_route_table(self))
        known = {(method, path) for method, path, _ in routes}
        additions = (
            ("GET", "/api/xpd-cron/health", self._xpd_handle_cron_health),
            ("POST", "/api/xpd-cron/jobs", self._xpd_handle_create_cron_job),
        )
        for method, path, handler in additions:
            if (method, path) not in known:
                routes.append((method, path, handler))
        return routes

    # Preserve the original async/sync nature for introspection-heavy Hermes
    # tests and third-party wrappers.
    if inspect.iscoroutinefunction(original_route_table):
        raise RuntimeError("Hermes API Server route table unexpectedly became async.")

    APIServerAdapter._http_route_table = route_table_with_xpd_cron
    APIServerAdapter._xpd_handle_cron_health = handle_xpd_cron_health
    APIServerAdapter._xpd_handle_create_cron_job = handle_create_xpd_cron_job
    APIServerAdapter._xpd_cron_patch = True
