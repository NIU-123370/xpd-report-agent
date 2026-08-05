from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from xpd_report_agent.api.session_service import (
    CLIENT_USER_ID_HEADER,
    redact_sensitive_text,
    require_owned_session,
)
from xpd_report_agent.api.sessions import (
    SessionChatRequest,
    SessionScope,
    session_chat_stream,
)
from xpd_report_agent.hermes_plugin.db_query.db import connect_readonly, get_mysql_config

router = APIRouter(prefix="/api")

SUPPORTED_TABLES = (
    "tb_live_goods_daily_stats",
    "tb_live_goods_session_stats",
    "tb_session_endtime_stats",
)
TIME_COLUMNS = frozenset(
    {
        "stat_date",
        "live_start_time",
        "live_end_time",
        "end_time",
        "start_time",
        "created_at",
        "paid_at",
        "pay_time",
        "order_time",
    }
)
BUYER_ID_COLUMNS = frozenset(
    {"buyer_id", "customer_id", "member_id", "consumer_id", "shopper_id"}
)
ORDER_ID_COLUMNS = frozenset(
    {"order_id", "parent_order_id", "trade_id", "trade_order_id"}
)
REFUND_REASON_COLUMNS = frozenset(
    {"refund_reason", "refund_reason_name", "refund_cause", "refund_type"}
)

ALLOWED_DAYS = (7, 30, 60, 90)
ALLOWED_TOP_N = (10, 20, 50)

PRESET_DEFINITIONS: dict[str, dict[str, Any]] = {
    "refund_diagnosis": {
        "title": "退款诊断",
        "summary": "识别退款金额、退款率的异常趋势和高风险商品或场次。",
        "description": "使用真实成交与退款汇总数据定位异常，不在缺少原因字段时猜测退款原因。",
        "default_days": 30,
        "default_focus": "overview",
        "supports_top_n": True,
        "default_top_n": 10,
        "focus_options": (
            {"value": "overview", "label": "综合诊断"},
            {"value": "items", "label": "商品退款"},
            {"value": "sessions", "label": "场次退款"},
        ),
    },
    "product_ranking": {
        "title": "商品排行",
        "summary": "按成交、订单、销量或退款表现查看商品排名。",
        "description": "基于商品粒度的真实报表数据生成 Top 排行和关键效率摘要。",
        "default_days": 30,
        "default_focus": "pay_amt",
        "supports_top_n": True,
        "default_top_n": 10,
        "focus_options": (
            {"value": "pay_amt", "label": "成交金额"},
            {"value": "pay_ord_cnt", "label": "支付订单数"},
            {"value": "pay_itm_qty", "label": "成交件数"},
            {"value": "refund_amt", "label": "退款金额"},
            {"value": "refund_rate", "label": "金额退款率"},
        ),
    },
    "repurchase_analysis": {
        "title": "复购分析",
        "summary": "识别重复购买客户、复购率与复购周期。",
        "description": "只有订单级数据同时具备稳定客户标识、订单标识和时间时才可执行。",
        "default_days": 90,
        "default_focus": "overview",
        "supports_top_n": False,
        "focus_options": ({"value": "overview", "label": "复购概览"},),
    },
}


class AnalysisRunRequest(BaseModel):
    preset_id: str = Field(min_length=1, max_length=64)
    days: Literal[7, 30, 60, 90] = 30
    focus: str | None = Field(default=None, min_length=1, max_length=64)
    top_n: Literal[10, 20, 50] = 10
    note: str | None = Field(default=None, max_length=500)


def _analysis_schema_columns_sync() -> dict[str, set[str]]:
    database = get_mysql_config()["database"]
    connection = connect_readonly()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, ORDINAL_POSITION
                """,
                (database,),
            )
            rows = list(cursor.fetchall())
    finally:
        connection.close()

    result: dict[str, set[str]] = {table: set() for table in SUPPORTED_TABLES}
    for row in rows:
        table_name = str(row["table_name"])
        if table_name in result:
            result[table_name].add(str(row["column_name"]).lower())
    return result


def _has_time(columns: set[str]) -> bool:
    return bool(columns & TIME_COLUMNS)


def _refund_focuses(columns_by_table: dict[str, set[str]]) -> set[str]:
    focuses: set[str] = set()
    for columns in columns_by_table.values():
        if not ({"pay_amt", "refund_amt"} <= columns and _has_time(columns)):
            continue
        focuses.add("overview")
        if "item_id" in columns:
            focuses.add("items")
        if "live_session_id" in columns:
            focuses.add("sessions")
    return focuses


def _ranking_focuses(columns_by_table: dict[str, set[str]]) -> set[str]:
    focuses: set[str] = set()
    for columns in columns_by_table.values():
        if "item_id" not in columns or not _has_time(columns):
            continue
        focuses.update(
            field
            for field in ("pay_amt", "pay_ord_cnt", "pay_itm_qty", "refund_amt")
            if field in columns
        )
        if {"pay_amt", "refund_amt"} <= columns:
            focuses.add("refund_rate")
    return focuses


def _repurchase_ready(columns_by_table: dict[str, set[str]]) -> bool:
    return any(
        bool(columns & BUYER_ID_COLUMNS)
        and bool(columns & ORDER_ID_COLUMNS)
        and _has_time(columns)
        for columns in columns_by_table.values()
    )


def _capabilities_from_columns(
    columns_by_table: dict[str, set[str]],
) -> dict[str, dict[str, Any]]:
    refund_focuses = _refund_focuses(columns_by_table)
    ranking_focuses = _ranking_focuses(columns_by_table)
    repurchase_ready = _repurchase_ready(columns_by_table)
    has_refund_reason = any(
        bool(columns & REFUND_REASON_COLUMNS) for columns in columns_by_table.values()
    )

    return {
        "refund_diagnosis": {
            "ready": "overview" in refund_focuses,
            "reason": None
            if "overview" in refund_focuses
            else "当前数据库缺少同时包含 pay_amt、refund_amt 和时间字段的报表表，无法执行退款诊断。",
            "available_focuses": sorted(refund_focuses),
            "limitations": []
            if has_refund_reason
            else [
                "现有表缺少退款原因字段，只能定位异常商品、场次或时段，不能归因具体退款原因。"
            ],
        },
        "product_ranking": {
            "ready": bool(ranking_focuses),
            "reason": None
            if ranking_focuses
            else "当前数据库缺少商品标识、时间字段或可排名经营指标，无法生成商品排行。",
            "available_focuses": sorted(ranking_focuses),
            "limitations": [],
        },
        "repurchase_analysis": {
            "ready": repurchase_ready,
            "reason": None
            if repurchase_ready
            else (
                "现有数据只有聚合买家数，缺少稳定的 buyer_id/customer_id 和 order_id，"
                "无法识别同一客户是否重复购买。"
            ),
            "available_focuses": ["overview"] if repurchase_ready else [],
            "limitations": []
            if repurchase_ready
            else ["pay_byr_cnt 是聚合计数，不能用来推算复购人数或复购率。"],
        },
    }


async def analysis_capabilities() -> dict[str, dict[str, Any]]:
    try:
        columns = await asyncio.to_thread(_analysis_schema_columns_sync)
        return _capabilities_from_columns(columns)
    except Exception as exc:
        error = redact_sensitive_text(str(exc))[:200]
        reason = "暂时无法检查报表字段，请确认 MySQL 连接和表结构。"
        if error:
            reason = f"{reason}（{error}）"
        return {
            preset_id: {
                "ready": False,
                "reason": reason,
                "available_focuses": [],
                "limitations": [],
            }
            for preset_id in PRESET_DEFINITIONS
        }


def _public_presets(
    capabilities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for preset_id, definition in PRESET_DEFINITIONS.items():
        capability = capabilities[preset_id]
        available = set(capability.get("available_focuses") or [])
        focus_options = [
            dict(option)
            for option in definition["focus_options"]
            if option["value"] in available
        ]
        default_focus = str(definition["default_focus"])
        if focus_options and default_focus not in {
            str(option["value"]) for option in focus_options
        }:
            default_focus = str(focus_options[0]["value"])
        result.append(
            {
                "preset_id": preset_id,
                "title": definition["title"],
                "summary": definition["summary"],
                "description": definition["description"],
                "ready": bool(capability["ready"]),
                "reason": capability.get("reason"),
                "limitations": list(capability.get("limitations") or []),
                "default_days": definition["default_days"],
                "allowed_days": list(ALLOWED_DAYS),
                "focus_options": focus_options,
                "default_focus": default_focus,
                "supports_top_n": definition["supports_top_n"],
                "default_top_n": definition.get("default_top_n"),
                "allowed_top_n": list(ALLOWED_TOP_N)
                if definition["supports_top_n"]
                else [],
            }
        )
    return result


@router.get("/analysis-presets")
async def list_analysis_presets(scope: SessionScope) -> dict[str, Any]:
    del scope
    capabilities = await analysis_capabilities()
    return {"ok": True, "data": _public_presets(capabilities)}


def _refund_task(days: int, focus: str, top_n: int) -> str:
    focus_instruction = {
        "overview": (
            "先计算总成交金额、总退款金额和金额退款率；字段存在时再计算订单退款率和件数退款率。"
            "随后展示按日结果，"
            f"并列出退款金额较高的前 {top_n} 个商品或场次。"
        ),
        "items": (
            f"按 item_id（有 item_title 时同时展示）汇总，列出退款金额较高的前 {top_n} 个商品，"
            "同时展示成交金额和金额退款率。"
        ),
        "sessions": (
            f"按 live_session_id 汇总，列出退款金额较高的前 {top_n} 场直播，"
            "同时展示成交金额和金额退款率。"
        ),
    }[focus]
    return (
        f"执行预设分析「退款诊断」。查询范围是最近 {days} 个完整自然日："
        f"CURRENT_DATE - INTERVAL {days} DAY 至 CURRENT_DATE（不含今天）。\n"
        f"{focus_instruction}\n"
        "优先使用退款金额识别风险。金额退款率必须使用 "
        "SUM(refund_amt) / NULLIF(SUM(pay_amt), 0)，订单退款率使用 "
        "SUM(refund_ord_cnt) / NULLIF(SUM(pay_ord_cnt), 0)，件数退款率使用 "
        "SUM(refund_itm_qty) / NULLIF(SUM(pay_itm_qty), 0)；字段不存在时跳过对应指标。"
        "不得对源表中的比率字段直接求和。极小成交额或订单量造成的高比率要展示分母并单独说明。"
        "商品级 pay_byr_cnt/refund_byr_cnt 跨商品或日期可能重复，不得解释为全店唯一买家数。"
        "如果数据没有退款原因字段，只能描述异常关联，不得猜测或编造具体退款原因。"
    )


def _ranking_task(days: int, focus: str, top_n: int) -> str:
    metric = {
        "pay_amt": ("SUM(pay_amt)", "成交金额"),
        "pay_ord_cnt": ("SUM(pay_ord_cnt)", "支付订单数"),
        "pay_itm_qty": ("SUM(pay_itm_qty)", "成交件数"),
        "refund_amt": ("SUM(refund_amt)", "退款金额"),
        "refund_rate": (
            "SUM(refund_amt) / NULLIF(SUM(pay_amt), 0)",
            "金额退款率",
        ),
    }[focus]
    return (
        f"执行预设分析「商品排行」。查询范围是最近 {days} 个完整自然日："
        f"CURRENT_DATE - INTERVAL {days} DAY 至 CURRENT_DATE（不含今天）。\n"
        f"按商品汇总，以 {metric[0]}（{metric[1]}）降序列出前 {top_n} 名，"
        "展示稳定的 item_id，存在 item_title 时仅作为展示标签。同时补充数据库中确实存在的"
        "成交、点击、加购和退款关键指标，但不得编造缺失字段或综合评分。"
        "点击率、转化率和退款率必须使用分子分母重新加权计算，不得对源比率求和或平均。"
        "按退款率排名时必须同时展示成交金额或订单量分母，并把小样本高比率标为观察项，"
        "不能直接下高风险结论。"
    )


def _repurchase_task(days: int) -> str:
    return (
        f"执行预设分析「复购分析」，范围为最近 {days} 个完整自然日。"
        "必须使用稳定的客户标识和订单级明细识别同一客户的多次购买；"
        "不得使用 pay_byr_cnt 等聚合计数推算复购。"
    )


def _analysis_task(req: AnalysisRunRequest, focus: str) -> str:
    if req.preset_id == "refund_diagnosis":
        task = _refund_task(req.days, focus, req.top_n)
    elif req.preset_id == "product_ranking":
        task = _ranking_task(req.days, focus, req.top_n)
    else:
        task = _repurchase_task(req.days)

    common = (
        "\n\n这是后端预设数据分析任务。不要调用 clarify；不要将示例值当作结果。"
        "必须遵循 db-multitable-query Skill 的查询决策流程，基于真实 Schema 和实际执行"
        "结果完成分析。本预设只做数据问答，不调用 export_report_file。"
        "先确认所选窗口内的实际日期范围、覆盖天数、商品数和场次数；"
        "若只有一天或一个场次，不得声称形成趋势或场次对比，必须明确写出样本不足。"
        "最终回答遵守系统输出契约。"
        "如果执行时发现必需字段不存在，必须明确说明数据不支持并停止，"
        "不得用其他聚合指标替代、猜测或编造数据。"
    )
    note = (req.note or "").strip()
    if note:
        common += f"\n用户补充要求（仅作为本次分析条件）：{note}"
    return task + common


def _blocked(preset_id: str, message: str, *, code: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": code, "preset_id": preset_id, "message": message},
    )


@router.post("/sessions/{session_id}/analyses")
async def run_analysis_preset(
    session_id: str,
    req: AnalysisRunRequest,
    request: Request,
    scope: SessionScope,
    raw_user_id: Annotated[
        str | None, Header(alias=CLIENT_USER_ID_HEADER)
    ] = None,
) -> StreamingResponse:
    # Preserve the cross-owner 404 behavior even when a preset is globally
    # unavailable for the current schema.
    require_owned_session(session_id, scope)
    definition = PRESET_DEFINITIONS.get(req.preset_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="未找到指定的预设分析。")

    capabilities = await analysis_capabilities()
    capability = capabilities[req.preset_id]
    if not capability["ready"]:
        raise _blocked(
            req.preset_id,
            str(capability["reason"]),
            code="analysis_data_not_supported",
        )

    focus = req.focus or str(definition["default_focus"])
    declared_focuses = {
        str(option["value"]) for option in definition["focus_options"]
    }
    if focus not in declared_focuses:
        raise HTTPException(status_code=422, detail="不支持的分析视角。")
    if focus not in set(capability.get("available_focuses") or []):
        raise _blocked(
            req.preset_id,
            "当前数据库字段不支持该分析视角。",
            code="analysis_focus_not_supported",
        )

    message = _analysis_task(req, focus)
    return await session_chat_stream(
        session_id,
        SessionChatRequest(message=message, stream=True),
        request,
        scope,
        raw_user_id,
    )
