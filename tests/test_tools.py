from __future__ import annotations

import json

from xpd_report_agent.hermes_plugin.db_query import tools


def decode(payload: str) -> dict:
    return json.loads(payload)


def test_db_get_schema_ddl_tool_returns_ddl_relationships_and_metrics(demo_db_path):
    result = decode(tools.db_get_schema_ddl({}))

    assert result["ok"] is True
    assert "CREATE TABLE customers" in result["ddl"]
    assert "CREATE TABLE orders" in result["ddl"]
    assert "CREATE TABLE refunds" in result["ddl"]
    assert "orders.customer_id = customers.customer_id" in result["relationships"]
    assert "order_items.product_id = products.product_id" in result["relationships"]
    assert result["metrics"]["GMV"] == "SUM(order_items.quantity * order_items.unit_price)"
    assert "退款率" in result["metrics"]


def test_db_schema_search_tool_returns_json(demo_db_path):
    result = decode(tools.db_schema_search({"question": "按城市统计最近30天的订单数和GMV"}))

    assert result["ok"] is True
    assert result["tables"]
    assert any(item["table"] == "customers" for item in result["tables"])


def test_db_get_table_profile_includes_samples(demo_db_path):
    result = decode(
        tools.db_get_table_profile(
            {"tables": ["orders", "customers"], "include_samples": True}
        )
    )

    assert result["ok"] is True
    assert result["tables"]["orders"]["ok"] is True
    assert result["tables"]["orders"]["sample_rows"]


def test_db_execute_sql_revalidates_and_truncates(demo_db_path):
    result = decode(
        tools.db_execute_sql(
            {
                "sql": "SELECT customer_id, customer_name FROM customers ORDER BY customer_id",
                "max_rows": 2,
            }
        )
    )

    assert result["ok"] is True
    assert result["row_count"] == 2
    assert result["truncated"] is True
    assert result["columns"] == ["customer_id", "customer_name"]


def test_db_execute_sql_rejects_invalid_sql(demo_db_path):
    result = decode(tools.db_execute_sql({"sql": "SELECT * FROM customers"}))

    assert result["ok"] is False
    assert "validation" in result


def test_sql_tools_report_missing_sqlglot(monkeypatch):
    monkeypatch.setattr(
        tools,
        "_load_sql_guard",
        lambda: (None, tools.SQLGLOT_MISSING_ERROR),
    )

    validate_result = decode(tools.db_validate_sql({"sql": "SELECT customer_id FROM customers"}))
    execute_result = decode(tools.db_execute_sql({"sql": "SELECT customer_id FROM customers"}))

    assert validate_result["ok"] is False
    assert "sqlglot" in validate_result["error"]
    assert execute_result["ok"] is False
    assert "sqlglot" in execute_result["error"]
