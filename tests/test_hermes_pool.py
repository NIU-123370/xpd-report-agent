from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from xpd_report_agent.api import hermes_routing
from xpd_report_agent.api.hermes_routing import resolve_hermes_node
from xpd_report_agent.hermes_pool import (
    HermesNode,
    HermesPoolConfigurationError,
    HermesRouteConflictError,
    HermesRouteUnavailableError,
    configured_hermes_nodes,
    hermes_discovery_mode,
    hermes_pool,
    hermes_pool_from_nodes,
    owner_scope_from_session_id,
    resolved_hermes_pool,
)

SCOPE_A = "a" * 20
SCOPE_B = "b" * 20
SESSION_A = f"xpd_{SCOPE_A}_session_a"
SESSION_A_SECOND = f"xpd_{SCOPE_A}_session_b"
SESSION_B = f"xpd_{SCOPE_B}_session_a"


class _HealthResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300


class _HealthClient:
    statuses: dict[str, int] = {}
    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None):
        type(self).calls.append(url)
        node_id = url.split("//", 1)[-1].split(":", 1)[0]
        return _HealthResponse(type(self).statuses.get(node_id, 503))


def _multi_env(tmp_path) -> dict[str, str]:
    return {
        "HERMES_GATEWAY_NODES": (
            "hermes-1=http://hermes-1:8642,"
            "hermes-2=http://hermes-2:8642,"
            "hermes-3=http://hermes-3:8642"
        ),
        "XPD_HERMES_SCHEDULER_NODE": "hermes-1",
        "XPD_HERMES_ROUTE_STATE_PATH": str(tmp_path / "routes.json"),
    }


def test_legacy_single_node_configuration_remains_supported():
    nodes = configured_hermes_nodes(
        {"HERMES_GATEWAY_HOST": "127.0.0.9", "HERMES_GATEWAY_PORT": "9999"}
    )

    assert [(node.node_id, node.origin) for node in nodes] == [("hermes", "http://127.0.0.9:9999")]


def test_multi_node_configuration_requires_stable_ids_and_origins(tmp_path):
    pool = hermes_pool(_multi_env(tmp_path))

    assert [node.node_id for node in pool.nodes] == [
        "hermes-1",
        "hermes-2",
        "hermes-3",
    ]
    assert pool.scheduler_node.node_id == "hermes-1"

    with pytest.raises(HermesPoolConfigurationError):
        configured_hermes_nodes({"HERMES_GATEWAY_NODES": "bad id=http://hermes-1:8642"})
    with pytest.raises(HermesPoolConfigurationError):
        configured_hermes_nodes(
            {
                "HERMES_GATEWAY_NODES": (
                    "hermes-1=http://hermes-1:8642,hermes-2=http://hermes-1:8642"
                )
            }
        )


def test_owner_and_session_routes_are_durable_and_sticky(tmp_path):
    env = _multi_env(tmp_path)
    first_pool = hermes_pool(env)
    assigned = first_pool.assign_scope(SCOPE_A)
    first_pool.bind_session(SESSION_A, assigned.node_id)

    second_pool = hermes_pool(env)

    assert second_pool.bound_node_for_scope(SCOPE_A) == assigned
    assert second_pool.bound_node_for_session(SESSION_A) == assigned
    state = json.loads((tmp_path / "routes.json").read_text(encoding="utf-8"))
    assert state["scopes"][SCOPE_A] == assigned.node_id
    assert state["sessions"][SESSION_A] == assigned.node_id


def test_persisted_scope_route_cannot_be_silently_reassigned(tmp_path):
    pool = hermes_pool(_multi_env(tmp_path))
    pool.bind_scope(SCOPE_A, "hermes-1")

    with pytest.raises(HermesRouteConflictError):
        pool.bind_scope(SCOPE_A, "hermes-2")
    with pytest.raises(HermesRouteConflictError):
        pool.bind_session(SESSION_A, "hermes-2")


def test_route_store_atomically_rebinds_scope_and_all_its_sessions(tmp_path):
    pool = hermes_pool(_multi_env(tmp_path))
    store = pool.route_store
    store.bind_session(SESSION_A, "hermes-1")
    store.bind_session(SESSION_A_SECOND, "hermes-1")
    store.bind_session(SESSION_B, "hermes-3")

    winner = store.rebind_scope_if_matches(
        SCOPE_A,
        expected_node_id="hermes-1",
        replacement_node_id="hermes-2",
    )

    assert winner == "hermes-2"
    snapshot = store.snapshot()
    assert snapshot["scopes"][SCOPE_A] == "hermes-2"
    assert snapshot["sessions"][SESSION_A] == "hermes-2"
    assert snapshot["sessions"][SESSION_A_SECOND] == "hermes-2"
    assert snapshot["scopes"][SCOPE_B] == "hermes-3"
    assert snapshot["sessions"][SESSION_B] == "hermes-3"

    # A competing request that still expects hermes-1 must not overwrite the winner.
    race_winner = store.rebind_scope_if_matches(
        SCOPE_A,
        expected_node_id="hermes-1",
        replacement_node_id="hermes-3",
    )
    assert race_winner == "hermes-2"
    assert store.snapshot()["sessions"][SESSION_A] == "hermes-2"


def test_concurrent_first_assignment_uses_the_binding_that_won(tmp_path, monkeypatch):
    pool = hermes_pool(_multi_env(tmp_path))
    original_bind_scope = pool.route_store.bind_scope
    raced = False

    def bind_after_competing_request(scope: str, node_id: str) -> None:
        nonlocal raced
        if not raced:
            raced = True
            competing_node_id = "hermes-2" if node_id != "hermes-2" else "hermes-3"
            original_bind_scope(scope, competing_node_id)
        original_bind_scope(scope, node_id)

    monkeypatch.setattr(pool.route_store, "bind_scope", bind_after_competing_request)

    assigned = pool.assign_scope(SCOPE_A)

    assert assigned.node_id == pool.route_store.node_id_for_scope(SCOPE_A)


def test_assignment_is_sticky_and_balances_new_scopes(tmp_path):
    pool = hermes_pool(_multi_env(tmp_path))
    scopes = [f"{index:020x}" for index in range(9)]
    assignments = [pool.assign_scope(scope).node_id for scope in scopes]
    counts = {node_id: assignments.count(node_id) for node_id in set(assignments)}

    assert hermes_pool(_multi_env(tmp_path)).assign_scope(scopes[0]).node_id == assignments[0]
    assert set(assignments) == {"hermes-1", "hermes-2", "hermes-3"}
    assert max(counts.values()) - min(counts.values()) <= 1


def test_session_owner_scope_parser_is_strict():
    assert owner_scope_from_session_id(SESSION_A) == SCOPE_A
    assert owner_scope_from_session_id("session_a") is None
    assert owner_scope_from_session_id(f"xpd_{SCOPE_A}_bad-suffix") is None


class _Discovery:
    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = 0

    async def discover_nodes(self):
        self.calls += 1
        return self.nodes


def test_resolved_pool_uses_kubernetes_discovery_and_enables_cas_rebinding(tmp_path):
    env = {
        "XPD_HERMES_DISCOVERY_MODE": "kubernetes",
        "XPD_HERMES_ROUTE_STATE_PATH": str(tmp_path / "routes.json"),
    }
    discovery = _Discovery(
        (
            HermesNode("hermes-pod-1", "http://hermes-pod-1.hermes:8642"),
            HermesNode("hermes-pod-2", "http://hermes-pod-2.hermes:8642"),
        )
    )

    pool = asyncio.run(resolved_hermes_pool(env, discovery=discovery))
    pool.bind_session(SESSION_A, "hermes-pod-1")
    rebound = pool.rebind_scope_if_matches(
        SCOPE_A,
        expected_node_id="hermes-pod-1",
        replacement_node_id="hermes-pod-2",
    )

    assert discovery.calls == 1
    assert pool.allow_route_rebinding is True
    assert rebound.node_id == "hermes-pod-2"
    assert pool.bound_node_for_session(SESSION_A).node_id == "hermes-pod-2"


def test_kubernetes_pool_with_one_ready_node_still_persists_routes(tmp_path):
    pool = hermes_pool_from_nodes(
        (HermesNode("hermes-0", "http://hermes-0.hermes:8642"),),
        {
            "XPD_HERMES_ROUTE_STATE_PATH": str(tmp_path / "routes.json"),
            "XPD_HERMES_SCHEDULER_NODE": "hermes-0",
        },
        allow_route_rebinding=True,
    )

    node = pool.node_for_session(SESSION_A)

    assert node.node_id == "hermes-0"
    assert pool.persistent_routing is True
    assert pool.route_store.node_id_for_scope(SCOPE_A) == "hermes-0"
    assert pool.route_store.node_id_for_session(SESSION_A) == "hermes-0"


def test_kubernetes_pool_allows_scheduler_leader_to_be_temporarily_absent(tmp_path):
    pool = hermes_pool_from_nodes(
        (HermesNode("hermes-1", "http://hermes-1.hermes:8642"),),
        {
            "XPD_HERMES_ROUTE_STATE_PATH": str(tmp_path / "routes.json"),
            "XPD_HERMES_SCHEDULER_NODE": "hermes-0",
        },
        allow_route_rebinding=True,
    )

    assert pool.primary_node.node_id == "hermes-1"
    with pytest.raises(HermesRouteUnavailableError):
        _ = pool.scheduler_node


def test_static_pool_keeps_automatic_rebinding_disabled(tmp_path):
    pool = hermes_pool_from_nodes(
        (
            HermesNode("hermes-1", "http://hermes-1:8642"),
            HermesNode("hermes-2", "http://hermes-2:8642"),
        ),
        {"XPD_HERMES_ROUTE_STATE_PATH": str(tmp_path / "routes.json")},
    )
    pool.bind_scope(SCOPE_A, "hermes-1")

    with pytest.raises(HermesRouteUnavailableError):
        pool.rebind_scope_if_matches(
            SCOPE_A,
            expected_node_id="hermes-1",
            replacement_node_id="hermes-2",
        )

    assert pool.bound_node_for_scope(SCOPE_A).node_id == "hermes-1"


def test_dynamic_routing_rebinds_sessions_from_scaled_down_pod(
    monkeypatch,
    tmp_path,
):
    env = {
        "XPD_HERMES_ROUTE_STATE_PATH": str(tmp_path / "routes.json"),
        "XPD_HERMES_SCHEDULER_NODE": "hermes-0",
    }
    old_pool = hermes_pool_from_nodes(
        (
            HermesNode("hermes-0", "http://hermes-0.hermes:8642"),
            HermesNode("hermes-2", "http://hermes-2.hermes:8642"),
        ),
        env,
        allow_route_rebinding=True,
    )
    old_pool.bind_session(SESSION_A, "hermes-2")
    old_pool.bind_session(SESSION_A_SECOND, "hermes-2")
    current_pool = hermes_pool_from_nodes(
        (
            HermesNode("hermes-0", "http://hermes-0.hermes:8642"),
            HermesNode("hermes-1", "http://hermes-1.hermes:8642"),
        ),
        env,
        allow_route_rebinding=True,
    )

    async def resolve_pool():
        return current_pool

    async def healthy_nodes(pool, *, api_key):
        assert pool is current_pool
        assert api_key == "test-key"
        return {
            "hermes-0": {"ok": True},
            "hermes-1": {"ok": True},
        }

    monkeypatch.setattr(hermes_routing, "resolved_hermes_pool", resolve_pool)
    monkeypatch.setattr(hermes_routing, "probe_hermes_nodes", healthy_nodes)

    rebound = asyncio.run(
        resolve_hermes_node(
            f"/api/sessions/{SESSION_A}/chat",
            scope=SCOPE_A,
            api_key="test-key",
        )
    )

    assert rebound.node_id in {"hermes-0", "hermes-1"}
    snapshot = current_pool.route_store.snapshot()
    assert snapshot["scopes"][SCOPE_A] == rebound.node_id
    assert snapshot["sessions"][SESSION_A] == rebound.node_id
    assert snapshot["sessions"][SESSION_A_SECOND] == rebound.node_id


def test_dynamic_scheduler_request_fails_cleanly_when_leader_is_not_ready(
    monkeypatch,
    tmp_path,
):
    pool = hermes_pool_from_nodes(
        (HermesNode("hermes-1", "http://hermes-1.hermes:8642"),),
        {
            "XPD_HERMES_ROUTE_STATE_PATH": str(tmp_path / "routes.json"),
            "XPD_HERMES_SCHEDULER_NODE": "hermes-0",
        },
        allow_route_rebinding=True,
    )

    async def resolve_pool():
        return pool

    monkeypatch.setattr(hermes_routing, "resolved_hermes_pool", resolve_pool)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(resolve_hermes_node("/api/xpd-cron/health"))

    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "HERMES_POOL_UNAVAILABLE"
    assert caught.value.detail["body"]["node_id"] == "hermes-0"


def test_session_ownership_is_checked_even_for_legacy_single_node(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_NODES", raising=False)
    monkeypatch.setenv("XPD_HERMES_DISCOVERY_MODE", "static")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            resolve_hermes_node(
                f"/api/sessions/{SESSION_A}/chat",
                scope=SCOPE_B,
            )
        )

    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "SESSION_OWNERSHIP_MISMATCH"


def test_discovery_mode_is_strict():
    assert hermes_discovery_mode({}) == "static"
    assert hermes_discovery_mode({"XPD_HERMES_DISCOVERY_MODE": "KUBERNETES"}) == ("kubernetes")
    with pytest.raises(HermesPoolConfigurationError):
        hermes_discovery_mode({"XPD_HERMES_DISCOVERY_MODE": "dns"})


def test_new_session_uses_healthy_node_and_keeps_it_when_health_changes(
    monkeypatch,
    tmp_path,
):
    for key, value in _multi_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(hermes_routing.httpx, "AsyncClient", _HealthClient)
    _HealthClient.statuses = {"hermes-1": 503, "hermes-2": 200, "hermes-3": 503}
    _HealthClient.calls = []

    first = asyncio.run(
        resolve_hermes_node(
            "/api/sessions",
            payload={"id": SESSION_A},
            api_key="test-key",
        )
    )
    calls_after_assignment = len(_HealthClient.calls)
    _HealthClient.statuses = {"hermes-1": 200, "hermes-2": 503, "hermes-3": 200}
    second = asyncio.run(
        resolve_hermes_node(
            f"/api/sessions/{SESSION_A}/chat",
            scope=SCOPE_A,
            api_key="test-key",
        )
    )

    assert first.node_id == "hermes-2"
    assert second.node_id == "hermes-2"
    assert len(_HealthClient.calls) == calls_after_assignment


def test_scheduler_paths_always_use_designated_leader(monkeypatch, tmp_path):
    for key, value in _multi_env(tmp_path).items():
        monkeypatch.setenv(key, value)

    node = asyncio.run(resolve_hermes_node("/api/xpd-cron/jobs", api_key="test-key"))

    assert node.node_id == "hermes-1"


def test_removed_sticky_node_fails_closed(monkeypatch, tmp_path):
    env = _multi_env(tmp_path)
    route_path = tmp_path / "routes.json"
    route_path.write_text(
        json.dumps(
            {
                "version": 1,
                "scopes": {SCOPE_A: "hermes-removed"},
                "sessions": {SESSION_A: "hermes-removed"},
            }
        ),
        encoding="utf-8",
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            resolve_hermes_node(
                f"/api/sessions/{SESSION_A}",
                scope=SCOPE_A,
                api_key="test-key",
            )
        )

    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "HERMES_POOL_UNAVAILABLE"
