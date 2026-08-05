from __future__ import annotations

from typing import Any

from xpd_report_agent.api.error_contract import ApiErrorResponse, error_payload
from xpd_report_agent.api.main import app


def _parameters(operation: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(parameter["in"]), str(parameter["name"])): parameter
        for parameter in operation.get("parameters") or []
    }


def _json_schema(operation: dict[str, Any], status_code: int) -> dict[str, Any]:
    response = operation["responses"][str(status_code)]
    return response["content"]["application/json"]["schema"]


def test_unified_error_model_matches_runtime_payload():
    payload = error_payload(
        status_code=503,
        detail={
            "code": "HERMES_UNAVAILABLE",
            "message": "Hermes is starting.",
            "retryable": True,
            "outcome_unknown": False,
        },
        request_id="req_contract_test",
    )

    parsed = ApiErrorResponse.model_validate(payload)

    assert parsed.model_dump() == payload
    assert set(ApiErrorResponse.model_json_schema()["required"]) == {
        "ok",
        "error",
        "detail",
    }


def test_middle_platform_openapi_declares_headers_statuses_and_models():
    schema = app.openapi()
    post = schema["paths"]["/api/v1/agent/runs"]["post"]
    get = schema["paths"]["/api/v1/agent/runs/{run_id}"]["get"]
    submit_input = schema["paths"]["/api/v1/agent/runs/{run_id}/input"]["post"]

    post_parameters = _parameters(post)
    assert post_parameters[("header", "Idempotency-Key")]["required"] is True
    assert post_parameters[("header", "X-User-Id")]["required"] is True
    assert post_parameters[("header", "X-Request-Id")]["required"] is False

    get_parameters = _parameters(get)
    assert get_parameters[("header", "X-User-Id")]["required"] is True
    assert get_parameters[("header", "X-Request-Id")]["required"] is False

    input_parameters = _parameters(submit_input)
    assert input_parameters[("header", "Idempotency-Key")]["required"] is True
    assert input_parameters[("header", "X-User-Id")]["required"] is True
    assert input_parameters[("header", "X-Request-Id")]["required"] is False

    expected_post_statuses = {200, 202, 400, 401, 404, 409, 422, 502, 503, 504}
    assert expected_post_statuses <= {int(status) for status in post["responses"]}
    expected_get_statuses = {200, 401, 404, 422, 503}
    assert expected_get_statuses <= {int(status) for status in get["responses"]}

    for operation, success_statuses in ((post, (200, 202)), (get, (200,))):
        for status_code in success_statuses:
            response_schema = _json_schema(operation, status_code)
            assert "$ref" in response_schema
            assert response_schema.get("additionalProperties") is not True

    for status_code in expected_post_statuses - {200, 202}:
        assert _json_schema(post, status_code)["$ref"].endswith("/ApiErrorResponse")
    for status_code in expected_get_statuses - {200}:
        assert _json_schema(get, status_code)["$ref"].endswith("/ApiErrorResponse")

    assert "ApiError" in schema["components"]["schemas"]
    assert "ApiErrorResponse" in schema["components"]["schemas"]
    result_schema = schema["components"]["schemas"]["AgentRunResultResponse"]
    assert "analysis" in result_schema["properties"]
    assert "analysis" in result_schema["required"]
    assert "StructuredAnalysis" in schema["components"]["schemas"]
    run_schema = schema["components"]["schemas"]["AgentRunResponse"]
    assert "clarification" in run_schema["properties"]
    assert "waiting_input" in run_schema["properties"]["status"]["enum"]


def test_legacy_chat_openapi_operations_are_deprecated():
    schema = app.openapi()

    assert schema["paths"]["/api/chat"]["post"]["deprecated"] is True
    assert schema["paths"]["/api/chat/stream"]["post"]["deprecated"] is True
    assert schema["paths"]["/api/sessions/{session_id}/chat"]["post"]["deprecated"] is True
