from __future__ import annotations

import inspect
import math
from collections.abc import Awaitable, Callable, Mapping

from fastapi import APIRouter, Response

from xpd_report_agent.api.agent_capacity import agent_capacity_health

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

MetricsSnapshot = Mapping[str, object]
MetricsProvider = Callable[[], MetricsSnapshot | Awaitable[MetricsSnapshot]]

_AGENT_METRICS = (
    (
        "limit",
        "xpd_agent_capacity_limit",
        "Configured maximum number of concurrent XPD agent analyses.",
    ),
    (
        "active",
        "xpd_agent_active",
        "Current number of active XPD agent analyses.",
    ),
    (
        "waiting",
        "xpd_agent_waiting",
        "Current number of XPD agent analyses waiting for capacity.",
    ),
    (
        "demand",
        "xpd_agent_demand",
        "Current total number of active and waiting XPD agent analyses.",
    ),
)

_HERMES_METRICS = (
    (
        "healthy",
        "xpd_hermes_nodes_healthy",
        "Current number of healthy Hermes nodes.",
    ),
    (
        "total",
        "xpd_hermes_nodes_total",
        "Configured number of Hermes nodes.",
    ),
)


async def _read_snapshot(provider: MetricsProvider) -> MetricsSnapshot:
    """Read one provider without allowing it to break Prometheus scrapes."""

    try:
        result = provider()
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Mapping):
            return result
    except Exception:
        pass
    return {}


def _safe_metric_value(value: object) -> str:
    """Render non-negative finite numbers and fail closed to zero."""

    return format(_safe_metric_number(value), ".15g")


def _safe_metric_number(value: object) -> int | float:
    """Normalize one metric input before rendering or arithmetic."""

    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if not isinstance(value, float) or not math.isfinite(value) or value < 0:
        return 0
    return value


def _render_metric_group(
    snapshot: MetricsSnapshot,
    definitions: tuple[tuple[str, str, str], ...],
) -> list[str]:
    lines: list[str] = []
    for source_key, metric_name, help_text in definitions:
        lines.extend(
            (
                f"# HELP {metric_name} {help_text}",
                f"# TYPE {metric_name} gauge",
                f"{metric_name} {_safe_metric_value(snapshot.get(source_key))}",
            )
        )
    return lines


def create_metrics_router(
    *,
    capacity_provider: MetricsProvider | None = None,
    hermes_provider: MetricsProvider | None = None,
) -> APIRouter:
    """Build the metrics router, optionally enriched with Hermes pool metrics.

    A Hermes provider returns ``{"healthy": number, "total": number}`` and may be
    synchronous or asynchronous. Keeping it optional lets deployments add network
    health probes without coupling this lightweight module to Hermes routing.
    """

    metrics_router = APIRouter()

    @metrics_router.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        agent_snapshot = await _read_snapshot(capacity_provider or agent_capacity_health)
        agent_snapshot = {
            **agent_snapshot,
            "demand": (
                _safe_metric_number(agent_snapshot.get("active"))
                + _safe_metric_number(agent_snapshot.get("waiting"))
            ),
        }
        lines = _render_metric_group(agent_snapshot, _AGENT_METRICS)

        if hermes_provider is not None:
            hermes_snapshot = await _read_snapshot(hermes_provider)
            lines.extend(_render_metric_group(hermes_snapshot, _HERMES_METRICS))

        return Response(
            content="\n".join(lines) + "\n",
            headers={"content-type": PROMETHEUS_CONTENT_TYPE},
        )

    return metrics_router


router = create_metrics_router()
