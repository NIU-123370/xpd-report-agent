from __future__ import annotations

from xpd_report_agent.hermes_plugin.db_query.join_graph import find_join_paths
from xpd_report_agent.hermes_plugin.db_query.schema_index import search_schema


def test_schema_search_hits_brand_gmv_tables_and_metrics(demo_db_path):
    result = search_schema("最近30天每个品牌的GMV是多少？", top_k=8)

    tables = {item["table"] for item in result["tables"]}
    metrics = {item["metric"] for item in result["metrics"]}

    assert {"products", "order_items"} <= tables
    assert "gmv" in metrics


def test_join_paths_find_foreign_key_route(demo_db_path):
    result = find_join_paths(["categories", "orders"])

    assert result["ok"] is True
    path = result["join_paths"][0]["path"]
    assert result["join_paths"][0]["reachable"] is True
    assert [edge["from_table"] for edge in path] == [
        "categories",
        "products",
        "order_items",
    ]
    assert path[-1]["to_table"] == "orders"


def test_join_paths_reject_unknown_tables(demo_db_path):
    result = find_join_paths(["orders", "not_a_table"])

    assert result["ok"] is False
    assert "Unknown tables" in result["error"]
