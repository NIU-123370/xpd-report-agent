from __future__ import annotations

from xpd_report_agent.api.db_skill import (
    db_skill_prompt,
    export_action_prompt,
    is_export_only_request,
    requested_analysis_type,
)
from xpd_report_agent.api.sessions import report_system_prompt


def test_database_skill_is_injected_for_data_work_but_not_pure_export():
    query_prompt = report_system_prompt(user_message="统计最近30天成交金额前10商品")
    combined_prompt = report_system_prompt(
        user_message="统计最近30天成交金额前10商品并导出 XLSX"
    )
    export_prompt = report_system_prompt(user_message="把刚才结果导出为 XLSX")

    assert "<db_query_skill>" in query_prompt
    assert "## 查询决策流程" in query_prompt
    assert "<db_query_skill>" in combined_prompt
    assert "<db_query_skill>" not in export_prompt


def test_pure_export_reuses_snapshot_without_database_tools():
    assert is_export_only_request("xlsx") is True
    assert is_export_only_request("把刚才结果导出为 CSV") is True
    assert is_export_only_request("统计退款金额并导出 PDF") is False
    assert is_export_only_request("不要导出 Excel") is False

    prompt = export_action_prompt("xlsx")
    assert "最近一个未过期的 result_id" in prompt
    assert "不要调用 db_schema_search" in prompt
    assert "db_execute_sql" in prompt
    assert "简单查询、对比分析或诊断分析" in prompt
    assert "用户确认前不得调用任何查询或导出工具" in prompt
    assert db_skill_prompt("xlsx") == ""
    assert export_action_prompt("不要导出 Excel") == ""


def test_non_database_conversation_does_not_load_database_skill():
    assert db_skill_prompt("你好") == ""
    assert export_action_prompt("你好") == ""


def test_requested_analysis_type_only_infers_explicit_report_depth():
    assert requested_analysis_type("导出最近7天成交金额合计") == "simple"
    assert requested_analysis_type("导出本月与上月成交额对比") == "comparison"
    assert requested_analysis_type("诊断退款率异常原因并导出") == "diagnostic"
    assert requested_analysis_type("把数据导出为 XLSX") is None
