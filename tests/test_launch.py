from __future__ import annotations

import os
import sys

import pytest

from xpd_report_agent.runtime.launcher import (
    LaunchError,
    LaunchManager,
    ServiceSpec,
    load_runtime_config,
    normalize_env,
)


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
