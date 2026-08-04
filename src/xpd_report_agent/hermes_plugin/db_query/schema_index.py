from __future__ import annotations

from .db import load_schema

TABLE_DESCRIPTIONS = {
    "tb_live_goods_daily_stats": (
        "直播商品日汇总表。一行表示一个商品在一天内的曝光、点击、加购、成交、退款、"
        "确认收货及预售尾款数据，粒度为 item_id + stat_date。"
    ),
    "tb_live_goods_session_stats": (
        "直播场次商品明细表。一行表示一个商品在一场直播中的曝光、点击、加购、成交及退款"
        "数据，粒度为 item_id + live_session_id。"
    ),
    "tb_session_endtime_stats": (
        "直播场次结束汇总表。一行表示一场直播的流量、观看、互动、商品、成交、退款和打赏"
        "总结，粒度为 live_session_id。"
    ),
}

METRICS = {
    "pay_amount": {
        "description": "成交金额，使用 SUM(pay_amt)。",
        "sql": "SUM(pay_amt)",
        "tables": [
            "tb_live_goods_daily_stats",
            "tb_live_goods_session_stats",
            "tb_session_endtime_stats",
        ],
    },
    "pay_order_count": {
        "description": "支付订单数，使用 SUM(pay_ord_cnt)。",
        "sql": "SUM(pay_ord_cnt)",
        "tables": [
            "tb_live_goods_daily_stats",
            "tb_live_goods_session_stats",
            "tb_session_endtime_stats",
        ],
    },
    "pay_buyer_count": {
        "description": "支付买家数，使用 SUM(pay_byr_cnt)。跨商品汇总可能重复，应说明口径。",
        "sql": "SUM(pay_byr_cnt)",
        "tables": [
            "tb_live_goods_daily_stats",
            "tb_live_goods_session_stats",
            "tb_session_endtime_stats",
        ],
    },
    "refund_amount": {
        "description": "退款金额，使用 SUM(refund_amt)。",
        "sql": "SUM(refund_amt)",
        "tables": [
            "tb_live_goods_daily_stats",
            "tb_live_goods_session_stats",
            "tb_session_endtime_stats",
        ],
    },
    "refund_rate": {
        "description": "金额退款率 = SUM(refund_amt) / NULLIF(SUM(pay_amt), 0)。",
        "sql": "SUM(refund_amt) / NULLIF(SUM(pay_amt), 0)",
        "tables": [
            "tb_live_goods_daily_stats",
            "tb_live_goods_session_stats",
            "tb_session_endtime_stats",
        ],
    },
    "item_click_rate": {
        "description": "商品点击率 = SUM(item_click_uv) / NULLIF(SUM(item_exposure_uv), 0)。",
        "sql": "SUM(item_click_uv) / NULLIF(SUM(item_exposure_uv), 0)",
        "tables": [
            "tb_live_goods_daily_stats",
            "tb_live_goods_session_stats",
            "tb_session_endtime_stats",
        ],
    },
}

SYNONYMS = {
    "商品": ["item_id", "item_title", "tb_live_goods_daily_stats", "tb_live_goods_session_stats"],
    "日数据": ["stat_date", "tb_live_goods_daily_stats"],
    "趋势": ["stat_date", "tb_live_goods_daily_stats"],
    "直播": ["live_session_id", "live_title", "tb_session_endtime_stats"],
    "场次": ["live_session_id", "tb_live_goods_session_stats", "tb_session_endtime_stats"],
    "曝光": ["item_exposure_uv", "item_exposure_pv", "channel_exposure_uv"],
    "点击": ["item_click_uv", "item_click_pv", "item_click_rate"],
    "观看": ["watch_uv", "watch_pv", "avg_watch_sec_per_user"],
    "互动": ["interact_uv", "like_cnt", "comment_cnt", "share_cnt"],
    "加购": ["cart_byr_cnt", "cart_itm_qty", "cart_amt"],
    "成交": ["pay_amount", "pay_amt", "pay_ord_cnt", "pay_byr_cnt"],
    "销售额": ["pay_amount", "pay_amt"],
    "GMV": ["pay_amount", "pay_amt"],
    "订单": ["pay_order_count", "pay_ord_cnt"],
    "转化率": ["pay_conversion_rate", "cart_conversion_rate", "item_click_rate"],
    "退款": ["refund_amount", "refund_amt", "refund_rate"],
    "退款率": ["refund_rate", "refund_byr_rate", "refund_order_rate"],
    "粉丝": ["new_fans_cnt", "fan_conversion_rate"],
    "打赏": ["reward_byr_cnt", "reward_cnt", "reward_rate"],
}


def _expanded_terms(question: str) -> set[str]:
    lowered = question.lower()
    terms = {term.lower() for term in lowered.split() if term}
    terms.add(lowered)

    for key, values in SYNONYMS.items():
        if key.lower() in lowered:
            terms.add(key.lower())
            terms.update(value.lower() for value in values)

    return terms


def search_schema(question: str, top_k: int = 8) -> dict:
    schema = load_schema()
    terms = _expanded_terms(question)
    lowered = question.lower()
    safe_top_k = max(1, min(int(top_k), 20))

    table_hits = []
    for table_name, meta in schema["tables"].items():
        columns = [column["name"] for column in meta["columns"]]
        text = " ".join(
            [table_name, TABLE_DESCRIPTIONS.get(table_name, ""), " ".join(columns)]
        ).lower()
        score = sum(1 for term in terms if term and term in text)
        if table_name.lower() in lowered:
            score += 5
        if score > 0:
            table_hits.append(
                {
                    "table": table_name,
                    "score": score,
                    "description": TABLE_DESCRIPTIONS.get(table_name, ""),
                    "columns": columns,
                }
            )
    table_hits.sort(key=lambda item: item["score"], reverse=True)

    metric_hits = []
    for metric_name, metric in METRICS.items():
        text = (metric_name + " " + metric["description"]).lower()
        score = sum(1 for term in terms if term and term in text)
        if metric_name.lower() in lowered:
            score += 5
        if score > 0:
            metric_hits.append({"metric": metric_name, **metric, "score": score})
    metric_hits.sort(key=lambda item: item["score"], reverse=True)

    return {
        "tables": table_hits[:safe_top_k],
        "metrics": metric_hits[:safe_top_k],
        "notes": [
            "日趋势使用 tb_live_goods_daily_stats，粒度为商品×日期。",
            "单场商品分析使用 tb_live_goods_session_stats，粒度为商品×直播场次。",
            "整场直播总结使用 tb_session_endtime_stats，粒度为直播场次。",
            "商品级买家数跨商品求和可能重复；整场口径优先使用场次结束汇总表。",
        ],
    }
