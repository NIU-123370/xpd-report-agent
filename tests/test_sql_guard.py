from __future__ import annotations

from xpd_report_agent.hermes_plugin.db_query.sql_guard import (
    validate_sql,
    wrap_with_limit,
)

BRAND_REFUND_SQL = """
WITH brand_gmv AS (
    SELECT
        p.brand_name AS brand_name,
        COUNT(DISTINCT o.order_id) AS order_count,
        SUM(oi.quantity * oi.unit_price) AS gmv
    FROM orders o
    JOIN order_items oi
        ON o.order_id = oi.order_id
    JOIN products p
        ON oi.product_id = p.product_id
    WHERE o.order_date >= date('now', '-30 day')
      AND o.status IN ('paid', 'shipped', 'completed')
    GROUP BY p.brand_name
),
brand_refund AS (
    SELECT
        p.brand_name AS brand_name,
        SUM(r.refund_amount) AS refund_amount
    FROM refunds r
    JOIN order_items oi
        ON r.order_item_id = oi.order_item_id
    JOIN products p
        ON oi.product_id = p.product_id
    JOIN orders o
        ON r.order_id = o.order_id
    WHERE o.order_date >= date('now', '-30 day')
      AND r.status = 'success'
    GROUP BY p.brand_name
)
SELECT
    g.brand_name,
    g.order_count,
    ROUND(g.gmv, 2) AS gmv,
    ROUND(COALESCE(r.refund_amount, 0), 2) AS refund_amount,
    ROUND(COALESCE(r.refund_amount, 0) / NULLIF(g.gmv, 0), 4) AS refund_rate
FROM brand_gmv g
LEFT JOIN brand_refund r
    ON g.brand_name = r.brand_name
ORDER BY g.gmv DESC
"""


def test_validate_sql_allows_cte_query_and_tracks_real_tables(demo_db_path):
    result = validate_sql(BRAND_REFUND_SQL)

    assert result["ok"] is True
    assert set(result["used_tables"]) == {"orders", "order_items", "products", "refunds"}
    assert result["explain"]


def test_validate_sql_allows_count_star(demo_db_path):
    result = validate_sql("SELECT COUNT(*) AS customer_count FROM customers")

    assert result["ok"] is True
    assert result["used_tables"] == ["customers"]


def test_validate_sql_rejects_select_wildcard(demo_db_path):
    result = validate_sql("SELECT * FROM orders")

    assert result["ok"] is False
    assert "SELECT *" in result["error"]


def test_validate_sql_rejects_dml_and_pragma(demo_db_path):
    delete_result = validate_sql("DELETE FROM orders WHERE order_id = 1")
    pragma_result = validate_sql("PRAGMA table_info(orders)")

    assert delete_result["ok"] is False
    assert "Forbidden SQL operation" in delete_result["error"]
    assert pragma_result["ok"] is False


def test_validate_sql_rejects_unknown_tables_and_multiple_statements(demo_db_path):
    unknown_result = validate_sql("SELECT order_id FROM missing_orders")
    multi_result = validate_sql("SELECT order_id FROM orders; SELECT customer_id FROM customers")

    assert unknown_result["ok"] is False
    assert "Unknown or disallowed tables" in unknown_result["error"]
    assert multi_result["ok"] is False
    assert "Only one SQL statement" in multi_result["error"]


def test_wrap_with_limit_clamps_max_rows():
    limited = wrap_with_limit("SELECT customer_id FROM customers", max_rows=50000)

    assert limited.endswith("LIMIT 1001")
