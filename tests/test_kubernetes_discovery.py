from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from xpd_report_agent.kubernetes_discovery import (
    KubernetesDiscoveryAPIError,
    KubernetesDiscoveryAuthorizationError,
    KubernetesDiscoveryConfig,
    KubernetesDiscoveryConfigurationError,
    KubernetesDiscoveryResponseError,
    KubernetesDiscoveryUnavailableError,
    KubernetesEndpointSliceDiscovery,
)


class _Response:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, factory: _ClientFactory):
        self.factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, *, headers, params):
        self.factory.calls.append((url, headers, params))
        if self.factory.delay:
            await asyncio.sleep(self.factory.delay)
        result = self.factory.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _ClientFactory:
    def __init__(self, *results: _Response | Exception, delay: float = 0):
        self.results = list(results)
        self.delay = delay
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.options: list[dict[str, Any]] = []

    def __call__(self, **kwargs):
        self.options.append(kwargs)
        return _Client(self)


def _config(tmp_path: Path, *, ttl_seconds: float = 5) -> KubernetesDiscoveryConfig:
    token_path = tmp_path / "token"
    ca_path = tmp_path / "ca.crt"
    token_path.write_text("service-account-token\n", encoding="utf-8")
    ca_path.write_text("test-ca", encoding="utf-8")
    return KubernetesDiscoveryConfig(
        api_server="https://10.96.0.1:443",
        namespace="reports",
        service_name="hermes-headless",
        hermes_port=8642,
        token_path=token_path,
        ca_cert_path=ca_path,
        ttl_seconds=ttl_seconds,
        timeout_seconds=2,
    )


def _endpoint(
    pod_name: str | None,
    *,
    ready: bool = True,
    terminating: bool | None = False,
    kind: str = "Pod",
) -> dict[str, Any]:
    conditions: dict[str, Any] = {"ready": ready}
    if terminating is not None:
        conditions["terminating"] = terminating
    endpoint: dict[str, Any] = {
        "addresses": ["10.244.1.2"],
        "conditions": conditions,
    }
    if pod_name is not None:
        endpoint["targetRef"] = {
            "kind": kind,
            "namespace": "reports",
            "name": pod_name,
        }
    return endpoint


def _payload(*endpoints: dict[str, Any], continuation: str = "") -> dict[str, Any]:
    return {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSliceList",
        "metadata": {"continue": continuation},
        "items": [{"endpoints": list(endpoints)}],
    }


def test_config_uses_injected_service_account_paths_and_expected_env_names(tmp_path):
    account_directory = tmp_path / "service-account"
    account_directory.mkdir()
    (account_directory / "token").write_text("token", encoding="utf-8")
    (account_directory / "namespace").write_text("file-namespace\n", encoding="utf-8")
    (account_directory / "ca.crt").write_text("ca", encoding="utf-8")
    env = {
        "KUBERNETES_SERVICE_HOST": "2001:db8::1",
        "KUBERNETES_SERVICE_PORT": "6443",
        "XPD_K8S_NAMESPACE": "reports",
        "XPD_K8S_HERMES_SERVICE": "hermes-headless",
        "XPD_K8S_HERMES_PORT": "9000",
        "XPD_K8S_DISCOVERY_TTL_SECONDS": "7.5",
    }

    config = KubernetesDiscoveryConfig.from_env(
        env,
        service_account_directory=account_directory,
    )

    assert config.api_server == "https://[2001:db8::1]:6443"
    assert config.namespace == "reports"
    assert config.service_name == "hermes-headless"
    assert config.hermes_port == 9000
    assert config.ttl_seconds == 7.5
    assert config.token_path == account_directory / "token"


def test_config_reads_namespace_file_and_rejects_invalid_values(tmp_path):
    account_directory = tmp_path / "service-account"
    account_directory.mkdir()
    (account_directory / "token").write_text("token", encoding="utf-8")
    (account_directory / "namespace").write_text("reports\n", encoding="utf-8")
    (account_directory / "ca.crt").write_text("ca", encoding="utf-8")
    base_env = {"KUBERNETES_SERVICE_HOST": "kubernetes.default.svc"}

    config = KubernetesDiscoveryConfig.from_env(
        base_env,
        service_account_directory=account_directory,
    )
    assert config.namespace == "reports"

    with pytest.raises(KubernetesDiscoveryConfigurationError):
        KubernetesDiscoveryConfig.from_env(
            {**base_env, "XPD_K8S_HERMES_PORT": "not-a-port"},
            service_account_directory=account_directory,
        )
    with pytest.raises(KubernetesDiscoveryConfigurationError):
        KubernetesDiscoveryConfig.from_env(
            {**base_env, "XPD_K8S_HERMES_SERVICE": "Bad_Service"},
            service_account_directory=account_directory,
        )
    with pytest.raises(KubernetesDiscoveryConfigurationError):
        KubernetesDiscoveryConfig.from_env(
            {**base_env, "XPD_K8S_DISCOVERY_TTL_SECONDS": "nan"},
            service_account_directory=account_directory,
        )


def test_discovery_returns_only_ready_non_terminating_pods(tmp_path):
    factory = _ClientFactory(
        _Response(
            200,
            _payload(
                _endpoint("hermes-2", terminating=None),
                _endpoint("hermes-not-ready", ready=False),
                _endpoint("hermes-terminating", terminating=True),
                _endpoint("not-a-pod", kind="Service"),
                _endpoint(None),
                _endpoint("hermes-1"),
            ),
        )
    )
    discovery = KubernetesEndpointSliceDiscovery(
        _config(tmp_path),
        client_factory=factory,
    )

    nodes = asyncio.run(discovery.discover_nodes())

    assert [(node.node_id, node.origin) for node in nodes] == [
        (
            "hermes-1",
            "http://hermes-1.hermes-headless.reports.svc:8642",
        ),
        (
            "hermes-2",
            "http://hermes-2.hermes-headless.reports.svc:8642",
        ),
    ]
    url, headers, params = factory.calls[0]
    assert url.endswith("/apis/discovery.k8s.io/v1/namespaces/reports/endpointslices")
    assert headers["Authorization"] == "Bearer service-account-token"
    assert params == {"labelSelector": "kubernetes.io/service-name=hermes-headless"}
    assert factory.options[0]["verify"] == str(tmp_path / "ca.crt")
    assert factory.options[0]["trust_env"] is False


def test_discovery_paginates_endpoint_slices(tmp_path):
    factory = _ClientFactory(
        _Response(200, _payload(_endpoint("hermes-2"), continuation="next-page")),
        _Response(200, _payload(_endpoint("hermes-1"))),
    )
    discovery = KubernetesEndpointSliceDiscovery(
        _config(tmp_path),
        client_factory=factory,
    )

    nodes = asyncio.run(discovery.discover_nodes())

    assert [node.node_id for node in nodes] == ["hermes-1", "hermes-2"]
    assert factory.calls[1][2]["continue"] == "next-page"


def test_ttl_cache_is_used_until_expiry_and_refresh_failure_is_closed(tmp_path):
    now = [100.0]
    factory = _ClientFactory(
        _Response(200, _payload(_endpoint("hermes-1"))),
        httpx.ConnectError("cluster API unavailable"),
    )
    discovery = KubernetesEndpointSliceDiscovery(
        _config(tmp_path, ttl_seconds=5),
        client_factory=factory,
        clock=lambda: now[0],
    )

    first = asyncio.run(discovery.discover_nodes())
    now[0] = 104.9
    cached = asyncio.run(discovery.discover_nodes())

    assert cached is first
    assert len(factory.calls) == 1

    now[0] = 105.0
    with pytest.raises(KubernetesDiscoveryAPIError):
        asyncio.run(discovery.discover_nodes())
    assert len(factory.calls) == 2


def test_async_lock_coalesces_concurrent_cache_refreshes(tmp_path):
    factory = _ClientFactory(
        _Response(200, _payload(_endpoint("hermes-1"))),
        delay=0.01,
    )
    discovery = KubernetesEndpointSliceDiscovery(
        _config(tmp_path),
        client_factory=factory,
    )

    async def discover_concurrently():
        return await asyncio.gather(*(discovery.discover_nodes() for _ in range(8)))

    results = asyncio.run(discover_concurrently())

    assert len(factory.calls) == 1
    assert all(result == results[0] for result in results)


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, KubernetesDiscoveryAuthorizationError),
        (403, KubernetesDiscoveryAuthorizationError),
        (500, KubernetesDiscoveryAPIError),
    ],
)
def test_rbac_and_api_errors_fail_closed(tmp_path, status_code, error_type):
    factory = _ClientFactory(_Response(status_code, {}))
    discovery = KubernetesEndpointSliceDiscovery(
        _config(tmp_path),
        client_factory=factory,
    )

    with pytest.raises(error_type):
        asyncio.run(discovery.discover_nodes())


def test_invalid_api_payload_and_invalid_pod_name_are_rejected(tmp_path):
    invalid_kind = _ClientFactory(
        _Response(
            200,
            {
                "apiVersion": "discovery.k8s.io/v1",
                "kind": "PodList",
                "metadata": {},
                "items": [],
            },
        )
    )
    with pytest.raises(KubernetesDiscoveryResponseError):
        asyncio.run(
            KubernetesEndpointSliceDiscovery(
                _config(tmp_path),
                client_factory=invalid_kind,
            ).discover_nodes()
        )

    invalid_pod = _ClientFactory(_Response(200, _payload(_endpoint("Invalid_Pod_Name"))))
    with pytest.raises(KubernetesDiscoveryResponseError):
        asyncio.run(
            KubernetesEndpointSliceDiscovery(
                _config(tmp_path),
                client_factory=invalid_pod,
            ).discover_nodes()
        )


def test_no_eligible_pods_is_reported_as_unavailable(tmp_path):
    factory = _ClientFactory(_Response(200, _payload(_endpoint("hermes-1", terminating=True))))
    discovery = KubernetesEndpointSliceDiscovery(
        _config(tmp_path),
        client_factory=factory,
    )

    with pytest.raises(KubernetesDiscoveryUnavailableError):
        asyncio.run(discovery.discover_nodes())
