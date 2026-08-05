from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from xpd_report_agent.api import artifact_store as artifact_store_api
from xpd_report_agent.api import main as app_main
from xpd_report_agent.api import sessions as sessions_api
from xpd_report_agent.api.artifact_store import session_exports_dir
from xpd_report_agent.api.reflections import ReflectionQueue
from xpd_report_agent.api.session_service import (
    count_completed_turns,
    owner_scope,
    redact_sensitive_text,
    user_owner_scope,
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
        if method == "POST" and "/clarifications/" in path and path.endswith("/answer"):
            return FakeResponse(
                {
                    "ok": True,
                    "session_id": session_id,
                    "clarification_id": parts[5],
                    "status": "answered",
                }
            )
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


def test_user_id_identity_mode_uses_authenticated_header(monkeypatch):
    monkeypatch.setenv("XPD_IDENTITY_MODE", "user_id")
    client = make_client(monkeypatch)

    created = client.post(
        "/api/sessions",
        headers={"X-User-Id": "operator-123"},
        json={},
    )

    assert created.status_code == 201
    scope = user_owner_scope("operator-123", secret="session-signing-test-key")
    assert created.json()["session"]["session_id"].startswith(f"xpd_{scope}_")

    session_id = created.json()["session"]["session_id"]
    chat = client.post(
        f"/api/sessions/{session_id}/chat",
        headers={"X-User-Id": "operator-123"},
        json={"message": "分析昨天的成交额", "stream": False},
    )
    assert chat.status_code == 200
    chat_call = next(
        call for call in FakeHermesClient.calls if call["url"].endswith("/chat")
    )
    assert chat_call["headers"]["X-Hermes-Session-Key"] == scope
    assert "operator-123" not in chat_call["headers"].values()


def test_user_id_identity_mode_rejects_missing_or_invalid_header(monkeypatch):
    monkeypatch.setenv("XPD_IDENTITY_MODE", "user_id")
    client = make_client(monkeypatch)

    missing = client.post("/api/sessions", headers=CLIENT_HEADERS, json={})
    invalid = client.post(
        "/api/sessions",
        headers={"X-User-Id": "operator 123"},
        json={},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401


def test_default_identity_mode_ignores_spoofed_user_id(monkeypatch):
    monkeypatch.delenv("XPD_IDENTITY_MODE", raising=False)
    client = make_client(monkeypatch)

    created = client.post(
        "/api/sessions",
        headers={**CLIENT_HEADERS, "X-User-Id": "operator-123"},
        json={},
    )

    assert created.status_code == 201
    scope = owner_scope(CLIENT_KEY, secret="session-signing-test-key")
    assert created.json()["session"]["session_id"].startswith(f"xpd_{scope}_")
    assert scope != user_owner_scope("operator-123", secret="session-signing-test-key")


def test_user_id_identity_mode_hides_another_users_session(monkeypatch):
    monkeypatch.setenv("XPD_IDENTITY_MODE", "user_id")
    client = make_client(monkeypatch)
    created = client.post(
        "/api/sessions",
        headers={"X-User-Id": "operator-a"},
        json={},
    )
    session_id = created.json()["session"]["session_id"]
    FakeHermesClient.calls.clear()

    response = client.get(
        f"/api/sessions/{session_id}",
        headers={"X-User-Id": "operator-b"},
    )

    assert response.status_code == 404
    assert FakeHermesClient.calls == []


def test_invalid_identity_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("XPD_IDENTITY_MODE", "unexpected")
    client = make_client(monkeypatch)

    response = client.post("/api/sessions", headers=CLIENT_HEADERS, json={})

    assert response.status_code == 500
    assert "XPD_IDENTITY_MODE" in response.json()["detail"]


def test_artifact_list_download_owner_isolation_and_session_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("XPD_FILE_STORAGE_PATH", str(tmp_path))
    client = make_client(monkeypatch)
    session_id = client.post("/api/sessions", headers=CLIENT_HEADERS, json={}).json()[
        "session"
    ]["session_id"]
    artifact_id = "art_" + "a" * 32
    exports = session_exports_dir(session_id, create=True)
    content = b"item_id,pay_amt\n1,100\n"
    (exports / f"{artifact_id}__report.csv").write_bytes(content)

    listed = client.get(
        f"/api/sessions/{session_id}/artifacts", headers=CLIENT_HEADERS
    )
    assert listed.status_code == 200
    artifact = listed.json()["data"][0]
    assert artifact["artifact_id"] == artifact_id
    assert artifact["filename"] == "report.csv"

    downloaded = client.get(
        artifact["download_url"],
        headers={**CLIENT_HEADERS, "Accept": "application/json"},
    )
    assert downloaded.status_code == 200
    assert downloaded.content == content
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert downloaded.headers["x-content-type-options"] == "nosniff"

    other_headers = {"X-XPD-Session-Key": "another-client-key-with-24-characters"}
    denied = client.get(artifact["download_url"], headers=other_headers)
    assert denied.status_code == 404

    invalid = client.get(
        f"/api/sessions/{session_id}/artifacts/art_invalid/download",
        headers=CLIENT_HEADERS,
    )
    assert invalid.status_code == 404

    deleted = client.delete(f"/api/sessions/{session_id}", headers=CLIENT_HEADERS)
    assert deleted.status_code == 200
    assert not (tmp_path / session_id).exists()


def test_oss_artifact_list_uses_stable_service_url_and_download_mints_fresh_url(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XPD_FILE_STORAGE_PATH", str(tmp_path))
    calls: list[str] = []

    def fake_remote_payload(path, *, session_id, artifact_id, filename):
        calls.append(artifact_id)
        sequence = len(calls)
        return {
            "storage": "oss",
            "oss_uri": f"oss://reports/{filename}",
            "object_key": filename,
            "download_url": f"https://signed.example/{filename}?version={sequence}",
            "download_url_expires_at": f"2030-01-01T00:00:0{sequence}+00:00",
        }

    monkeypatch.setattr(
        artifact_store_api,
        "remote_artifact_payload",
        fake_remote_payload,
    )
    client = make_client(monkeypatch)
    session_id = client.post("/api/sessions", headers=CLIENT_HEADERS, json={}).json()[
        "session"
    ]["session_id"]
    artifact_id = "art_" + "b" * 32
    exports = session_exports_dir(session_id, create=True)
    (exports / f"{artifact_id}__经营报告.xlsx").write_bytes(b"xlsx")

    listed = client.get(
        f"/api/sessions/{session_id}/artifacts",
        headers=CLIENT_HEADERS,
    )

    assert listed.status_code == 200
    artifact = listed.json()["data"][0]
    stable_url = f"/api/sessions/{session_id}/artifacts/{artifact_id}/download"
    assert artifact["download_url"] == stable_url
    assert "signed.example" not in str(artifact)
    assert "download_url_expires_at" not in artifact

    resolved = client.get(
        stable_url,
        headers={**CLIENT_HEADERS, "Accept": "application/json"},
    )

    assert resolved.status_code == 200
    assert resolved.headers["cache-control"] == "private, no-store"
    assert resolved.json()["download_url"].endswith("?version=2")
    assert calls == [artifact_id, artifact_id]


def test_sse_relay_waits_for_complete_frame_then_emits_artifact_and_falls_back(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XPD_FILE_STORAGE_PATH", str(tmp_path))
    client = make_client(monkeypatch)
    session_id = client.post("/api/sessions", headers=CLIENT_HEADERS, json={}).json()[
        "session"
    ]["session_id"]
    exports = session_exports_dir(session_id, create=True)
    old_id = "art_" + "1" * 32
    immediate_id = "art_" + "2" * 32
    fallback_id = "art_" + "3" * 32
    (exports / f"{old_id}__old.csv").write_text("old", encoding="utf-8")
    baseline = sessions_api._artifact_ids(session_id)

    async def upstream():
        yield "event: tool.com"
        (exports / f"{immediate_id}__new.xlsx").write_bytes(b"new")
        yield 'pleted\r\ndata: {"tool_name":"export_report_file"}\r\n'
        yield "\r\n"
        yield 'event: assistant.delta\ndata: {"delta":"done"}\n\n'
        (exports / f"{fallback_id}__fallback.csv").write_text(
            "fallback", encoding="utf-8"
        )

    async def collect() -> str:
        output: list[str] = []
        async for chunk in sessions_api._relay_hermes_sse_with_artifacts(
            upstream(),
            session_id=session_id,
            existing_artifact_ids=baseline,
            emitted_artifact_ids=set(),
        ):
            output.append(chunk)
        return "".join(output)

    relayed = asyncio.run(collect())
    tool_frame = (
        'event: tool.completed\r\ndata: {"tool_name":"export_report_file"}\r\n\r\n'
    )

    assert relayed.startswith(tool_frame)
    assert relayed.count("event: artifact.ready") == 2
    assert old_id not in relayed
    assert relayed.index(immediate_id) > relayed.index(tool_frame)
    assert relayed.index(immediate_id) < relayed.index("event: assistant.delta")
    assert relayed.index(fallback_id) > relayed.index("event: assistant.delta")


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


def test_clarification_answer_checks_owner_and_forwards_opaque_scope(monkeypatch):
    client = make_client(monkeypatch)
    session_id = client.post("/api/sessions", headers=CLIENT_HEADERS, json={}).json()[
        "session"
    ]["session_id"]

    response = client.post(
        f"/api/sessions/{session_id}/clarifications/clarify_123/answer",
        headers=CLIENT_HEADERS,
        json={"answer": "按支付金额"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "answered"
    call = FakeHermesClient.calls[-1]
    assert call["json"] == {"answer": "按支付金额"}
    assert call["headers"]["X-Hermes-Session-Key"] != CLIENT_KEY


def test_clarification_answer_hides_cross_owner_session_before_upstream(monkeypatch):
    client = make_client(monkeypatch)

    response = client.post(
        "/api/sessions/xpd_someone_else_session/clarifications/clarify_123/answer",
        headers=CLIENT_HEADERS,
        json={"answer": "支付金额"},
    )

    assert response.status_code == 404
    assert FakeHermesClient.calls == []


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


def test_memory_capacity_notice_uses_personal_scope_in_user_id_mode(
    tmp_path, monkeypatch
):
    scope = "a" * 20
    root = tmp_path / "memories"
    personal = root / "users" / scope
    merchant = root / "merchant"
    personal.mkdir(parents=True)
    merchant.mkdir()
    (root / "MEMORY.md").write_text("x" * 999, encoding="utf-8")
    (personal / "MEMORY.md").write_text("x" * 205, encoding="utf-8")
    (personal / "USER.md").write_text("用户画像", encoding="utf-8")
    (merchant / "MEMORY.md").write_text("统一经营口径", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("XPD_IDENTITY_MODE", "user_id")
    monkeypatch.setenv("XPD_MEMORY_CHAR_LIMIT", "100")
    monkeypatch.setenv("XPD_USER_CHAR_LIMIT", "100")

    notice = sessions_api.memory_capacity_notice(scope)

    assert "MEMORY.md=205/256" in notice
    assert "USER.md=4/256" in notice
    assert "merchant/MEMORY.md=6 字符" in notice
    assert "999" not in notice


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
