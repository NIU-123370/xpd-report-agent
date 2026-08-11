from fastapi.testclient import TestClient

from xpd_report_agent.api import main as app_main
from xpd_report_agent.api.session_service import user_owner_scope

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


def test_memory_file_endpoint_accepts_user_id_when_enabled(tmp_path, monkeypatch):
    _configure_memory_home(tmp_path, monkeypatch)
    monkeypatch.setenv("XPD_IDENTITY_MODE", "user_id")
    scope = user_owner_scope("operator-123", secret="session-signing-test-key")
    merchant_dir = tmp_path / "memories" / "merchant"
    personal_dir = tmp_path / "memories" / "users" / scope
    merchant_dir.mkdir()
    personal_dir.mkdir(parents=True)
    (merchant_dir / "MEMORY.md").write_text(
        "[type=metric] 成交额统一使用 pay_amt。", encoding="utf-8"
    )
    (personal_dir / "MEMORY.md").write_text(
        "[type=lesson] 该用户关注商品异常。", encoding="utf-8"
    )
    (personal_dir / "USER.md").write_text(
        "[type=preference] 该用户偏好日报。", encoding="utf-8"
    )
    client = TestClient(app_main.app)

    response = client.get("/api/memories", headers={"X-User-Id": "operator-123"})

    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "authenticated_user"
    assert body["identity_mode"] == "user_id"
    assert [item["filename"] for item in body["data"]] == [
        "merchant/MEMORY.md",
        "personal/MEMORY.md",
        "personal/USER.md",
    ]
    assert [item["content"] for item in body["data"]] == [
        "[type=metric] 成交额统一使用 pay_amt。",
        "[type=lesson] 该用户关注商品异常。",
        "[type=preference] 该用户偏好日报。",
    ]
    assert body["data"][0]["read_only"] is True
    assert body["data"][1]["read_only"] is False


def test_user_memory_view_does_not_fall_back_to_local_or_another_user(
    tmp_path, monkeypatch
):
    _configure_memory_home(tmp_path, monkeypatch)
    monkeypatch.setenv("XPD_IDENTITY_MODE", "user_id")
    scope_a = user_owner_scope("operator-a", secret="session-signing-test-key")
    personal_a = tmp_path / "memories" / "users" / scope_a
    personal_a.mkdir(parents=True)
    (personal_a / "MEMORY.md").write_text("A 的记忆", encoding="utf-8")
    (personal_a / "USER.md").write_text("A 的画像", encoding="utf-8")
    client = TestClient(app_main.app)

    response = client.get("/api/memories", headers={"X-User-Id": "operator-b"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data[1]["content"] == ""
    assert data[2]["content"] == ""
    assert "A 的记忆" not in str(data)
    assert "查询前先确认日期口径" not in str(data)


def test_shared_memory_root_is_independent_from_instance_hermes_home(
    tmp_path, monkeypatch
):
    instance_home = tmp_path / "instances" / "hermes-2"
    shared_memory = tmp_path / "shared" / "memories"
    instance_home.mkdir(parents=True)
    shared_memory.mkdir(parents=True)
    (shared_memory / "MEMORY.md").write_text("共享记忆", encoding="utf-8")
    (shared_memory / "USER.md").write_text("共享画像", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(instance_home))
    monkeypatch.setenv("XPD_MEMORY_ROOT", str(shared_memory))
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "gateway-test-key")
    monkeypatch.setenv("XPD_SESSION_SIGNING_SECRET", "session-signing-test-key")
    client = TestClient(app_main.app)

    response = client.get("/api/memories", headers=CLIENT_HEADERS)

    assert response.status_code == 200
    assert [item["content"] for item in response.json()["data"]] == [
        "共享记忆",
        "共享画像",
    ]
