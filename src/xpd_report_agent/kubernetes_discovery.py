from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from xpd_report_agent.hermes_pool import HermesNode

SERVICE_ACCOUNT_DIRECTORY = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_DNS_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_SERVICE_NAME_PATTERN = re.compile(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?")


class KubernetesDiscoveryError(RuntimeError):
    """Base error for Kubernetes EndpointSlice discovery."""


class KubernetesDiscoveryConfigurationError(KubernetesDiscoveryError):
    """Raised when in-cluster discovery configuration is invalid."""


class KubernetesDiscoveryAuthorizationError(KubernetesDiscoveryError):
    """Raised when the service account cannot list EndpointSlices."""


class KubernetesDiscoveryAPIError(KubernetesDiscoveryError):
    """Raised when the Kubernetes API cannot be queried successfully."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class KubernetesDiscoveryResponseError(KubernetesDiscoveryError):
    """Raised when the Kubernetes API returns an invalid discovery payload."""


class KubernetesDiscoveryUnavailableError(KubernetesDiscoveryError):
    """Raised when no ready Hermes Pod is available."""


def _parse_positive_int(value: object, *, name: str, default: int) -> int:
    raw = str(value if value is not None else default).strip()
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise KubernetesDiscoveryConfigurationError(f"{name} must be an integer.") from exc
    if not 1 <= parsed <= 65535:
        raise KubernetesDiscoveryConfigurationError(f"{name} must be between 1 and 65535.")
    return parsed


def _parse_positive_float(value: object, *, name: str, default: float) -> float:
    raw = str(value if value is not None else default).strip()
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise KubernetesDiscoveryConfigurationError(f"{name} must be a positive number.") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise KubernetesDiscoveryConfigurationError(f"{name} must be a positive number.")
    return parsed


def _is_dns_label(value: str) -> bool:
    return len(value) <= 63 and bool(_DNS_LABEL_PATTERN.fullmatch(value))


def _is_dns_subdomain(value: str) -> bool:
    return 0 < len(value) <= 253 and all(_is_dns_label(label) for label in value.split("."))


def _api_host(value: str) -> str:
    host = value.strip()
    if not host or any(character.isspace() for character in host):
        raise KubernetesDiscoveryConfigurationError(
            "KUBERNETES_SERVICE_HOST must be a host name or IP address."
        )
    if host.startswith("[") and host.endswith("]"):
        candidate = host[1:-1]
    else:
        candidate = host
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        if not _is_dns_subdomain(candidate):
            raise KubernetesDiscoveryConfigurationError(
                "KUBERNETES_SERVICE_HOST must be a host name or IP address."
            ) from None
        return candidate
    return f"[{address.compressed}]" if address.version == 6 else address.compressed


def _read_text_file(path: Path, *, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise KubernetesDiscoveryConfigurationError(
            f"Kubernetes service-account {label} could not be read."
        ) from exc
    if not value:
        raise KubernetesDiscoveryConfigurationError(f"Kubernetes service-account {label} is empty.")
    return value


@dataclass(frozen=True)
class KubernetesDiscoveryConfig:
    api_server: str
    namespace: str
    service_name: str
    hermes_port: int
    token_path: Path
    ca_cert_path: Path
    ttl_seconds: float = 5.0
    timeout_seconds: float = 3.0

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        service_account_directory: Path | None = None,
        token_path: Path | None = None,
        namespace_path: Path | None = None,
        ca_cert_path: Path | None = None,
    ) -> KubernetesDiscoveryConfig:
        environment = os.environ if env is None else env
        account_directory = service_account_directory or SERVICE_ACCOUNT_DIRECTORY
        resolved_token_path = token_path or account_directory / "token"
        resolved_namespace_path = namespace_path or account_directory / "namespace"
        resolved_ca_path = ca_cert_path or account_directory / "ca.crt"

        host = _api_host(str(environment.get("KUBERNETES_SERVICE_HOST", "")))
        api_port = _parse_positive_int(
            environment.get("KUBERNETES_SERVICE_PORT", "443"),
            name="KUBERNETES_SERVICE_PORT",
            default=443,
        )
        namespace = str(environment.get("XPD_K8S_NAMESPACE", "")).strip()
        if not namespace:
            namespace = _read_text_file(resolved_namespace_path, label="namespace")
        if not _is_dns_label(namespace):
            raise KubernetesDiscoveryConfigurationError(
                "XPD_K8S_NAMESPACE must be a valid DNS label."
            )

        service_name = str(environment.get("XPD_K8S_HERMES_SERVICE", "hermes-headless")).strip()
        if not _SERVICE_NAME_PATTERN.fullmatch(service_name):
            raise KubernetesDiscoveryConfigurationError(
                "XPD_K8S_HERMES_SERVICE must be a valid Kubernetes Service name."
            )

        hermes_port = _parse_positive_int(
            environment.get("XPD_K8S_HERMES_PORT", "8642"),
            name="XPD_K8S_HERMES_PORT",
            default=8642,
        )
        ttl_seconds = _parse_positive_float(
            environment.get("XPD_K8S_DISCOVERY_TTL_SECONDS", "5"),
            name="XPD_K8S_DISCOVERY_TTL_SECONDS",
            default=5.0,
        )
        timeout_seconds = _parse_positive_float(
            environment.get("XPD_K8S_DISCOVERY_TIMEOUT_SECONDS", "3"),
            name="XPD_K8S_DISCOVERY_TIMEOUT_SECONDS",
            default=3.0,
        )
        if not resolved_ca_path.is_file():
            raise KubernetesDiscoveryConfigurationError(
                "Kubernetes service-account CA certificate could not be read."
            )

        return cls(
            api_server=f"https://{host}:{api_port}",
            namespace=namespace,
            service_name=service_name,
            hermes_port=hermes_port,
            token_path=resolved_token_path,
            ca_cert_path=resolved_ca_path,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout_seconds,
        )


class KubernetesEndpointSliceDiscovery:
    """Discover ready Hermes Pods from the in-cluster EndpointSlice API."""

    def __init__(
        self,
        config: KubernetesDiscoveryConfig,
        *,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cached_nodes: tuple[HermesNode, ...] | None = None
        self._cache_expires_at = 0.0

    async def discover_nodes(self) -> tuple[HermesNode, ...]:
        now = self._clock()
        if self._cached_nodes is not None and now < self._cache_expires_at:
            return self._cached_nodes

        async with self._lock:
            now = self._clock()
            if self._cached_nodes is not None and now < self._cache_expires_at:
                return self._cached_nodes

            nodes = await self._fetch_nodes()
            self._cached_nodes = nodes
            self._cache_expires_at = self._clock() + self.config.ttl_seconds
            return nodes

    async def _fetch_nodes(self) -> tuple[HermesNode, ...]:
        token = _read_text_file(self.config.token_path, label="token")
        url = (
            f"{self.config.api_server}/apis/discovery.k8s.io/v1/namespaces/"
            f"{self.config.namespace}/endpointslices"
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        items: list[Any] = []
        continuation: str | None = None
        seen_continuations: set[str] = set()

        try:
            async with self._client_factory(
                timeout=self.config.timeout_seconds,
                verify=str(self.config.ca_cert_path),
                trust_env=False,
            ) as client:
                while True:
                    params = {
                        "labelSelector": (f"kubernetes.io/service-name={self.config.service_name}")
                    }
                    if continuation:
                        params["continue"] = continuation
                    response = await client.get(url, headers=headers, params=params)
                    self._raise_for_status(response)
                    payload = self._response_payload(response)
                    page_items = payload.get("items")
                    if not isinstance(page_items, list):
                        raise KubernetesDiscoveryResponseError(
                            "EndpointSliceList.items must be an array."
                        )
                    items.extend(page_items)
                    metadata = payload.get("metadata", {})
                    if not isinstance(metadata, dict):
                        raise KubernetesDiscoveryResponseError(
                            "EndpointSliceList.metadata must be an object."
                        )
                    next_token = metadata.get("continue", "")
                    if next_token is None:
                        next_token = ""
                    if not isinstance(next_token, str):
                        raise KubernetesDiscoveryResponseError(
                            "EndpointSliceList.metadata.continue must be a string."
                        )
                    continuation = next_token.strip() or None
                    if not continuation:
                        break
                    if continuation in seen_continuations:
                        raise KubernetesDiscoveryResponseError(
                            "EndpointSlice pagination token was repeated."
                        )
                    seen_continuations.add(continuation)
        except KubernetesDiscoveryError:
            raise
        except httpx.HTTPError as exc:
            raise KubernetesDiscoveryAPIError(
                "Kubernetes EndpointSlice API request failed."
            ) from exc

        nodes_by_id: dict[str, HermesNode] = {}
        for item in items:
            self._append_slice_nodes(item, nodes_by_id)
        if not nodes_by_id:
            raise KubernetesDiscoveryUnavailableError(
                "No ready, non-terminating Hermes Pod was discovered."
            )
        return tuple(nodes_by_id[node_id] for node_id in sorted(nodes_by_id))

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status_code = int(response.status_code)
        if status_code in {401, 403}:
            raise KubernetesDiscoveryAuthorizationError(
                "The Kubernetes service account cannot list EndpointSlices."
            )
        if not 200 <= status_code < 300:
            raise KubernetesDiscoveryAPIError(
                "Kubernetes EndpointSlice API returned an error.",
                status_code=status_code,
            )

    @staticmethod
    def _response_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise KubernetesDiscoveryResponseError(
                "Kubernetes EndpointSlice API returned invalid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise KubernetesDiscoveryResponseError(
                "Kubernetes EndpointSlice response must be an object."
            )
        if payload.get("apiVersion") != "discovery.k8s.io/v1":
            raise KubernetesDiscoveryResponseError(
                "Kubernetes EndpointSlice response has an unexpected apiVersion."
            )
        if payload.get("kind") != "EndpointSliceList":
            raise KubernetesDiscoveryResponseError(
                "Kubernetes EndpointSlice response has an unexpected kind."
            )
        return payload

    def _append_slice_nodes(
        self,
        item: Any,
        nodes_by_id: dict[str, HermesNode],
    ) -> None:
        if not isinstance(item, dict):
            raise KubernetesDiscoveryResponseError("Each EndpointSlice must be an object.")
        endpoints = item.get("endpoints")
        if not isinstance(endpoints, list):
            raise KubernetesDiscoveryResponseError("EndpointSlice.endpoints must be an array.")
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                raise KubernetesDiscoveryResponseError(
                    "Each EndpointSlice endpoint must be an object."
                )
            conditions = endpoint.get("conditions", {})
            if not isinstance(conditions, dict):
                raise KubernetesDiscoveryResponseError(
                    "EndpointSlice endpoint conditions must be an object."
                )
            if conditions.get("ready") is not True:
                continue
            if conditions.get("terminating") is True:
                continue
            target_ref = endpoint.get("targetRef")
            if not isinstance(target_ref, dict) or target_ref.get("kind") != "Pod":
                continue
            pod_name = target_ref.get("name")
            if not isinstance(pod_name, str) or not _is_dns_subdomain(pod_name):
                raise KubernetesDiscoveryResponseError("A ready Pod targetRef has an invalid name.")
            target_namespace = target_ref.get("namespace")
            if target_namespace is not None and target_namespace != self.config.namespace:
                raise KubernetesDiscoveryResponseError(
                    "A ready Pod targetRef belongs to another namespace."
                )
            origin = (
                f"http://{pod_name}.{self.config.service_name}."
                f"{self.config.namespace}.svc:{self.config.hermes_port}"
            )
            node = HermesNode(node_id=pod_name, origin=origin)
            existing = nodes_by_id.get(pod_name)
            if existing is not None and existing != node:
                raise KubernetesDiscoveryResponseError(
                    "A Hermes Pod was returned with conflicting endpoints."
                )
            nodes_by_id[pod_name] = node


_default_discovery: KubernetesEndpointSliceDiscovery | None = None
_default_discovery_config: KubernetesDiscoveryConfig | None = None


def default_kubernetes_discovery(
    env: Mapping[str, str] | None = None,
) -> KubernetesEndpointSliceDiscovery:
    """Return a process-wide cached discovery client for the real environment."""

    config = KubernetesDiscoveryConfig.from_env(env)
    if env is not None:
        return KubernetesEndpointSliceDiscovery(config)

    global _default_discovery, _default_discovery_config
    if _default_discovery is None or _default_discovery_config != config:
        _default_discovery = KubernetesEndpointSliceDiscovery(config)
        _default_discovery_config = config
    return _default_discovery
