from __future__ import annotations

import logging
import re
import uuid
from http import HTTPStatus
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
    504: "UPSTREAM_TIMEOUT",
}


class ApiError(BaseModel):
    """Stable machine-readable fields in the unified HTTP error envelope."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Stable application error code used for branching.")
    message: str = Field(description="Human-readable summary; clients must not branch on it.")
    retryable: bool = Field(
        description=(
            "Whether the underlying failure is transient. For a terminal Agent run, "
            "this alone does not mean that retry attempts remain."
        )
    )
    outcome_unknown: bool = Field(
        description=(
            "Whether an upstream side effect may have happened and automatic replay is unsafe."
        )
    )
    request_id: str = Field(description="Request ID of the HTTP attempt that produced the error.")


class ApiErrorResponse(BaseModel):
    """Unified error envelope returned by FastAPI endpoints."""

    model_config = ConfigDict(extra="forbid")

    ok: Literal[False]
    error: ApiError
    detail: Any = Field(
        description=(
            "Backward-compatible diagnostics with an unstable shape. Clients should branch "
            "only on fields under error."
        )
    )


def documented_error_responses(*status_codes: int) -> dict[int, dict[str, Any]]:
    """Build reusable FastAPI ``responses=`` entries for the error envelope."""

    responses: dict[int, dict[str, Any]] = {}
    for status_code in status_codes:
        try:
            description = HTTPStatus(status_code).phrase
        except ValueError:
            description = "Request failed"
        responses[status_code] = {
            "model": ApiErrorResponse,
            "description": description,
        }
    return responses


def request_id_from_header(value: str | None) -> str:
    candidate = (value or "").strip()
    if _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return f"req_{uuid.uuid4().hex}"


def api_error(
    status_code: int,
    *,
    code: str,
    message: str,
    retryable: bool = False,
    outcome_unknown: bool = False,
    **details: Any,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "retryable": retryable,
            "outcome_unknown": outcome_unknown,
            **details,
        },
    )


def error_payload(
    *,
    status_code: int,
    detail: Any,
    request_id: str,
) -> dict[str, Any]:
    default_code = _STATUS_CODES.get(status_code, "HTTP_ERROR")
    try:
        default_message = HTTPStatus(status_code).phrase
    except ValueError:
        default_message = "Request failed"

    if isinstance(detail, dict):
        code = str(detail.get("code") or default_code)
        message = str(detail.get("message") or default_message)
        retryable = bool(detail.get("retryable", status_code in {429, 502, 503, 504}))
        outcome_unknown = bool(detail.get("outcome_unknown", False))
    else:
        code = default_code
        message = str(detail or default_message)
        retryable = status_code in {429, 502, 503, 504}
        outcome_unknown = False

    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "outcome_unknown": outcome_unknown,
            "request_id": request_id,
        },
        # Keep FastAPI's established field for existing local clients while
        # exposing the stable error contract above to the middle platform.
        "detail": detail,
    }


def install_error_contract(app: FastAPI) -> None:
    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request_id = request_id_from_header(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", request_id_from_header(None))
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(
                error_payload(
                    status_code=exc.status_code,
                    detail=exc.detail,
                    request_id=request_id,
                )
            ),
            headers=exc.headers,
        )

    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", request_id_from_header(None))
        detail = {
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed.",
            "retryable": False,
            "outcome_unknown": False,
            "errors": exc.errors(),
        }
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder(
                error_payload(status_code=422, detail=detail, request_id=request_id)
            ),
        )

    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", request_id_from_header(None))
        logger.exception("Unhandled API error request_id=%s", request_id, exc_info=exc)
        detail = {
            "code": "INTERNAL_ERROR",
            "message": "Internal server error.",
            "retryable": False,
            "outcome_unknown": False,
        }
        return JSONResponse(
            status_code=500,
            content=jsonable_encoder(
                error_payload(status_code=500, detail=detail, request_id=request_id)
            ),
        )

    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
