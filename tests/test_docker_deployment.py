from __future__ import annotations

import runpy
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = PROJECT_ROOT / "deploy" / "docker"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_dockerfile_separates_hermes_runtime_from_persistent_state():
    dockerfile = _read(DOCKER_DIR / "Dockerfile")

    assert "FROM project-deps AS fastapi" in dockerfile
    assert "FROM hermes-runtime AS hermes" in dockerfile
    assert "HERMES_AGENT_DIR=/opt/hermes-agent" in dockerfile
    assert "HERMES_BIN=/opt/hermes-agent/venv/bin/hermes" in dockerfile
    assert 'mkdir -p "$HOME/.hermes"' in dockerfile
    assert "$HOME/.hermes/hermes-agent" not in dockerfile

    fastapi_stage = dockerfile.split("FROM project-deps AS fastapi", 1)[1].split(
        "FROM hermes-runtime AS hermes", 1
    )[0]
    assert "/opt/hermes-agent" not in fastapi_stage
    assert "XPD_CONTAINER_ROLE=fastapi" in fastapi_stage


def test_hermes_image_installs_and_verifies_flock():
    dockerfile = _read(DOCKER_DIR / "Dockerfile")
    hermes_runtime_stage = dockerfile.split("FROM project-deps AS hermes-runtime", 1)[1].split(
        "FROM project-deps AS fastapi", 1
    )[0]

    assert "util-linux" in hermes_runtime_stage
    assert "command -v flock >/dev/null" in hermes_runtime_stage


def test_dockerfile_keeps_expensive_dependencies_in_a_stable_layer():
    dockerfile = _read(DOCKER_DIR / "Dockerfile")

    assert "docker.io/docker/dockerfile" not in dockerfile
    dependency_copy = dockerfile.index("COPY pyproject.toml uv.lock /app/")
    source_copy = dockerfile.index("COPY . /app")
    assert dependency_copy < source_copy
    assert "uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert "uv lock --refresh" not in dockerfile
    assert dockerfile.count("uv pip check --python") >= 2
    assert "uv pip freeze --python" in dockerfile
    assert "--constraints" in dockerfile
    assert "git -c http.version=HTTP/1.1" in dockerfile
    assert "for attempt in 1 2 3" in dockerfile
    assert "UV_HTTP_TIMEOUT=300" in dockerfile
    assert "from=hermes_seed" in dockerfile
    assert "sha256sum -c hermes-agent.tar.gz.sha256" in dockerfile
    assert "rev-parse --verify HEAD^{commit}" in dockerfile
    assert 'git config --system --add safe.directory "$HERMES_AGENT_DIR"' in dockerfile


def test_compose_runs_one_fastapi_and_three_fixed_hermes_services():
    compose = yaml.safe_load(_read(DOCKER_DIR / "compose.yaml"))
    services = compose["services"]
    hermes_services = [services[f"hermes-{index}"] for index in range(1, 4)]
    fastapi = services["xpd-report-agent"]

    assert set(services) == {
        "hermes-1",
        "hermes-2",
        "hermes-3",
        "xpd-report-agent",
    }
    assert all(service["build"]["target"] == "hermes" for service in hermes_services)
    assert fastapi["build"]["target"] == "fastapi"
    for service in hermes_services:
        assert service["build"]["additional_contexts"] == {"hermes_seed": "./hermes-seed"}
    assert "additional_contexts" not in fastapi["build"]
    assert {service["image"] for service in hermes_services} == {"xpd-report-agent-hermes:dev"}
    assert all(service["image"] != fastapi["image"] for service in hermes_services)
    assert fastapi["container_name"] == "xpd-report-agent"
    assert [service["container_name"] for service in hermes_services] == [
        "xpd-report-agent-hermes-1",
        "xpd-report-agent-hermes-2",
        "xpd-report-agent-hermes-3",
    ]
    assert all(
        service["environment"]["XPD_CONTAINER_ROLE"] == "hermes" for service in hermes_services
    )
    assert [service["environment"]["XPD_HERMES_NODE_ID"] for service in hermes_services] == [
        "hermes-1",
        "hermes-2",
        "hermes-3",
    ]
    assert fastapi["environment"]["XPD_CONTAINER_ROLE"] == "fastapi"


def test_compose_keeps_all_hermes_nodes_internal_and_starts_fastapi_with_them():
    compose = yaml.safe_load(_read(DOCKER_DIR / "compose.yaml"))
    hermes_services = {
        name: compose["services"][name] for name in ("hermes-1", "hermes-2", "hermes-3")
    }
    fastapi = compose["services"]["xpd-report-agent"]

    assert all(
        service["environment"]["HERMES_GATEWAY_HOST"] == "0.0.0.0"
        for service in hermes_services.values()
    )
    assert fastapi["environment"]["HERMES_GATEWAY_HOST"] == "hermes-1"
    assert fastapi["environment"]["HERMES_GATEWAY_NODES"] == (
        "hermes-1=http://hermes-1:8642,hermes-2=http://hermes-2:8642,hermes-3=http://hermes-3:8642"
    )
    assert fastapi["environment"]["XPD_HERMES_SCHEDULER_NODE"] == "hermes-1"
    assert fastapi["depends_on"] == {
        name: {"condition": "service_started"} for name in hermes_services
    }
    assert all(service["expose"] == ["8642"] for service in hermes_services.values())
    assert all("ports" not in service for service in hermes_services.values())
    assert fastapi["ports"] == ["8000:8000"]
    assert all(
        "ProxyHandler({})" in service["healthcheck"]["test"][-1]
        for service in hermes_services.values()
    )
    assert "ProxyHandler({})" in fastapi["healthcheck"]["test"][-1]


def test_compose_assigns_cron_to_hermes_1_only():
    services = yaml.safe_load(_read(DOCKER_DIR / "compose.yaml"))["services"]

    assert all(
        services[name]["environment"]["XPD_HERMES_SCHEDULER_NODE"] == "hermes-1"
        for name in ("hermes-1", "hermes-2", "hermes-3")
    )
    # hermes-1 inherits both switches from the protected env file. Production
    # defaults keep them false; operators may explicitly enable the leader.
    assert "XPD_HERMES_CRON_PATCH" not in services["hermes-1"]["environment"]
    assert "XPD_SCHEDULES_ENABLED" not in services["hermes-1"]["environment"]
    for name in ("hermes-2", "hermes-3"):
        assert services[name]["environment"]["XPD_HERMES_CRON_PATCH"] == "false"
        assert services[name]["environment"]["XPD_SCHEDULES_ENABLED"] == "false"


def test_compose_shares_memory_and_files_but_isolates_each_hermes_home():
    compose = yaml.safe_load(_read(DOCKER_DIR / "compose.yaml"))
    services = compose["services"]
    required_volumes = {
        "hermes-state:/var/lib/xpd-report-agent/.hermes",
        "report-files:/app/data/report-files",
    }

    for service in services.values():
        assert service["stop_grace_period"] == "60s"
        assert required_volumes <= set(service["volumes"])
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "20m", "max-file": "5"},
        }
        assert service["environment"]["XPD_FILE_STORAGE_PATH"] == ("/app/data/report-files")
        assert service["environment"]["XPD_HERMES_SHARED_HOME"] == (
            "/var/lib/xpd-report-agent/.hermes"
        )
        assert service["environment"]["XPD_MEMORY_ROOT"] == (
            "/var/lib/xpd-report-agent/.hermes/memories"
        )
        assert service["environment"]["XPD_CRON_SCRIPT_DIR"] == (
            "/var/lib/xpd-report-agent/.hermes/scripts"
        )
        assert service["environment"]["XPD_CRON_CALLBACK_ORIGIN"] == (
            "http://xpd-report-agent:8000"
        )

    assert services["xpd-report-agent"]["environment"]["HERMES_HOME"] == (
        "/var/lib/xpd-report-agent/.hermes"
    )
    assert {
        services[name]["environment"]["HERMES_HOME"]
        for name in ("hermes-1", "hermes-2", "hermes-3")
    } == {
        "/var/lib/xpd-report-agent/.hermes/instances/hermes-1",
        "/var/lib/xpd-report-agent/.hermes/instances/hermes-2",
        "/var/lib/xpd-report-agent/.hermes/instances/hermes-3",
    }

    assert compose["volumes"]["hermes-state"]["name"] == ("xpd-report-agent-hermes-state")
    assert compose["volumes"]["report-files"] is None


def test_entrypoint_runs_exactly_one_role_after_preflight():
    entrypoint = _read(DOCKER_DIR / "entrypoint.sh")

    preflight = "/app/.venv/bin/python -m xpd_report_agent.runtime.deployment_preflight --quiet"
    assert preflight in entrypoint
    assert entrypoint.index(preflight) < entrypoint.index("hermes.sh run")
    assert entrypoint.index(preflight) < entrypoint.index("fastapi.sh run")
    assert 'case "${XPD_CONTAINER_ROLE:-}" in' in entrypoint
    assert "exec /app/scripts/services/hermes.sh run" in entrypoint
    assert "exec /app/scripts/services/fastapi.sh run" in entrypoint
    assert "wait -n" not in entrypoint
    assert "hermes.sh run &" not in entrypoint


def test_docker_context_excludes_secret_environment_files():
    patterns = {
        line.strip()
        for line in _read(PROJECT_ROOT / ".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {".env", ".env.*", "**/.env", "**/.env.*", "deploy/docker/*.env"} <= patterns
    assert "deploy/docker/hermes-seed/*.tar.gz" in patterns
    assert "deploy/docker/hermes-seed/*.sha256" in patterns


def test_docker_environment_template_contains_production_model_and_identity_config():
    template = _read(DOCKER_DIR / "xpd-report-agent.env.example")

    for expected in (
        "HERMES_LLM_PROVIDER=custom",
        "HERMES_LLM_MODEL=",
        "HERMES_LLM_BASE_URL=",
        "HERMES_LLM_API_MODE=chat_completions",
        "HERMES_LLM_API_KEY=REPLACE_WITH_",
        "XPD_IDENTITY_MODE=user_id",
        "XPD_SERVICE_AUTH_ENABLED=true",
        "XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED=false",
        "FASTAPI_RELOAD=false",
        "XPD_AGENT_MAX_CONCURRENCY=20",
        "XPD_SCHEDULES_ENABLED=false",
        "XPD_HERMES_CRON_PATCH=false",
    ):
        assert expected in template

    for compose_managed in (
        "FASTAPI_HOST=",
        "FASTAPI_PORT=",
        "HERMES_GATEWAY_HOST=",
        "HERMES_GATEWAY_PORT=",
        "HERMES_GATEWAY_NODES=",
        "XPD_HERMES_SCHEDULER_NODE=",
        "XPD_HERMES_NODE_ID=",
        "XPD_CRON_CALLBACK_ORIGIN=",
    ):
        assert not any(line.startswith(compose_managed) for line in template.splitlines())


def test_systemd_healthcheck_probes_wildcard_bind_addresses_via_loopback():
    namespace = runpy.run_path(str(PROJECT_ROOT / "deploy" / "systemd" / "healthcheck.py"))
    probe_host = namespace["_probe_host"]

    assert probe_host("0.0.0.0") == "127.0.0.1"
    assert probe_host("::") == "[::1]"
