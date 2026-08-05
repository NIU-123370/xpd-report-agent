from __future__ import annotations

import yaml

from xpd_report_agent.hermes_plugin.db_query import (
    MYSQL_REQUIRED_ENV,
    register,
    schemas,
    tools,
)


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
    assert "export_report_file" in names

    export_entry = next(item for item in ctx.tools if item["name"] == "export_report_file")
    assert export_entry["toolset"] == "report_file"
    assert export_entry["requires_env"] == ["XPD_FILE_STORAGE_PATH"]

    entry = next(item for item in ctx.tools if item["name"] == "db_get_schema_ddl")
    assert entry["schema"] == schemas.DB_GET_SCHEMA_DDL
    assert entry["handler"] == tools.db_get_schema_ddl
    assert entry["requires_env"] == MYSQL_REQUIRED_ENV


def test_plugin_manifest_lists_schema_ddl_first():
    with open(
        "src/xpd_report_agent/hermes_plugin/db_query/plugin.yaml",
        encoding="utf-8",
    ) as handle:
        manifest = yaml.safe_load(handle)

    assert manifest["provides_tools"][0] == "db_get_schema_ddl"
    assert "db_get_schema_ddl" in manifest["provides_tools"]
    assert "export_report_file" in manifest["provides_tools"]
