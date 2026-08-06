from __future__ import annotations

import asyncio
import os
import re
import stat
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from xpd_report_agent.api.agent_capacity import agent_capacity_slot
from xpd_report_agent.api.artifact_store import list_session_artifacts
from xpd_report_agent.api.schedule_store import (
    SCHEDULE_TIMEZONE,
    new_callback_token,
    new_schedule_id,
    schedule_store,
    utc_now,
)
from xpd_report_agent.api.session_service import redact_sensitive_text
from xpd_report_agent.api.sessions import (
    SessionScope,
    _get_session,
    _hermes_json,
    report_system_prompt,
)
from xpd_report_agent.hermes_plugin.db_query.db import connect_readonly, get_mysql_config

router = APIRouter(prefix="/api")

REPORT_LABELS = {
    "daily_operations": "经营日报",
    "weekly_brand": "品牌表现报告",
}
FORMAT_LABELS = {
    "xlsx": "Excel（XLSX）",
    "csv": "CSV",
    "markdown": "Markdown",
    "pdf": "PDF",
    "json": "JSON",
}
TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
SCHEDULE_ID_PATTERN = re.compile(r"sched_[0-9a-f]{32}")
BRAND_COLUMN_NAMES = frozenset(
    {"brand", "brand_name", "brand_id", "品牌", "品牌名称", "品牌id"}
)


class ScheduleUpsertRequest(BaseModel):
    report_type: Literal["daily_operations", "weekly_brand"]
    frequency: Literal["once", "daily", "weekly"]
    run_date: str | None = Field(default=None, max_length=10)
    weekday: int | None = Field(default=None, ge=1, le=7)
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    format: Literal["xlsx", "csv", "markdown", "pdf", "json"] = "xlsx"


class ScheduledCallbackRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)


def schedules_enabled() -> bool:
    return os.getenv("XPD_SCHEDULES_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _schedules_enabled() -> bool:
    return schedules_enabled()


def _require_schedules_enabled() -> None:
    if not _schedules_enabled():
        raise HTTPException(status_code=503, detail="Scheduled reports are disabled.")


def _brand_dimension_matches() -> list[dict[str, str]]:
    database = get_mysql_config()["database"]
    connection = connect_readonly()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, ORDINAL_POSITION
                """,
                (database,),
            )
            rows = list(cursor.fetchall())
    finally:
        connection.close()

    table_columns: dict[str, set[str]] = {}
    for row in rows:
        table_columns.setdefault(str(row["table_name"]), set()).add(
            str(row["column_name"])
        )
    matches = []
    for table_name, column_names in table_columns.items():
        if "item_id" not in column_names:
            continue
        for column_name in sorted(column_names):
            if (
                column_name.lower() in BRAND_COLUMN_NAMES
                or column_name in BRAND_COLUMN_NAMES
            ):
                matches.append({"table": table_name, "column": column_name})
    return matches


def _report_capabilities_sync() -> dict[str, Any]:
    capabilities: dict[str, Any] = {
        "daily_operations": {
            "ready": True,
            "reason": None,
        },
        "weekly_brand": {
            "ready": False,
            "reason": "当前数据库缺少明确的品牌字段或商品品牌维表。",
        },
    }
    try:
        matches = _brand_dimension_matches()
        if matches:
            capabilities["weekly_brand"] = {
                "ready": True,
                "reason": None,
                "brand_columns": matches,
            }
    except Exception as exc:
        capabilities["weekly_brand"]["reason"] = (
            "暂时无法检查品牌维度：" + redact_sensitive_text(str(exc))[:300]
        )
    return capabilities


async def report_capabilities() -> dict[str, Any]:
    return await asyncio.to_thread(_report_capabilities_sync)


def _parse_run_date(value: str | None) -> date:
    if not value:
        raise HTTPException(status_code=422, detail="单次任务必须选择执行日期。")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="执行日期必须是 YYYY-MM-DD。") from exc


def _native_schedule(req: ScheduleUpsertRequest) -> str:
    hour, minute = (int(part) for part in req.time.split(":"))
    if req.frequency == "once":
        run_date = _parse_run_date(req.run_date)
        run_at = datetime(
            run_date.year,
            run_date.month,
            run_date.day,
            hour,
            minute,
            tzinfo=ZoneInfo(SCHEDULE_TIMEZONE),
        )
        now = datetime.now(ZoneInfo(SCHEDULE_TIMEZONE))
        if run_at <= now:
            raise HTTPException(status_code=422, detail="单次任务的执行时间必须晚于当前时间。")
        return run_at.isoformat(timespec="minutes")
    if req.frequency == "daily":
        return f"{minute} {hour} * * *"
    if req.weekday is None:
        raise HTTPException(status_code=422, detail="每周任务必须选择星期。")
    cron_weekday = 0 if req.weekday == 7 else req.weekday
    return f"{minute} {hour} * * {cron_weekday}"


def _normalized_fields(req: ScheduleUpsertRequest) -> dict[str, Any]:
    return {
        "report_type": req.report_type,
        "report_label": REPORT_LABELS[req.report_type],
        "frequency": req.frequency,
        "run_date": req.run_date if req.frequency == "once" else None,
        "weekday": req.weekday if req.frequency == "weekly" else None,
        "time": req.time,
        "timezone": SCHEDULE_TIMEZONE,
        "format": req.format,
    }


def _cron_scripts_dir() -> Path:
    configured = os.getenv("XPD_CRON_SCRIPT_DIR", "").strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        path = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser() / "scripts"
    if not path.is_absolute():
        raise RuntimeError("XPD_CRON_SCRIPT_DIR must be an absolute path.")
    return path.resolve()


def _callback_origin() -> str:
    configured = os.getenv("XPD_CRON_CALLBACK_ORIGIN", "").strip().rstrip("/")
    if configured:
        origin = configured
    else:
        host = os.getenv("FASTAPI_HOST", "127.0.0.1").strip()
        if host in {"0.0.0.0", "::", "[::]", "localhost"}:
            host = "127.0.0.1"
        port = os.getenv("FASTAPI_PORT", "8000").strip()
        origin = f"http://{host}:{port}"
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("XPD_CRON_CALLBACK_ORIGIN must be an HTTP(S) origin.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("XPD_CRON_CALLBACK_ORIGIN must not contain credentials or a query.")
    return origin


def _script_name(schedule_id: str) -> str:
    if not SCHEDULE_ID_PATTERN.fullmatch(schedule_id):
        raise ValueError("Invalid schedule id.")
    return f"xpd_scheduled_report_{schedule_id}.py"


def _write_callback_script(schedule_id: str, callback_token: str) -> Path:
    scripts_dir = _cron_scripts_dir()
    scripts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(scripts_dir, 0o700)
    except OSError:
        pass
    path = scripts_dir / _script_name(schedule_id)
    callback_url = (
        f"{_callback_origin()}/api/internal/scheduled-reports/{schedule_id}/run"
    )
    content = f'''from __future__ import annotations

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

request = Request(
    {callback_url!r},
    data=json.dumps({{"token": {callback_token!r}}}).encode("utf-8"),
    headers={{"Content-Type": "application/json"}},
    method="POST",
)
try:
    with urlopen(request, timeout=10) as response:
        if response.status not in {{200, 202}}:
            raise RuntimeError(f"callback returned HTTP {{response.status}}")
except (HTTPError, URLError, OSError, RuntimeError) as exc:
    print(f"scheduled report callback failed: {{exc}}", file=sys.stderr)
    raise SystemExit(1)

print('{{"wakeAgent": false}}')
'''
    temporary = path.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    temporary.replace(path)
    return path


def _remove_callback_script(schedule_id: str) -> None:
    path = _cron_scripts_dir() / _script_name(schedule_id)
    with suppress(FileNotFoundError):
        path.unlink()


def _native_job_updates(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "native_job_id": job.get("id"),
        "enabled": bool(job.get("enabled", True)),
        "state": job.get("state") or "scheduled",
        "next_run_at": job.get("next_run_at"),
        "native_last_run_at": job.get("last_run_at"),
        "native_last_status": job.get("last_status"),
        "native_last_error": redact_sensitive_text(str(job.get("last_error") or ""))[:500]
        or None,
    }


async def _create_native_job(
    *,
    schedule_id: str,
    owner_scope: str,
    native_schedule: str,
) -> dict[str, Any]:
    payload = await _hermes_json(
        "POST",
        "/api/xpd-cron/jobs",
        payload={
            "schedule_id": schedule_id,
            "name": f"xpd-report:{owner_scope}:{schedule_id}",
            "schedule": native_schedule,
            "script": _script_name(schedule_id),
        },
        action="create native scheduled report job",
    )
    job = payload.get("job")
    if not isinstance(job, dict) or not job.get("id"):
        raise HTTPException(status_code=502, detail="Hermes did not return a cron job.")
    return job


async def _native_job_action(job_id: str, action: str) -> dict[str, Any]:
    path = f"/api/jobs/{job_id}"
    method = "GET"
    if action == "delete":
        method = "DELETE"
    elif action in {"pause", "resume", "run"}:
        method = "POST"
        path += f"/{action}"
    return await _hermes_json(method, path, action=f"{action} native cron job")


async def _require_report_ready(report_type: str) -> None:
    if report_type != "weekly_brand":
        return
    capabilities = await report_capabilities()
    brand = capabilities["weekly_brand"]
    if not brand.get("ready"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "blocked_missing_brand_dimension",
                "message": brand.get("reason"),
            },
        )


def _session_title(run: dict[str, Any]) -> str:
    local = datetime.fromisoformat(str(run["scheduled_for"])).astimezone(
        ZoneInfo(SCHEDULE_TIMEZONE)
    )
    if run["report_type"] == "daily_operations":
        report_date = local.date() - timedelta(days=1)
        return f"经营日报 · {report_date.isoformat()}"
    this_monday = local.date() - timedelta(days=local.weekday())
    report_monday = this_monday - timedelta(days=7)
    iso_year, iso_week, _ = report_monday.isocalendar()
    return f"品牌表现报告 · {iso_year}年第{iso_week:02d}周"


def _scheduled_prompt(run: dict[str, Any]) -> str:
    local = datetime.fromisoformat(str(run["scheduled_for"])).astimezone(
        ZoneInfo(SCHEDULE_TIMEZONE)
    )
    output_format = str(run.get("report_format") or "xlsx")
    format_label = FORMAT_LABELS.get(output_format, output_format)
    if run["report_type"] == "daily_operations":
        period_start = local.date() - timedelta(days=1)
        title = f"{period_start.isoformat()} 经营日报"
        analysis = (
            f"仅统计北京时间 {period_start.isoformat()} 00:00 至 23:59:59 的完整自然日数据。"
            "基于三张已支持表，输出核心成交、流量、退款、商品和直播场次指标；"
            "优先形成一张同时包含总体指标与可行动明细的结果表。"
        )
    else:
        this_monday = local.date() - timedelta(days=local.weekday())
        period_start = this_monday - timedelta(days=7)
        period_end = period_start + timedelta(days=6)
        title = f"{period_start.isoformat()} 至 {period_end.isoformat()} 品牌表现报告"
        analysis = (
            f"仅统计北京时间 {period_start.isoformat()} 00:00 至 "
            f"{period_end.isoformat()} 23:59:59 的上一完整自然周数据。"
            "必须使用数据库中的明确品牌字段，按品牌汇总成交、流量、退款及重点商品表现；"
            "不得从 item_title 猜测品牌。"
        )

    xlsx_instruction = (
        "XLSX 的 analysis_type 固定为 diagnostic，并传入有数据证据的 analysis。"
        if output_format == "xlsx"
        else ""
    )
    return (
        "这是用户已经在前端明确启用的无人值守定时报告任务。"
        "不要调用 clarify，也不要等待人工回答；遇到非关键歧义时采用保守口径并写入报告假设。\n\n"
        f"任务：生成《{title}》。{analysis}\n"
        "必须先读取真实 schema，之后依次完成 SQL 校验和 SQL 执行。"
        "SQL 只能查询 tb_live_goods_daily_stats、tb_live_goods_session_stats、"
        "tb_session_endtime_stats 及其明确可关联的维表，不得编造字段或数据。\n"
        f"必须在 db_execute_sql 时设置 capture_for_export=true，并使用返回的 result_id "
        f"调用 export_report_file，导出格式为 {output_format}（{format_label}）。"
        f"{xlsx_instruction}"
        "即使查询为空，也要导出带字段说明、统计周期和空数据说明的文件。\n"
        "最终回答使用简体中文，概述统计周期、核心结论、查询验证状态和下载文件，"
        "不得输出 SQL 语句或 SQL 代码块；"
        "所有判断必须能由本次已执行查询支持。"
    )


class ScheduledReportRunner:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def max_attempts() -> int:
        try:
            return max(1, min(3, int(os.getenv("XPD_SCHEDULE_RUN_MAX_ATTEMPTS", "2"))))
        except ValueError:
            return 2

    def spawn(self, run_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task_name = f"scheduled-report:{run_id}"
        if any(not task.done() and task.get_name() == task_name for task in self._tasks):
            return
        task = loop.create_task(self._run(run_id), name=task_name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def resume_pending(self) -> None:
        for run_id in schedule_store().resumable_run_ids(max_attempts=self.max_attempts()):
            self.spawn(run_id)

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _ensure_session(self, run: dict[str, Any]) -> None:
        session_id = str(run["session_id"])
        owner_scope = str(run["owner_scope"])
        try:
            await _get_session(session_id, owner_scope)
            return
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        await _hermes_json(
            "POST",
            "/api/sessions",
            payload={"id": session_id, "title": _session_title(run)},
            action="create scheduled report session",
        )

    async def _close_session(self, session_id: str, reason: str) -> None:
        with suppress(HTTPException):
            await _hermes_json(
                "PATCH",
                f"/api/sessions/{session_id}",
                payload={"end_reason": reason},
                action="close scheduled report session",
            )

    async def _generate_report(
        self,
        *,
        store,
        run_id: str,
        session_id: str,
        attempts: int,
    ) -> list[dict[str, Any]] | None:
        """Wait for global capacity before marking or submitting the run."""

        async with agent_capacity_slot():
            run = store.update_run(
                run_id,
                {
                    "status": "running",
                    "attempt_count": attempts + 1,
                    "started_at": utc_now(),
                    "error_summary": None,
                },
            )
            if not run:
                return None
            owner_scope = str(run["owner_scope"])
            await self._ensure_session(run)
            # A previous process may have completed the expensive Agent turn
            # just before it stopped. Existing artifacts are the success fact.
            if not list_session_artifacts(session_id):
                run = store.update_run(run_id, {"submission_started": True}) or run
                scheduled_prompt = _scheduled_prompt(run)
                await _hermes_json(
                    "POST",
                    f"/api/sessions/{session_id}/chat",
                    scope=owner_scope,
                    timeout=None,
                    payload={
                        "message": scheduled_prompt,
                        "system_message": report_system_prompt(
                            owner_scope,
                            user_message=scheduled_prompt,
                        ),
                    },
                    action="generate scheduled report",
                )
            return list_session_artifacts(session_id)

    async def _run(self, run_id: str) -> None:
        store = schedule_store()
        run = store.get_run(run_id)
        if not run or run.get("status") == "succeeded":
            return
        session_id = str(run["session_id"])
        if run.get("status") == "running" and run.get("submission_started"):
            if list_session_artifacts(session_id):
                await self._close_session(session_id, "scheduled_report")
                store.update_run(
                    run_id,
                    {
                        "status": "succeeded",
                        "error_summary": None,
                        "completed_at": utc_now(),
                    },
                )
            else:
                store.update_run(
                    run_id,
                    {
                        "status": "failed",
                        "error_summary": (
                            "上次定时报告已提交给 Hermes，但结果未知；为避免重复生成，未自动重放。"
                        ),
                        "completed_at": utc_now(),
                    },
                )
            return
        attempts = int(run.get("attempt_count") or 0)
        if attempts >= self.max_attempts():
            return
        try:
            artifacts = await self._generate_report(
                store=store,
                run_id=run_id,
                session_id=session_id,
                attempts=attempts,
            )
            if artifacts is None:
                return
            if not artifacts:
                raise RuntimeError("定时报告分析已结束，但没有生成可下载文件。")
            await self._close_session(session_id, "scheduled_report")
            store.update_run(
                run_id,
                {
                    "status": "succeeded",
                    "error_summary": None,
                    "completed_at": utc_now(),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            summary = redact_sensitive_text(str(exc))[:500] or "定时报告生成失败。"
            detail = exc.detail if isinstance(exc, HTTPException) else None
            safely_retryable = bool(
                isinstance(detail, dict)
                and detail.get("retryable")
                and not detail.get("outcome_unknown")
            )
            current = store.update_run(
                run_id,
                {
                    "status": "failed",
                    "error_summary": summary,
                    "completed_at": utc_now(),
                },
            )
            if (
                safely_retryable
                and current
                and int(current.get("attempt_count") or 0) < self.max_attempts()
            ):
                await asyncio.sleep(2)
                store.update_run(
                    run_id,
                    {
                        "status": "pending",
                        "submission_started": False,
                        "completed_at": None,
                    },
                )
                await self._run(run_id)
            else:
                await self._close_session(session_id, "scheduled_report_failed")


_runner = ScheduledReportRunner()


def scheduled_report_runner() -> ScheduledReportRunner:
    return _runner


async def resume_scheduled_reports() -> None:
    if _schedules_enabled():
        await scheduled_report_runner().resume_pending()


async def shutdown_scheduled_reports() -> None:
    await scheduled_report_runner().shutdown()


async def _refresh_schedule(schedule_id: str, owner_scope: str) -> dict[str, Any] | None:
    store = schedule_store()
    schedule = store.get_owned(schedule_id, owner_scope)
    if not schedule:
        return None
    job_id = str(schedule.get("native_job_id") or "")
    if not job_id:
        return store.public_schedule(schedule)
    try:
        payload = await _native_job_action(job_id, "get")
        job = payload.get("job") or {}
        updates = _native_job_updates(job)
        if schedule.get("state") in {"completed", "failed"}:
            updates["state"] = schedule["state"]
            updates["enabled"] = False
        schedule = store.update(schedule_id, updates) or schedule
    except HTTPException:
        # A temporary Hermes outage must not hide the user's persisted config.
        pass
    return store.public_schedule(schedule)


@router.get("/schedules")
async def list_schedules(scope: SessionScope) -> dict[str, Any]:
    _require_schedules_enabled()
    persisted = schedule_store().list_owned(scope)
    refreshed = await asyncio.gather(
        *(_refresh_schedule(item["schedule_id"], scope) for item in persisted)
    )
    return {
        "ok": True,
        "data": [item for item in refreshed if item is not None],
        "timezone": SCHEDULE_TIMEZONE,
        "capabilities": await report_capabilities(),
    }


@router.post("/schedules", status_code=201)
async def create_schedule(req: ScheduleUpsertRequest, scope: SessionScope) -> dict[str, Any]:
    _require_schedules_enabled()
    await _require_report_ready(req.report_type)
    native_schedule = _native_schedule(req)
    schedule_id = new_schedule_id()
    callback_token = new_callback_token()
    _write_callback_script(schedule_id, callback_token)
    job: dict[str, Any] | None = None
    try:
        job = await _create_native_job(
            schedule_id=schedule_id,
            owner_scope=scope,
            native_schedule=native_schedule,
        )
        now = utc_now()
        record = {
            "schedule_id": schedule_id,
            "owner_scope": scope,
            "callback_token": callback_token,
            "native_schedule": native_schedule,
            **_normalized_fields(req),
            **_native_job_updates(job),
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "pending_manual_run_id": None,
            "created_at": now,
            "updated_at": now,
        }
        schedule = schedule_store().create(record)
    except Exception:
        if job and job.get("id"):
            with suppress(HTTPException):
                await _native_job_action(str(job["id"]), "delete")
        _remove_callback_script(schedule_id)
        raise
    return {"ok": True, "schedule": schedule}


@router.put("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    req: ScheduleUpsertRequest,
    scope: SessionScope,
) -> dict[str, Any]:
    _require_schedules_enabled()
    store = schedule_store()
    existing = store.get_owned(schedule_id, scope)
    if not existing:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    await _require_report_ready(req.report_type)
    native_schedule = _native_schedule(req)
    updates = {**_normalized_fields(req), "native_schedule": native_schedule}
    if native_schedule != existing.get("native_schedule"):
        old_job_id = str(existing.get("native_job_id") or "")
        old_enabled = bool(existing.get("enabled", True))
        new_job: dict[str, Any] | None = None
        if old_job_id and old_enabled:
            await _native_job_action(old_job_id, "pause")
        try:
            new_job = await _create_native_job(
                schedule_id=schedule_id,
                owner_scope=scope,
                native_schedule=native_schedule,
            )
            if not old_enabled:
                await _native_job_action(str(new_job["id"]), "pause")
                new_job["enabled"] = False
                new_job["state"] = "paused"
            updates.update(_native_job_updates(new_job))
            updated = store.update(schedule_id, updates)
            if old_job_id:
                with suppress(HTTPException):
                    await _native_job_action(old_job_id, "delete")
        except Exception:
            if new_job and new_job.get("id"):
                with suppress(HTTPException):
                    await _native_job_action(str(new_job["id"]), "delete")
            if old_job_id and old_enabled:
                with suppress(HTTPException):
                    await _native_job_action(old_job_id, "resume")
            raise
    else:
        updated = store.update(schedule_id, updates)
    return {"ok": True, "schedule": store.public_schedule(updated or existing)}


async def _set_schedule_enabled(
    schedule_id: str,
    scope: str,
    *,
    enabled: bool,
) -> dict[str, Any]:
    store = schedule_store()
    schedule = store.get_owned(schedule_id, scope)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    job_id = str(schedule.get("native_job_id") or "")
    if not job_id:
        raise HTTPException(status_code=409, detail="Native cron job is missing.")
    payload = await _native_job_action(job_id, "resume" if enabled else "pause")
    job = payload.get("job") or {}
    updated = store.update(
        schedule_id,
        {
            **_native_job_updates(job),
            "enabled": enabled,
            "state": "scheduled" if enabled else "paused",
        },
    )
    return store.public_schedule(updated or schedule)


@router.post("/schedules/{schedule_id}/pause")
async def pause_schedule(schedule_id: str, scope: SessionScope) -> dict[str, Any]:
    _require_schedules_enabled()
    return {"ok": True, "schedule": await _set_schedule_enabled(schedule_id, scope, enabled=False)}


@router.post("/schedules/{schedule_id}/resume")
async def resume_schedule(schedule_id: str, scope: SessionScope) -> dict[str, Any]:
    _require_schedules_enabled()
    schedule = schedule_store().get_owned(schedule_id, scope)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    await _require_report_ready(str(schedule.get("report_type") or ""))
    return {"ok": True, "schedule": await _set_schedule_enabled(schedule_id, scope, enabled=True)}


@router.post("/schedules/{schedule_id}/run", status_code=202)
async def run_schedule_now(schedule_id: str, scope: SessionScope) -> dict[str, Any]:
    _require_schedules_enabled()
    store = schedule_store()
    schedule = store.get_owned(schedule_id, scope)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    if not schedule.get("enabled", True):
        raise HTTPException(status_code=409, detail="请先恢复该定时任务，再立即生成。")
    await _require_report_ready(str(schedule.get("report_type") or ""))
    marker = store.set_manual_trigger(schedule_id, scope)
    if not marker:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    try:
        await _native_job_action(str(schedule["native_job_id"]), "run")
    except Exception:
        store.clear_manual_trigger(schedule_id, marker)
        raise
    return {
        "ok": True,
        "status": "queued",
        "message": "已交给 Hermes Cron，将在下一次分钟级扫描时开始生成。",
    }


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str, scope: SessionScope) -> dict[str, Any]:
    _require_schedules_enabled()
    store = schedule_store()
    schedule = store.get_owned(schedule_id, scope)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    job_id = str(schedule.get("native_job_id") or "")
    if job_id:
        try:
            await _native_job_action(job_id, "delete")
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
    removed = store.remove_owned(schedule_id, scope)
    _remove_callback_script(schedule_id)
    return {"ok": True, "schedule_id": schedule_id, "deleted": bool(removed)}


@router.post(
    "/internal/scheduled-reports/{schedule_id}/run",
    status_code=202,
    include_in_schema=False,
)
async def scheduled_report_callback(
    schedule_id: str,
    req: ScheduledCallbackRequest,
) -> dict[str, Any]:
    _require_schedules_enabled()
    if not SCHEDULE_ID_PATTERN.fullmatch(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found.")
    try:
        run, claimed = schedule_store().claim_run(schedule_id, req.token)
    except PermissionError:
        raise HTTPException(status_code=404, detail="Schedule not found.") from None
    if run is None:
        return {"ok": True, "accepted": False, "reason": "schedule_disabled_or_missing"}
    if claimed:
        scheduled_report_runner().spawn(str(run["run_id"]))
    return {
        "ok": True,
        "accepted": claimed,
        "run": schedule_store().public_run(run),
    }
