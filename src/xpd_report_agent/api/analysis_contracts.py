from __future__ import annotations

from typing import Literal

from xpd_report_agent.api.db_skill import is_database_request, requested_analysis_type
from xpd_report_agent.api.merchant_questions import match_merchant_questions
from xpd_report_agent.api.structured_analysis import StructuredAnalysis

AnalysisType = Literal["query", "comparison", "diagnosis"]

_EXPLICIT_TYPE_MAP: dict[str, AnalysisType] = {
    "simple": "query",
    "comparison": "comparison",
    "diagnostic": "diagnosis",
}


def infer_analysis_type(user_message: str | None) -> AnalysisType:
    explicit = requested_analysis_type(user_message)
    if explicit:
        return _EXPLICIT_TYPE_MAP[explicit]

    matches = match_merchant_questions(user_message, top_k=1)
    if matches and float(matches[0].get("match_score") or 0) >= 0.50:
        matched_type = str(matches[0].get("analysis_type") or "")
        if matched_type in {"query", "comparison", "diagnosis"}:
            return matched_type  # type: ignore[return-value]
    return "query"


def analysis_output_contract_prompt(user_message: str | None) -> str:
    if not is_database_request(user_message):
        return ""
    analysis_type = infer_analysis_type(user_message)
    if analysis_type == "query":
        requirements = (
            "只查询回答问题所需的数据；首句给出直接答案。至少说明时间范围、指标口径和关键数据质量。"
            "不强行增加驱动分析或经营建议。"
        )
    elif analysis_type == "comparison":
        requirements = (
            "必须取得当前期和可靠基准期；用户未指定时使用等长上一周期。至少输出当前值、基准值、"
            "绝对变化、变化率和一个有证据的事实洞察。问题涉及整体经营表现时，还应查询可用的商品、"
            "场次或日期拆分来说明主要驱动；无法拆分时明确限制。"
        )
    else:
        requirements = (
            "必须先证明异常或变化确实存在，再查询可靠基准和至少一个可用维度的拆分。输出核心结论、"
            "异常定位、驱动证据以及绑定指标证据的建议动作；事实、推断和建议分开。缺少原因字段时只能"
            "说明数值贡献或待验证假设，不得声称因果。"
        )
    return (
        f'<analysis_output_contract analysis_type="{analysis_type}">\n'
        f"本轮服务端判定为 {analysis_type} 类型。{requirements}\n"
        "结果为空、周期不完整、零分母、小样本或缺少维度时，不得为了满足结构而补造内容；应降低结论"
        "强度，并在数据质量或限制中明确说明。用户可见答案不得输出 SQL。\n"
        "</analysis_output_contract>"
    )


def apply_analysis_output_contract(
    analysis: StructuredAnalysis,
    *,
    expected_type: AnalysisType,
) -> StructuredAnalysis:
    """Normalize model output and remove unsupported evidence claims."""

    analysis.analysis_type = expected_type
    analysis.insights = [
        item for item in analysis.insights if item.evidence and item.metric_refs
    ]
    analysis.drivers = [
        item for item in analysis.drivers if item.evidence and item.metric_refs
    ]
    analysis.anomalies = [item for item in analysis.anomalies if item.metric_refs]
    analysis.recommendations = [
        item
        for item in analysis.recommendations
        if item.metric_refs and item.rationale.strip()
    ]

    empty_result = bool(analysis.data_quality and analysis.data_quality.empty_result)
    missing: list[str] = []
    if expected_type in {"comparison", "diagnosis"} and not analysis.comparisons:
        missing.append("可靠的当前期与基准期对比")
    if expected_type == "diagnosis" and not (analysis.drivers or analysis.anomalies):
        missing.append("有指标证据的驱动或异常定位")
    if expected_type == "diagnosis" and not analysis.recommendations:
        missing.append("绑定指标证据的建议动作")
    if missing and not empty_result:
        limitation = "本轮尚未形成" + "、".join(missing) + "，结论深度受限。"
        if limitation not in analysis.limitations:
            analysis.limitations.append(limitation)
    return analysis
