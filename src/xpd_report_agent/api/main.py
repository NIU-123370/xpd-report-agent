from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

import httpx
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from xpd_report_agent.api.agent_capacity import agent_capacity_slot
from xpd_report_agent.api.analysis_contracts import analysis_output_contract_prompt
from xpd_report_agent.api.error_contract import (
    REQUEST_ID_HEADER,
    install_error_contract,
    request_id_from_header,
)
from xpd_report_agent.api.merchant_questions import merchant_question_prompt
from xpd_report_agent.api.metric_definitions import metric_definition_prompt
from xpd_report_agent.api.prompts import REPORT_SYSTEM_PROMPT
from xpd_report_agent.api.service_auth import (
    authorize_service_request,
    service_auth_config,
    service_auth_error_response,
    service_auth_health,
)
from xpd_report_agent.hermes_plugin.db_query.db import connect_readonly
from xpd_report_agent.paths import PROJECT_ROOT
from xpd_report_agent.runtime.hermes_clarify import clarify_timeout_seconds
from xpd_report_agent.runtime.hermes_config import required_memory_tools_from_env

ROOT = PROJECT_ROOT
STATIC_DIR = Path(__file__).resolve().parent / "static"
LEGACY_ENV_PATH = ROOT / ".env"
ENV_PATH = ROOT / "configs" / "local.env"
REQUIRED_DB_TOOLS = [
    "db_get_schema_ddl",
    "db_schema_search",
    "db_get_table_profile",
    "db_get_join_paths",
    "db_validate_sql",
    "db_execute_sql",
]
REQUIRED_CLARIFY_TOOLS = ["clarify"]
REQUIRED_REPORT_FILE_TOOLS = ["read_file", "export_report_file"]


def _load_project_env() -> None:
    """Load developer env files only outside a managed service runtime."""

    if os.getenv("LAUNCH_MANAGED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    file_env = {
        **dotenv_values(LEGACY_ENV_PATH),
        **dotenv_values(ENV_PATH),
    }
    for key, value in file_env.items():
        if key and value is not None:
            os.environ.setdefault(key, value)


_load_project_env()

REQUIRED_MEMORY_TOOLS = required_memory_tools_from_env()

SYSTEM_PROMPT = REPORT_SYSTEM_PROMPT

# Import after local configuration has been loaded into os.environ. The
# session/reflection module resolves its durable paths lazily from that config.
from xpd_report_agent.api.analysis_presets import (  # noqa: E402
    router as analysis_presets_router,
)
from xpd_report_agent.api.memories import (  # noqa: E402
    router as memories_router,
)
from xpd_report_agent.api.schedules import (  # noqa: E402
    resume_scheduled_reports,
    schedules_enabled,
    shutdown_scheduled_reports,
)
from xpd_report_agent.api.schedules import (  # noqa: E402
    router as schedules_router,
)
from xpd_report_agent.api.sessions import (  # noqa: E402
    agent_run_health,
    idle_session_sweeper,
    memory_consolidation_health,
    memory_consolidation_sweeper,
    resume_agent_runs,
    resume_reflection_jobs,
    shutdown_agent_runs,
    shutdown_memory_consolidation,
)
from xpd_report_agent.api.sessions import (  # noqa: E402
    router as sessions_router,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Production systemd enables service authentication explicitly. Fail at
    # startup instead of silently exposing an API with a missing key.
    service_auth_config()
    await resume_reflection_jobs()
    await resume_scheduled_reports()
    await resume_agent_runs()
    sweeper = asyncio.create_task(idle_session_sweeper(), name="xpd-idle-session-sweeper")
    memory_sweeper = asyncio.create_task(
        memory_consolidation_sweeper(),
        name="xpd-memory-consolidation-sweeper",
    )
    try:
        yield
    finally:
        memory_sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await memory_sweeper
        await shutdown_memory_consolidation()
        await shutdown_agent_runs()
        await shutdown_scheduled_reports()
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper


app = FastAPI(title="xpd-report-agent", version="0.1.0", lifespan=lifespan)
install_error_contract(app)


@app.middleware("http")
async def enforce_service_auth(request: Request, call_next):
    path = request.url.path
    protected = path.startswith("/api/") and not path.startswith(
        "/api/internal/scheduled-reports/"
    )
    if protected:
        try:
            authorize_service_request(request.headers.get("Authorization"))
        except HTTPException as exc:
            request_id = getattr(
                request.state,
                "request_id",
                request_id_from_header(request.headers.get(REQUEST_ID_HEADER)),
            )
            return service_auth_error_response(exc, request_id=request_id)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(sessions_router)
app.include_router(memories_router)
app.include_router(schedules_router)
app.include_router(analysis_presets_router)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    stream: bool = True
    history: list[ChatMessage] = Field(default_factory=list)


class ReadinessResponse(BaseModel):
    ok: bool
    status: Literal["ready", "not_ready"]
    checks: dict[str, bool]


def hermes_base_url() -> str:
    return f"{hermes_origin()}/v1"


def hermes_origin() -> str:
    host = os.getenv("HERMES_GATEWAY_HOST", "127.0.0.1")
    port = os.getenv("HERMES_GATEWAY_PORT", "8642")
    return f"http://{host}:{port}"


def hermes_api_key() -> str:
    return os.getenv("HERMES_GATEWAY_API_KEY", "")


def hermes_model() -> str:
    return os.getenv("HERMES_GATEWAY_MODEL", "hermes-agent")


def require_hermes_api_key() -> str:
    key = hermes_api_key()
    if not key:
        raise HTTPException(
            status_code=500,
            detail="HERMES_GATEWAY_API_KEY is not set for the FastAPI wrapper.",
        )
    return key


def build_payload(req: ChatRequest, *, stream: bool) -> dict:
    history = [
        message.model_dump()
        for message in req.history
        if message.role in {"user", "assistant"}
    ]
    system_prompt = SYSTEM_PROMPT
    if analysis_prompt := analysis_output_contract_prompt(req.message):
        system_prompt = f"{system_prompt}\n\n{analysis_prompt}"
    if metric_prompt := metric_definition_prompt(req.message):
        system_prompt = f"{system_prompt}\n\n{metric_prompt}"
    if question_prompt := merchant_question_prompt(req.message):
        system_prompt = f"{system_prompt}\n\n{question_prompt}"
    return {
        "model": hermes_model(),
        "messages": [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": req.message},
        ],
        "stream": stream,
    }


def extract_content(response_json: dict) -> str:
    choices = response_json.get("choices") or []
    if not choices:
        return ""

    first = choices[0]
    message = first.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)

    return ""


def extract_available_db_tools(toolsets_json: dict) -> list[str]:
    tools = set()
    for toolset in toolsets_json.get("data") or []:
        if not toolset.get("enabled"):
            continue
        for tool_name in toolset.get("tools") or []:
            if isinstance(tool_name, str) and tool_name.startswith("db_"):
                tools.add(tool_name)
    ordered = [tool_name for tool_name in REQUIRED_DB_TOOLS if tool_name in tools]
    extras = sorted(tools - set(REQUIRED_DB_TOOLS))
    return ordered + extras


def extract_available_memory_tools(toolsets_json: dict) -> list[str]:
    tools = set()
    for toolset in toolsets_json.get("data") or []:
        if not toolset.get("enabled"):
            continue
        for tool_name in toolset.get("tools") or []:
            if tool_name in REQUIRED_MEMORY_TOOLS:
                tools.add(tool_name)
    return [tool_name for tool_name in REQUIRED_MEMORY_TOOLS if tool_name in tools]


def extract_available_clarify_tools(toolsets_json: dict) -> list[str]:
    tools = set()
    for toolset in toolsets_json.get("data") or []:
        if not toolset.get("enabled"):
            continue
        for tool_name in toolset.get("tools") or []:
            if tool_name in REQUIRED_CLARIFY_TOOLS:
                tools.add(tool_name)
    return [tool_name for tool_name in REQUIRED_CLARIFY_TOOLS if tool_name in tools]


def extract_available_report_file_tools(toolsets_json: dict) -> list[str]:
    tools = set()
    for toolset in toolsets_json.get("data") or []:
        if not toolset.get("enabled"):
            continue
        for tool_name in toolset.get("tools") or []:
            if tool_name in REQUIRED_REPORT_FILE_TOOLS:
                tools.add(tool_name)
    return [tool_name for tool_name in REQUIRED_REPORT_FILE_TOOLS if tool_name in tools]


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict:
    result = {
        "ok": False,
        "hermes_base_url": hermes_base_url(),
        "hermes_api_key_configured": bool(hermes_api_key()),
        "service_auth": service_auth_health(),
        "hermes": {"ok": False, "status_code": None, "error": None},
        "agent_runs": agent_run_health(),
        "memory_consolidation": memory_consolidation_health(),
        "db_query": {
            "ok": False,
            "required_tools": REQUIRED_DB_TOOLS,
            "available_tools": [],
            "missing_tools": REQUIRED_DB_TOOLS,
            "toolsets_status_code": None,
            "error": None,
        },
        "memory": {
            "ok": False,
            "required_tools": REQUIRED_MEMORY_TOOLS,
            "available_tools": [],
            "missing_tools": REQUIRED_MEMORY_TOOLS,
            "periodic_reflection_interval": int(os.getenv("XPD_REFLECTION_INTERVAL", "3")),
            "error": None,
        },
        "clarify": {
            "ok": False,
            "required_tools": REQUIRED_CLARIFY_TOOLS,
            "available_tools": [],
            "missing_tools": REQUIRED_CLARIFY_TOOLS,
            "toolsets_status_code": None,
            "patch_status_code": None,
            "timeout_seconds": clarify_timeout_seconds(),
            "error": None,
        },
        "report_files": {
            "ok": False,
            "required_tools": REQUIRED_REPORT_FILE_TOOLS,
            "available_tools": [],
            "missing_tools": REQUIRED_REPORT_FILE_TOOLS,
            "toolsets_status_code": None,
            "patch_status_code": None,
            "oss": None,
            "error": None,
        },
        "cron": {
            "ok": not schedules_enabled(),
            "enabled": schedules_enabled(),
            "native": True,
            "timezone": os.getenv("HERMES_TIMEZONE", "Asia/Shanghai"),
            "patch_status_code": None,
            "ticker_alive": False,
            "ticker_interval_seconds": None,
            "error": None,
        },
    }

    if not hermes_api_key():
        result["hermes"]["error"] = "HERMES_GATEWAY_API_KEY is not set"
        result["db_query"]["error"] = "HERMES_GATEWAY_API_KEY is not set"
        result["memory"]["error"] = "HERMES_GATEWAY_API_KEY is not set"
        result["clarify"]["error"] = "HERMES_GATEWAY_API_KEY is not set"
        result["report_files"]["error"] = "HERMES_GATEWAY_API_KEY is not set"
        result["cron"]["error"] = "HERMES_GATEWAY_API_KEY is not set"
        return result

    try:
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            response = await client.get(
                f"{hermes_base_url()}/health",
                headers={"Authorization": f"Bearer {hermes_api_key()}"},
            )
            result["hermes"]["status_code"] = response.status_code
            result["hermes"]["ok"] = response.is_success
            if not response.is_success:
                result["hermes"]["error"] = response.text[:500]

            toolsets_response = await client.get(
                f"{hermes_base_url()}/toolsets",
                headers={"Authorization": f"Bearer {hermes_api_key()}"},
            )
            result["db_query"]["toolsets_status_code"] = toolsets_response.status_code
            result["clarify"]["toolsets_status_code"] = toolsets_response.status_code
            result["report_files"]["toolsets_status_code"] = toolsets_response.status_code
            if toolsets_response.is_success:
                available_tools = extract_available_db_tools(toolsets_response.json())
                missing_tools = [
                    tool_name
                    for tool_name in REQUIRED_DB_TOOLS
                    if tool_name not in available_tools
                ]
                result["db_query"]["available_tools"] = available_tools
                result["db_query"]["missing_tools"] = missing_tools
                result["db_query"]["ok"] = not missing_tools
                if missing_tools:
                    result["db_query"]["error"] = "Required db-query tools are not exposed by Hermes API Server."

                available_memory_tools = extract_available_memory_tools(
                    toolsets_response.json()
                )
                missing_memory_tools = [
                    tool_name
                    for tool_name in REQUIRED_MEMORY_TOOLS
                    if tool_name not in available_memory_tools
                ]
                result["memory"]["available_tools"] = available_memory_tools
                result["memory"]["missing_tools"] = missing_memory_tools
                result["memory"]["ok"] = not missing_memory_tools
                if missing_memory_tools:
                    result["memory"]["error"] = (
                        "Required session_search/memory tools are not exposed by "
                        "Hermes API Server."
                    )

                available_clarify_tools = extract_available_clarify_tools(
                    toolsets_response.json()
                )
                missing_clarify_tools = [
                    tool_name
                    for tool_name in REQUIRED_CLARIFY_TOOLS
                    if tool_name not in available_clarify_tools
                ]
                result["clarify"]["available_tools"] = available_clarify_tools
                result["clarify"]["missing_tools"] = missing_clarify_tools
                if missing_clarify_tools:
                    result["clarify"]["error"] = (
                        "Required clarify tool is not exposed by Hermes API Server."
                    )

                available_report_file_tools = extract_available_report_file_tools(
                    toolsets_response.json()
                )
                missing_report_file_tools = [
                    tool_name
                    for tool_name in REQUIRED_REPORT_FILE_TOOLS
                    if tool_name not in available_report_file_tools
                ]
                result["report_files"]["available_tools"] = available_report_file_tools
                result["report_files"]["missing_tools"] = missing_report_file_tools
                if missing_report_file_tools:
                    result["report_files"]["error"] = (
                        "Required report file tools are not exposed by Hermes API Server."
                    )
            else:
                result["db_query"]["error"] = toolsets_response.text[:500]
                result["memory"]["error"] = toolsets_response.text[:500]
                result["clarify"]["error"] = toolsets_response.text[:500]
                result["report_files"]["error"] = toolsets_response.text[:500]

            clarify_response = await client.get(
                f"{hermes_origin()}/api/clarifications/health",
                headers={"Authorization": f"Bearer {hermes_api_key()}"},
            )
            result["clarify"]["patch_status_code"] = clarify_response.status_code
            if clarify_response.is_success:
                clarify_health = clarify_response.json()
                result["clarify"]["timeout_seconds"] = clarify_health.get(
                    "timeout_seconds", result["clarify"]["timeout_seconds"]
                )
                result["clarify"]["ok"] = bool(
                    not result["clarify"]["missing_tools"]
                    and clarify_health.get("ok")
                    and clarify_health.get("enabled")
                )
                if not result["clarify"]["ok"] and result["clarify"]["error"] is None:
                    result["clarify"]["error"] = (
                        "Hermes clarify runtime bridge is not ready."
                    )
            elif result["clarify"]["error"] is None:
                result["clarify"]["error"] = clarify_response.text[:500]

            report_file_response = await client.get(
                f"{hermes_origin()}/api/report-files/health",
                headers={"Authorization": f"Bearer {hermes_api_key()}"},
            )
            result["report_files"]["patch_status_code"] = report_file_response.status_code
            if report_file_response.is_success:
                patch_health = report_file_response.json()
                result["report_files"]["oss"] = patch_health.get("oss")
                result["report_files"]["ok"] = bool(
                    not result["report_files"]["missing_tools"]
                    and patch_health.get("ok")
                    and patch_health.get("enabled")
                    and patch_health.get("storage_configured")
                    and patch_health.get("storage_writable")
                )
                if (
                    not result["report_files"]["ok"]
                    and result["report_files"]["error"] is None
                ):
                    result["report_files"]["error"] = (
                        "Hermes report file runtime bridge is not ready."
                    )
            elif result["report_files"]["error"] is None:
                result["report_files"]["error"] = report_file_response.text[:500]

            if schedules_enabled():
                cron_response = await client.get(
                    f"{hermes_origin()}/api/xpd-cron/health",
                    headers={"Authorization": f"Bearer {hermes_api_key()}"},
                )
                result["cron"]["patch_status_code"] = cron_response.status_code
                if cron_response.is_success:
                    cron_health = cron_response.json()
                    result["cron"].update(
                        {
                            "ok": bool(
                                cron_health.get("ok")
                                and cron_health.get("enabled")
                                and cron_health.get("native")
                            ),
                            "enabled": True,
                            "timezone": cron_health.get(
                                "timezone", result["cron"]["timezone"]
                            ),
                            "ticker_alive": bool(cron_health.get("ticker_alive")),
                            "ticker_interval_seconds": cron_health.get(
                                "ticker_interval_seconds"
                            ),
                        }
                    )
                    if not result["cron"]["ok"]:
                        result["cron"]["error"] = (
                            "Hermes native cron bridge is not ready."
                        )
                else:
                    result["cron"]["error"] = cron_response.text[:500]
    except Exception as exc:
        result["hermes"]["error"] = str(exc)
        result["db_query"]["error"] = str(exc)
        result["memory"]["error"] = str(exc)
        result["clarify"]["error"] = str(exc)
        result["report_files"]["error"] = str(exc)
        result["cron"]["error"] = str(exc)

    result["ok"] = bool(
        result["service_auth"]["ok"]
        and result["hermes"]["ok"]
        and result["agent_runs"]["ok"]
        and result["db_query"]["ok"]
        and result["memory"]["ok"]
        and result["clarify"]["ok"]
        and result["report_files"]["ok"]
        and result["cron"]["ok"]
    )

    return result


def _mysql_readiness_check() -> dict:
    connection = None
    try:
        connection = connect_readonly()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 AS ready")
            row = cursor.fetchone()
        ok = isinstance(row, dict) and row.get("ready") == 1
        return {
            "ok": ok,
            "error": None if ok else "MySQL readiness query returned an unexpected result.",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": "MySQL readiness query failed.",
            "error_type": type(exc).__name__,
        }
    finally:
        if connection is not None:
            with suppress(Exception):
                connection.close()
@app.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "Service is not ready"}},
)
async def ready() -> JSONResponse:
    """Check that the Agent runtime and its real MySQL connection are ready."""

    runtime, mysql = await asyncio.gather(
        health(),
        asyncio.to_thread(_mysql_readiness_check),
    )
    checks = {
        "runtime": bool(runtime.get("ok")),
        "mysql": bool(mysql.get("ok")),
    }
    ok = all(checks.values())
    payload = {"ok": ok, "status": "ready" if ok else "not_ready", "checks": checks}
    return JSONResponse(status_code=200 if ok else 503, content=payload)


@app.post("/api/chat", deprecated=True)
async def chat(req: ChatRequest, response: Response) -> dict:
    response.headers["Deprecation"] = "true"
    response.headers["Warning"] = (
        '299 xpd-report-agent "Legacy stateless chat; use the Session or Agent Run API"'
    )
    key = require_hermes_api_key()
    payload = build_payload(req, stream=False)

    try:
        async with agent_capacity_slot():
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                response = await client.post(
                    f"{hermes_base_url()}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Hermes API returned an error.",
                "status_code": exc.response.status_code,
                "body": exc.response.text[:2000],
            },
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": "Hermes API request failed.", "error": str(exc)},
        ) from exc

    raw = response.json()
    return {
        "ok": True,
        "content": extract_content(raw),
        "raw": raw,
    }


@app.post("/api/chat/stream", deprecated=True)
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    key = require_hermes_api_key()
    payload = build_payload(req, stream=True)

    async def events():
        try:
            async with agent_capacity_slot():
                async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                    async with client.stream(
                        "POST",
                        f"{hermes_base_url()}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    ) as response:
                        if not response.is_success:
                            body = await response.aread()
                            yield _sse_error(
                                {
                                    "message": "Hermes API returned an error.",
                                    "status_code": response.status_code,
                                    "body": body.decode("utf-8", errors="replace")[:2000],
                                }
                            )
                            return
                        async for chunk in response.aiter_text():
                            if chunk:
                                yield chunk
        except httpx.HTTPError as exc:
            yield _sse_error({"message": "Hermes API request failed.", "error": str(exc)})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Deprecation": "true",
            "Warning": (
                '299 xpd-report-agent "Legacy stateless chat; use the Session or Agent Run API"'
            ),
        },
    )


def _sse_error(payload: dict) -> str:
    return f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


MIDDLE_PLATFORM_OPENAPI_PATHS = frozenset(
    {
        "/ready",
        "/api/v1/agent/runs",
        "/api/v1/agent/runs/{run_id}",
        "/api/v1/agent/runs/{run_id}/input",
        "/api/sessions/{session_id}/artifacts/{artifact_id}/download",
    }
)


def middle_platform_openapi() -> dict:
    """Expose the stable middle-platform operations and readiness check."""

    if app.openapi_schema is not None:
        return app.openapi_schema
    schema = get_openapi(
        title="直播数据分析 Agent 中台接口",
        version="1.0.0",
        description="包含四个稳定业务接口和一个服务就绪检查接口。",
        routes=app.routes,
    )
    schema["paths"] = {
        path: operations
        for path, operations in schema.get("paths", {}).items()
        if path in MIDDLE_PLATFORM_OPENAPI_PATHS
    }
    app.openapi_schema = schema
    return schema


app.openapi = middle_platform_openapi
