from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from xpd_report_agent.api.error_contract import api_error
from xpd_report_agent.hermes_pool import (
    HermesNode,
    HermesPool,
    HermesPoolConfigurationError,
    HermesRouteConflictError,
    HermesRouteStore,
    HermesRouteUnavailableError,
    default_route_state_path,
    hermes_discovery_mode,
    hermes_pool,
    owner_scope_from_session_id,
    resolved_hermes_pool,
)

_SESSION_PATH_PATTERN = re.compile(r"^/api/sessions/([^/?]+)")
_SCHEDULER_PATH_PREFIXES = ("/api/xpd-cron", "/api/jobs")


def session_id_from_request(
    path: str,
    payload: dict[str, Any] | None = None,
) -> str | None:
    match = _SESSION_PATH_PATTERN.match(path)
    if match:
        return match.group(1)
    if path.rstrip("/") == "/api/sessions" and isinstance(payload, dict):
        session_id = payload.get("id")
        return str(session_id) if session_id else None
    return None


def _pool_unavailable(message: str, *, node_id: str | None = None):
    body = {"node_id": node_id} if node_id else None
    return api_error(
        503,
        code="HERMES_POOL_UNAVAILABLE",
        message=message,
        retryable=True,
        outcome_unknown=False,
        body=body,
    )


async def probe_hermes_nodes(
    pool: HermesPool | None = None,
    *,
    api_key: str = "",
    timeout: float = 2.0,
) -> dict[str, dict[str, Any]]:
    resolved_pool = pool or hermes_pool()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def probe(node: HermesNode) -> tuple[str, dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.get(
                    f"{node.base_url}/health",
                    headers=headers,
                )
            return node.node_id, {
                "ok": response.is_success,
                "status_code": response.status_code,
                "error": None if response.is_success else "health check failed",
            }
        except httpx.HTTPError as exc:
            return node.node_id, {
                "ok": False,
                "status_code": None,
                "error": type(exc).__name__,
            }

    results = await asyncio.gather(*(probe(node) for node in resolved_pool.nodes))
    return dict(results)


async def _available_node_ids(pool: HermesPool, *, api_key: str) -> list[str]:
    health = await probe_hermes_nodes(pool, api_key=api_key)
    return [node.node_id for node in pool.nodes if bool(health.get(node.node_id, {}).get("ok"))]


async def resolve_hermes_node(
    path: str,
    *,
    scope: str | None = None,
    payload: dict[str, Any] | None = None,
    api_key: str = "",
) -> HermesNode:
    """Resolve one node before a request and never fail over mid-request."""

    try:
        pool = await resolved_hermes_pool()
    except (HermesPoolConfigurationError, RuntimeError, OSError) as exc:
        raise _pool_unavailable("Hermes service discovery is unavailable.") from exc

    session_id = session_id_from_request(path, payload)
    session_scope = owner_scope_from_session_id(session_id) if session_id else None
    if scope and session_scope and scope != session_scope:
        raise api_error(
            403,
            code="SESSION_OWNERSHIP_MISMATCH",
            message="The session does not belong to the authenticated owner.",
            retryable=False,
        )
    route_scope = session_scope or scope

    if any(path.startswith(prefix) for prefix in _SCHEDULER_PATH_PREFIXES):
        try:
            return pool.scheduler_node
        except HermesRouteUnavailableError as exc:
            raise _pool_unavailable(
                "The Hermes scheduler leader is not ready.",
                node_id=pool.scheduler_node_id,
            ) from exc
    if not pool.persistent_routing:
        return pool.primary_node

    try:
        if session_id:
            persisted_node_id = pool.route_store.node_id_for_session(session_id)
        elif route_scope:
            persisted_node_id = pool.route_store.node_id_for_scope(route_scope)
        else:
            persisted_node_id = None
    except (ValueError, HermesRouteUnavailableError) as exc:
        raise _pool_unavailable("Hermes routing state is unavailable.") from exc

    if persisted_node_id:
        try:
            existing = pool.node(persisted_node_id)
            if session_id:
                pool.bind_session(session_id, existing.node_id)
            return existing
        except HermesRouteConflictError as exc:
            raise _pool_unavailable("Hermes routing state is inconsistent.") from exc
        except HermesRouteUnavailableError as exc:
            if not pool.allow_route_rebinding or route_scope is None:
                raise _pool_unavailable(
                    "The Hermes node assigned to this session is no longer configured.",
                    node_id=persisted_node_id,
                ) from exc

            available_node_ids = await _available_node_ids(pool, api_key=api_key)
            if not available_node_ids:
                raise _pool_unavailable(
                    "No healthy Hermes node is available to migrate this session."
                ) from exc
            replacement = pool.select_node(
                route_scope,
                available_node_ids=available_node_ids,
            )
            try:
                rebound = pool.rebind_scope_if_matches(
                    route_scope,
                    expected_node_id=persisted_node_id,
                    replacement_node_id=replacement.node_id,
                )
                if session_id:
                    pool.bind_session(session_id, rebound.node_id)
                return rebound
            except (HermesRouteConflictError, HermesRouteUnavailableError) as rebind_exc:
                raise _pool_unavailable(
                    "No Hermes node is available for this session."
                ) from rebind_exc

    available_node_ids = await _available_node_ids(pool, api_key=api_key)
    if not available_node_ids:
        raise _pool_unavailable("No healthy Hermes node is available for a new assignment.")

    try:
        if route_scope:
            node = pool.assign_scope(
                route_scope,
                available_node_ids=available_node_ids,
            )
            if session_id:
                pool.bind_session(session_id, node.node_id)
            return node
    except (HermesRouteConflictError, HermesRouteUnavailableError) as exc:
        raise _pool_unavailable("No Hermes node is available for this session.") from exc

    # Unscoped administrative reads use the first currently healthy node.
    return pool.node(available_node_ids[0])


def forget_session_route(session_id: str) -> None:
    if hermes_discovery_mode() == "kubernetes":
        HermesRouteStore(default_route_state_path()).remove_session(session_id)
        return
    hermes_pool().remove_session(session_id)
