from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import HTTPException, Security
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from xpd_report_agent.api.error_contract import (
    REQUEST_ID_HEADER,
    error_payload,
)

SERVICE_AUTH_ENABLED_ENV = "XPD_SERVICE_AUTH_ENABLED"
SERVICE_API_KEY_ENV = "XPD_SERVICE_API_KEY"
MANAGED_RUNTIME_ENV = "LAUNCH_MANAGED"
SERVICE_AUTH_SCHEME = "Bearer"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ServiceAuthConfigurationError(RuntimeError):
    """The service-auth configuration is invalid or incomplete."""


@dataclass(frozen=True)
class ServiceAuthConfig:
    enabled: bool
    api_key: str | None


service_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="ServiceBearerAuth",
    description="Bearer credential used by the trusted middle-platform backend.",
)


def _configured_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ServiceAuthConfigurationError(
        f"{name} must be one of: 1, true, yes, on, 0, false, no, off."
    )


def managed_runtime_enabled() -> bool:
    """Return whether the process is running under the managed launcher."""

    return _configured_bool(MANAGED_RUNTIME_ENV, default=False)


def service_auth_enabled() -> bool:
    """Use the explicit switch, otherwise require auth in managed runtimes."""

    explicit = os.getenv(SERVICE_AUTH_ENABLED_ENV)
    if explicit is None:
        return managed_runtime_enabled()
    return _configured_bool(SERVICE_AUTH_ENABLED_ENV, default=False)


def _configured_api_key() -> str | None:
    raw = os.getenv(SERVICE_API_KEY_ENV)
    if raw is None or not raw:
        return None
    if raw != raw.strip() or any(character.isspace() for character in raw):
        raise ServiceAuthConfigurationError(
            f"{SERVICE_API_KEY_ENV} must be a non-empty Bearer token without whitespace."
        )
    if any(character in "\r\n\x00" for character in raw):
        raise ServiceAuthConfigurationError(f"{SERVICE_API_KEY_ENV} contains invalid characters.")
    return raw


def service_auth_config() -> ServiceAuthConfig:
    enabled = service_auth_enabled()
    api_key = _configured_api_key()
    if enabled and not api_key:
        raise ServiceAuthConfigurationError(
            f"{SERVICE_API_KEY_ENV} is required when service authentication is enabled."
        )
    return ServiceAuthConfig(enabled=enabled, api_key=api_key)


def _bearer_token(authorization: str | None) -> str | None:
    if not isinstance(authorization, str):
        return None
    scheme, separator, token = authorization.strip().partition(" ")
    if not separator or scheme.casefold() != SERVICE_AUTH_SCHEME.casefold():
        return None
    if not token or token != token.strip() or any(character.isspace() for character in token):
        return None
    return token


def bearer_authorization_matches(authorization: str | None, expected_api_key: str) -> bool:
    """Validate one Bearer header using a constant-time secret comparison."""

    token = _bearer_token(authorization)
    if token is None or not isinstance(expected_api_key, str) or not expected_api_key:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected_api_key.encode("utf-8"))


def service_auth_configuration_exception(
    error: ServiceAuthConfigurationError,
) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "SERVICE_AUTH_MISCONFIGURED",
            "message": str(error),
            "retryable": False,
            "outcome_unknown": False,
        },
    )


def service_auth_denied_exception() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "code": "SERVICE_AUTH_FAILED",
            "message": "A valid service Bearer credential is required.",
            "retryable": False,
            "outcome_unknown": False,
        },
        headers={"WWW-Authenticate": SERVICE_AUTH_SCHEME},
    )


def authorize_service_request(authorization: str | None) -> None:
    """Authorize a raw header value; suitable for dependencies and middleware."""

    try:
        config = service_auth_config()
    except ServiceAuthConfigurationError as exc:
        raise service_auth_configuration_exception(exc) from exc
    if not config.enabled:
        return
    if not bearer_authorization_matches(authorization, config.api_key or ""):
        raise service_auth_denied_exception()


def require_service_auth(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(service_bearer_scheme),
    ] = None,
) -> None:
    """FastAPI dependency for endpoints protected by service authentication."""

    authorization = None
    if credentials is not None:
        authorization = f"{credentials.scheme} {credentials.credentials}"
    authorize_service_request(authorization)


def service_auth_error_response(
    exc: HTTPException,
    *,
    request_id: str,
) -> JSONResponse:
    """Convert an auth exception into the project's unified middleware response."""

    headers = dict(exc.headers or {})
    headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(
            error_payload(
                status_code=exc.status_code,
                detail=exc.detail,
                request_id=request_id,
            )
        ),
        headers=headers,
    )


def service_auth_health() -> dict[str, Any]:
    """Return secret-free configuration health for the main readiness payload."""

    try:
        config = service_auth_config()
    except ServiceAuthConfigurationError as exc:
        try:
            enabled: bool | None = service_auth_enabled()
        except ServiceAuthConfigurationError:
            enabled = None
        return {
            "ok": False,
            "enabled": enabled,
            "configured": False,
            "error": str(exc),
        }
    return {
        "ok": True,
        "enabled": config.enabled,
        "configured": config.api_key is not None,
        "error": None,
    }
