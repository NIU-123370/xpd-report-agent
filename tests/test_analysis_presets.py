from __future__ import annotations

from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from xpd_report_agent.api import analysis_presets as presets_api
from xpd_report_agent.api import main as app_main
from xpd_report_agent.api.session_service import owner_scope

CLIENT_KEY = "analysis-client-key-with-24-characters"
CLIENT_HEADERS = {"X-XPD-Session-Key": CLIENT_KEY}


def supported_columns() -> dict[str, set[str]]:
    return {
        "tb_live_goods_daily_stats": {
            "item_id",
            "item_title",
            "stat_date",
            "pay_amt",
            "pay_ord_cnt",
            "pay_itm_qty",
            "refund_amt",
        },
        "tb_live_goods_session_stats": {
            "item_id",
            "live_session_id",
            "live_start_time",
            "pay_amt",
            "refund_amt",
        },
        "tb_session_endtime_stats": {
            "live_session_id",
            "live_start_time",
            "pay_amt",
            "refund_amt",
            "pay_byr_cnt",
        },
    }


def test_capabilities_enable_refund_and_ranking_but_block_aggregate_repurchase():
    capabilities = presets_api._capabilities_from_columns(supported_columns())

    assert capabilities["refund_diagnosis"]["ready"] is True
    assert capabilities["refund_diagnosis"]["available_focuses"] == [
        "items",
        "overview",
        "sessions",
    ]
    assert "不能归因" in capabilities["refund_diagnosis"]["limitations"][0]
    assert capabilities["product_ranking"]["ready"] is True
    assert "refund_rate" in capabilities["product_ranking"]["available_focuses"]
    assert capabilities["repurchase_analysis"]["ready"] is False
    assert "buyer_id/customer_id" in capabilities["repurchase_analysis"]["reason"]
    assert "pay_byr_cnt" in capabilities["repurchase_analysis"]["limitations"][0]


def test_repurchase_requires_buyer_order_and_time_in_same_supported_table():
    columns = supported_columns()
    columns["tb_live_goods_daily_stats"].update(
        {"buyer_id", "order_id"}
    )

    capabilities = presets_api._capabilities_from_columns(columns)

    assert capabilities["repurchase_analysis"]["ready"] is True
    assert capabilities["repurchase_analysis"]["available_focuses"] == ["overview"]


def test_list_presets_returns_frontend_contract(monkeypatch):
    monkeypatch.setenv("XPD_SESSION_SIGNING_SECRET", "analysis-signing-secret")

    async def capabilities():
        return presets_api._capabilities_from_columns(supported_columns())

    monkeypatch.setattr(presets_api, "analysis_capabilities", capabilities)
    response = TestClient(app_main.app).get(
        "/api/analysis-presets", headers=CLIENT_HEADERS
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert [item["preset_id"] for item in data] == [
        "refund_diagnosis",
        "product_ranking",
        "repurchase_analysis",
    ]
    refund = data[0]
    assert refund["ready"] is True
    assert refund["default_days"] == 30
    assert refund["allowed_days"] == [7, 30, 60, 90]
    assert refund["default_focus"] == "overview"
    assert refund["allowed_top_n"] == [10, 20, 50]
    repurchase = data[2]
    assert repurchase["ready"] is False
    assert repurchase["focus_options"] == []


def test_run_preset_reuses_session_sse_and_pins_real_sql_workflow(monkeypatch):
    monkeypatch.setenv("XPD_SESSION_SIGNING_SECRET", "analysis-signing-secret")
    captured = {}

    async def capabilities():
        return presets_api._capabilities_from_columns(supported_columns())

    async def stream(session_id, req, request, scope, raw_user_id):
        captured.update(session_id=session_id, req=req, scope=scope)

        async def events():
            yield 'event: content.delta\ndata: {"delta":"ok"}\n\n'

        return StreamingResponse(events(), media_type="text/event-stream")

    monkeypatch.setattr(presets_api, "analysis_capabilities", capabilities)
    monkeypatch.setattr(presets_api, "session_chat_stream", stream)

    scope = owner_scope(CLIENT_KEY, secret="analysis-signing-secret")
    response = TestClient(app_main.app).post(
        f"/api/sessions/xpd_{scope}_placeholder/analyses",
        headers=CLIENT_HEADERS,
        json={
            "preset_id": "refund_diagnosis",
            "days": 60,
            "focus": "items",
            "top_n": 20,
            "note": "关注大额退款",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    prompt = captured["req"].message
    assert "最近 60 个完整自然日" in prompt
    assert "前 20 个商品" in prompt
    assert "db-multitable-query Skill" in prompt
    assert "基于真实 Schema 和实际执行结果" in prompt
    assert "db_get_schema_ddl" not in prompt
    assert "db_validate_sql" not in prompt
    assert "db_execute_sql" not in prompt
    assert "不调用 export_report_file" in prompt
    assert "不得猜测或编造具体退款原因" in prompt
    assert "样本不足" in prompt
    assert "关注大额退款" in prompt


def test_run_repurchase_returns_409_before_contacting_hermes(monkeypatch):
    monkeypatch.setenv("XPD_SESSION_SIGNING_SECRET", "analysis-signing-secret")
    called = False

    async def capabilities():
        return presets_api._capabilities_from_columns(supported_columns())

    async def stream(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Hermes must not be called")

    monkeypatch.setattr(presets_api, "analysis_capabilities", capabilities)
    monkeypatch.setattr(presets_api, "session_chat_stream", stream)

    scope = owner_scope(CLIENT_KEY, secret="analysis-signing-secret")
    response = TestClient(app_main.app).post(
        f"/api/sessions/xpd_{scope}_placeholder/analyses",
        headers=CLIENT_HEADERS,
        json={"preset_id": "repurchase_analysis", "days": 90},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "analysis_data_not_supported"
    assert detail["preset_id"] == "repurchase_analysis"
    assert "无法识别同一客户" in detail["message"]
    assert called is False


def test_blocked_preset_hides_cross_owner_session_before_capability_check(monkeypatch):
    monkeypatch.setenv("XPD_SESSION_SIGNING_SECRET", "analysis-signing-secret")
    checked = False

    async def capabilities():
        nonlocal checked
        checked = True
        return presets_api._capabilities_from_columns(supported_columns())

    monkeypatch.setattr(presets_api, "analysis_capabilities", capabilities)
    response = TestClient(app_main.app).post(
        "/api/sessions/xpd_00000000000000000000_other/analyses",
        headers=CLIENT_HEADERS,
        json={"preset_id": "repurchase_analysis", "days": 90},
    )

    assert response.status_code == 404
    assert checked is False


def test_run_rejects_unavailable_focus_before_contacting_hermes(monkeypatch):
    monkeypatch.setenv("XPD_SESSION_SIGNING_SECRET", "analysis-signing-secret")
    columns = supported_columns()
    columns["tb_live_goods_daily_stats"].discard("pay_itm_qty")

    async def capabilities():
        return presets_api._capabilities_from_columns(columns)

    monkeypatch.setattr(presets_api, "analysis_capabilities", capabilities)
    scope = owner_scope(CLIENT_KEY, secret="analysis-signing-secret")
    response = TestClient(app_main.app).post(
        f"/api/sessions/xpd_{scope}_placeholder/analyses",
        headers=CLIENT_HEADERS,
        json={
            "preset_id": "product_ranking",
            "focus": "pay_itm_qty",
            "days": 30,
            "top_n": 10,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "analysis_focus_not_supported"
