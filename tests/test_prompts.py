from __future__ import annotations

from xpd_report_agent.api.main import SYSTEM_PROMPT


def test_wrapper_prompt_requires_schema_ddl_and_blocks_builtin_tools():
    assert "db_get_schema_ddl" in SYSTEM_PROMPT
    assert "第一个工具调用必须是 db_get_schema_ddl" in SYSTEM_PROMPT
    assert "execute_code" in SYSTEM_PROMPT
    assert "terminal" in SYSTEM_PROMPT
    assert "browser" in SYSTEM_PROMPT
    assert "reasoning、reasoning_content、thinking" in SYSTEM_PROMPT
    assert "全部使用简体中文" in SYSTEM_PROMPT


def test_skill_requires_schema_ddl_and_blocks_builtin_tools():
    with open("skills/db-multitable-query/SKILL.md", encoding="utf-8") as handle:
        skill = handle.read()

    assert "db_get_schema_ddl" in skill
    assert "第一次工具调用必须是 `db_get_schema_ddl`" in skill
    assert "execute_code" in skill
    assert "terminal" in skill
    assert "browser" in skill
    assert "模型思考过程" in skill
    assert "全部使用简体中文" in skill
    assert "# DB Multi-table Query Skill" not in skill
