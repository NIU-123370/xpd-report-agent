from __future__ import annotations

from fastapi.testclient import TestClient

from xpd_report_agent.api import main as app_main


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text="OK"):
        self._payload = payload or {}
        self.status_code = status_code
        self.text = text
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.is_success:
            request = None
            response = self
            raise app_main.httpx.HTTPStatusError("bad status", request=request, response=response)


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None):
        if url.endswith("/api/clarifications/health"):
            return FakeResponse(
                {"ok": True, "enabled": True, "timeout_seconds": 300}
            )
        if url.endswith("/api/report-files/health"):
            return FakeResponse(
                {
                    "ok": True,
                    "enabled": True,
                    "storage_configured": True,
                    "storage_writable": True,
                }
            )
        if url.endswith("/api/xpd-cron/health"):
            return FakeResponse(
                {
                    "ok": True,
                    "enabled": True,
                    "native": True,
                    "timezone": "Asia/Shanghai",
                    "ticker_alive": True,
                    "ticker_interval_seconds": 60,
                }
            )
        if url.endswith("/toolsets"):
            return FakeResponse(
                {
                    "object": "list",
                    "platform": "api_server",
                    "data": [
                        {
                            "name": "db_query",
                            "enabled": True,
                            "tools": [
                                *app_main.REQUIRED_DB_TOOLS,
                                *app_main.REQUIRED_MEMORY_TOOLS,
                                *app_main.REQUIRED_CLARIFY_TOOLS,
                                *app_main.REQUIRED_REPORT_FILE_TOOLS,
                            ],
                        }
                    ],
                }
            )
        return FakeResponse({"status": "ok"})

    async def post(self, url, headers=None, json=None):
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "结论：测试响应",
                        }
                    }
                ]
            }
        )


class FakeStreamResponse(FakeResponse):
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_text(self):
        yield 'data: {"choices":[{"delta":{"content":"结论"}}]}\n\n'
        yield 'data: {"choices":[{"delta":{"content":"：流式"}}]}\n\n'
        yield "data: [DONE]\n\n"


class FakeStreamingAsyncClient(FakeAsyncClient):
    def stream(self, method, url, headers=None, json=None):
        return FakeStreamResponse()


def test_managed_runtime_does_not_load_project_env(monkeypatch):
    key = "XPD_TEST_MANAGED_ENV_LOADING"
    monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LAUNCH_MANAGED", "true")
    monkeypatch.setattr(
        app_main,
        "dotenv_values",
        lambda _: {key: "must-not-be-loaded"},
    )

    app_main._load_project_env()

    assert key not in app_main.os.environ


def test_unmanaged_runtime_still_loads_project_env(monkeypatch):
    key = "XPD_TEST_LOCAL_ENV_LOADING"
    monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("LAUNCH_MANAGED", raising=False)
    monkeypatch.setattr(
        app_main,
        "dotenv_values",
        lambda _: {key: "loaded-for-local-development"},
    )

    app_main._load_project_env()

    assert app_main.os.environ[key] == "loaded-for-local-development"


def test_health_reports_missing_key(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_API_KEY", raising=False)
    client = TestClient(app_main.app)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["hermes_api_key_configured"] is False
    assert body["db_query"]["ok"] is False


def test_health_reports_db_query_tools(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("XPD_SCHEDULES_ENABLED", "true")
    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(app_main.app)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["hermes"]["ok"] is True
    assert body["db_query"]["ok"] is True
    assert body["db_query"]["missing_tools"] == []
    assert body["db_query"]["available_tools"] == app_main.REQUIRED_DB_TOOLS
    assert body["memory"]["ok"] is True
    assert body["memory"]["available_tools"] == app_main.REQUIRED_MEMORY_TOOLS
    assert body["clarify"]["ok"] is True
    assert body["clarify"]["available_tools"] == app_main.REQUIRED_CLARIFY_TOOLS
    assert body["report_files"]["ok"] is True
    assert body["report_files"]["available_tools"] == app_main.REQUIRED_REPORT_FILE_TOOLS
    assert body["cron"]["ok"] is True
    assert body["cron"]["native"] is True
    assert body["cron"]["timezone"] == "Asia/Shanghai"


def test_health_treats_disabled_schedules_as_healthy(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("XPD_SCHEDULES_ENABLED", "false")
    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(app_main, "agent_run_health", lambda: {"ok": True})
    client = TestClient(app_main.app)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["cron"]["ok"] is True
    assert body["cron"]["enabled"] is False
    assert body["cron"]["patch_status_code"] is None


def test_health_falls_back_for_invalid_clarify_timeout(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("XPD_CLARIFY_TIMEOUT_SECONDS", "not-a-number")
    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(app_main.app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["clarify"]["timeout_seconds"] == 300


def test_health_reports_missing_db_query_tools(monkeypatch):
    class MissingToolsClient(FakeAsyncClient):
        async def get(self, url, headers=None):
            if url.endswith("/toolsets"):
                return FakeResponse(
                    {
                        "object": "list",
                        "platform": "api_server",
                        "data": [
                            {
                                "name": "hermes-api-server",
                                "enabled": True,
                                "tools": ["search_files", "execute_code"],
                            }
                        ],
                    }
                )
            return FakeResponse({"status": "ok"})

    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "test-key")
    monkeypatch.setattr(app_main.httpx, "AsyncClient", MissingToolsClient)
    client = TestClient(app_main.app)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["db_query"]["ok"] is False
    assert "db_get_schema_ddl" in body["db_query"]["missing_tools"]
    assert "Required db-query tools" in body["db_query"]["error"]


def test_ready_checks_runtime_and_real_mysql(monkeypatch):
    async def healthy_runtime():
        return {"ok": True}

    monkeypatch.setattr(app_main, "health", healthy_runtime)
    monkeypatch.setattr(
        app_main,
        "_mysql_readiness_check",
        lambda: {"ok": True, "error": None},
    )
    client = TestClient(app_main.app)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "ready",
        "checks": {"runtime": True, "mysql": True},
    }


def test_ready_returns_503_when_mysql_is_unavailable(monkeypatch):
    async def healthy_runtime():
        return {"ok": True}

    monkeypatch.setattr(app_main, "health", healthy_runtime)
    monkeypatch.setattr(
        app_main,
        "_mysql_readiness_check",
        lambda: {"ok": False, "error": "failed"},
    )
    client = TestClient(app_main.app)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {"runtime": True, "mysql": False}


def test_chat_proxies_to_hermes(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "test-key")
    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(app_main.app)

    response = client.post("/api/chat", json={"message": "数据库里有多少客户？"})

    assert response.status_code == 200
    assert response.headers["Deprecation"] == "true"
    assert "Legacy stateless chat" in response.headers["Warning"]
    body = response.json()
    assert body["ok"] is True
    assert body["content"] == "结论：测试响应"


def test_service_auth_protects_all_public_api_routes(monkeypatch):
    monkeypatch.setenv("XPD_SERVICE_AUTH_ENABLED", "true")
    monkeypatch.setenv("XPD_SERVICE_API_KEY", "middle-platform-service-secret")
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "test-key")
    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(app_main.app)

    denied = client.post("/api/chat", json={"message": "分析数据"})
    allowed = client.post(
        "/api/chat",
        headers={"Authorization": "Bearer middle-platform-service-secret"},
        json={"message": "分析数据"},
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "SERVICE_AUTH_FAILED"
    assert allowed.status_code == 200
    assert allowed.json()["content"] == "结论：测试响应"


def test_chat_stream_proxies_sse(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "test-key")
    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeStreamingAsyncClient)
    client = TestClient(app_main.app)

    response = client.post(
        "/api/chat/stream",
        json={"message": "最近30天每个品牌的GMV是多少？", "stream": True},
    )

    assert response.status_code == 200
    assert response.headers["Deprecation"] == "true"
    assert "Legacy stateless chat" in response.headers["Warning"]
    assert "结论" in response.text
    assert "流式" in response.text


def test_wrapper_derives_base_url_from_gateway_env(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_HOST", "127.0.0.9")
    monkeypatch.setenv("HERMES_GATEWAY_PORT", "9999")
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "gateway-key")
    monkeypatch.setenv("HERMES_GATEWAY_MODEL", "hermes-agent-test")

    assert app_main.hermes_base_url() == "http://127.0.0.9:9999/v1"
    assert app_main.hermes_api_key() == "gateway-key"
    assert app_main.hermes_model() == "hermes-agent-test"


def test_streaming_tool_progress_is_only_rendered_in_analysis_panel():
    source = (app_main.STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "appendAssistantProgress(assistantView, progressText);" in source
    assert "assistantView.contentEl.textContent = progressText" not in source
