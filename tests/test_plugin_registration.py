from __future__ import annotations

import yaml

from xpd_report_agent.hermes_plugin.db_query import register, schemas, tools


class FakeContext:
    def __init__(self):
        self.tools = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)


def test_register_includes_schema_ddl_tool():
    ctx = FakeContext()

    register(ctx)

    names = [item["name"] for item in ctx.tools]
    assert names[0] == "db_get_schema_ddl"
    assert "db_get_schema_ddl" in names

    entry = next(item for item in ctx.tools if item["name"] == "db_get_schema_ddl")
    assert entry["schema"] == schemas.DB_GET_SCHEMA_DDL
    assert entry["handler"] == tools.db_get_schema_ddl
    assert entry["requires_env"] == ["HERMES_DEMO_SQLITE_PATH"]


def test_plugin_manifest_lists_schema_ddl_first():
    with open(
        "src/xpd_report_agent/hermes_plugin/db_query/plugin.yaml",
        encoding="utf-8",
    ) as handle:
        manifest = yaml.safe_load(handle)

    assert manifest["provides_tools"][0] == "db_get_schema_ddl"
    assert "db_get_schema_ddl" in manifest["provides_tools"]
