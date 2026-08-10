from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpd_report_agent.api import main as app_main
from xpd_report_agent.api import metrics as metrics_module
from xpd_report_agent.api.metrics import (
    PROMETHEUS_CONTENT_TYPE,
    create_metrics_router,
)


def _client(*, capacity_provider, hermes_provider=None) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_metrics_router(
            capacity_provider=capacity_provider,
            hermes_provider=hermes_provider,
        )
    )
    return TestClient(app)


def test_metrics_exposes_agent_capacity_in_prometheus_text_format():
    client = _client(capacity_provider=lambda: {"limit": 20, "active": 7, "waiting": 2})

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == PROMETHEUS_CONTENT_TYPE
    assert response.text.endswith("\n")
    assert "# TYPE xpd_agent_capacity_limit gauge\n" in response.text
    assert "xpd_agent_capacity_limit 20\n" in response.text
    assert "xpd_agent_active 7\n" in response.text
    assert "xpd_agent_waiting 2\n" in response.text
    assert "xpd_agent_demand 9\n" in response.text


def test_default_router_reads_agent_capacity_health(monkeypatch):
    monkeypatch.setattr(
        metrics_module,
        "agent_capacity_health",
        lambda: {"limit": 14, "active": 4, "waiting": 1},
    )
    app = FastAPI()
    app.include_router(metrics_module.router)

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert "xpd_agent_capacity_limit 14\n" in response.text
    assert "xpd_agent_active 4\n" in response.text
    assert "xpd_agent_waiting 1\n" in response.text


def test_metrics_zeros_invalid_capacity_values():
    client = _client(
        capacity_provider=lambda: {
            "limit": -1,
            "active": float("nan"),
            "waiting": float("inf"),
        }
    )

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "xpd_agent_capacity_limit 0\n" in response.text
    assert "xpd_agent_active 0\n" in response.text
    assert "xpd_agent_waiting 0\n" in response.text
    assert "xpd_agent_demand 0\n" in response.text


def test_metrics_zeros_capacity_when_provider_fails():
    def failing_provider():
        raise RuntimeError("capacity state unavailable")

    response = _client(capacity_provider=failing_provider).get("/metrics")

    assert response.status_code == 200
    assert "xpd_agent_capacity_limit 0\n" in response.text
    assert "xpd_agent_active 0\n" in response.text
    assert "xpd_agent_waiting 0\n" in response.text


def test_metrics_can_include_async_hermes_pool_provider():
    async def hermes_provider():
        return {"healthy": 2, "total": 3}

    response = _client(
        capacity_provider=lambda: {"limit": 20, "active": 1, "waiting": 0},
        hermes_provider=hermes_provider,
    ).get("/metrics")

    assert response.status_code == 200
    assert "xpd_hermes_nodes_healthy 2\n" in response.text
    assert "xpd_hermes_nodes_total 3\n" in response.text


def test_metrics_zeros_hermes_pool_when_provider_returns_invalid_data():
    response = _client(
        capacity_provider=lambda: {"limit": 20, "active": 1, "waiting": 0},
        hermes_provider=lambda: {"healthy": True, "total": "three"},
    ).get("/metrics")

    assert response.status_code == 200
    assert "xpd_hermes_nodes_healthy 0\n" in response.text
    assert "xpd_hermes_nodes_total 0\n" in response.text


def test_app_metrics_uses_resolved_dynamic_hermes_pool(monkeypatch):
    nodes = tuple(
        SimpleNamespace(node_id=node_id)
        for node_id in ("hermes-pod-a", "hermes-pod-b", "hermes-pod-c")
    )
    pool = SimpleNamespace(nodes=nodes)
    captured = {}

    async def resolve_pool():
        return pool

    async def probe_pool(resolved_pool, *, api_key):
        captured.update(pool=resolved_pool, api_key=api_key)
        return {
            "hermes-pod-a": {"ok": True},
            "hermes-pod-b": {"ok": False},
            "hermes-pod-c": {"ok": True},
            "stale-pod": {"ok": True},
        }

    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "metrics-test-key")
    monkeypatch.setattr(app_main, "resolved_hermes_pool", resolve_pool)
    monkeypatch.setattr(app_main, "probe_hermes_nodes", probe_pool)

    response = TestClient(app_main.app).get("/metrics")

    assert response.status_code == 200
    assert "xpd_hermes_nodes_healthy 2\n" in response.text
    assert "xpd_hermes_nodes_total 3\n" in response.text
    assert captured == {"pool": pool, "api_key": "metrics-test-key"}


def test_app_metrics_stays_available_when_dynamic_discovery_fails(monkeypatch):
    async def failing_resolve_pool():
        raise RuntimeError("Kubernetes API unavailable")

    monkeypatch.setattr(app_main, "resolved_hermes_pool", failing_resolve_pool)

    response = TestClient(app_main.app).get("/metrics")

    assert response.status_code == 200
    assert "xpd_agent_capacity_limit " in response.text
    assert "xpd_hermes_nodes_healthy 0\n" in response.text
    assert "xpd_hermes_nodes_total 0\n" in response.text


def test_app_metrics_stays_available_when_hermes_probe_fails(monkeypatch):
    pool = SimpleNamespace(nodes=(SimpleNamespace(node_id="hermes-pod-a"),))

    async def resolve_pool():
        return pool

    async def failing_probe(*args, **kwargs):
        raise RuntimeError("Hermes health probe unavailable")

    monkeypatch.setattr(app_main, "resolved_hermes_pool", resolve_pool)
    monkeypatch.setattr(app_main, "probe_hermes_nodes", failing_probe)

    response = TestClient(app_main.app).get("/metrics")

    assert response.status_code == 200
    assert "xpd_hermes_nodes_healthy 0\n" in response.text
    assert "xpd_hermes_nodes_total 0\n" in response.text
