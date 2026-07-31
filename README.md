# xpd-report-agent

`xpd-report-agent` 验证 Hermes Agent 通过自定义 `db-query` 插件查询 SQLite 电商
数据库的完整链路，并提供 FastAPI Wrapper 和静态聊天页面。

数据库问题遵循以下工具链路：

```text
db_get_schema_ddl
-> db_schema_search
-> db_get_table_profile
-> db_get_join_paths
-> db_validate_sql
-> db_execute_sql
```

## 开发环境

项目要求 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run pytest
uv run ruff check .
```

创建并检查示例数据库：

```bash
uv run python scripts/create_demo_db.py
uv run python scripts/inspect_db.py
```

## 配置

```bash
cp configs/local.env.example configs/local.env
```

编辑 `configs/local.env`，至少设置 `HERMES_LLM_API_KEY`。进程环境变量优先于配置
文件；根目录 `.env` 仅作为历史项目兼容入口。

`HERMES_GATEWAY_API_KEY` 是本地 Hermes Gateway 的 bearer token，
`HERMES_LLM_API_KEY` 是模型供应商密钥，两者用途不同。真实密钥不得提交。

## 启动

先准备 Hermes 插件、Skill、依赖和配置：

```bash
scripts/services/hermes.sh prepare
```

统一管理 Hermes Gateway 和 FastAPI：

```bash
scripts/launch.sh start
scripts/launch.sh status
scripts/launch.sh restart
scripts/launch.sh stop
```

也可以只管理单个服务：

```bash
scripts/launch.sh start hermes
scripts/launch.sh start fastapi
```

启动完成后打开 `http://127.0.0.1:8000/`。

## 目录

- `src/xpd_report_agent/`：应用、Hermes 插件和运行时代码。
- `configs/`：可提交的配置模板；`local.env` 仅供本地使用。
- `scripts/`：数据库工具和服务管理入口。
- `skills/`：Hermes Skill。
- `tests/`：自动化测试。
- `docs/prds/`：产品需求文档。
- `docs/plans/`：设计和实施计划。
- `docs/archs/`：系统架构和代码说明。
