from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from xpd_report_agent.runtime.launcher import (
    LaunchError,
    LaunchManager,
    ServiceSpec,
    load_runtime_config,
    normalize_env,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HERMES_SERVICE_SCRIPT = PROJECT_ROOT / "scripts" / "services" / "hermes.sh"


def _write_command_stub(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s" "${0##*/}" >> "$XPD_TEST_CALL_LOG"\n'
        'printf "|%s" "$@" >> "$XPD_TEST_CALL_LOG"\n'
        'printf "\\n" >> "$XPD_TEST_CALL_LOG"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _stage_hermes_service(
    tmp_path: Path,
    *,
    local_env: str | None = None,
) -> tuple[Path, dict[str, str], Path]:
    root = tmp_path / "project"
    script = root / "scripts" / "services" / "hermes.sh"
    script.parent.mkdir(parents=True)
    script.write_text(HERMES_SERVICE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    configs_dir = root / "configs"
    configs_dir.mkdir()
    (configs_dir / "hermes-runtime.lock").write_text(
        "HERMES_AGENT_REPOSITORY=https://github.com/NousResearch/hermes-agent.git\n"
        "HERMES_AGENT_VERSION=0.19.0\n"
        "HERMES_AGENT_COMMIT=a61183b56fdb45b9d2a0f2f6b8482e665ccf702f\n",
        encoding="utf-8",
    )

    plugin_dir = root / "src" / "xpd_report_agent" / "hermes_plugin" / "db_query"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("name: test\n", encoding="utf-8")
    skill_dir = root / "skills" / "db-multitable-query"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Test skill\n", encoding="utf-8")
    if local_env is not None:
        (configs_dir / "local.env").write_text(local_env, encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("project-python", "hermes-python", "hermes", "uv"):
        _write_command_stub(bin_dir / name)
    (bin_dir / "hermes").write_text(
        (bin_dir / "hermes").read_text(encoding="utf-8")
        + 'if [ "${1:-}" = "--version" ]; then\n'
        + '  echo "Hermes Agent v0.19.0 (test) · upstream a61183b5"\n'
        + "fi\n",
        encoding="utf-8",
    )

    call_log = tmp_path / "calls.log"
    env = os.environ.copy()
    current_path = env.get("PATH", "")
    env.update(
        {
            "HOME": str(home),
            "LAUNCH_MANAGED": "true",
            "PROJECT_PYTHON": str(bin_dir / "project-python"),
            "HERMES_PY": str(bin_dir / "hermes-python"),
            "HERMES_BIN": str(bin_dir / "hermes"),
            "HERMES_REQUIRE_LLM_API_KEY": "false",
            "XPD_TEST_CALL_LOG": str(call_log),
            "PATH": os.pathsep.join(filter(None, (str(bin_dir), current_path))),
        }
    )
    return script, env, call_log


def _run_hermes_service(script: Path, env: dict[str, str]) -> None:
    subprocess.run(
        ["bash", str(script), "run"],
        cwd=script.parents[2],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_hermes_service_run_does_not_bootstrap_by_default(tmp_path):
    script, env, call_log = _stage_hermes_service(tmp_path)
    env.pop("HERMES_BOOTSTRAP_ON_START", None)

    _run_hermes_service(script, env)

    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "hermes|--version",
        "project-python|scripts/configure_hermes.py",
        "hermes|gateway|run|--external-supervisor",
    ]


def test_hermes_service_run_preserves_explicit_bootstrap(tmp_path):
    script, env, call_log = _stage_hermes_service(tmp_path)
    env["HERMES_BOOTSTRAP_ON_START"] = "true"

    _run_hermes_service(script, env)

    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "hermes|--version"
    assert calls[1].startswith("uv|export|")
    assert calls[2].startswith("uv|pip|install|")
    assert calls[3:] == [
        "project-python|scripts/configure_hermes.py",
        "hermes|plugins|enable|db-query",
        "hermes|gateway|run|--external-supervisor",
    ]


def test_hermes_service_process_env_overrides_local_env(tmp_path):
    script, env, call_log = _stage_hermes_service(
        tmp_path,
        local_env="HERMES_BOOTSTRAP_ON_START=false\n",
    )
    env["LAUNCH_MANAGED"] = "false"
    env["HERMES_BOOTSTRAP_ON_START"] = "true"

    _run_hermes_service(script, env)

    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "hermes|--version"
    assert calls[1].startswith("uv|export|")
    assert "hermes|plugins|enable|db-query" in calls


def test_hermes_service_rejects_runtime_version_mismatch(tmp_path):
    script, env, _call_log = _stage_hermes_service(tmp_path)
    lock_path = script.parents[2] / "configs" / "hermes-runtime.lock"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace(
            "HERMES_AGENT_VERSION=0.19.0",
            "HERMES_AGENT_VERSION=9.9.9",
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(script), "verify"],
        cwd=script.parents[2],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Hermes version mismatch. Required v9.9.9." in result.stderr


def test_hermes_service_accepts_matching_local_revision_marker(tmp_path):
    script, env, _call_log = _stage_hermes_service(tmp_path)
    hermes_bin = Path(env["HERMES_BIN"])
    hermes_bin.write_text(
        hermes_bin.read_text(encoding="utf-8").replace(
            "upstream a61183b5",
            "local a61183b5",
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(script), "verify"],
        cwd=script.parents[2],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0


def test_normalize_env_derives_gateway_and_fastapi_variables(tmp_path):
    config = normalize_env(
        {
            "HERMES_GATEWAY_HOST": "127.0.0.2",
            "HERMES_GATEWAY_PORT": "9000",
            "HERMES_GATEWAY_API_KEY": "gateway-secret",
            "MYSQL_HOST": "127.0.0.2",
            "MYSQL_PORT": "3307",
            "MYSQL_USER": "report_reader",
            "MYSQL_PASSWORD": "mysql-secret",
            "MYSQL_DATABASE": "reports",
            "XPD_MERCHANT_QUESTION_LIBRARY_ENABLED": "false",
            "XPD_MERCHANT_QUESTION_TOP_K": "2",
            "XPD_MERCHANT_QUESTION_LIBRARY_PATH": "/srv/questions.yaml",
            "XPD_METRIC_DEFINITION_LIBRARY_ENABLED": "false",
            "XPD_METRIC_DEFINITION_TOP_K": "4",
            "XPD_METRIC_DEFINITION_LIBRARY_PATH": "/srv/metrics.yaml",
        },
        root=tmp_path,
    )

    assert config.env["API_SERVER_HOST"] == "127.0.0.2"
    assert config.env["API_SERVER_PORT"] == "9000"
    assert config.env["API_SERVER_KEY"] == "gateway-secret"
    assert config.env["LAUNCH_MANAGED"] == "true"
    assert config.env["MYSQL_HOST"] == "127.0.0.2"
    assert config.env["MYSQL_PORT"] == "3307"
    assert config.env["MYSQL_USER"] == "report_reader"
    assert config.env["MYSQL_PASSWORD"] == "mysql-secret"
    assert config.env["MYSQL_DATABASE"] == "reports"
    assert config.env["XPD_HERMES_CLARIFY_PATCH"] == "true"
    assert config.env["XPD_CLARIFY_TIMEOUT_SECONDS"] == "300"
    assert config.env["XPD_HERMES_REPORT_FILE_PATCH"] == "true"
    assert config.env["XPD_HERMES_CRON_PATCH"] == "false"
    assert config.env["XPD_HERMES_USER_MEMORY_PATCH"] == "true"
    assert config.env["XPD_IDENTITY_MODE"] == "session_key"
    assert config.env["XPD_SERVICE_AUTH_ENABLED"] == "false"
    assert config.env["XPD_SERVICE_API_KEY"] == ""
    assert config.env["XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED"] == "false"
    assert config.env["XPD_MERCHANT_MEMORY_ENABLED"] == "true"
    assert config.env["XPD_MERCHANT_MEMORY_CHAR_LIMIT"] == "2200"
    assert config.env["XPD_MERCHANT_QUESTION_LIBRARY_ENABLED"] == "false"
    assert config.env["XPD_MERCHANT_QUESTION_TOP_K"] == "2"
    assert config.env["XPD_MERCHANT_QUESTION_LIBRARY_PATH"] == "/srv/questions.yaml"
    assert config.env["XPD_METRIC_DEFINITION_LIBRARY_ENABLED"] == "false"
    assert config.env["XPD_METRIC_DEFINITION_TOP_K"] == "4"
    assert config.env["XPD_METRIC_DEFINITION_LIBRARY_PATH"] == "/srv/metrics.yaml"
    assert config.env["XPD_SCHEDULES_ENABLED"] == "false"
    assert config.env["XPD_AGENT_MAX_CONCURRENCY"] == "3"
    assert config.env["XPD_AGENT_RUN_MAX_ATTEMPTS"] == "2"
    assert config.env["XPD_AGENT_CHAT_TIMEOUT_SECONDS"] == "600"
    assert config.env["XPD_FINAL_REFLECTION_TIMEOUT_SECONDS"] == "180"
    assert config.env["XPD_HERMES_CONNECT_MAX_ATTEMPTS"] == "3"
    assert config.env["XPD_MYSQL_READ_MAX_ATTEMPTS"] == "2"
    assert config.env["XPD_MYSQL_READ_RETRY_BACKOFF_MS"] == "100"
    assert config.env["HERMES_TIMEZONE"] == "Asia/Shanghai"
    assert config.env["XPD_FILE_STORAGE_PATH"] == str(
        (tmp_path / "data" / "report-files").resolve()
    )
    assert config.env["XPD_REPORT_OSS_BUCKET"] == "starpartner-biz"
    assert config.env["XPD_REPORT_OSS_PREFIX"] == "public/dev/agent-report-files"


def test_normalize_env_does_not_map_legacy_variables(tmp_path):
    config = normalize_env(
        {
            "API_SERVER_HOST": "127.0.0.3",
            "API_SERVER_PORT": "9100",
            "API_SERVER_KEY": "legacy-secret",
        },
        root=tmp_path,
    )

    assert config.env["HERMES_GATEWAY_HOST"] == "127.0.0.1"
    assert config.env["HERMES_GATEWAY_PORT"] == "8642"
    assert config.env["HERMES_GATEWAY_API_KEY"] == "dev-secret"
    assert config.env["HERMES_BOOTSTRAP_ON_START"] == "false"


def test_launch_default_log_dir_is_root_logs(tmp_path):
    config = normalize_env({}, root=tmp_path)
    manager = LaunchManager(root=tmp_path, env=config.env)

    assert manager.log_dir == tmp_path / "logs"
    assert manager.run_dir == tmp_path / ".run"


def test_runtime_config_precedence(tmp_path):
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    (tmp_path / ".env").write_text(
        "HERMES_GATEWAY_PORT=8001\nHERMES_GATEWAY_MODEL=legacy-model\n",
        encoding="utf-8",
    )
    (configs_dir / "local.env").write_text(
        "HERMES_GATEWAY_PORT=8002\nHERMES_GATEWAY_MODEL=config-model\n",
        encoding="utf-8",
    )

    config = load_runtime_config(
        root=tmp_path,
        environ={"HERMES_GATEWAY_PORT": "8003"},
    )

    assert config.env["HERMES_GATEWAY_PORT"] == "8003"
    assert config.env["HERMES_GATEWAY_MODEL"] == "config-model"


def test_default_services_use_migrated_script_paths(tmp_path):
    config = normalize_env({}, root=tmp_path)
    manager = LaunchManager(root=tmp_path, env=config.env)

    assert manager.services["hermes"].command[1] == str(
        tmp_path / "scripts" / "services" / "hermes.sh"
    )
    assert manager.services["fastapi"].command[1] == str(
        tmp_path / "scripts" / "services" / "fastapi.sh"
    )


def test_launch_start_stop_sleep_service(tmp_path):
    services = {
        "hermes": ServiceSpec(
            name="hermes",
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
    }
    manager = LaunchManager(
        root=tmp_path,
        env=os.environ.copy(),
        services=services,
        run_dir=tmp_path / "run",
        log_dir=tmp_path / "logs",
    )

    try:
        manager.start("hermes")
        pid = manager.read_pid("hermes")
        assert pid is not None
        assert manager.is_process_running(pid)
    finally:
        manager.stop("hermes")

    assert manager.read_pid("hermes") is None


def test_launch_rolls_back_started_service_when_next_service_fails(tmp_path):
    services = {
        "hermes": ServiceSpec(
            name="hermes",
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
        ),
        "fastapi": ServiceSpec(
            name="fastapi",
            command=[sys.executable, "-c", "raise SystemExit(3)"],
        ),
    }
    manager = LaunchManager(
        root=tmp_path,
        env=os.environ.copy(),
        services=services,
        run_dir=tmp_path / "run",
        log_dir=tmp_path / "logs",
    )

    with pytest.raises(LaunchError):
        manager.start("all")

    assert manager.read_pid("hermes") is None
    assert manager.read_pid("fastapi") is None
