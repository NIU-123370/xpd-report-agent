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


def test_chat_proxies_to_hermes(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "test-key")
    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(app_main.app)

    response = client.post("/api/chat", json={"message": "数据库里有多少客户？"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["content"] == "结论：测试响应"


def test_chat_stream_proxies_sse(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "test-key")
    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeStreamingAsyncClient)
    client = TestClient(app_main.app)

    response = client.post(
        "/api/chat/stream",
        json={"message": "最近30天每个品牌的GMV是多少？", "stream": True},
    )

    assert response.status_code == 200
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
