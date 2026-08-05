from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from xpd_report_agent.api import schedules as schedules_api
from xpd_report_agent.api.schedule_store import ScheduleStore

OWNER_A = "0123456789abcdefabcd"
OWNER_B = "fedcba9876543210abcd"
SCHEDULE_ID = "sched_" + "a" * 32


@pytest.fixture(autouse=True)
def _enable_schedules_for_schedule_tests(monkeypatch):
    monkeypatch.setenv("XPD_SCHEDULES_ENABLED", "true")


def schedule_record(*, token: str = "t" * 48) -> dict:
    return {
        "schedule_id": SCHEDULE_ID,
        "owner_scope": OWNER_A,
        "callback_token": token,
        "native_job_id": "a1b2c3d4e5f6",
        "native_schedule": "0 8 * * *",
        "report_type": "daily_operations",
        "report_label": "经营日报",
        "frequency": "daily",
        "run_date": None,
        "weekday": None,
        "time": "08:00",
        "timezone": "Asia/Shanghai",
        "format": "xlsx",
        "enabled": True,
        "state": "scheduled",
        "next_run_at": None,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "pending_manual_run_id": None,
        "created_at": "2026-08-04T00:00:00+00:00",
        "updated_at": "2026-08-04T00:00:00+00:00",
    }


def test_native_schedule_supports_once_daily_and_weekly():
    once = schedules_api.ScheduleUpsertRequest(
        report_type="daily_operations",
        frequency="once",
        run_date="2099-08-05",
        time="09:35",
        format="xlsx",
    )
    daily = schedules_api.ScheduleUpsertRequest(
        report_type="daily_operations",
        frequency="daily",
        time="08:05",
        format="csv",
    )
    sunday = schedules_api.ScheduleUpsertRequest(
        report_type="weekly_brand",
        frequency="weekly",
        weekday=7,
        time="08:30",
        format="markdown",
    )
    pdf_report = schedules_api.ScheduleUpsertRequest(
        report_type="daily_operations",
        frequency="daily",
        time="08:45",
        format="pdf",
    )
    json_report = schedules_api.ScheduleUpsertRequest(
        report_type="daily_operations",
        frequency="daily",
        time="09:00",
        format="json",
    )

    assert schedules_api._native_schedule(once) == "2099-08-05T09:35+08:00"
    assert schedules_api._native_schedule(daily) == "5 8 * * *"
    assert schedules_api._native_schedule(sunday) == "30 8 * * 0"
    assert schedules_api._native_schedule(pdf_report) == "45 8 * * *"
    assert schedules_api._native_schedule(json_report) == "0 9 * * *"


def test_once_schedule_rejects_past_minute():
    request = schedules_api.ScheduleUpsertRequest(
        report_type="daily_operations",
        frequency="once",
        run_date="2020-01-01",
        time="00:00",
        format="xlsx",
    )

    with pytest.raises(HTTPException) as exc:
        schedules_api._native_schedule(request)

    assert exc.value.status_code == 422


def test_store_isolates_owner_and_hides_callback_token(tmp_path):
    store = ScheduleStore(tmp_path / "schedules.json")
    public = store.create(schedule_record())

    assert "callback_token" not in public
    assert [item["schedule_id"] for item in store.list_owned(OWNER_A)] == [SCHEDULE_ID]
    assert store.list_owned(OWNER_B) == []
    assert store.get_owned(SCHEDULE_ID, OWNER_B) is None


def test_cron_callback_claim_is_idempotent_and_manual_run_is_distinct(tmp_path):
    store = ScheduleStore(tmp_path / "schedules.json")
    token = "callback-token-with-more-than-thirty-two-characters"
    store.create(schedule_record(token=token))
    now = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)

    first, claimed = store.claim_run(SCHEDULE_ID, token, now=now)
    duplicate, duplicate_claimed = store.claim_run(SCHEDULE_ID, token, now=now)

    assert claimed is True
    assert duplicate_claimed is False
    assert duplicate["run_id"] == first["run_id"]
    assert first["trigger_type"] == "scheduled"

    # Move beyond the callback replay window, then request an explicit run.
    store.update(
        SCHEDULE_ID,
        {"last_callback_at": "2026-08-04T00:00:00+00:00"},
    )
    marker = store.set_manual_trigger(SCHEDULE_ID, OWNER_A)
    manual, manual_claimed = store.claim_run(SCHEDULE_ID, token, now=now)

    assert marker
    assert manual_claimed is True
    assert manual["trigger_type"] == "manual"
    assert manual["run_id"] != first["run_id"]


def test_wrong_callback_token_is_rejected(tmp_path):
    store = ScheduleStore(tmp_path / "schedules.json")
    store.create(schedule_record())

    with pytest.raises(PermissionError):
        store.claim_run(SCHEDULE_ID, "wrong-token")


def test_scheduled_session_metadata_is_read_only(tmp_path):
    store = ScheduleStore(tmp_path / "schedules.json")
    token = "t" * 48
    store.create(schedule_record(token=token))
    run, _ = store.claim_run(
        SCHEDULE_ID,
        token,
        now=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
    )

    metadata = store.metadata_for_session(run["session_id"])

    assert metadata == {
        "origin": "scheduled",
        "read_only": True,
        "schedule_id": SCHEDULE_ID,
        "report_kind": "daily_operations",
        "run_status": "pending",
        "scheduled_for": "2026-08-05T08:00:00+08:00",
        "error_summary": None,
    }


def test_brand_report_is_blocked_without_explicit_brand_dimension(monkeypatch):
    monkeypatch.setattr(
        schedules_api,
        "_brand_dimension_matches",
        lambda: [],
    )

    capabilities = schedules_api._report_capabilities_sync()

    assert capabilities["daily_operations"]["ready"] is True
    assert capabilities["weekly_brand"]["ready"] is False


def test_create_schedule_uses_native_no_agent_bridge_and_owner_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("XPD_SCHEDULE_STATE_PATH", str(tmp_path / "schedules.json"))
    monkeypatch.setenv("XPD_CRON_SCRIPT_DIR", str(tmp_path / "scripts"))
    calls = []

    async def ready(report_type):
        calls.append(("ready", report_type))

    async def create_native_job(**kwargs):
        calls.append(("create", kwargs))
        return {
            "id": "a1b2c3d4e5f6",
            "enabled": True,
            "state": "scheduled",
            "next_run_at": "2026-08-05T08:00:00+08:00",
        }

    monkeypatch.setattr(schedules_api, "_require_report_ready", ready)
    monkeypatch.setattr(schedules_api, "_create_native_job", create_native_job)
    request = schedules_api.ScheduleUpsertRequest(
        report_type="daily_operations",
        frequency="daily",
        time="08:00",
        format="xlsx",
    )

    result = asyncio.run(schedules_api.create_schedule(request, OWNER_A))

    assert result["schedule"]["report_type"] == "daily_operations"
    assert result["schedule"]["next_run_at"] == "2026-08-05T08:00:00+08:00"
    assert calls[1][1]["owner_scope"] == OWNER_A
    script_files = list((tmp_path / "scripts").glob("xpd_scheduled_report_*.py"))
    assert len(script_files) == 1
    script_text = script_files[0].read_text(encoding="utf-8")
    assert "/api/internal/scheduled-reports/" in script_text
    assert "wakeAgent" in script_text


def test_scheduled_prompt_pins_period_and_requires_validated_export():
    prompt = schedules_api._scheduled_prompt(
        {
            "scheduled_for": "2026-08-05T08:00:00+08:00",
            "report_type": "daily_operations",
            "report_format": "xlsx",
        }
    )

    assert "2026-08-04 00:00 至 23:59:59" in prompt
    assert "不要调用 clarify" in prompt
    assert "capture_for_export=true" in prompt
    assert "export_report_file" in prompt
    assert "analysis_type 固定为 diagnostic" in prompt
