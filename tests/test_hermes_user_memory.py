from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from xpd_report_agent.runtime import hermes_user_memory

DELIMITER = "\n§\n"


class FakeMemoryStore:
    def __init__(self, memory_char_limit=2200, user_char_limit=1375):
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        self.memory_entries = []
        self.user_entries = []
        self._system_prompt_snapshot = {"memory": "", "user": ""}

    @staticmethod
    def _read_file(path: Path):
        if not path.exists():
            return []
        return [item for item in path.read_text(encoding="utf-8").split(DELIMITER) if item]

    @staticmethod
    def _write_file(path: Path, entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DELIMITER.join(entries), encoding="utf-8")

    @staticmethod
    def _sanitize_entries_for_snapshot(entries, _filename):
        return list(entries)

    def _render_block(self, target, entries):
        return f"{target.upper()}:" + "|".join(entries) if entries else ""

    def _entries_for(self, target):
        return self.user_entries if target == "user" else self.memory_entries

    def load_from_disk(self):
        root = Path(sys.modules["hermes_constants"].get_hermes_home()) / "memories"
        self.memory_entries = self._read_file(root / "MEMORY.md")
        self.user_entries = self._read_file(root / "USER.md")
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }

    def save_to_disk(self, target):
        root = Path(sys.modules["hermes_constants"].get_hermes_home()) / "memories"
        filename = "USER.md" if target == "user" else "MEMORY.md"
        self._write_file(root / filename, self._entries_for(target))

    def format_for_system_prompt(self, target):
        return self._system_prompt_snapshot.get(target) or None


def _install_fake_hermes(monkeypatch, tmp_path):
    calls = []

    class FakeAdapter:
        def _create_agent(
            self,
            ephemeral_system_prompt=None,
            session_id=None,
            gateway_session_key=None,
        ):
            calls.append((session_id, gateway_session_key))
            store = FakeMemoryStore()
            store.load_from_disk()
            return SimpleNamespace(
                session_id=session_id,
                _memory_store=store,
                _memory_enabled=True,
                _user_profile_enabled=True,
            )

    gateway = types.ModuleType("gateway")
    platforms = types.ModuleType("gateway.platforms")
    api_server = types.ModuleType("gateway.platforms.api_server")
    api_server.APIServerAdapter = FakeAdapter
    platforms.api_server = api_server
    memory_tool = types.ModuleType("tools.memory_tool")
    memory_tool.MemoryStore = FakeMemoryStore
    tools = types.ModuleType("tools")
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms)
    monkeypatch.setitem(sys.modules, "gateway.platforms.api_server", api_server)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.memory_tool", memory_tool)
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)
    hermes_user_memory.install_patch()
    return FakeAdapter, calls


def test_user_mode_loads_shared_and_personal_memory_without_cross_user_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("XPD_IDENTITY_MODE", "user_id")
    root = tmp_path / "memories"
    scope_a = "a" * 20
    scope_b = "b" * 20
    (root / "merchant").mkdir(parents=True)
    (root / "users" / scope_a).mkdir(parents=True)
    (root / "MEMORY.md").write_text("本地根记忆", encoding="utf-8")
    (root / "USER.md").write_text("本地根画像", encoding="utf-8")
    (root / "merchant" / "MEMORY.md").write_text("统一成交口径", encoding="utf-8")
    (root / "users" / scope_a / "MEMORY.md").write_text("A个人记忆", encoding="utf-8")
    (root / "users" / scope_a / "USER.md").write_text("A个人画像", encoding="utf-8")
    Adapter, _calls = _install_fake_hermes(monkeypatch, tmp_path)

    agent_a = Adapter()._create_agent(
        session_id=f"xpd_{scope_a}_session_a", gateway_session_key=scope_a
    )
    agent_b = Adapter()._create_agent(
        session_id=f"xpd_{scope_b}_session_b", gateway_session_key=scope_b
    )

    assert agent_a._memory_store.memory_entries == ["A个人记忆"]
    assert agent_a._memory_store.user_entries == ["A个人画像"]
    prompt = agent_a._memory_store.format_for_system_prompt("memory")
    assert "统一成交口径" in prompt
    assert "A个人记忆" in prompt
    assert "本地根记忆" not in prompt
    assert agent_b._memory_store.memory_entries == []
    assert agent_b._memory_store.user_entries == []

    agent_a._memory_store.memory_entries.append("A新增反思")
    merchant_before = (root / "merchant" / "MEMORY.md").read_bytes()
    agent_a._memory_store.save_to_disk("memory")
    assert "A新增反思" in (root / "users" / scope_a / "MEMORY.md").read_text(
        encoding="utf-8"
    )
    assert (root / "merchant" / "MEMORY.md").read_bytes() == merchant_before
    assert (root / "MEMORY.md").read_text(encoding="utf-8") == "本地根记忆"


def test_local_mode_keeps_legacy_root_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("XPD_IDENTITY_MODE", "session_key")
    root = tmp_path / "memories"
    root.mkdir()
    (root / "MEMORY.md").write_text("本地记忆", encoding="utf-8")
    (root / "USER.md").write_text("本地画像", encoding="utf-8")
    Adapter, _calls = _install_fake_hermes(monkeypatch, tmp_path)

    agent = Adapter()._create_agent(
        session_id="xpd_local_session", gateway_session_key="not-an-owner-scope"
    )

    assert type(agent._memory_store) is FakeMemoryStore
    assert agent._memory_store.memory_entries == ["本地记忆"]
    assert agent._memory_store.user_entries == ["本地画像"]


def test_user_mode_rejects_missing_or_mismatched_scope_before_agent_creation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("XPD_IDENTITY_MODE", "user_id")
    Adapter, calls = _install_fake_hermes(monkeypatch, tmp_path)
    scope_a = "a" * 20
    scope_b = "b" * 20

    with pytest.raises(PermissionError):
        Adapter()._create_agent(session_id=f"xpd_{scope_a}_session", gateway_session_key=None)
    with pytest.raises(PermissionError):
        Adapter()._create_agent(
            session_id=f"xpd_{scope_a}_session", gateway_session_key=scope_b
        )

    assert calls == []
