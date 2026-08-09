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

    assert "HERMES_AGENT_DIR=/opt/hermes-agent" in dockerfile
    assert "HERMES_BIN=/opt/hermes-agent/venv/bin/hermes" in dockerfile
    assert 'mkdir -p "$HOME/.hermes"' in dockerfile
    assert '$HOME/.hermes/hermes-agent' not in dockerfile


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
    assert "ENV UV_HTTP_TIMEOUT=300" in dockerfile


def test_compose_persists_state_and_allows_graceful_shutdown():
    compose = yaml.safe_load(_read(DOCKER_DIR / "compose.yaml"))
    service = compose["services"]["xpd-report-agent"]

    assert service["stop_grace_period"] == "60s"
    assert "hermes-state:/var/lib/xpd-report-agent/.hermes" in service["volumes"]
    assert "report-files:/app/data/report-files" in service["volumes"]
    assert service["logging"] == {
        "driver": "json-file",
        "options": {"max-size": "20m", "max-file": "5"},
    }
    assert "ProxyHandler({})" in service["healthcheck"]["test"][-1]
    assert compose["volumes"]["hermes-state"]["name"] == (
        "xpd-report-agent-hermes-state"
    )


def test_entrypoint_fails_preflight_before_starting_services():
    entrypoint = _read(DOCKER_DIR / "entrypoint.sh")

    preflight = (
        "/app/.venv/bin/python -m "
        "xpd_report_agent.runtime.deployment_preflight --quiet"
    )
    assert preflight in entrypoint
    assert entrypoint.index(preflight) < entrypoint.index("hermes.sh run")
    assert "ProxyHandler({})" in entrypoint
    assert 'configured_host in {"", "0.0.0.0"}' in entrypoint
    assert "Hermes Gateway exited before becoming healthy" in entrypoint


def test_docker_context_excludes_secret_environment_files():
    patterns = {
        line.strip()
        for line in _read(PROJECT_ROOT / ".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {".env", ".env.*", "**/.env", "**/.env.*", "deploy/docker/*.env"} <= patterns


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
    ):
        assert expected in template


def test_systemd_healthcheck_probes_wildcard_bind_addresses_via_loopback():
    namespace = runpy.run_path(str(PROJECT_ROOT / "deploy" / "systemd" / "healthcheck.py"))
    probe_host = namespace["_probe_host"]

    assert probe_host("0.0.0.0") == "127.0.0.1"
    assert probe_host("::") == "[::1]"
