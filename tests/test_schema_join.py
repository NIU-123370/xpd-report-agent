from __future__ import annotations

from xpd_report_agent.hermes_plugin.db_query.join_graph import find_join_paths
from xpd_report_agent.hermes_plugin.db_query.schema_index import search_schema


def test_schema_search_hits_daily_product_table_and_payment_metric(report_schema):
    result = search_schema("最近30天每个商品的销售额趋势？", top_k=8)

    tables = {item["table"] for item in result["tables"]}
    metrics = {item["metric"] for item in result["metrics"]}

    assert "tb_live_goods_daily_stats" in tables
    assert "pay_amount" in metrics


def test_join_paths_connect_session_products_to_session_summary(report_schema):
    result = find_join_paths(
        ["tb_live_goods_session_stats", "tb_session_endtime_stats"]
    )

    assert result["ok"] is True
    path = result["join_paths"][0]["path"]
    assert result["join_paths"][0]["reachable"] is True
    assert path == [
        {
            "from_table": "tb_live_goods_session_stats",
            "from_column": "live_session_id",
            "to_table": "tb_session_endtime_stats",
            "to_column": "live_session_id",
            "join_condition": (
                "tb_live_goods_session_stats.live_session_id = "
                "tb_session_endtime_stats.live_session_id"
            ),
        }
    ]


def test_join_paths_reject_unknown_tables(report_schema):
    result = find_join_paths(["tb_session_endtime_stats", "not_a_table"])

    assert result["ok"] is False
    assert "Unknown tables" in result["error"]
