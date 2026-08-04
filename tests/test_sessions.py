from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from xpd_report_agent.api import main as app_main
from xpd_report_agent.api import sessions as sessions_api
from xpd_report_agent.api.reflections import ReflectionQueue
from xpd_report_agent.api.session_service import (
    count_completed_turns,
    owner_scope,
    redact_sensitive_text,
)

CLIENT_KEY = "client-test-key-with-at-least-24-chars"
CLIENT_HEADERS = {"X-XPD-Session-Key": CLIENT_KEY}


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

    async def request(self, method, url, headers=None, json=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers or {}, "json": json}
        )
        path = url.split("127.0.0.1:8642", 1)[-1]
        if method == "POST" and path == "/api/sessions":
            session_id = json["id"]
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

        if method == "GET" and path.startswith("/api/sessions?"):
            return FakeResponse({"data": list(self.sessions.values())})

        parts = path.split("/")
        session_id = parts[3] if len(parts) > 3 else ""
        session = self.sessions.get(session_id)
        if not session:
            return FakeResponse({"error": "not found"}, 404)

        if method == "GET" and path.endswith("/messages"):
            return FakeResponse(
                {"session_id": session_id, "data": self.messages.get(session_id, [])}
            )
        if method == "GET":
            return FakeResponse({"session": session})
        if method == "PATCH":
            if "title" in json:
                session["title"] = json["title"]
            if json.get("end_reason"):
                session["end_reason"] = json["end_reason"]
                session["ended_at"] = 200.0
            return FakeResponse({"session": session})
        if method == "POST" and path.endswith("/chat"):
            self.messages[session_id].extend(
                [
                    {"id": 1, "session_id": session_id, "role": "user", "content": json["message"]},
                    {
                        "id": 2,
                        "session_id": session_id,
                        "role": "assistant",
                        "content": "测试回答",
                        "reasoning_content": "先确认查询口径，再形成回答。",
                    },
                ]
            )
            session["message_count"] = len(self.messages[session_id])
            return FakeResponse(
                {
                    "session_id": session_id,
                    "message": {"role": "assistant", "content": "测试回答"},
                    "usage": {"total_tokens": 4},
                }
            )
        if method == "DELETE":
            self.sessions.pop(session_id, None)
            self.messages.pop(session_id, None)
            return FakeResponse({"id": session_id, "deleted": True})
        return FakeResponse({}, 400)


def make_client(monkeypatch) -> TestClient:
    FakeHermesClient.reset()
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "gateway-test-key")
    monkeypatch.setenv("XPD_SESSION_SIGNING_SECRET", "session-signing-test-key")
    monkeypatch.setenv("XPD_FINAL_REFLECTION_ENABLED", "false")
    monkeypatch.setattr(sessions_api.httpx, "AsyncClient", FakeHermesClient)
    return TestClient(app_main.app)


def test_session_crud_uses_owned_native_hermes_session(monkeypatch):
    client = make_client(monkeypatch)

    created = client.post("/api/sessions", headers=CLIENT_HEADERS, json={})
    assert created.status_code == 201
    session = created.json()["session"]
    scope = owner_scope(CLIENT_KEY, secret="session-signing-test-key")
    assert session["session_id"].startswith(f"xpd_{scope}_")
    assert session["status"] == "active"

    listed = client.get("/api/sessions", headers=CLIENT_HEADERS).json()["data"]
    assert [item["session_id"] for item in listed] == [session["session_id"]]

    renamed = client.patch(
        f"/api/sessions/{session['session_id']}",
        headers=CLIENT_HEADERS,
        json={"title": "直播日报"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["session"]["title"] == "直播日报"

    deleted = client.delete(
        f"/api/sessions/{session['session_id']}", headers=CLIENT_HEADERS
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_session_owner_mismatch_is_hidden_before_upstream_call(monkeypatch):
    client = make_client(monkeypatch)

    response = client.get(
        "/api/sessions/xpd_someone_else_0123456789abcdef", headers=CLIENT_HEADERS
    )

    assert response.status_code == 404
    assert FakeHermesClient.calls == []


def test_session_chat_sends_only_current_message_and_opaque_memory_scope(monkeypatch):
    client = make_client(monkeypatch)
    session_id = client.post("/api/sessions", headers=CLIENT_HEADERS, json={}).json()[
        "session"
    ]["session_id"]

    response = client.post(
        f"/api/sessions/{session_id}/chat",
        headers=CLIENT_HEADERS,
        json={"message": "查一下昨天的商品成交额", "stream": False},
    )

    assert response.status_code == 200
    assert response.json()["content"] == "测试回答"
    assert response.json()["reasoning"] == "先确认查询口径，再形成回答。"
    chat_call = next(call for call in FakeHermesClient.calls if call["url"].endswith("/chat"))
    assert chat_call["json"]["message"] == "查一下昨天的商品成交额"
    assert "history" not in chat_call["json"]
    assert "session_search" in chat_call["json"]["system_message"]
    assert chat_call["headers"]["X-Hermes-Session-Key"] != CLIENT_KEY


def test_messages_expose_one_normalized_reasoning_field(monkeypatch):
    client = make_client(monkeypatch)
    session_id = client.post("/api/sessions", headers=CLIENT_HEADERS, json={}).json()[
        "session"
    ]["session_id"]
    FakeHermesClient.messages[session_id] = [
        {
            "id": 1,
            "session_id": session_id,
            "role": "assistant",
            "content": "可见结论",
            "reasoning": "旧字段思考",
            "reasoning_content": "\n模型完整思考过程\n",
        }
    ]

    response = client.get(
        f"/api/sessions/{session_id}/messages", headers=CLIENT_HEADERS
    )

    assert response.status_code == 200
    message = response.json()["data"][0]
    assert message["content"] == "可见结论"
    assert message["reasoning"] == "\n模型完整思考过程\n"
    assert "reasoning_content" not in message


def test_sensitive_credentials_are_redacted_before_reflection():
    content = (
        "password: abc123 token=secret-token-value "
        "Authorization: Bearer abcdefghijklmnop "
        "mysql://root:pass@localhost/reports"
    )

    redacted = redact_sensitive_text(content)

    assert "abc123" not in redacted
    assert "secret-token-value" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "root:pass" not in redacted


def test_completed_turn_count_ignores_tool_messages_and_tool_call_assistants():
    messages = [
        {"role": "user", "content": "查询 GMV"},
        {"role": "assistant", "content": "", "tool_calls": [{"name": "db_execute_sql"}]},
        {"role": "tool", "content": "query result"},
        {"role": "assistant", "content": "GMV 是 100"},
        {"role": "user", "content": "按类目拆分"},
        {"role": "assistant", "content": "类目结果如下"},
    ]

    assert count_completed_turns(messages) == 2


def test_memory_capacity_notice_enforces_configured_watermark(tmp_path, monkeypatch):
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("x" * 205, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("XPD_MEMORY_CHAR_LIMIT", "100")
    monkeypatch.setenv("XPD_USER_CHAR_LIMIT", "100")
    monkeypatch.setenv("XPD_MEMORY_CONSOLIDATION_RATIO", "0.8")

    notice = sessions_api.memory_capacity_notice()

    assert "MEMORY.md=205/256" in notice
    assert "已达到整理水位" in notice


def test_reflection_queue_is_durable_and_idempotent(tmp_path: Path):
    calls = []

    async def executor(job):
        calls.append(job["idempotency_key"])
        return {"session_summary": "done"}

    async def scenario():
        queue = ReflectionQueue(executor, path=tmp_path / "reflections.json")
        first = queue.schedule(
            session_id="xpd_owner_session",
            owner_scope="owner",
            turn_end=3,
            end_reason="user_close",
        )
        second = queue.schedule(
            session_id="xpd_owner_session",
            owner_scope="owner",
            turn_end=3,
            end_reason="user_close",
        )
        assert first["reflection_id"] == second["reflection_id"]
        await asyncio.gather(*list(queue._tasks))
        completed = queue.get(first["reflection_id"])
        assert completed["status"] == "succeeded"

        reloaded = ReflectionQueue(executor, path=tmp_path / "reflections.json")
        duplicate = reloaded.schedule(
            session_id="xpd_owner_session",
            owner_scope="owner",
            turn_end=3,
            end_reason="user_close",
        )
        assert duplicate["status"] == "succeeded"

    asyncio.run(scenario())
    assert len(calls) == 1
