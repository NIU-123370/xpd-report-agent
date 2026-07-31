from __future__ import annotations

from xpd_report_agent.hermes_plugin.db_query.db import (
    get_sample_rows,
    load_schema,
    quote_ident,
)


def test_load_schema_reads_tables_columns_and_foreign_keys(demo_db_path):
    schema = load_schema()

    assert set(schema["tables"]) == {
        "customers",
        "categories",
        "products",
        "orders",
        "order_items",
        "payments",
        "refunds",
    }
    assert schema["tables"]["orders"]["row_count"] > 0
    assert "order_id" in schema["tables"]["orders"]["primary_key"]
    assert {
        "from_table": "orders",
        "from_column": "customer_id",
        "to_table": "customers",
        "to_column": "customer_id",
    } in schema["foreign_keys"]


def test_get_sample_rows_validates_table_names(demo_db_path):
    rows = get_sample_rows("customers", limit=2)

    assert len(rows) == 2
    assert {"customer_id", "customer_name", "city", "signup_date"} <= set(rows[0])


def test_quote_ident_rejects_unsafe_identifiers():
    assert quote_ident("order_items") == '"order_items"'

    try:
        quote_ident("orders; drop table orders")
    except ValueError as exc:
        assert "Unsafe identifier" in str(exc)
    else:
        raise AssertionError("unsafe identifier was accepted")
