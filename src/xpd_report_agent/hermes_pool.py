from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

NODE_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
OWNER_SCOPE_PATTERN = re.compile(r"[0-9a-f]{20}")
SESSION_ID_PATTERN = re.compile(r"xpd_([0-9a-f]{20})_[A-Za-z0-9_]+")


class HermesPoolConfigurationError(ValueError):
    """Raised when the configured Hermes node pool is invalid."""


class HermesRouteConflictError(RuntimeError):
    """Raised when an existing sticky route would be changed implicitly."""


class HermesRouteUnavailableError(RuntimeError):
    """Raised when a persisted route points to a node no longer configured."""


@dataclass(frozen=True)
class HermesNode:
    node_id: str
    origin: str

    @property
    def base_url(self) -> str:
        return f"{self.origin}/v1"


class HermesNodeDiscovery(Protocol):
    async def discover_nodes(self) -> Sequence[HermesNode]: ...


def _normalize_origin(value: str) -> str:
    origin = value.strip().rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HermesPoolConfigurationError("Hermes node URL must be an absolute HTTP(S) origin.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HermesPoolConfigurationError(
            "Hermes node URL must not contain credentials, a query, or a fragment."
        )
    if parsed.path not in {"", "/"}:
        raise HermesPoolConfigurationError("Hermes node URL must not contain a path.")
    return origin


def configured_hermes_nodes(
    env: Mapping[str, str] | None = None,
) -> tuple[HermesNode, ...]:
    environment = os.environ if env is None else env
    configured = str(environment.get("HERMES_GATEWAY_NODES", "")).strip()
    if not configured:
        host = str(environment.get("HERMES_GATEWAY_HOST", "127.0.0.1")).strip()
        port = str(environment.get("HERMES_GATEWAY_PORT", "8642")).strip()
        return (HermesNode("hermes", _normalize_origin(f"http://{host}:{port}")),)

    nodes: list[HermesNode] = []
    seen_ids: set[str] = set()
    seen_origins: set[str] = set()
    for raw_entry in configured.split(","):
        entry = raw_entry.strip()
        if not entry or "=" not in entry:
            raise HermesPoolConfigurationError(
                "HERMES_GATEWAY_NODES must use node-id=http://host:port entries."
            )
        raw_node_id, raw_origin = entry.split("=", 1)
        node_id = raw_node_id.strip()
        if not NODE_ID_PATTERN.fullmatch(node_id):
            raise HermesPoolConfigurationError(f"Invalid Hermes node id: {node_id!r}.")
        origin = _normalize_origin(raw_origin)
        if node_id in seen_ids:
            raise HermesPoolConfigurationError(f"Duplicate Hermes node id: {node_id}.")
        if origin in seen_origins:
            raise HermesPoolConfigurationError(f"Duplicate Hermes node URL: {origin}.")
        seen_ids.add(node_id)
        seen_origins.add(origin)
        nodes.append(HermesNode(node_id, origin))
    if not nodes:
        raise HermesPoolConfigurationError("At least one Hermes node is required.")
    return tuple(nodes)


def owner_scope_from_session_id(session_id: str) -> str | None:
    match = SESSION_ID_PATTERN.fullmatch(str(session_id or ""))
    return match.group(1) if match else None


def default_route_state_path(env: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    configured = str(environment.get("XPD_HERMES_ROUTE_STATE_PATH", "")).strip()
    if configured:
        return Path(configured).expanduser()
    hermes_home = Path(str(environment.get("HERMES_HOME", "~/.hermes"))).expanduser()
    return hermes_home / "xpd-report-agent" / "hermes-routes.json"


class HermesRouteStore:
    """Durable owner/session-to-node bindings for a single FastAPI scheduler."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._thread_lock = threading.RLock()

    @property
    def _lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {"version": 1, "scopes": {}, "sessions": {}}

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_state()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HermesRouteUnavailableError("Hermes routing state could not be read.") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise HermesRouteUnavailableError("Hermes routing state has an invalid format.")
        scopes = raw.get("scopes")
        sessions = raw.get("sessions")
        if not isinstance(scopes, dict) or not isinstance(sessions, dict):
            raise HermesRouteUnavailableError("Hermes routing state has an invalid format.")
        return {"version": 1, "scopes": dict(scopes), "sessions": dict(sessions)}

    def _write_unlocked(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(
                    state,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @contextmanager
    def _locked_state(self, *, write: bool) -> Iterator[dict[str, Any]]:
        with self._thread_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    state = self._load_unlocked()
                    yield state
                    if write:
                        self._write_unlocked(state)
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def node_id_for_scope(self, scope: str) -> str | None:
        if not OWNER_SCOPE_PATTERN.fullmatch(scope):
            raise ValueError("Invalid owner scope.")
        with self._locked_state(write=False) as state:
            value = state["scopes"].get(scope)
            return str(value) if value else None

    def node_id_for_session(self, session_id: str) -> str | None:
        scope = owner_scope_from_session_id(session_id)
        if scope is None:
            raise ValueError("Invalid owned session id.")
        with self._locked_state(write=False) as state:
            value = state["sessions"].get(session_id) or state["scopes"].get(scope)
            return str(value) if value else None

    def bind_scope(self, scope: str, node_id: str) -> None:
        if not OWNER_SCOPE_PATTERN.fullmatch(scope):
            raise ValueError("Invalid owner scope.")
        with self._locked_state(write=True) as state:
            existing = state["scopes"].get(scope)
            if existing and existing != node_id:
                raise HermesRouteConflictError(
                    f"Owner scope is already assigned to Hermes node {existing}."
                )
            state["scopes"][scope] = node_id

    def bind_session(self, session_id: str, node_id: str) -> None:
        scope = owner_scope_from_session_id(session_id)
        if scope is None:
            raise ValueError("Invalid owned session id.")
        with self._locked_state(write=True) as state:
            existing_scope = state["scopes"].get(scope)
            existing_session = state["sessions"].get(session_id)
            if existing_scope and existing_scope != node_id:
                raise HermesRouteConflictError(
                    f"Owner scope is already assigned to Hermes node {existing_scope}."
                )
            if existing_session and existing_session != node_id:
                raise HermesRouteConflictError(
                    f"Session is already assigned to Hermes node {existing_session}."
                )
            state["scopes"][scope] = node_id
            state["sessions"][session_id] = node_id

    def rebind_scope_if_matches(
        self,
        scope: str,
        *,
        expected_node_id: str,
        replacement_node_id: str,
    ) -> str | None:
        """Atomically move one scope and all its sessions when expected still wins."""

        if not OWNER_SCOPE_PATTERN.fullmatch(scope):
            raise ValueError("Invalid owner scope.")
        with self._locked_state(write=True) as state:
            current_node_id = state["scopes"].get(scope)
            if current_node_id != expected_node_id:
                return str(current_node_id) if current_node_id else None
            state["scopes"][scope] = replacement_node_id
            for session_id in tuple(state["sessions"]):
                if owner_scope_from_session_id(session_id) == scope:
                    state["sessions"][session_id] = replacement_node_id
            return replacement_node_id

    def remove_session(self, session_id: str) -> None:
        with self._locked_state(write=True) as state:
            state["sessions"].pop(session_id, None)

    def snapshot(self) -> dict[str, Any]:
        with self._locked_state(write=False) as state:
            return json.loads(json.dumps(state))


class HermesPool:
    def __init__(
        self,
        nodes: Sequence[HermesNode],
        *,
        route_store: HermesRouteStore,
        scheduler_node_id: str | None = None,
        allow_route_rebinding: bool = False,
    ) -> None:
        if not nodes:
            raise HermesPoolConfigurationError("At least one Hermes node is required.")
        self.nodes = tuple(nodes)
        self._by_id = {node.node_id: node for node in self.nodes}
        if len(self._by_id) != len(self.nodes):
            raise HermesPoolConfigurationError("Hermes node ids must be unique.")
        self.route_store = route_store
        self.allow_route_rebinding = allow_route_rebinding
        self.scheduler_node_id = scheduler_node_id or self.nodes[0].node_id
        if self.scheduler_node_id not in self._by_id and not self.allow_route_rebinding:
            raise HermesPoolConfigurationError(
                "XPD_HERMES_SCHEDULER_NODE must reference a configured Hermes node."
            )

    @property
    def multi_node(self) -> bool:
        return len(self.nodes) > 1

    @property
    def persistent_routing(self) -> bool:
        """Whether assignments must survive changes to the discovered node set."""

        return self.multi_node or self.allow_route_rebinding

    @property
    def primary_node(self) -> HermesNode:
        return self.nodes[0]

    @property
    def scheduler_node(self) -> HermesNode:
        return self.node(self.scheduler_node_id)

    def node(self, node_id: str) -> HermesNode:
        try:
            return self._by_id[node_id]
        except KeyError as exc:
            raise HermesRouteUnavailableError(
                f"Hermes node {node_id!r} is not configured."
            ) from exc

    def _select_node(self, route_key: str, available_node_ids: Sequence[str] | None) -> HermesNode:
        candidates = (
            [self.node(node_id) for node_id in available_node_ids]
            if available_node_ids is not None
            else list(self.nodes)
        )
        if not candidates:
            raise HermesRouteUnavailableError("No Hermes node is available for assignment.")
        scope_bindings = self.route_store.snapshot()["scopes"]
        assignment_counts = {
            node.node_id: sum(
                1
                for assigned_node_id in scope_bindings.values()
                if assigned_node_id == node.node_id
            )
            for node in candidates
        }
        least_assigned = min(assignment_counts.values())
        candidates = [
            node for node in candidates if assignment_counts[node.node_id] == least_assigned
        ]
        return max(
            candidates,
            key=lambda node: hashlib.sha256(
                f"xpd-hermes-route-v1:{route_key}:{node.node_id}".encode()
            ).digest(),
        )

    def select_node(
        self,
        route_key: str,
        *,
        available_node_ids: Sequence[str] | None = None,
    ) -> HermesNode:
        """Select a balanced node without changing the durable route store."""

        return self._select_node(route_key, available_node_ids)

    def bound_node_for_scope(self, scope: str) -> HermesNode | None:
        if not self.persistent_routing:
            return self.primary_node
        node_id = self.route_store.node_id_for_scope(scope)
        return self.node(node_id) if node_id else None

    def bound_node_for_session(self, session_id: str) -> HermesNode | None:
        if not self.persistent_routing:
            return self.primary_node
        node_id = self.route_store.node_id_for_session(session_id)
        return self.node(node_id) if node_id else None

    def assign_scope(
        self,
        scope: str,
        *,
        available_node_ids: Sequence[str] | None = None,
    ) -> HermesNode:
        existing = self.bound_node_for_scope(scope)
        if existing is not None:
            return existing
        node = self._select_node(scope, available_node_ids)
        if self.persistent_routing:
            try:
                self.route_store.bind_scope(scope, node.node_id)
            except HermesRouteConflictError:
                # Another concurrent first request for this owner may have
                # won the durable binding after our initial read.
                existing = self.bound_node_for_scope(scope)
                if existing is not None:
                    return existing
                raise
        return node

    def bind_scope(self, scope: str, node_id: str) -> HermesNode:
        node = self.node(node_id)
        if self.persistent_routing:
            self.route_store.bind_scope(scope, node_id)
        return node

    def bind_session(self, session_id: str, node_id: str) -> HermesNode:
        node = self.node(node_id)
        if self.persistent_routing:
            self.route_store.bind_session(session_id, node_id)
        return node

    def node_for_session(self, session_id: str) -> HermesNode:
        existing = self.bound_node_for_session(session_id)
        if existing is not None:
            return existing
        scope = owner_scope_from_session_id(session_id)
        if scope is None:
            raise ValueError("Invalid owned session id.")
        node = self.assign_scope(scope)
        return self.bind_session(session_id, node.node_id)

    def remove_session(self, session_id: str) -> None:
        if self.persistent_routing:
            self.route_store.remove_session(session_id)

    def rebind_scope_if_matches(
        self,
        scope: str,
        *,
        expected_node_id: str,
        replacement_node_id: str,
    ) -> HermesNode:
        """Move a Kubernetes sticky route without overwriting a concurrent winner."""

        if not self.allow_route_rebinding:
            raise HermesRouteUnavailableError(
                "Automatic Hermes route rebinding is disabled for static discovery."
            )
        self.node(replacement_node_id)
        current_node_id = self.route_store.rebind_scope_if_matches(
            scope,
            expected_node_id=expected_node_id,
            replacement_node_id=replacement_node_id,
        )
        if current_node_id is None:
            raise HermesRouteUnavailableError(
                "The owner scope has no persisted Hermes route to rebind."
            )
        return self.node(current_node_id)


def hermes_discovery_mode(env: Mapping[str, str] | None = None) -> str:
    environment = os.environ if env is None else env
    mode = str(environment.get("XPD_HERMES_DISCOVERY_MODE", "static")).strip().lower()
    if mode in {"", "static"}:
        return "static"
    if mode == "kubernetes":
        return mode
    raise HermesPoolConfigurationError(
        "XPD_HERMES_DISCOVERY_MODE must be 'static' or 'kubernetes'."
    )


def hermes_pool_from_nodes(
    nodes: Sequence[HermesNode],
    env: Mapping[str, str] | None = None,
    *,
    allow_route_rebinding: bool = False,
) -> HermesPool:
    environment = os.environ if env is None else env
    resolved_nodes = tuple(nodes)
    if not resolved_nodes:
        raise HermesPoolConfigurationError("At least one Hermes node is required.")
    scheduler_node_id = str(
        environment.get("XPD_HERMES_SCHEDULER_NODE", resolved_nodes[0].node_id)
    ).strip()
    return HermesPool(
        resolved_nodes,
        route_store=HermesRouteStore(default_route_state_path(environment)),
        scheduler_node_id=scheduler_node_id,
        allow_route_rebinding=allow_route_rebinding,
    )


def hermes_pool(env: Mapping[str, str] | None = None) -> HermesPool:
    """Build the backward-compatible static or legacy single-node pool."""

    environment = os.environ if env is None else env
    return hermes_pool_from_nodes(configured_hermes_nodes(environment), environment)


async def resolved_hermes_pool(
    env: Mapping[str, str] | None = None,
    *,
    discovery: HermesNodeDiscovery | None = None,
) -> HermesPool:
    """Build a pool from static configuration or dynamic Kubernetes discovery."""

    environment = os.environ if env is None else env
    if hermes_discovery_mode(environment) == "static":
        return hermes_pool(environment)

    resolved_discovery = discovery
    if resolved_discovery is None:
        # Keep the import local so the discovery module can use HermesNode without
        # creating a module import cycle.
        from xpd_report_agent.kubernetes_discovery import default_kubernetes_discovery

        resolved_discovery = default_kubernetes_discovery(None if env is None else environment)
    nodes = await resolved_discovery.discover_nodes()
    return hermes_pool_from_nodes(
        nodes,
        environment,
        allow_route_rebinding=True,
    )
