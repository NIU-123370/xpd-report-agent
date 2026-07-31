from __future__ import annotations

from .db import load_schema

TABLE_DESCRIPTIONS = {
    "customers": "客户表，包含客户姓名、城市、注册日期。一行一个客户。",
    "categories": "商品类目表，包含护肤、彩妆、香氛、个护等类目。",
    "products": "商品表，包含商品名称、品牌、类目、标价。一行一个商品。",
    "orders": "订单主表，包含客户、下单日期、订单状态、销售渠道。一行一笔订单。",
    "order_items": "订单明细表，包含订单、商品、数量、成交单价。一行一个订单商品。",
    "payments": "支付表，包含订单支付方式、支付金额、支付状态。",
    "refunds": "退款表，包含订单退款金额、退款原因、退款状态。",
}

METRICS = {
    "gmv": {
        "description": "GMV，商品成交额，按订单明细 quantity * unit_price 计算。",
        "sql": "SUM(order_items.quantity * order_items.unit_price)",
        "tables": ["order_items", "orders"],
    },
    "order_count": {
        "description": "订单数，使用 COUNT(DISTINCT orders.order_id)。",
        "sql": "COUNT(DISTINCT orders.order_id)",
        "tables": ["orders"],
    },
    "refund_amount": {
        "description": "退款金额，只统计 refunds.status = 'success'。",
        "sql": "SUM(refunds.refund_amount)",
        "tables": ["refunds"],
    },
    "refund_rate": {
        "description": "退款率 = 退款金额 / GMV。",
        "tables": ["refunds", "order_items"],
    },
    "paid_amount": {
        "description": "支付金额，只统计 payments.status = 'success'。",
        "sql": "SUM(payments.amount)",
        "tables": ["payments"],
    },
}

SYNONYMS = {
    "客户": ["customers", "customer_name", "city", "signup_date"],
    "用户": ["customers", "customer_name", "city", "signup_date"],
    "城市": ["city", "customers"],
    "订单": ["orders", "order_count", "order_items"],
    "订单数": ["order_count", "orders", "下单"],
    "销售额": ["gmv", "成交额", "金额", "销售", "order_items"],
    "GMV": ["gmv", "销售额", "成交额", "order_items"],
    "成交额": ["gmv", "销售额", "order_items"],
    "商品": ["products", "product_name", "order_items"],
    "品牌": ["brand_name", "products"],
    "类目": ["category_name", "categories", "products"],
    "品类": ["category_name", "categories", "products"],
    "渠道": ["channel", "orders"],
    "支付": ["payments", "payment_method", "paid_amount"],
    "支付方式": ["payments", "payment_method", "paid_amount"],
    "退款": ["refunds", "refund_amount", "refund_rate"],
    "退款率": ["refund_rate", "refunds", "GMV"],
    "复购": ["customers", "orders", "order_count"],
    "客单价": ["gmv", "orders", "order_items"],
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
        columns = [c["name"] for c in meta["columns"]]
        text = " ".join(
            [
                table_name,
                TABLE_DESCRIPTIONS.get(table_name, ""),
                " ".join(columns),
            ]
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

    table_hits.sort(key=lambda x: x["score"], reverse=True)

    metric_hits = []
    for metric_name, metric in METRICS.items():
        text = (metric_name + " " + metric["description"]).lower()
        score = sum(1 for term in terms if term and term in text)
        if metric_name.lower() in lowered:
            score += 5
        if score > 0:
            metric_hits.append({"metric": metric_name, **metric, "score": score})

    metric_hits.sort(key=lambda x: x["score"], reverse=True)

    return {
        "tables": table_hits[:safe_top_k],
        "metrics": metric_hits[:safe_top_k],
        "notes": [
            "GMV 默认使用 order_items.quantity * order_items.unit_price。",
            "有效订单默认使用 orders.status IN ('paid', 'shipped', 'completed')。",
            "退款金额默认只统计 refunds.status = 'success'。",
            "支付金额默认只统计 payments.status = 'success'。",
        ],
    }
