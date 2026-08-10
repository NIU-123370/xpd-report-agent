from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from xpd_report_agent.runtime import launcher as launcher_module
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


def _write_flock_stub(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import fcntl\n"
        "import sys\n"
        "fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX)\n",
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
    _write_flock_stub(bin_dir / "flock")
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
            "HERMES_HOME": str(home / ".hermes"),
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
        (f"project-python|scripts/configure_hermes.py|--config|{env['HERMES_HOME']}/config.yaml"),
        "hermes|gateway|run|--external-supervisor",
    ]


def test_hermes_service_run_preserves_explicit_bootstrap(tmp_path):
    script, env, call_log = _stage_hermes_service(tmp_path)
    env["HERMES_BOOTSTRAP_ON_START"] = "true"

    _run_hermes_service(script, env)

    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "hermes|--version"
    assert calls[1].startswith("uv|sync|--project|")
    assert "|--frozen|--no-dev|--python|" in calls[1]
    assert calls[2].startswith("uv|pip|check|--python|")
    assert calls[3].startswith("uv|pip|freeze|--python|")
    assert calls[4].startswith("uv|pip|install|--python|")
    assert "|--constraints|" in calls[4]
    assert calls[4].endswith("|--group|hermes-plugin")
    assert calls[5].startswith("uv|pip|check|--python|")
    assert calls[6:] == [
        (f"project-python|scripts/configure_hermes.py|--config|{env['HERMES_HOME']}/config.yaml"),
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
    assert calls[1].startswith("uv|sync|--project|")
    assert calls[2].startswith("uv|pip|check|--python|")
    assert any(call.endswith("|--group|hermes-plugin") for call in calls)
    assert "hermes|plugins|enable|db-query" in calls


def test_hermes_service_supports_explicit_agent_directory(tmp_path):
    script, env, call_log = _stage_hermes_service(tmp_path)
    custom_agent_dir = tmp_path / "custom-hermes-agent"
    custom_bin = custom_agent_dir / "venv" / "bin"
    custom_bin.mkdir(parents=True)
    source_hermes = Path(env.pop("HERMES_BIN"))
    custom_hermes = custom_bin / "hermes"
    custom_hermes.write_text(source_hermes.read_text(encoding="utf-8"), encoding="utf-8")
    custom_hermes.chmod(0o755)
    env.pop("HERMES_PY", None)
    env["HERMES_AGENT_DIR"] = str(custom_agent_dir)

    _run_hermes_service(script, env)

    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "hermes|--version",
        (f"project-python|scripts/configure_hermes.py|--config|{env['HERMES_HOME']}/config.yaml"),
        "hermes|gateway|run|--external-supervisor",
    ]


def test_hermes_stateful_worker_forces_cron_off(tmp_path):
    script, env, _call_log = _stage_hermes_service(tmp_path)
    role_log = tmp_path / "node-role.log"
    env.update(
        {
            "XPD_HERMES_NODE_ID": "hermes-2",
            "XPD_HERMES_SCHEDULER_NODE": "hermes-0",
            "XPD_HERMES_CRON_PATCH": "true",
            "XPD_SCHEDULES_ENABLED": "true",
            "XPD_TEST_NODE_ROLE_LOG": str(role_log),
        }
    )
    project_python = Path(env["PROJECT_PYTHON"])
    project_python.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s|%s|%s" "$XPD_HERMES_NODE_ID" '
        '"$XPD_HERMES_CRON_PATCH" "$XPD_SCHEDULES_ENABLED" '
        '> "$XPD_TEST_NODE_ROLE_LOG"\n',
        encoding="utf-8",
    )
    project_python.chmod(0o755)

    _run_hermes_service(script, env)

    assert role_log.read_text(encoding="utf-8") == "hermes-2|false|false"


def test_hermes_service_serializes_shared_home_configuration(tmp_path):
    script, env, _call_log = _stage_hermes_service(tmp_path)
    shared_home = tmp_path / "shared hermes home"
    env["HERMES_HOME"] = str(shared_home)
    env["HERMES_BOOTSTRAP_ON_START"] = "true"
    guard_dir = tmp_path / "configure.guard"
    overlap_log = tmp_path / "configure.overlap"
    configure_log = tmp_path / "configure.log"
    env.update(
        {
            "XPD_TEST_CONFIGURE_GUARD": str(guard_dir),
            "XPD_TEST_CONFIGURE_OVERLAP": str(overlap_log),
            "XPD_TEST_CONFIGURE_LOG": str(configure_log),
        }
    )
    project_python = Path(env["PROJECT_PYTHON"])
    project_python.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if ! mkdir "$XPD_TEST_CONFIGURE_GUARD" 2>/dev/null; then\n'
        '  printf "overlap\\n" >> "$XPD_TEST_CONFIGURE_OVERLAP"\n'
        "  exit 90\n"
        "fi\n"
        'printf "configured\\n" >> "$XPD_TEST_CONFIGURE_LOG"\n',
        encoding="utf-8",
    )
    project_python.chmod(0o755)
    hermes_bin = Path(env["HERMES_BIN"])
    hermes_bin.write_text(
        hermes_bin.read_text(encoding="utf-8")
        + 'if [ "${1:-}" = "plugins" ] && [ "${2:-}" = "enable" ]; then\n'
        + '  if [ ! -d "$XPD_TEST_CONFIGURE_GUARD" ]; then\n'
        + '    printf "plugin-outside-lock\\n" >> "$XPD_TEST_CONFIGURE_OVERLAP"\n'
        + "    exit 91\n"
        + "  fi\n"
        + "  sleep 0.2\n"
        + '  rmdir "$XPD_TEST_CONFIGURE_GUARD"\n'
        + "fi\n",
        encoding="utf-8",
    )

    processes = [
        subprocess.Popen(
            ["bash", str(script), "run"],
            cwd=script.parents[2],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=10) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], results
    assert not overlap_log.exists()
    assert configure_log.read_text(encoding="utf-8").splitlines() == [
        "configured",
        "configured",
    ]
    assert (shared_home / ".xpd-bootstrap.lock").is_file()
    assert (shared_home / "plugins" / "db-query" / "plugin.yaml").is_file()
    assert (shared_home / "skills" / "db-multitable-query" / "SKILL.md").is_file()


def test_hermes_service_process_xpd_db_alias_overrides_file_mysql_name(tmp_path):
    script, env, _call_log = _stage_hermes_service(
        tmp_path,
        local_env="MYSQL_HOST=old.internal\nMYSQL_DATABASE=old_database\n",
    )
    env["LAUNCH_MANAGED"] = "false"
    env.pop("MYSQL_HOST", None)
    env.pop("MYSQL_DATABASE", None)
    env["XPD_DB_HOST"] = "new.internal"
    env["XPD_DB_NAME"] = "new_database"
    env_log = tmp_path / "database-env.log"
    env["XPD_TEST_DATABASE_ENV_LOG"] = str(env_log)
    hermes_bin = Path(env["HERMES_BIN"])
    hermes_bin.write_text(
        hermes_bin.read_text(encoding="utf-8")
        + 'if [ "${1:-}" = "gateway" ]; then\n'
        + '  printf "%s|%s" "$MYSQL_HOST" "$MYSQL_DATABASE" > "$XPD_TEST_DATABASE_ENV_LOG"\n'
        + "fi\n",
        encoding="utf-8",
    )

    _run_hermes_service(script, env)

    assert env_log.read_text(encoding="utf-8") == "new.internal|new_database"


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
            "XPD_MYSQL_QUERY_TIMEOUT_MS": "12000",
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
    assert config.env["XPD_MYSQL_QUERY_TIMEOUT_MS"] == "12000"
    assert config.env["HERMES_TIMEZONE"] == "Asia/Shanghai"
    assert config.env["XPD_FILE_STORAGE_PATH"] == str(
        (tmp_path / "data" / "report-files").resolve()
    )
    assert config.env["XPD_REPORT_OSS_BUCKET"] == "starpartner-biz"
    assert config.env["XPD_REPORT_OSS_PREFIX"] == "public/dev/agent-report-files"


def test_normalize_env_maps_xpd_dms_aliases(tmp_path):
    config = normalize_env(
        {
            "XPD_DB_HOST": "rds.internal",
            "XPD_DB_PORT": "3307",
            "XPD_DB_USERNAME": "main_biz_dev",
            "XPD_DB_PASSWORD": "secret",
            "XPD_DB_NAME": "main_biz_dev",
        },
        root=tmp_path,
    )

    assert config.env["MYSQL_HOST"] == "rds.internal"
    assert config.env["MYSQL_PORT"] == "3307"
    assert config.env["MYSQL_USER"] == "main_biz_dev"
    assert config.env["MYSQL_PASSWORD"] == "secret"
    assert config.env["MYSQL_DATABASE"] == "main_biz_dev"


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


def test_runtime_config_preserves_priority_across_database_aliases(tmp_path):
    (tmp_path / ".env").write_text(
        "MYSQL_HOST=old.internal\nMYSQL_DATABASE=old_database\n",
        encoding="utf-8",
    )
    config = load_runtime_config(
        root=tmp_path,
        environ={
            "XPD_DB_HOST": "new.internal",
            "XPD_DB_NAME": "new_database",
        },
    )

    assert config.env["MYSQL_HOST"] == "new.internal"
    assert config.env["MYSQL_DATABASE"] == "new_database"
    assert config.env["XPD_DB_HOST"] == "new.internal"
    assert config.env["XPD_DB_NAME"] == "new_database"


def test_default_services_use_migrated_script_paths(tmp_path):
    config = normalize_env({}, root=tmp_path)
    manager = LaunchManager(root=tmp_path, env=config.env)

    assert manager.services["hermes"].command[1] == str(
        tmp_path / "scripts" / "services" / "hermes.sh"
    )
    assert manager.services["fastapi"].command[1] == str(
        tmp_path / "scripts" / "services" / "fastapi.sh"
    )


def test_default_service_health_urls_probe_loopback_for_wildcard_binds(tmp_path):
    config = normalize_env(
        {
            "HERMES_GATEWAY_HOST": "0.0.0.0",
            "FASTAPI_HOST": "::",
        },
        root=tmp_path,
    )
    manager = LaunchManager(root=tmp_path, env=config.env)

    assert manager.services["hermes"].health_url == "http://127.0.0.1:8642/v1/health"
    assert manager.services["fastapi"].health_url == "http://[::1]:8000/health"


def test_launch_health_probe_uses_no_proxy_opener(tmp_path, monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class RecordingOpener:
        def __init__(self):
            self.urls: list[str] = []

        def open(self, request, timeout):
            self.urls.append(request.full_url)
            return Response()

    opener = RecordingOpener()
    monkeypatch.setattr(launcher_module, "_NO_PROXY_OPENER", opener)
    spec = ServiceSpec(
        name="probe",
        command=[],
        health_url="http://127.0.0.1:9999/health",
    )
    manager = LaunchManager(
        root=tmp_path,
        env=os.environ.copy(),
        services={"probe": spec},
    )

    assert manager.probe(spec)
    assert opener.urls == ["http://127.0.0.1:9999/health"]


def test_systemd_units_force_runtime_env_and_prepare_offline():
    systemd_dir = PROJECT_ROOT / "deploy" / "systemd"
    hermes = (systemd_dir / "xpd-hermes.service.in").read_text(encoding="utf-8")
    fastapi = (systemd_dir / "xpd-fastapi.service.in").read_text(encoding="utf-8")
    prepare = (systemd_dir / "xpd-hermes-prepare.service.in").read_text(
        encoding="utf-8"
    )

    preflight = (
        "ExecStartPre=/usr/bin/env HOME=@SERVICE_HOME@ LAUNCH_MANAGED=true "
        "XPD_SERVICE_AUTH_ENABLED=true @PROJECT_ROOT@/.venv/bin/python "
        "-m xpd_report_agent.runtime.deployment_preflight --quiet"
    )
    assert preflight in hermes
    assert preflight in fastapi
    assert preflight in prepare
    forced_environment = (
        "ExecStart=/usr/bin/env HOME=@SERVICE_HOME@ LAUNCH_MANAGED=true "
        "XPD_SERVICE_AUTH_ENABLED=true"
    )
    assert forced_environment in hermes
    assert forced_environment in fastapi
    assert (
        "Conflicts=xpd-report-agent.target xpd-hermes.service xpd-fastapi.service"
        in prepare
    )
    assert "TimeoutStartSec=60min" in prepare
    assert forced_environment in prepare


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
