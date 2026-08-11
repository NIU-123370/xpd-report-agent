from __future__ import annotations

import sys
from types import ModuleType

from xpd_report_agent.runtime import hermes_cron_scheduler


def _fake_scheduler_provider(monkeypatch):
    provider_module = ModuleType("cron.scheduler_provider")
    upstream = object()
    calls = []

    def resolve_cron_scheduler():
        calls.append("resolve")
        return upstream

    provider_module.resolve_cron_scheduler = resolve_cron_scheduler
    cron_module = ModuleType("cron")
    cron_module.scheduler_provider = provider_module
    monkeypatch.setitem(sys.modules, "cron", cron_module)
    monkeypatch.setitem(sys.modules, "cron.scheduler_provider", provider_module)
    return provider_module, upstream, calls


def test_scheduler_node_keeps_upstream_provider(monkeypatch):
    provider_module, upstream, calls = _fake_scheduler_provider(monkeypatch)
    monkeypatch.setenv("XPD_HERMES_NODE_ID", "hermes-0")
    monkeypatch.setenv("XPD_HERMES_SCHEDULER_NODE", "hermes-0")

    assert hermes_cron_scheduler.install_patch() is True

    assert provider_module.resolve_cron_scheduler() is upstream
    assert calls == ["resolve"]


def test_worker_provider_never_ticks_or_recovers(monkeypatch):
    provider_module, _upstream, calls = _fake_scheduler_provider(monkeypatch)
    monkeypatch.setenv("XPD_HERMES_NODE_ID", "hermes-2")
    monkeypatch.setenv("XPD_HERMES_SCHEDULER_NODE", "hermes-0")

    assert hermes_cron_scheduler.install_patch() is True
    provider = provider_module.resolve_cron_scheduler()

    class StopEvent:
        def __init__(self):
            self.wait_calls = 0

        def wait(self):
            self.wait_calls += 1

    stop_event = StopEvent()
    provider.start(stop_event, adapters=object(), loop=object())

    assert provider.name == "xpd-worker-disabled"
    assert stop_event.wait_calls == 1
    assert provider.recover_interrupted() == 0
    assert provider.fire_due("job-id") is False
    assert calls == []


def test_configured_scheduler_fails_closed_when_node_id_is_missing(monkeypatch):
    provider_module, _upstream, calls = _fake_scheduler_provider(monkeypatch)
    monkeypatch.delenv("XPD_HERMES_NODE_ID", raising=False)
    monkeypatch.setenv("XPD_HERMES_SCHEDULER_NODE", "hermes-0")

    assert hermes_cron_scheduler.install_patch() is True

    assert provider_module.resolve_cron_scheduler().name == "xpd-worker-disabled"
    assert calls == []
