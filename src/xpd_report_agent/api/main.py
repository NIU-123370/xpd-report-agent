from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

import httpx
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from xpd_report_agent.api.prompts import REPORT_SYSTEM_PROMPT
from xpd_report_agent.paths import PROJECT_ROOT

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
REQUIRED_MEMORY_TOOLS = ["session_search", "memory"]

file_env = {
    **dotenv_values(LEGACY_ENV_PATH),
    **dotenv_values(ENV_PATH),
}
for key, value in file_env.items():
    if key and value is not None:
        os.environ.setdefault(key, value)

SYSTEM_PROMPT = REPORT_SYSTEM_PROMPT

# Import after local configuration has been loaded into os.environ. The
# session/reflection module resolves its durable paths lazily from that config.
from xpd_report_agent.api.memories import (  # noqa: E402
    router as memories_router,
)
from xpd_report_agent.api.sessions import (  # noqa: E402
    idle_session_sweeper,
    resume_reflection_jobs,
)
from xpd_report_agent.api.sessions import (  # noqa: E402
    router as sessions_router,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await resume_reflection_jobs()
    sweeper = asyncio.create_task(idle_session_sweeper(), name="xpd-idle-session-sweeper")
    try:
        yield
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper


app = FastAPI(title="xpd-report-agent", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(sessions_router)
app.include_router(memories_router)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    stream: bool = True
    history: list[ChatMessage] = Field(default_factory=list)


def hermes_base_url() -> str:
    host = os.getenv("HERMES_GATEWAY_HOST", "127.0.0.1")
    port = os.getenv("HERMES_GATEWAY_PORT", "8642")
    return f"http://{host}:{port}/v1"


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
    return {
        "model": hermes_model(),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict:
    result = {
        "ok": False,
        "hermes_base_url": hermes_base_url(),
        "hermes_api_key_configured": bool(hermes_api_key()),
        "hermes": {"ok": False, "status_code": None, "error": None},
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
    }

    if not hermes_api_key():
        result["hermes"]["error"] = "HERMES_GATEWAY_API_KEY is not set"
        result["db_query"]["error"] = "HERMES_GATEWAY_API_KEY is not set"
        result["memory"]["error"] = "HERMES_GATEWAY_API_KEY is not set"
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
            else:
                result["db_query"]["error"] = toolsets_response.text[:500]
                result["memory"]["error"] = toolsets_response.text[:500]
    except Exception as exc:
        result["hermes"]["error"] = str(exc)
        result["db_query"]["error"] = str(exc)
        result["memory"]["error"] = str(exc)

    result["ok"] = bool(
        result["hermes"]["ok"]
        and result["db_query"]["ok"]
        and result["memory"]["ok"]
    )

    return result


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict:
    key = require_hermes_api_key()
    payload = build_payload(req, stream=False)

    try:
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


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    key = require_hermes_api_key()
    payload = build_payload(req, stream=True)

    async def events():
        try:
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

    return StreamingResponse(events(), media_type="text/event-stream")


def _sse_error(payload: dict) -> str:
    return f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
