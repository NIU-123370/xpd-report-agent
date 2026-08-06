from xpd_report_agent.api.analysis_contracts import (
    analysis_output_contract_prompt,
    apply_analysis_output_contract,
    infer_analysis_type,
)
from xpd_report_agent.api.sessions import report_system_prompt
from xpd_report_agent.api.structured_analysis import StructuredAnalysis


def test_analysis_type_uses_explicit_intent_and_question_library():
    assert infer_analysis_type("昨天成交额是多少") == "query"
    assert infer_analysis_type("最近7天卖得怎么样") == "comparison"
    assert infer_analysis_type("退款率怎么突然升高了") == "diagnosis"
    assert infer_analysis_type("诊断成交下降原因") == "diagnosis"


def test_analysis_contract_sets_real_query_depth():
    comparison = analysis_output_contract_prompt("最近7天卖得怎么样")
    diagnosis = analysis_output_contract_prompt("为什么成交下降了")

    assert 'analysis_type="comparison"' in comparison
    assert "当前值、基准值、绝对变化、变化率" in comparison
    assert 'analysis_type="diagnosis"' in diagnosis
    assert "至少一个可用维度的拆分" in diagnosis
    assert analysis_output_contract_prompt("你好") == ""
    assert analysis_output_contract_prompt("把刚才结果导出") == ""


def test_session_prompt_injects_only_one_analysis_contract():
    prompt = report_system_prompt(user_message="退款率怎么突然升高了")

    assert prompt.count("<analysis_output_contract ") == 1
    assert 'analysis_type="diagnosis"' in prompt


def test_server_normalizes_type_and_removes_unbound_claims():
    analysis = StructuredAnalysis.model_validate(
        {
            "analysis_type": "query",
            "conclusion": "退款率上升",
            "comparisons": [
                {
                    "metric": "金额退款率",
                    "current_value": 0.2,
                    "baseline_value": 0.1,
                }
            ],
            "insights": [
                {"statement": "有证据", "evidence": "20% 对比 10%", "metric_refs": ["金额退款率"]},
                "没有证据",
            ],
            "drivers": [
                {"statement": "商品A贡献增长", "metric_refs": ["退款金额"]}
            ],
            "recommendations": [
                {"action": "泛化建议", "rationale": "", "metric_refs": []},
                {
                    "action": "复盘商品A",
                    "rationale": "贡献退款增量",
                    "metric_refs": ["退款金额"],
                },
            ],
        }
    )

    normalized = apply_analysis_output_contract(analysis, expected_type="diagnosis")

    assert normalized.analysis_type == "diagnosis"
    assert [item.statement for item in normalized.insights] == ["有证据"]
    assert normalized.drivers == []
    assert [item.action for item in normalized.recommendations] == ["复盘商品A"]
    assert "驱动或异常定位" in normalized.limitations[0]


def test_missing_comparison_is_exposed_as_a_limitation_not_fabricated():
    analysis = StructuredAnalysis(conclusion="数据不足")

    normalized = apply_analysis_output_contract(analysis, expected_type="comparison")

    assert normalized.comparisons == []
    assert "当前期与基准期对比" in normalized.limitations[0]
