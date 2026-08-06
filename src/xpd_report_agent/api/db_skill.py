from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_SKILL_PATH = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "db-multitable-query"
    / "SKILL.md"
)
_MAX_SKILL_CHARS = 16_000

_EXPORT_SIGNAL = re.compile(
    r"(?:导出|下载|保存为|转成|生成(?:文件|报表|报告)?|"
    r"\bxlsx\b|\bexcel\b|\bcsv\b|\bpdf\b|\bjson\b|markdown|电子表格|工作簿)",
    re.IGNORECASE,
)
_NEGATED_EXPORT = re.compile(
    r"(?:不要|不用|无需|别|禁止|取消).{0,10}"
    r"(?:导出|下载|生成|xlsx|excel|csv|pdf|json|文件)",
    re.IGNORECASE,
)
_DATABASE_QUERY_SIGNAL = re.compile(
    r"(?:数据库|SQL|查询|统计|分析|计算|排行|排名|趋势|对比|多少|哪些|找出|"
    r"最近|过去|本周|上周|本月|上月|今天|昨天|日期|时间|"
    r"直播|场次|商品|订单|销售|成交|支付|退款|曝光|点击|加购|客户|买家|"
    r"粉丝|互动|打赏|转化|GMV|pay_|refund_|item_|live_)",
    re.IGNORECASE,
)

_SIMPLE_ANALYSIS_SIGNAL = re.compile(
    r"(?:简单查询|数据查询|直接答案|仅?导出明细|只要明细|原始数据|明细数据|"
    r"(?:多少|合计|总计|排行|排名|前\s*\d+|top\s*\d+))",
    re.IGNORECASE,
)
_COMPARISON_ANALYSIS_SIGNAL = re.compile(
    r"(?:对比(?:分析)?|比较(?:分析)?|环比|同比|较上|上一周期|趋势|变化(?:率|情况)?)",
    re.IGNORECASE,
)
_DIAGNOSTIC_ANALYSIS_SIGNAL = re.compile(
    r"(?:诊断分析|异常诊断|为什么|原因|归因|驱动因素|异常|问题定位|优化建议|建议动作)",
    re.IGNORECASE,
)


def requested_analysis_type(user_message: str | None) -> str | None:
    """Infer an explicitly requested report depth; return None when it is ambiguous."""

    message = str(user_message or "").strip()
    if not message:
        return None
    if _DIAGNOSTIC_ANALYSIS_SIGNAL.search(message):
        return "diagnostic"
    if _COMPARISON_ANALYSIS_SIGNAL.search(message):
        return "comparison"
    if _SIMPLE_ANALYSIS_SIGNAL.search(message):
        return "simple"
    return None


def _strip_frontmatter(content: str) -> str:
    if not content.startswith("---"):
        return content.strip()
    end = content.find("\n---", 3)
    if end < 0:
        return content.strip()
    return content[end + 4 :].strip()


@lru_cache(maxsize=1)
def db_skill_body() -> str:
    """Load the one application-owned DB Skill without opening Hermes' skill catalog."""

    content = _SKILL_PATH.read_text(encoding="utf-8")
    if len(content) > _MAX_SKILL_CHARS:
        raise RuntimeError("db-multitable-query Skill exceeds the prompt size limit.")
    return _strip_frontmatter(content)


def is_export_only_request(user_message: str | None) -> bool:
    """Return true for format conversion of an existing result, not a new query."""

    message = str(user_message or "").strip()
    if not message or _NEGATED_EXPORT.search(message):
        return False
    return bool(_EXPORT_SIGNAL.search(message)) and not bool(
        _DATABASE_QUERY_SIGNAL.search(message)
    )


def is_database_request(user_message: str | None) -> bool:
    message = str(user_message or "").strip()
    return bool(message) and not is_export_only_request(message) and bool(
        _DATABASE_QUERY_SIGNAL.search(message)
    )


def db_skill_prompt(user_message: str | None) -> str:
    """Inject DB reasoning only when this turn actually requests data work."""

    message = str(user_message or "").strip()
    if not is_database_request(message):
        return ""
    return (
        "<db_query_skill>\n"
        "以下是本应用唯一允许使用的数据库领域 Skill；仅用于本轮数据库查询与分析。\n"
        f"{db_skill_body()}\n"
        "</db_query_skill>"
    )


def export_action_prompt(user_message: str | None) -> str:
    """Describe a pure export transport action without loading database reasoning."""

    if not is_export_only_request(user_message):
        return ""
    return (
        "当前请求只是把当前会话最近一次查询结果转换为文件，不是新的数据库查询。"
        "导出前先从本轮消息和会话中最近一次分析判断报告类型：简单查询、对比分析或诊断分析。"
        "如果最近一次分析已明确属于其中一种，直接沿用，不得重复询问；如果上下文仍无法确定，"
        "必须先向用户澄清，并提供且只提供这三个候选项。用户确认前不得调用任何查询或导出工具。"
        "导出 XLSX 时必须把已确认类型写入 analysis_type，并把已有且可验证的 metrics、comparisons、"
        "trends、drivers、recommendations、metric_definitions、data_scope、data_quality 写入 analysis；"
        "analysis 内的键必须使用下列 canonical 英文 JSON 键："
        "comparisons 每项使用 metric、current_value、baseline_value、absolute_change、"
        "relative_change、unit、baseline_label、favorability；"
        "trends 每项使用 period、metric、current_value、baseline_value、absolute_change、"
        "relative_change、unit、anomaly；"
        "drivers 每项使用 statement、evidence、metric_refs、contribution、confidence、"
        "dimension、member、metric、current_value、contribution_value、contribution_rate；"
        "metric_definitions 每项使用 name、formula、unit、aggregation、numerator、denominator、grain。"
        "不得把 date、session 或“退货件数”等非 canonical 键当作上述对象字段。"
        "JSON 键保持上述英文，但所有会在工作簿中展示的指标名、维度名、标签、结论、证据、"
        "异常说明、建议和状态值必须使用简体中文：favorability 只用“有利”“不利”"
        "或“未判定”，优先级/严重程度使用“高”“中”“低”，真假状态使用“是”“否”，不得使用 "
        "F/U、FAVORABLE/UNFAVORABLE、PASS/WARNING/FAIL 或 high/medium/low 作为可见值。"
        "SQL、数据表名、原始字段名、时区标识以及商品和品牌原文保留原样。"
        "缺少的数据留空，不得为了填充工作表而编造。"
        "不要调用 db_schema_search、db_get_table_profile、db_get_join_paths、"
        "db_validate_sql 或 db_execute_sql；直接使用会话历史中最近一个未过期的 result_id "
        "调用 export_report_file。若没有可用 result_id，明确说明结果已不存在并请用户重新查询，"
        "不得自行重建数据。"
    )
