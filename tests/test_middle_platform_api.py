from __future__ import annotations

import asyncio
import json
import time

from fastapi.testclient import TestClient

from xpd_report_agent.api import main as app_main
from xpd_report_agent.api import sessions as sessions_api
from xpd_report_agent.api.session_service import user_owner_scope

CLIENT_HEADERS = {"X-User-Id": "middle-platform-test-user"}


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload or {}
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.text = "OK"
        self.content = b"{}"

    def json(self):
        return self._payload


class FakeHermesClient:
    sessions: dict[str, dict] = {}
    messages: dict[str, list[dict]] = {}
    calls: list[dict] = []
    chat_responses: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    @classmethod
    def reset(cls):
        cls.sessions = {}
        cls.messages = {}
        cls.calls = []
        cls.chat_responses = []

    async def request(self, method, url, headers=None, json=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        path = url.split("127.0.0.1:8642", 1)[-1]
        if method == "POST" and path == "/api/sessions":
            session_id = json["id"]
            if session_id in self.sessions:
                return FakeResponse({"error": "exists"}, 409)
            session = {
                "id": session_id,
                "title": json.get("title"),
                "source": "api_server",
                "started_at": 100.0,
                "last_active": 100.0,
                "message_count": 0,
            }
            self.sessions[session_id] = session
            self.messages[session_id] = []
            return FakeResponse({"session": session}, 201)

        parts = path.split("/")
        session_id = parts[3] if len(parts) > 3 else ""
        session = self.sessions.get(session_id)
        if not session:
            return FakeResponse({"error": "not found"}, 404)
        if method == "GET" and path.endswith("/messages"):
            return FakeResponse({"session_id": session_id, "data": self.messages[session_id]})
        if method == "GET":
            return FakeResponse({"session": session})
        if method == "PATCH":
            if "title" in json:
                session["title"] = json["title"]
            return FakeResponse({"session": session})
        if method == "POST" and path.endswith("/chat"):
            content = (
                self.chat_responses.pop(0)
                if self.chat_responses
                else "经营分析完成"
            )
            self.messages[session_id].extend(
                [
                    {"role": "user", "content": json["message"]},
                    {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": "先检查数据，再汇总结论。",
                    },
                ]
            )
            session["message_count"] = len(self.messages[session_id])
            return FakeResponse(
                {
                    "session_id": session_id,
                    "message": {"role": "assistant", "content": content},
                    "usage": {"total_tokens": 10},
                }
            )
        return FakeResponse({}, 400)


def configured_client(tmp_path, monkeypatch):
    FakeHermesClient.reset()
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "gateway-test-key")
    monkeypatch.setenv("XPD_SESSION_SIGNING_SECRET", "session-signing-test-key")
    monkeypatch.setenv("XPD_IDENTITY_MODE", "user_id")
    monkeypatch.setenv("XPD_FINAL_REFLECTION_ENABLED", "false")
    monkeypatch.setenv("XPD_SCHEDULES_ENABLED", "false")
    monkeypatch.setenv("XPD_AGENT_RECONCILE_SECONDS", "0")
    monkeypatch.setenv("XPD_AGENT_OUTCOME_RECONCILE_SECONDS", "0")
    monkeypatch.setenv("XPD_HERMES_RETRY_BASE_SECONDS", "0")
    monkeypatch.setattr(sessions_api.httpx, "AsyncClient", FakeHermesClient)
    sessions_api.agent_run_store.path = tmp_path / "agent-runs.json"
    sessions_api._agent_run_tasks.clear()
    sessions_api._active_chat_sessions.clear()
    return TestClient(app_main.app)


def wait_for_run(
    client: TestClient,
    status_url: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get(status_url, headers=headers or CLIENT_HEADERS)
        assert response.status_code == 200
        run = response.json()["run"]
        if run["status"] in {"succeeded", "failed"}:
            return run
        time.sleep(0.01)
    raise AssertionError("Agent run did not finish in time")


def wait_for_run_status(
    client: TestClient,
    status_url: str,
    expected_status: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get(status_url, headers=headers or CLIENT_HEADERS)
        assert response.status_code == 200
        run = response.json()["run"]
        if run["status"] == expected_status:
            return run
        if run["status"] in {"succeeded", "failed"}:
            raise AssertionError(
                f"Agent run reached {run['status']} instead of {expected_status}: {run}"
            )
        time.sleep(0.01)
    raise AssertionError(f"Agent run did not reach {expected_status} in time")


def test_middle_platform_run_is_idempotent_and_recoverable(tmp_path, monkeypatch):
    with configured_client(tmp_path, monkeypatch) as client:
        headers = {
            **CLIENT_HEADERS,
            "Idempotency-Key": "order-analysis-001",
            "X-Request-Id": "request-from-middle-platform",
        }
        created = client.post(
            "/api/v1/agent/runs",
            headers=headers,
            json={"message": "分析最近七天商品表现", "title": "商品周报"},
        )
        assert created.status_code == 202
        first = created.json()
        assert first["run"]["request_id"] == "request-from-middle-platform"
        assert first["status_url"].startswith("/api/v1/agent/runs/run_")

        completed = wait_for_run(client, first["status_url"])
        assert completed["status"] == "succeeded"
        assert completed["result"]["content"] == "经营分析完成"
        analysis = completed["result"]["analysis"]
        assert analysis["schema_version"] == "1.2"
        assert analysis["structured"] is False
        assert analysis["analysis_type"] == "query"
        assert analysis["conclusion"] == "经营分析完成"
        assert analysis["data_period"] is None
        assert analysis["data_scope"] is None
        assert analysis["metrics"] == []
        assert analysis["metric_definitions"] == []
        assert analysis["comparisons"] == []
        assert analysis["insights"] == []
        assert analysis["drivers"] == []
        assert analysis["anomalies"] == []
        assert analysis["recommendations"] == []
        assert analysis["assumptions"] == []
        assert analysis["limitations"] == []
        assert analysis["data_quality"] is None
        assert analysis["executed_queries"] == []
        assert analysis["sql"] == []
        assert completed["result"]["progress"] == ["已完成分析并整理结论"]
        assert "reasoning" not in completed["result"]
        chat_call = next(
            call for call in FakeHermesClient.calls if call["url"].endswith("/chat")
        )
        assert "<XPD_ANALYSIS_JSON>" in chat_call["json"]["system_message"]

        repeated = client.post(
            "/api/v1/agent/runs",
            headers=headers,
            json={"message": "分析最近七天商品表现", "title": "商品周报"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["run"]["run_id"] == completed["run_id"]
        chat_calls = [call for call in FakeHermesClient.calls if call["url"].endswith("/chat")]
        assert len(chat_calls) == 1


def test_middle_platform_run_stream_returns_safe_progress_and_answer(
    tmp_path, monkeypatch
):
    with configured_client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/v1/agent/runs",
            headers={**CLIENT_HEADERS, "Idempotency-Key": "stream-analysis-001"},
            json={"message": "分析最近七天商品表现"},
        )
        run = wait_for_run(client, created.json()["status_url"])

        response = client.get(
            f"/api/v1/agent/runs/{run['run_id']}/stream",
            headers=CLIENT_HEADERS,
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: progress" in response.text
        assert "已完成分析并整理结论" in response.text
        assert "event: answer.delta" in response.text
        assert "经营分析完成" in response.text
        assert "event: run.completed" in response.text
        assert "先检查数据，再汇总结论" not in response.text
        assert "reasoning" not in response.text


def test_middle_platform_run_waits_persistently_and_resumes_with_idempotent_input(
    tmp_path, monkeypatch
):
    client = configured_client(tmp_path, monkeypatch)
    clarification_envelope = """<XPD_CLARIFICATION_REQUEST>
{"question":"销量按件数还是订单数？","choices":["件数","订单数"]}
</XPD_CLARIFICATION_REQUEST>"""
    FakeHermesClient.chat_responses = [clarification_envelope, "按件数统计完成"]
    run_headers = {
        **CLIENT_HEADERS,
        "Idempotency-Key": "clarifying-analysis-001",
    }

    with client:
        created = client.post(
            "/api/v1/agent/runs",
            headers=run_headers,
            json={"message": "查看销量最高的商品"},
        )
        assert created.status_code == 202
        status_url = created.json()["status_url"]
        waiting = wait_for_run_status(client, status_url, "waiting_input")

        assert waiting["result"] is None
        assert waiting["error"] is None
        assert waiting["clarification"]["question"] == "销量按件数还是订单数？"
        assert waiting["clarification"]["choices"] == ["件数", "订单数"]
        assert len([call for call in FakeHermesClient.calls if call["url"].endswith("/chat")]) == 1

    # A restarted service must leave waiting_input dormant rather than replaying
    # the original analysis or holding an Agent capacity slot.
    sessions_api._agent_run_tasks.clear()
    with TestClient(app_main.app) as restarted:
        persisted = restarted.get(status_url, headers=CLIENT_HEADERS)
        assert persisted.status_code == 200
        assert persisted.json()["run"]["status"] == "waiting_input"
        assert len([call for call in FakeHermesClient.calls if call["url"].endswith("/chat")]) == 1

        hidden = restarted.post(
            f"/api/v1/agent/runs/{waiting['run_id']}/input",
            headers={
                "X-User-Id": "another-middle-platform-user",
                "Idempotency-Key": "clarification-input-hidden",
            },
            json={"answer": "按件数"},
        )
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "AGENT_RUN_NOT_FOUND"

        answered = restarted.post(
            f"/api/v1/agent/runs/{waiting['run_id']}/input",
            headers={
                **CLIENT_HEADERS,
                "Idempotency-Key": "clarification-input-001",
            },
            json={"answer": "按件数"},
        )
        assert answered.status_code == 202
        assert answered.json()["run"]["run_id"] == waiting["run_id"]

        completed = wait_for_run(restarted, status_url)
        assert completed["status"] == "succeeded"
        assert completed["run_id"] == waiting["run_id"]
        assert completed["attempt_count"] == 2
        assert completed["result"]["content"] == "按件数统计完成"
        assert completed["clarification"] is None

        chat_calls = [
            call for call in FakeHermesClient.calls if call["url"].endswith("/chat")
        ]
        assert len(chat_calls) == 2
        assert "<XPD_CLARIFICATION_REQUEST>" in chat_calls[0]["json"]["system_message"]
        assert "用户回答：按件数" in chat_calls[1]["json"]["message"]
        assert chat_calls[1]["json"]["message"] != "查看销量最高的商品"

        duplicate = restarted.post(
            f"/api/v1/agent/runs/{waiting['run_id']}/input",
            headers={
                **CLIENT_HEADERS,
                "Idempotency-Key": "clarification-input-001",
            },
            json={"answer": "按件数"},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["run"]["status"] == "succeeded"
        assert len(
            [call for call in FakeHermesClient.calls if call["url"].endswith("/chat")]
        ) == 2

        conflict = restarted.post(
            f"/api/v1/agent/runs/{waiting['run_id']}/input",
            headers={
                **CLIENT_HEADERS,
                "Idempotency-Key": "clarification-input-001",
            },
            json={"answer": "按订单数"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "INPUT_IDEMPOTENCY_CONFLICT"

        not_waiting = restarted.post(
            f"/api/v1/agent/runs/{waiting['run_id']}/input",
            headers={
                **CLIENT_HEADERS,
                "Idempotency-Key": "clarification-input-002",
            },
            json={"answer": "按订单数"},
        )
        assert not_waiting.status_code == 409
        assert not_waiting.json()["error"]["code"] == "AGENT_RUN_NOT_WAITING_INPUT"


def test_middle_platform_identity_is_used_for_report_oss_filename_context(
    tmp_path, monkeypatch
):
    with configured_client(tmp_path, monkeypatch) as client:
        storage = tmp_path / "report-files"
        monkeypatch.setenv("XPD_FILE_STORAGE_PATH", str(storage))
        monkeypatch.setenv("XPD_REPORT_OSS_ENABLED", "true")
        monkeypatch.setenv(
            "XPD_REPORT_OSS_ENDPOINT", "https://oss-cn-beijing.aliyuncs.com"
        )
        monkeypatch.setenv("XPD_REPORT_OSS_REGION", "cn-beijing")
        monkeypatch.setenv("XPD_REPORT_OSS_ACCESS_KEY_ID", "test-key")
        monkeypatch.setenv("XPD_REPORT_OSS_ACCESS_KEY_SECRET", "test-secret")
        response = client.post(
            "/api/v1/agent/runs",
            headers={
                **CLIENT_HEADERS,
                "Idempotency-Key": "report-context-001",
                "X-Request-Id": "trace-from-middle-platform",
            },
            json={"message": "分析并导出数据"},
        )
        assert response.status_code == 202
        run = wait_for_run(client, response.json()["status_url"])
        context_path = storage / run["session_id"] / ".report-oss-context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))

        assert context["uid"] == "middle-platform-test-user"
        assert context["trace_id"] == "trace-from-middle-platform"


def test_middle_platform_run_requires_service_bearer_when_enabled(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XPD_SERVICE_AUTH_ENABLED", "true")
    monkeypatch.setenv("XPD_SERVICE_API_KEY", "middle-platform-service-secret")
    with configured_client(tmp_path, monkeypatch) as client:
        headers = {**CLIENT_HEADERS, "Idempotency-Key": "authenticated-run-001"}
        denied = client.post(
            "/api/v1/agent/runs",
            headers=headers,
            json={"message": "分析数据"},
        )
        allowed = client.post(
            "/api/v1/agent/runs",
            headers={
                **headers,
                "Authorization": "Bearer middle-platform-service-secret",
            },
            json={"message": "分析数据"},
        )

        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "SERVICE_AUTH_FAILED"
        assert allowed.status_code == 202
        wait_for_run(
            client,
            allowed.json()["status_url"],
            headers={
                **CLIENT_HEADERS,
                "Authorization": "Bearer middle-platform-service-secret",
            },
        )


def test_same_idempotency_key_with_different_message_conflicts(tmp_path, monkeypatch):
    with configured_client(tmp_path, monkeypatch) as client:
        headers = {**CLIENT_HEADERS, "Idempotency-Key": "same-key"}
        first = client.post(
            "/api/v1/agent/runs",
            headers=headers,
            json={"message": "第一次分析"},
        )
        assert first.status_code == 202
        wait_for_run(client, first.json()["status_url"])

        conflict = client.post(
            "/api/v1/agent/runs",
            headers=headers,
            json={"message": "完全不同的第二次分析"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert conflict.json()["error"]["retryable"] is False


def test_missing_idempotency_key_uses_unified_error_contract(tmp_path, monkeypatch):
    with configured_client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/v1/agent/runs",
            headers={**CLIENT_HEADERS, "X-Request-Id": "caller-request-9"},
            json={"message": "分析数据"},
        )
        assert response.headers["X-Request-Id"] == "caller-request-9"
        assert response.status_code == 422
        assert response.json()["error"] == {
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed.",
            "retryable": False,
            "outcome_unknown": False,
            "request_id": "caller-request-9",
        }

        invalid_body = client.post(
            "/api/v1/agent/runs",
            headers={**CLIENT_HEADERS, "Idempotency-Key": "invalid-body"},
            json={},
        )
        assert invalid_body.status_code == 422
        assert invalid_body.json()["error"]["code"] == "VALIDATION_ERROR"
        assert invalid_body.json()["error"]["retryable"] is False


def test_run_status_is_owner_scoped(tmp_path, monkeypatch):
    with configured_client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/v1/agent/runs",
            headers={**CLIENT_HEADERS, "Idempotency-Key": "private-run"},
            json={"message": "分析数据"},
        )
        status_url = response.json()["status_url"]
        denied = client.get(
            status_url,
            headers={"X-User-Id": "another-middle-platform-user"},
        )
        assert denied.status_code == 404
        assert denied.json()["error"]["code"] == "AGENT_RUN_NOT_FOUND"


def test_running_turn_is_reconciled_after_service_restart(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    scope = user_owner_scope(
        CLIENT_HEADERS["X-User-Id"],
        secret="session-signing-test-key",
    )
    session_id = f"xpd_{scope}_restart_recovery"
    FakeHermesClient.sessions[session_id] = {
        "id": session_id,
        "source": "api_server",
        "started_at": 100.0,
        "last_active": 100.0,
        "message_count": 2,
    }
    FakeHermesClient.messages[session_id] = [
        {"role": "user", "content": "恢复这次分析"},
        {"role": "assistant", "content": "已在重启前完成"},
    ]
    record, _ = sessions_api.agent_run_store.create_or_get(
        owner_scope=scope,
        session_id=session_id,
        idempotency_key="restart-run-001",
        request={"message": "恢复这次分析"},
        checkpoint={"baseline_message_count": 0, "artifact_ids": []},
    )
    sessions_api.agent_run_store.mark_running(record["run_id"], scope)

    with client:
        completed = wait_for_run(
            client,
            f"/api/v1/agent/runs/{record['run_id']}",
        )

    assert completed["status"] == "succeeded"
    assert completed["result"]["content"] == "已在重启前完成"
    assert completed["result"]["recovered"] is True
    assert not any(call["url"].endswith("/chat") for call in FakeHermesClient.calls)


def test_unknown_upstream_submission_is_not_replayed_after_restart(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    scope = user_owner_scope(
        CLIENT_HEADERS["X-User-Id"],
        secret="session-signing-test-key",
    )
    session_id = f"xpd_{scope}_unknown_outcome"
    FakeHermesClient.sessions[session_id] = {
        "id": session_id,
        "source": "api_server",
        "started_at": 100.0,
        "last_active": 100.0,
        "message_count": 0,
    }
    FakeHermesClient.messages[session_id] = []
    record, _ = sessions_api.agent_run_store.create_or_get(
        owner_scope=scope,
        session_id=session_id,
        idempotency_key="unknown-run-001",
        request={"message": "不要重复执行"},
        checkpoint={
            "baseline_message_count": 0,
            "artifact_ids": [],
            "upstream_submission_started": True,
        },
    )
    sessions_api.agent_run_store.mark_running(record["run_id"], scope)
    sessions_api.agent_run_store.mark_running(record["run_id"], scope)

    with client:
        failed = wait_for_run(client, f"/api/v1/agent/runs/{record['run_id']}")

    assert failed["status"] == "failed"
    assert failed["attempt_count"] == 2
    assert failed["error"]["code"] == "AGENT_RUN_OUTCOME_UNKNOWN"
    assert failed["error"]["outcome_unknown"] is True
    assert not any(call["url"].endswith("/chat") for call in FakeHermesClient.calls)


def test_running_record_at_attempt_limit_is_reconciled_then_closed(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    scope = user_owner_scope(
        CLIENT_HEADERS["X-User-Id"],
        secret="session-signing-test-key",
    )
    session_id = f"xpd_{scope}_retry_exhausted"
    FakeHermesClient.sessions[session_id] = {
        "id": session_id,
        "source": "api_server",
        "started_at": 100.0,
        "last_active": 100.0,
        "message_count": 0,
    }
    FakeHermesClient.messages[session_id] = []
    record, _ = sessions_api.agent_run_store.create_or_get(
        owner_scope=scope,
        session_id=session_id,
        idempotency_key="exhausted-run-001",
        request={"message": "不要超过重试上限"},
        checkpoint={
            "baseline_message_count": 0,
            "artifact_ids": [],
            "upstream_submission_started": False,
        },
    )
    sessions_api.agent_run_store.mark_running(record["run_id"], scope)
    sessions_api.agent_run_store.mark_running(record["run_id"], scope)

    with client:
        failed = wait_for_run(client, f"/api/v1/agent/runs/{record['run_id']}")

    assert failed["status"] == "failed"
    assert failed["attempt_count"] == 2
    assert failed["error"]["code"] == "AGENT_RUN_RETRY_EXHAUSTED"
    assert failed["error"]["retryable"] is False
    assert failed["error"]["retry_exhausted"] is True


def test_submitted_long_run_keeps_reconciling_until_final_message(tmp_path, monkeypatch):
    configured_client(tmp_path, monkeypatch)
    monkeypatch.setenv("XPD_AGENT_OUTCOME_RECONCILE_SECONDS", "0.03")
    scope = user_owner_scope(
        CLIENT_HEADERS["X-User-Id"],
        secret="session-signing-test-key",
    )
    session_id = f"xpd_{scope}_delayed_recovery"
    record, _ = sessions_api.agent_run_store.create_or_get(
        owner_scope=scope,
        session_id=session_id,
        idempotency_key="delayed-recovery-001",
        request={"message": "等待长分析完成"},
        checkpoint={
            "baseline_message_count": 0,
            "artifact_ids": [],
            "upstream_submission_started": True,
        },
    )
    sessions_api.agent_run_store.mark_running(record["run_id"], scope)
    inspections = 0

    async def delayed_messages(_session_id):
        nonlocal inspections
        inspections += 1
        messages = [{"role": "user", "content": "等待长分析完成"}]
        if inspections >= 2:
            messages.append({"role": "assistant", "content": "长分析最终完成"})
        return {"session_id": session_id, "data": messages}

    monkeypatch.setattr(sessions_api, "_session_messages_payload", delayed_messages)

    asyncio.run(sessions_api._execute_agent_run(record["run_id"], scope))

    completed = sessions_api.agent_run_store.get_owned(record["run_id"], scope)
    assert completed["status"] == "succeeded"
    assert completed["result"]["content"] == "长分析最终完成"
    assert inspections >= 2
    assert not any(call["url"].endswith("/chat") for call in FakeHermesClient.calls)


def test_reconcile_get_error_is_safely_requeued_without_task_crash(tmp_path, monkeypatch):
    configured_client(tmp_path, monkeypatch)
    scope = user_owner_scope(
        CLIENT_HEADERS["X-User-Id"],
        secret="session-signing-test-key",
    )
    session_id = f"xpd_{scope}_reconcile_retry"
    FakeHermesClient.sessions[session_id] = {
        "id": session_id,
        "source": "api_server",
        "started_at": 100.0,
        "last_active": 100.0,
        "message_count": 0,
    }
    FakeHermesClient.messages[session_id] = []
    record, _ = sessions_api.agent_run_store.create_or_get(
        owner_scope=scope,
        session_id=session_id,
        idempotency_key="reconcile-get-retry-001",
        request={"message": "GET 恢复后安全重试"},
        checkpoint={
            "baseline_message_count": 0,
            "artifact_ids": [],
            "upstream_submission_started": False,
        },
    )
    sessions_api.agent_run_store.mark_running(record["run_id"], scope)
    original_messages = sessions_api._session_messages_payload
    inspections = 0

    async def flaky_messages(requested_session_id):
        nonlocal inspections
        inspections += 1
        if inspections == 1:
            raise sessions_api.api_error(
                503,
                code="HERMES_UNAVAILABLE",
                message="temporary GET failure",
                retryable=True,
                outcome_unknown=False,
            )
        return await original_messages(requested_session_id)

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(sessions_api, "_session_messages_payload", flaky_messages)
    monkeypatch.setattr(sessions_api.asyncio, "sleep", no_delay)

    asyncio.run(sessions_api._execute_agent_run(record["run_id"], scope))

    completed = sessions_api.agent_run_store.get_owned(record["run_id"], scope)
    assert completed["status"] == "succeeded"
    assert completed["attempt_count"] == 2
    assert len([call for call in FakeHermesClient.calls if call["url"].endswith("/chat")]) == 1


def test_client_safe_retry_clears_old_submission_checkpoint(tmp_path, monkeypatch):
    with configured_client(tmp_path, monkeypatch) as client:
        scope = user_owner_scope(
            CLIENT_HEADERS["X-User-Id"],
            secret="session-signing-test-key",
        )
        session_id = f"xpd_{scope}_client_retry"
        FakeHermesClient.sessions[session_id] = {
            "id": session_id,
            "source": "api_server",
            "started_at": 100.0,
            "last_active": 100.0,
            "message_count": 0,
        }
        FakeHermesClient.messages[session_id] = []
        record, _ = sessions_api.agent_run_store.create_or_get(
            owner_scope=scope,
            session_id=session_id,
            idempotency_key="client-safe-retry-001",
            request={"message": "客户端触发安全重试"},
            checkpoint={
                "baseline_message_count": 0,
                "artifact_ids": [],
                "upstream_submission_started": True,
            },
        )
        sessions_api.agent_run_store.mark_running(record["run_id"], scope)
        sessions_api.agent_run_store.mark_failed(
            record["run_id"],
            scope,
            error={
                "code": "HERMES_UNAVAILABLE",
                "message": "connection was never established",
                "retryable": True,
                "outcome_unknown": False,
                "request_id": record["request_id"],
            },
        )

        retried = client.post(
            f"/api/sessions/{session_id}/runs",
            headers={**CLIENT_HEADERS, "Idempotency-Key": "client-safe-retry-001"},
            json={"message": "客户端触发安全重试"},
        )
        assert retried.status_code == 202
        completed = wait_for_run(
            client,
            f"/api/v1/agent/runs/{record['run_id']}",
        )

    assert completed["status"] == "succeeded"
    assert completed["attempt_count"] == 2
    assert len([call for call in FakeHermesClient.calls if call["url"].endswith("/chat")]) == 1
