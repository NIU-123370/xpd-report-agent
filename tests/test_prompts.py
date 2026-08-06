from __future__ import annotations

from xpd_report_agent.api.main import SYSTEM_PROMPT
from xpd_report_agent.api.prompts import CHINESE_REASONING_REMINDER
from xpd_report_agent.api.sessions import report_system_prompt


def test_wrapper_prompt_owns_global_contract_and_delegates_database_workflow():
    assert "db-multitable-query Skill" in SYSTEM_PROMPT
    assert "已有查询结果转换为文件不属于数据库 Skill" in SYSTEM_PROMPT
    assert "execute_code" in SYSTEM_PROMPT
    assert "terminal" in SYSTEM_PROMPT
    assert "browser" in SYSTEM_PROMPT
    assert "export_report_file" in SYSTEM_PROMPT
    assert "reasoning、reasoning_content、thinking" in SYSTEM_PROMPT
    assert "全部使用简体中文" in SYSTEM_PROMPT
    assert "长期记忆" in SYSTEM_PROMPT
    assert "最终回答规则" in SYSTEM_PROMPT
    assert "严格区分“数据事实”“分析推断”“建议动作”" in SYSTEM_PROMPT
    assert "用户可见的最终答案中不得输出 SQL" in SYSTEM_PROMPT
    assert "导出文件的“查询审计”工作表" in SYSTEM_PROMPT
    assert "简单查询、对比分析、诊断分析" in SYSTEM_PROMPT
    assert "确认前不得查询或导出" in SYSTEM_PROMPT
    assert CHINESE_REASONING_REMINDER in SYSTEM_PROMPT


def test_each_session_turn_ends_with_chinese_reasoning_requirement():
    prompt = report_system_prompt(user_message="查询最近七天成交金额")

    assert prompt.endswith(CHINESE_REASONING_REMINDER)
    assert "从 reasoning、reasoning_content 或 thinking 的第一个词开始" in prompt
    assert "禁止先生成英文分析再翻译" in prompt

    detailed_workflow_terms = (
        "db_get_schema_ddl",
        "db_schema_search",
        "db_get_table_profile",
        "db_get_join_paths",
        "db_validate_sql",
        "db_execute_sql",
        "capture_for_export",
    )
    assert all(term not in SYSTEM_PROMPT for term in detailed_workflow_terms)


def test_skill_owns_database_decisions_and_uses_targeted_schema_discovery():
    with open("skills/db-multitable-query/SKILL.md", encoding="utf-8") as handle:
        skill = handle.read()

    ordered_tools = (
        "`db_schema_search`",
        "`db_get_table_profile`",
        "`db_get_join_paths`",
        "`db_validate_sql`",
        "`db_execute_sql`",
    )
    positions = [skill.index(tool) for tool in ordered_tools]
    assert positions == sorted(positions)
    assert "`include_samples=false`" in skill
    assert "`include_samples=true`" in skill
    assert "`db_get_schema_ddl` 只用于诊断和检索失败后的后备" in skill
    assert "普通查询不要调用它" in skill
    assert skill.index("`db_get_schema_ddl`") > skill.index("`db_execute_sql`")

    assert "每轮最多询问一个关键问题" in skill
    assert "用户确认前不得调用数据库或导出工具" in skill
    assert "低风险歧义采用合理默认值" in skill
    assert "export_report_file" not in skill
    assert "result_id" not in skill
    assert "仅把已有结果转换为文件时不要使用" in skill
    assert "全部使用简体中文" not in skill
