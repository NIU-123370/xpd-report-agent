from __future__ import annotations

import json
import time
from importlib import import_module
from typing import Any

from .db import connect_readonly, get_sample_rows, get_schema_ddl, load_schema
from .join_graph import find_join_paths
from .schema_index import TABLE_DESCRIPTIONS, search_schema

BUSINESS_METRICS = {
    "成交金额": "SUM(pay_amt)",
    "支付订单数": "SUM(pay_ord_cnt)",
    "支付买家数": "SUM(pay_byr_cnt)",
    "退款金额": "SUM(refund_amt)",
    "退款率": "SUM(refund_amt) / NULLIF(SUM(pay_amt), 0)",
    "商品支付转化率": "pay_byr_cnt / item_click_uv；优先使用源字段 pay_conversion_rate",
}

SQLGLOT_MISSING_ERROR = (
    "Missing Python dependency 'sqlglot' in the Hermes runtime. "
    "Install plugin dependencies in the Hermes venv before validating or executing SQL."
)


def to_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def error_json(message: Any) -> str:
    return to_json({"ok": False, "error": str(message)})


def coerce_args(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    if args is None:
        return kwargs
    if kwargs:
        merged = dict(args)
        merged.update(kwargs)
        return merged
    return args


def _load_sql_guard():
    try:
        return import_module(".sql_guard", __package__), None
    except ModuleNotFoundError as exc:
        if exc.name == "sqlglot":
            return None, SQLGLOT_MISSING_ERROR
        raise


def db_get_schema_ddl(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    try:
        coerce_args(args, **kwargs)
        schema = load_schema()
        relationships = [
            f"{fk['from_table']}.{fk['from_column']} = {fk['to_table']}.{fk['to_column']}"
            for fk in schema["foreign_keys"]
        ]
        return to_json(
            {
                "ok": True,
                "ddl": get_schema_ddl(),
                "relationships": relationships,
                "metrics": BUSINESS_METRICS,
            }
        )
    except Exception as exc:
        return error_json(exc)


def db_schema_search(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    try:
        payload = coerce_args(args, **kwargs)
        question = payload["question"]
        top_k = int(payload.get("top_k", 8))
        result = search_schema(question, top_k=top_k)
        return to_json({"ok": True, "question": question, **result})
    except Exception as exc:
        return error_json(exc)


def db_get_table_profile(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    try:
        payload = coerce_args(args, **kwargs)
        tables = payload["tables"]
        include_samples = bool(payload.get("include_samples", True))
        schema = load_schema()

        result = {}
        for table in tables:
            if table not in schema["tables"]:
                result[table] = {"ok": False, "error": "table not found"}
                continue

            result[table] = {
                "ok": True,
                "description": TABLE_DESCRIPTIONS.get(table, ""),
                **schema["tables"][table],
            }
            if include_samples:
                result[table]["sample_rows"] = get_sample_rows(table, limit=3)

        return to_json({"ok": True, "tables": result})
    except Exception as exc:
        return error_json(exc)


def db_get_join_paths(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    try:
        payload = coerce_args(args, **kwargs)
        return to_json(find_join_paths(payload["tables"]))
    except Exception as exc:
        return error_json(exc)


def db_validate_sql(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    try:
        payload = coerce_args(args, **kwargs)
        sql_guard, error = _load_sql_guard()
        if error:
            return to_json({"ok": False, "error": error})
        return to_json(sql_guard.validate_sql(payload["sql"]))
    except Exception as exc:
        return error_json(exc)


def db_execute_sql(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    try:
        payload = coerce_args(args, **kwargs)
        sql = payload["sql"]
        sql_guard, error = _load_sql_guard()
        if error:
            return to_json({"ok": False, "error": error})

        max_rows = sql_guard.clamp_max_rows(int(payload.get("max_rows", 100)))

        validation = sql_guard.validate_sql(sql)
        if not validation.get("ok"):
            return to_json(
                {
                    "ok": False,
                    "error": "SQL validation failed before execution",
                    "validation": validation,
                }
            )

        started = time.time()
        limited_sql = sql_guard.wrap_with_limit(sql, max_rows=max_rows)
        conn = connect_readonly()
        try:
            with conn.cursor() as cursor:
                cursor.execute(limited_sql)
                rows = list(cursor.fetchall())
        finally:
            conn.close()

        data = [dict(r) for r in rows[:max_rows]]
        columns = list(data[0].keys()) if data else []
        elapsed_ms = int((time.time() - started) * 1000)

        return to_json(
            {
                "ok": True,
                "columns": columns,
                "rows": data,
                "row_count": len(data),
                "truncated": len(rows) > max_rows,
                "elapsed_ms": elapsed_ms,
                "sql": sql,
            }
        )
    except Exception as exc:
        return error_json(exc)
