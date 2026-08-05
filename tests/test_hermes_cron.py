from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

from xpd_report_agent.runtime import hermes_cron


class FakeWeb:
    @staticmethod
    def json_response(payload, status=200):
        return SimpleNamespace(payload=payload, status=status)


class FakeAdapter:
    def _http_route_table(self):
        return [("GET", "/health", lambda request: None)]

    @staticmethod
    def _check_auth(request):
        return request.auth_error


class FakeRequest:
    def __init__(self, body):
        self.body = body
        self.auth_error = None

    async def json(self):
        return self.body


def test_patch_creates_only_valid_native_no_agent_callback_job(tmp_path, monkeypatch):
    schedule_id = "sched_" + "a" * 32
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script_name = f"xpd_scheduled_report_{schedule_id}.py"
    (scripts / script_name).write_text("print('ok')\n", encoding="utf-8")
    heartbeat = tmp_path / "ticker_heartbeat"
    heartbeat.write_text("ok", encoding="utf-8")
    create_calls = []

    api_server = ModuleType("gateway.platforms.api_server")
    api_server.APIServerAdapter = FakeAdapter
    api_server.web = FakeWeb
    platforms = ModuleType("gateway.platforms")
    platforms.api_server = api_server
    gateway = ModuleType("gateway")
    gateway.platforms = platforms

    constants = ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path

    jobs = ModuleType("cron.jobs")
    jobs.TICKER_HEARTBEAT_FILE = heartbeat
    jobs.TICKER_INTERVAL_SECONDS = 60
    jobs.list_jobs = lambda include_disabled=True: []

    def create_job(**kwargs):
        create_calls.append(kwargs)
        return {"id": "a1b2c3d4e5f6", "enabled": True, **kwargs}

    jobs.create_job = create_job
    scheduler = ModuleType("cron.scheduler")
    scheduler._notify_provider_jobs_changed = lambda: None
    cron = ModuleType("cron")
    cron.jobs = jobs
    cron.scheduler = scheduler

    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms)
    monkeypatch.setitem(sys.modules, "gateway.platforms.api_server", api_server)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "cron", cron)
    monkeypatch.setitem(sys.modules, "cron.jobs", jobs)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)

    hermes_cron.install_patch()
    adapter = FakeAdapter()
    routes = {(method, path) for method, path, _ in adapter._http_route_table()}
    assert ("GET", "/api/xpd-cron/health") in routes
    assert ("POST", "/api/xpd-cron/jobs") in routes

    request = FakeRequest(
        {
            "schedule_id": schedule_id,
            "name": f"xpd-report:0123456789abcdefabcd:{schedule_id}",
            "schedule": "0 8 * * *",
            "script": script_name,
        }
    )
    response = asyncio.run(adapter._xpd_handle_create_cron_job(request))

    assert response.status == 201
    assert response.payload["job"]["id"] == "a1b2c3d4e5f6"
    assert create_calls == [
        {
            "prompt": "",
            "schedule": "0 8 * * *",
            "name": f"xpd-report:0123456789abcdefabcd:{schedule_id}",
            "deliver": "local",
            "script": script_name,
            "no_agent": True,
        }
    ]
