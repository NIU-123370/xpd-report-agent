from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

STRUCTURED_ANALYSIS_START = "<XPD_ANALYSIS_JSON>"
STRUCTURED_ANALYSIS_END = "</XPD_ANALYSIS_JSON>"
RUN_CLARIFICATION_START = "<XPD_CLARIFICATION_REQUEST>"
RUN_CLARIFICATION_END = "</XPD_CLARIFICATION_REQUEST>"


class AnalysisDataPeriod(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: str | None = Field(default=None, max_length=64)
    end: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=200)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)


class AnalysisMetric(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=120)
    value: int | float | str | None = None
    unit: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=300)


class AnalysisMetricDefinition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=120)
    formula: str = Field(default="", max_length=1000)
    unit: str | None = Field(default=None, max_length=40)
    aggregation: str | None = Field(default=None, max_length=200)
    numerator: str | None = Field(default=None, max_length=300)
    denominator: str | None = Field(default=None, max_length=300)
    grain: str | None = Field(default=None, max_length=300)


class AnalysisDataScope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    period_start: str | None = Field(default=None, max_length=64)
    period_end: str | None = Field(default=None, max_length=64)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    grain: str | None = Field(default=None, max_length=300)
    filters: list[str] = Field(default_factory=list, max_length=30)
    dimensions: list[str] = Field(default_factory=list, max_length=30)
    source_tables: list[str] = Field(default_factory=list, max_length=30)
    deduplication: str | None = Field(default=None, max_length=1000)


class AnalysisInsight(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statement: str = Field(min_length=1, max_length=1000)
    evidence: str | None = Field(default=None, max_length=1000)
    metric_refs: list[str] = Field(default_factory=list, max_length=20)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"statement": value}
        return value


class AnalysisComparison(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metric: str = Field(min_length=1, max_length=120)
    current_value: int | float | str | None = None
    baseline_value: int | float | str | None = None
    absolute_change: int | float | str | None = None
    relative_change: int | float | str | None = None
    unit: str | None = Field(default=None, max_length=40)
    baseline_label: str | None = Field(default=None, max_length=120)
    favorability: str | None = Field(default=None, max_length=20)


class AnalysisTrend(BaseModel):
    model_config = ConfigDict(extra="ignore")

    period: str = Field(min_length=1, max_length=120)
    metric: str = Field(min_length=1, max_length=120)
    current_value: int | float | str | None
    baseline_value: int | float | str | None = None
    absolute_change: int | float | str | None = None
    relative_change: int | float | str | None = None
    unit: str | None = Field(default=None, max_length=40)
    anomaly: str | None = Field(default=None, max_length=1000)


class AnalysisDriver(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statement: str = Field(min_length=1, max_length=1000)
    evidence: str | None = Field(default=None, max_length=1000)
    metric_refs: list[str] = Field(default_factory=list, max_length=20)
    contribution: int | float | str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class AnalysisAnomaly(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statement: str = Field(min_length=1, max_length=1000)
    severity: Literal["低", "中", "高", "low", "medium", "high"] = "中"
    metric_refs: list[str] = Field(default_factory=list, max_length=20)
    observed_value: int | float | str | None = None
    baseline_value: int | float | str | None = None


class AnalysisRecommendation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str = Field(min_length=1, max_length=1000)
    rationale: str = Field(default="", max_length=1000)
    priority: Literal["低", "中", "高", "low", "medium", "high"] = "中"
    metric_refs: list[str] = Field(default_factory=list, max_length=20)


class AnalysisDataQuality(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query_count: int = Field(default=0, ge=0, le=100)
    returned_row_count: int | None = Field(default=None, ge=0)
    truncated: bool = False
    empty_result: bool = False
    validation_passed: bool | None = None
    freshness: dict[str, Any] | None = None
    period_coverage: dict[str, Any] | None = None
    null_counts: dict[str, int] = Field(default_factory=dict)
    zero_denominators: dict[str, int] = Field(default_factory=dict)
    small_samples: dict[str, Any] = Field(default_factory=dict)
    dimensions: dict[str, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list, max_length=30)
    notes: list[str] = Field(default_factory=list, max_length=30)


class AnalysisExecutedQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sql: str = Field(min_length=1, max_length=100_000)
    validation_passed: bool = True
    returned_row_count: int | None = Field(default=None, ge=0)
    truncated: bool = False
    elapsed_ms: int | None = Field(default=None, ge=0)


class StructuredAnalysis(BaseModel):
    """Stable, versioned analysis contract returned to API clients."""

    model_config = ConfigDict(extra="ignore")

    schema_version: Literal["1.0", "1.1", "1.2"] = "1.2"
    structured: bool = True
    analysis_type: Literal["query", "comparison", "diagnosis"] = "query"
    conclusion: str = Field(default="", max_length=20_000)
    data_period: AnalysisDataPeriod | None = None
    data_scope: AnalysisDataScope | None = None
    metrics: list[AnalysisMetric] = Field(default_factory=list, max_length=50)
    metric_definitions: list[AnalysisMetricDefinition] = Field(
        default_factory=list, max_length=50
    )
    comparisons: list[AnalysisComparison] = Field(default_factory=list, max_length=50)
    trends: list[AnalysisTrend] = Field(default_factory=list, max_length=100)
    insights: list[AnalysisInsight] = Field(default_factory=list, max_length=30)
    drivers: list[AnalysisDriver] = Field(default_factory=list, max_length=30)
    anomalies: list[AnalysisAnomaly] = Field(default_factory=list, max_length=30)
    recommendations: list[AnalysisRecommendation] = Field(
        default_factory=list, max_length=30
    )
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    data_quality: AnalysisDataQuality | None = None
    executed_queries: list[AnalysisExecutedQuery] = Field(
        default_factory=list, max_length=20
    )
    # Kept for v1.0 clients. New clients should prefer executed_queries.
    sql: list[str] = Field(default_factory=list, max_length=20)


class RunClarificationRequest(BaseModel):
    """Machine-readable pause requested by a non-interactive Agent Run."""

    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=1, max_length=2000)
    choices: list[str] = Field(default_factory=list, max_length=4)


STRUCTURED_ANALYSIS_INSTRUCTION = f"""
中台结构化结果要求：
1. 先正常输出给用户阅读的中文分析答案；该可见答案不得包含 SQL 语句、SQL 代码块或“查询明细”章节。
2. 在答案最后追加且只追加一个下述标记块；不要用 Markdown 代码块包裹它。
3. 标记块内必须是严格 JSON，字段必须与示例一致：
{STRUCTURED_ANALYSIS_START}
{{
  "schema_version": "1.2",
  "analysis_type": "comparison",
  "conclusion": "一句话核心结论",
  "data_period": {{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "label": "最近7天", "timezone": "Asia/Shanghai"}},
  "data_scope": {{"period_start": "YYYY-MM-DD", "period_end": "YYYY-MM-DD", "timezone": "Asia/Shanghai", "grain": "商品+自然日", "filters": ["有效支付订单"], "dimensions": ["item_id"], "source_tables": ["报表表名"], "deduplication": "去重方式"}},
  "metrics": [{{"name": "成交金额", "value": 123.45, "unit": "元", "description": "可选说明"}}],
  "metric_definitions": [{{"name": "成交金额", "formula": "SUM(pay_amt)", "unit": "元", "aggregation": "求和", "numerator": null, "denominator": null, "grain": "商品+自然日"}}],
  "comparisons": [{{"metric": "成交金额", "current_value": 123.45, "baseline_value": 100, "absolute_change": 23.45, "relative_change": 0.2345, "unit": "元", "baseline_label": "等长上一周期", "favorability": "有利"}}],
  "trends": [{{"period": "2026-08-01", "metric": "成交金额", "current_value": 123.45, "baseline_value": 100, "absolute_change": 23.45, "relative_change": 0.2345, "unit": "元", "anomaly": "未见明显异常"}}],
  "insights": [{{"statement": "成交金额增加23.45元", "evidence": "当前123.45元、基准100元", "metric_refs": ["成交金额"], "confidence": 1.0}}],
  "drivers": [{{"statement": "商品A贡献了主要增量", "evidence": "成交额增加20元", "metric_refs": ["成交金额"], "contribution": 0.85, "confidence": 0.95}}],
  "anomalies": [{{"statement": "商品B退款率显著偏高", "severity": "高", "metric_refs": ["退款率"], "observed_value": 0.2, "baseline_value": 0.08}}],
  "recommendations": [{{"action": "优先复盘商品B", "rationale": "退款率高于基准", "priority": "高", "metric_refs": ["退款率"]}}],
  "assumptions": ["分析口径或数据限制"],
  "limitations": ["缺少退款原因字段，不能判断因果"],
  "data_quality": {{"query_count": 1, "returned_row_count": 10, "truncated": false, "empty_result": false, "validation_passed": true, "freshness": {{"field": "data_freshness", "latest": "YYYY-MM-DD", "lag_days": 1}}, "period_coverage": {{"requested_start": "YYYY-MM-DD", "requested_end": "YYYY-MM-DD", "observed_start": "YYYY-MM-DD", "observed_end": "YYYY-MM-DD", "covered_days": 7, "expected_days": 7, "coverage_ratio": 1.0, "complete": true}}, "null_counts": {{}}, "zero_denominators": {{}}, "small_samples": {{"threshold": 30, "columns": {{}}}}, "dimensions": {{"required": ["item_id"], "present": ["item_id"], "missing": []}}, "warnings": [], "notes": []}},
  "executed_queries": [{{"sql": "本轮实际执行的只读 SQL", "validation_passed": true, "returned_row_count": 10, "truncated": false, "elapsed_ms": 20}}],
  "sql": ["与 executed_queries 相同的 SQL，保留用于兼容 v1.0 客户端"]
}}
{STRUCTURED_ANALYSIS_END}
4. JSON 键必须使用示例中的 canonical 英文键名，不得把“成交金额”“日期”等中文标签当作对象键。
   除 SQL、表名、原始字段名、时区标识和商品/品牌原文等技术或业务标识外，所有会在报告中展示的
   名称、标签、结论、证据、异常说明和状态都必须使用简体中文；影响方向使用“有利/不利/未判定”，
   优先级和严重程度使用“高/中/低”，真假状态使用“是/否”；不得输出 F/U、FAVORABLE/UNFAVORABLE、
   PASS/WARNING/FAIL 或 high/medium/low 等英文可见状态。
5. insights 只能写数据事实，并必须提供 evidence、metric_refs 和 confidence；drivers 中的推断必须有
   evidence 和 confidence；recommendations 必须通过 metric_refs 绑定本轮指标或异常证据，不得输出
   无数据依据的泛化建议。
6. 只能写入实际查询结果中存在的指标和本轮实际执行过的 SQL，不得推测、补齐或伪造；SQL 仅写入
   标记块内的 executed_queries 和 sql 字段，不能出现在标记块外的用户可见答案中。
7. data_scope 和 metric_definitions 必须依据实际 SQL、表结构和已确认口径填写；data_quality 必须原样
   依据工具返回结果填写；executed_queries 必须依据 row_count、truncated、elapsed_ms 和校验状态填写。
   无法确定的字段使用 null，不得猜测。
8. 无法确定的时间范围用 null；无数据的列表用 []。简单查询的 comparisons、trends、drivers、anomalies、
   recommendations 可以为空，不要为了填满结构而制造内容。
9. analysis_type 必须遵守本轮 `<analysis_output_contract>`：comparison 在有数据且具备可靠基准时至少
   填写 comparisons 和带证据的 insights；diagnosis 在此基础上还应填写带证据的 drivers 或
   anomalies，以及通过 metric_refs 绑定证据的 recommendations。若数据不足，必须写入 limitations，
   不得制造字段内容来满足数量要求。
""".strip()


RUN_CLARIFICATION_INSTRUCTION = f"""
持久化 Run 澄清协议（优先于上述通用 clarify 规则和结构化结果要求）：
1. 当前请求是非流式、可持久化恢复的 Agent Run；不要调用 clarify 工具。
2. 在调用任何数据库工具之前，先判断是否存在会实质改变 SQL、指标口径、数据粒度或结论的歧义。
3. 若存在这种歧义，本轮不得调用任何数据库、导出或其他工具；最终输出只包含一个下述标记块，不要输出普通回答，也不要追加 {STRUCTURED_ANALYSIS_START} 标记块：
{RUN_CLARIFICATION_START}
{{"question":"一个简短、必须由用户决定的问题","choices":["候选项1","候选项2"]}}
{RUN_CLARIFICATION_END}
4. choices 可以为空数组；有明确候选项时最多提供 4 个。每轮最多请求一个澄清。
5. 若当前用户消息是对上一轮澄清问题的回答，将它视为已确认口径，继续原始分析；不要重复询问已回答的同一问题。
6. 不存在实质性歧义时，忽略本协议的标记块，正常执行查询并输出结构化结果。
""".strip()


def _remove_marker_block(content: str) -> str:
    start = content.rfind(STRUCTURED_ANALYSIS_START)
    if start < 0:
        return content.strip()
    end = content.find(STRUCTURED_ANALYSIS_END, start)
    if end < 0:
        return content[:start].rstrip()
    end += len(STRUCTURED_ANALYSIS_END)
    return (content[:start] + content[end:]).strip()


def strip_structured_analysis_block(content: str) -> str:
    return _remove_marker_block(content) if isinstance(content, str) else ""


def parse_structured_analysis(content: str) -> tuple[str, StructuredAnalysis]:
    """Extract the private model envelope and always return a valid contract."""

    raw_content = content if isinstance(content, str) else ""
    clean_content = _remove_marker_block(raw_content)
    start = raw_content.rfind(STRUCTURED_ANALYSIS_START)
    end = raw_content.find(STRUCTURED_ANALYSIS_END, start) if start >= 0 else -1
    if start >= 0 and end >= 0:
        raw_json = raw_content[start + len(STRUCTURED_ANALYSIS_START) : end].strip()
        try:
            payload: Any = json.loads(raw_json)
            if isinstance(payload, dict) and isinstance(payload.get("analysis"), dict):
                payload = payload["analysis"]
            analysis = StructuredAnalysis.model_validate(payload)
            analysis.structured = True
            return clean_content, analysis
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
            pass

    return clean_content, StructuredAnalysis(
        structured=False,
        conclusion=clean_content[:20_000],
    )


def parse_run_clarification(content: str) -> RunClarificationRequest | None:
    """Extract and validate the durable Run clarification envelope."""

    raw_content = content if isinstance(content, str) else ""
    start = raw_content.rfind(RUN_CLARIFICATION_START)
    end = raw_content.find(RUN_CLARIFICATION_END, start) if start >= 0 else -1
    if start < 0 or end < 0:
        return None
    raw_json = raw_content[start + len(RUN_CLARIFICATION_START) : end].strip()
    try:
        payload: Any = json.loads(raw_json)
        if isinstance(payload, dict) and isinstance(payload.get("clarification"), dict):
            payload = payload["clarification"]
        if not isinstance(payload, dict):
            return None
        question = str(payload.get("question") or "").strip()
        raw_choices = payload.get("choices")
        if raw_choices is None:
            choices: list[str] = []
        elif isinstance(raw_choices, list):
            choices = [
                str(choice).strip()
                for choice in raw_choices
                if isinstance(choice, str) and choice.strip()
            ][:4]
        else:
            return None
        return RunClarificationRequest(question=question, choices=choices)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
        return None
