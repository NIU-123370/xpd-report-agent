from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from xpd_report_agent.api import service_auth
from xpd_report_agent.api.error_contract import install_error_contract


@pytest.fixture(autouse=True)
def _clean_service_auth_environment(monkeypatch):
    for name in (
        service_auth.SERVICE_AUTH_ENABLED_ENV,
        service_auth.SERVICE_API_KEY_ENV,
        service_auth.MANAGED_RUNTIME_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def test_local_runtime_defaults_to_disabled():
    assert service_auth.service_auth_enabled() is False
    service_auth.authorize_service_request(None)
    assert service_auth.service_auth_health() == {
        "ok": True,
        "enabled": False,
        "configured": False,
        "error": None,
    }


def test_managed_runtime_defaults_to_enabled_and_requires_key(monkeypatch):
    monkeypatch.setenv(service_auth.MANAGED_RUNTIME_ENV, "true")

    with pytest.raises(HTTPException) as caught:
        service_auth.authorize_service_request(None)

    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "SERVICE_AUTH_MISCONFIGURED"
    assert service_auth.service_auth_health()["ok"] is False
    assert service_auth.service_auth_health()["enabled"] is True
    assert service_auth.service_auth_health()["configured"] is False


def test_explicit_disable_overrides_managed_default(monkeypatch):
    monkeypatch.setenv(service_auth.MANAGED_RUNTIME_ENV, "true")
    monkeypatch.setenv(service_auth.SERVICE_AUTH_ENABLED_ENV, "false")

    service_auth.authorize_service_request(None)

    assert service_auth.service_auth_health()["ok"] is True
    assert service_auth.service_auth_health()["enabled"] is False


def test_bearer_auth_uses_constant_time_comparison(monkeypatch):
    calls: list[tuple[bytes, bytes]] = []

    def compare(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return left == right

    monkeypatch.setattr(service_auth.hmac, "compare_digest", compare)

    assert service_auth.bearer_authorization_matches("bearer secret-token", "secret-token")
    assert not service_auth.bearer_authorization_matches("Bearer wrong-token", "secret-token")
    assert calls == [
        (b"secret-token", b"secret-token"),
        (b"wrong-token", b"secret-token"),
    ]


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic secret-token", "Bearer", "Bearer token with spaces"],
)
def test_invalid_authorization_is_rejected(monkeypatch, authorization):
    monkeypatch.setenv(service_auth.SERVICE_AUTH_ENABLED_ENV, "true")
    monkeypatch.setenv(service_auth.SERVICE_API_KEY_ENV, "secret-token")

    with pytest.raises(HTTPException) as caught:
        service_auth.authorize_service_request(authorization)

    assert caught.value.status_code == 401
    assert caught.value.detail == {
        "code": "SERVICE_AUTH_FAILED",
        "message": "A valid service Bearer credential is required.",
        "retryable": False,
        "outcome_unknown": False,
    }
    assert caught.value.headers == {"WWW-Authenticate": "Bearer"}


def test_fastapi_dependency_and_unified_error_response(monkeypatch):
    monkeypatch.setenv(service_auth.SERVICE_AUTH_ENABLED_ENV, "true")
    monkeypatch.setenv(service_auth.SERVICE_API_KEY_ENV, "secret-token")
    app = FastAPI()
    install_error_contract(app)

    @app.get("/protected", dependencies=[Depends(service_auth.require_service_auth)])
    async def protected():
        return {"ok": True}

    client = TestClient(app)
    denied = client.get("/protected", headers={"X-Request-Id": "auth-test-request"})
    allowed = client.get(
        "/protected",
        headers={"Authorization": "Bearer secret-token"},
    )

    assert denied.status_code == 401
    assert denied.headers["WWW-Authenticate"] == "Bearer"
    assert denied.json()["error"] == {
        "code": "SERVICE_AUTH_FAILED",
        "message": "A valid service Bearer credential is required.",
        "retryable": False,
        "outcome_unknown": False,
        "request_id": "auth-test-request",
    }
    assert allowed.status_code == 200
    assert allowed.json() == {"ok": True}


def test_middleware_error_response_uses_unified_contract():
    exc = service_auth.service_auth_denied_exception()

    response = service_auth.service_auth_error_response(exc, request_id="req_middleware")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert b'"request_id":"req_middleware"' in response.body


def test_invalid_boolean_and_invalid_key_are_configuration_errors(monkeypatch):
    monkeypatch.setenv(service_auth.SERVICE_AUTH_ENABLED_ENV, "sometimes")
    assert service_auth.service_auth_health()["ok"] is False
    assert service_auth.service_auth_health()["enabled"] is None

    monkeypatch.setenv(service_auth.SERVICE_AUTH_ENABLED_ENV, "true")
    monkeypatch.setenv(service_auth.SERVICE_API_KEY_ENV, "contains whitespace")
    with pytest.raises(service_auth.ServiceAuthConfigurationError):
        service_auth.service_auth_config()
