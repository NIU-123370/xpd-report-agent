from fastapi.testclient import TestClient

from xpd_report_agent.api import main as app_main

CLIENT_HEADERS = {
    "X-XPD-Session-Key": "memory-view-test-key-with-at-least-24-chars"
}


def _configure_memory_home(tmp_path, monkeypatch):
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(
        "[type=lesson] 查询前先确认日期口径。", encoding="utf-8"
    )
    (memory_dir / "USER.md").write_text(
        "[type=preference] 用户偏好中文回答。", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "gateway-test-key")
    monkeypatch.setenv("XPD_SESSION_SIGNING_SECRET", "session-signing-test-key")


def test_memory_files_are_exposed_as_read_only_snapshots(tmp_path, monkeypatch):
    _configure_memory_home(tmp_path, monkeypatch)
    client = TestClient(app_main.app)

    response = client.get("/api/memories", headers=CLIENT_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "local_hermes_profile"
    assert [item["filename"] for item in body["data"]] == ["MEMORY.md", "USER.md"]
    assert body["data"][0]["content"] == "[type=lesson] 查询前先确认日期口径。"
    assert body["data"][0]["used_chars"] > 0
    assert body["data"][0]["limit_chars"] == 2200
    assert body["data"][0]["modified_at"] is not None


def test_memory_file_response_preserves_exact_content(tmp_path, monkeypatch):
    _configure_memory_home(tmp_path, monkeypatch)
    (tmp_path / "memories" / "MEMORY.md").write_text(
        "password: exact-test-value", encoding="utf-8"
    )
    client = TestClient(app_main.app)

    item = client.get("/api/memories", headers=CLIENT_HEADERS).json()["data"][0]

    assert item["content"] == "password: exact-test-value"


def test_memory_file_endpoint_requires_session_key(tmp_path, monkeypatch):
    _configure_memory_home(tmp_path, monkeypatch)
    client = TestClient(app_main.app)

    response = client.get("/api/memories")

    assert response.status_code == 401
