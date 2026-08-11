from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

LOGGER = logging.getLogger("xpd_report_agent.hermes_cron_scheduler")
NODE_ID_ENV = "XPD_HERMES_NODE_ID"
SCHEDULER_NODE_ENV = "XPD_HERMES_SCHEDULER_NODE"


def _scheduler_is_local() -> bool:
    scheduler_node = os.getenv(SCHEDULER_NODE_ENV, "").strip()
    if not scheduler_node:
        # Preserve the single-node/local deployment behaviour when no leader
        # has been configured.
        return True
    return os.getenv(NODE_ID_ENV, "").strip() == scheduler_node


class WorkerNoopCronScheduler:
    """Cron provider used by query-only Hermes workers.

    It deliberately does not call Hermes recovery, tick, reconciliation, or
    fire hooks. ``start`` only waits for gateway shutdown so the provider
    thread has the same lifecycle as Hermes' built-in scheduler thread.
    """

    name = "xpd-worker-disabled"

    def is_available(self) -> bool:
        return True

    def start(self, stop_event: Any, **_kwargs: Any) -> None:
        LOGGER.info(
            "Hermes Cron ticker disabled on worker node %s; scheduler node is %s",
            os.getenv(NODE_ID_ENV, "").strip() or "<unset>",
            os.getenv(SCHEDULER_NODE_ENV, "").strip() or "<unset>",
        )
        stop_event.wait()

    def stop(self) -> None:
        return None

    def on_jobs_changed(self) -> None:
        return None

    def recover_interrupted(self) -> int:
        return 0

    def fire_due(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def reconcile(self) -> None:
        return None


def install_patch() -> bool:
    """Make Hermes' Cron provider resolver aware of the configured node role."""

    try:
        from cron import scheduler_provider
    except ModuleNotFoundError as exc:
        if exc.name in {"cron", "cron.scheduler_provider"}:
            return False
        raise

    if getattr(scheduler_provider, "_xpd_scheduler_role_patch", False):
        return True

    upstream_resolver = scheduler_provider.resolve_cron_scheduler

    @wraps(upstream_resolver)
    def resolve_cron_scheduler_for_node() -> Any:
        if _scheduler_is_local():
            return upstream_resolver()
        return WorkerNoopCronScheduler()

    scheduler_provider.resolve_cron_scheduler = resolve_cron_scheduler_for_node
    scheduler_provider._xpd_scheduler_role_patch = True
    return True
