from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from xpd_report_agent.runtime import hermes_config as hermes_config_module
from xpd_report_agent.runtime.hermes_config import (
    API_SERVER_TOOLSETS,
    configure_config,
)


def test_configure_config_enables_db_query_for_api_server(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "plugins": {"enabled": ["other"], "disabled": ["db-query"]},
                "platform_toolsets": {"cli": ["terminal"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = configure_config(config_path, model_config={})
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert result["api_server_toolsets"] == API_SERVER_TOOLSETS
    assert result["session_search_enabled"] is True
    assert result["required_memory_tools"] == ["session_search", "memory"]
    assert "db-query" in data["plugins"]["enabled"]
    assert "db-query" not in data["plugins"]["disabled"]
    assert data["platform_toolsets"]["api_server"] == API_SERVER_TOOLSETS
    assert "db_query" in data["known_plugin_toolsets"]["api_server"]
    assert "report_file" in data["known_plugin_toolsets"]["api_server"]
    assert "file" in data["platform_toolsets"]["api_server"]
    assert data["memory"] == {
        "memory_enabled": True,
        "user_profile_enabled": True,
        "memory_char_limit": 2200,
        "user_char_limit": 1375,
        "nudge_interval": 3,
        "flush_min_turns": 3,
    }
    assert result["timezone"] == "Asia/Shanghai"
    assert result["cron"]["max_parallel_jobs"] == 1
    assert data["timezone"] == "Asia/Shanghai"
    assert data["cron"]["max_parallel_jobs"] == 1


def test_user_id_mode_disables_unscoped_session_search_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XPD_IDENTITY_MODE", "user_id")
    monkeypatch.delenv("XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED", raising=False)
    config_path = tmp_path / "config.yaml"

    result = configure_config(config_path, model_config={})
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert "session_search" not in result["api_server_toolsets"]
    assert "session_search" not in data["platform_toolsets"]["api_server"]
    assert "memory" in result["api_server_toolsets"]
    assert result["session_search_enabled"] is False
    assert result["required_memory_tools"] == ["memory"]


def test_user_id_mode_requires_explicit_unsafe_switch_for_session_search(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XPD_IDENTITY_MODE", "user_id")
    monkeypatch.setenv("XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED", "true")
    config_path = tmp_path / "config.yaml"

    result = configure_config(config_path, model_config={})

    assert "session_search" in result["api_server_toolsets"]
    assert result["session_search_enabled"] is True
    assert result["required_memory_tools"] == ["session_search", "memory"]


def test_configure_config_writes_model_config_without_leaking_key(tmp_path):
    config_path = tmp_path / "config.yaml"

    result = configure_config(
        config_path,
        model_config={
            "default": "qwen3.7-max",
            "provider": "custom",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_mode": "chat_completions",
            "api_key": "secret-key",
        },
        require_model_key=True,
    )
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert result["model_configured"] is True
    assert data["model"] == {
        "default": "qwen3.7-max",
        "provider": "custom",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_mode": "chat_completions",
        "api_key": "secret-key",
    }
    assert "secret-key" not in str(result)


def test_configure_config_preserves_existing_model_key_when_env_key_empty(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"model": {"api_key": "existing-key"}}, sort_keys=False),
        encoding="utf-8",
    )

    configure_config(
        config_path,
        model_config={"default": "qwen3.7-max", "api_key": ""},
        require_model_key=True,
    )
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert data["model"]["default"] == "qwen3.7-max"
    assert data["model"]["api_key"] == "existing-key"


def test_configure_config_requires_model_key(tmp_path):
    config_path = tmp_path / "config.yaml"

    try:
        configure_config(config_path, model_config={"default": "qwen3.7-max"}, require_model_key=True)
    except RuntimeError as exc:
        assert "HERMES_LLM_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected missing model api_key to fail.")


def test_configure_config_uses_unique_atomic_temporary_files(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    replacements: list[tuple[Path, Path]] = []
    fsync_calls: list[int] = []
    real_replace = hermes_config_module.os.replace
    real_fsync = hermes_config_module.os.fsync

    def recording_replace(source, target):
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    def recording_fsync(file_descriptor):
        fsync_calls.append(file_descriptor)
        real_fsync(file_descriptor)

    monkeypatch.setattr(hermes_config_module.os, "replace", recording_replace)
    monkeypatch.setattr(hermes_config_module.os, "fsync", recording_fsync)

    configure_config(config_path, model_config={})
    configure_config(config_path, model_config={})

    temporary_paths = [source for source, _target in replacements]
    assert len(set(temporary_paths)) == 2
    assert all(path.parent == config_path.parent for path in temporary_paths)
    assert all(path.name.startswith(".config.yaml.") for path in temporary_paths)
    assert all(path.suffix == ".tmp" for path in temporary_paths)
    assert all(target == config_path for _source, target in replacements)
    assert all(not path.exists() for path in temporary_paths)
    assert len(fsync_calls) == 4
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["plugins"]


def test_configure_config_removes_temporary_file_when_replace_fails(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    original = "model:\n  api_key: existing\n"
    config_path.write_text(original, encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(hermes_config_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        configure_config(config_path, model_config={})

    assert config_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".config.yaml.*.tmp")) == []
