from __future__ import annotations

from xpd_report_agent.api.structured_analysis import (
    parse_run_clarification,
    parse_structured_analysis,
)


def test_structured_analysis_is_validated_and_removed_from_content():
    content = """核心结论：成交金额增长。
<XPD_ANALYSIS_JSON>
{
  "schema_version": "1.0",
  "conclusion": "成交金额增长",
  "data_period": {"start": "2026-08-01", "end": "2026-08-05", "label": "最近5天"},
  "metrics": [{"name": "成交金额", "value": 123.45, "unit": "元"}],
  "insights": ["成交金额增长"],
  "assumptions": ["按自然日统计"],
  "sql": ["SELECT SUM(pay_amt) FROM tb_live_goods_daily_stats"]
}
</XPD_ANALYSIS_JSON>"""

    clean_content, analysis = parse_structured_analysis(content)

    assert clean_content == "核心结论：成交金额增长。"
    assert analysis.structured is True
    assert analysis.metrics[0].value == 123.45
    assert analysis.insights[0].statement == "成交金额增长"
    assert analysis.sql == ["SELECT SUM(pay_amt) FROM tb_live_goods_daily_stats"]


def test_invalid_structured_analysis_falls_back_to_text():
    clean_content, analysis = parse_structured_analysis(
        "可读结论\n<XPD_ANALYSIS_JSON>{not-json}</XPD_ANALYSIS_JSON>"
    )

    assert clean_content == "可读结论"
    assert analysis.structured is False
    assert analysis.conclusion == "可读结论"


def test_structured_analysis_v11_carries_evidence_and_quality():
    content = """分析完成。
<XPD_ANALYSIS_JSON>
{
  "schema_version": "1.1",
  "analysis_type": "diagnosis",
  "conclusion": "商品A是退款增长的主要贡献项",
  "comparisons": [{"metric":"退款金额","current_value":120,"baseline_value":80,"absolute_change":40,"relative_change":0.5,"unit":"元"}],
  "drivers": [{"statement":"商品A贡献主要增量","evidence":"退款金额增加35元","metric_refs":["退款金额"],"contribution":0.875,"confidence":0.95}],
  "anomalies": [{"statement":"商品A退款率偏高","severity":"high","metric_refs":["退款率"],"observed_value":0.2,"baseline_value":0.08}],
  "recommendations": [{"action":"优先复盘商品A","rationale":"贡献退款增量的87.5%","priority":"high","metric_refs":["退款金额"]}],
  "limitations": ["缺少退款原因字段"],
  "data_quality": {"query_count":1,"returned_row_count":10,"truncated":false,"empty_result":false,"validation_passed":true,"notes":[]},
  "executed_queries": [{"sql":"SELECT item_id, SUM(refund_amt) FROM report GROUP BY item_id","validation_passed":true,"returned_row_count":10,"truncated":false,"elapsed_ms":12}],
  "sql": ["SELECT item_id, SUM(refund_amt) FROM report GROUP BY item_id"]
}
</XPD_ANALYSIS_JSON>"""

    clean_content, analysis = parse_structured_analysis(content)

    assert clean_content == "分析完成。"
    assert analysis.schema_version == "1.1"
    assert analysis.analysis_type == "diagnosis"
    assert analysis.comparisons[0].relative_change == 0.5
    assert analysis.drivers[0].confidence == 0.95
    assert analysis.recommendations[0].metric_refs == ["退款金额"]
    assert analysis.data_quality is not None
    assert analysis.data_quality.returned_row_count == 10
    assert analysis.executed_queries[0].elapsed_ms == 12


def test_structured_analysis_v12_has_typed_scope_definitions_and_insights():
    content = """完成。
<XPD_ANALYSIS_JSON>
{
  "schema_version": "1.2",
  "conclusion": "成交金额增长",
  "data_scope": {"period_start":"2026-08-01","period_end":"2026-08-07","grain":"商品+自然日","filters":["有效支付"],"dimensions":["item_id"],"source_tables":["daily_report"],"deduplication":"按商品和日期聚合"},
  "metric_definitions": [{"name":"退款率","formula":"SUM(refund_amt)/SUM(pay_amt)","unit":"%","numerator":"退款金额","denominator":"成交金额","grain":"整体"}],
  "insights": [{"statement":"成交金额增长20%","evidence":"当前120元，基准100元","metric_refs":["成交金额"],"confidence":1.0}]
}
</XPD_ANALYSIS_JSON>"""

    _, analysis = parse_structured_analysis(content)

    assert analysis.schema_version == "1.2"
    assert analysis.data_scope is not None
    assert analysis.data_scope.dimensions == ["item_id"]
    assert analysis.metric_definitions[0].denominator == "成交金额"
    assert analysis.insights[0].metric_refs == ["成交金额"]
    assert analysis.insights[0].evidence == "当前120元，基准100元"


def test_run_clarification_envelope_is_machine_parsed_and_bounded():
    clarification = parse_run_clarification(
        """<XPD_CLARIFICATION_REQUEST>
{"question":"销量按件数还是订单数？","choices":["件数","订单数", "", "GMV", "超出上限"]}
</XPD_CLARIFICATION_REQUEST>"""
    )

    assert clarification is not None
    assert clarification.question == "销量按件数还是订单数？"
    assert clarification.choices == ["件数", "订单数", "GMV", "超出上限"]


def test_invalid_run_clarification_envelope_is_ignored():
    assert (
        parse_run_clarification(
            "<XPD_CLARIFICATION_REQUEST>{not-json}</XPD_CLARIFICATION_REQUEST>"
        )
        is None
    )
