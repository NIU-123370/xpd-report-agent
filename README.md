# xpd-report-agent

`xpd-report-agent` 验证 Hermes Agent 通过自定义 `db-query` 插件查询 MySQL
淘宝直播报表数据库的完整链路，并提供 FastAPI Wrapper 和静态聊天页面。

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

## 配置

```bash
cp configs/local.env.example configs/local.env
```

编辑 `configs/local.env`，设置 `HERMES_LLM_API_KEY` 和 `MYSQL_*` 连接参数。进程环境变量优先于配置
文件；根目录 `.env` 仅作为历史项目兼容入口。

`HERMES_GATEWAY_API_KEY` 是本地 Hermes Gateway 的 bearer token，
`HERMES_LLM_API_KEY` 是模型供应商密钥，两者用途不同。真实密钥不得提交。

会话与记忆默认开启：

- Hermes API Server 暴露 `db_query`、`session_search`、`memory`。
- 浏览器使用本地随机 session-key，FastAPI 只向 Hermes 转发不可逆的作用域标识。
- Hermes `state.db` 是会话消息事实源，前端请求不再重复发送完整历史。
- 每 3 轮由 Hermes 原生后台复盘；结束会话时由持久化任务补做最终复盘。
- `MEMORY.md` 和 `USER.md` 的默认上限分别为 2200 和 1375 字符。

生产环境必须单独设置高熵 `XPD_SESSION_SIGNING_SECRET`，并将整个 Hermes Home
（默认 `~/.hermes`）挂载到持久卷；不要只持久化项目目录。

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

页面侧边栏提供历史记录，可新建、打开、重命名、结束和删除会话。已结束会话只读；刷新页面后
会恢复当前活跃会话及其 Hermes 持久化消息。“Agent 记忆”入口会原样展示当前
`MEMORY.md` 和 `USER.md` 内容、字符容量与最后更新时间，供测试观察，不提供网页编辑。
Assistant 消息中的“模型思考”在生成期间自动展开、连续流式更新并跟随内容滚动到底部，文本
填满当前行后自然换行；用户可随时收起。正式答案开始输出以及整轮结束后，思考窗口和完整内容
仍会保留。历史会话恢复时默认折叠，可点击展开。
思考内容原样展示，不做脱敏或摘要；同一轮中的多个片段会汇总进一个窗口，只展示该轮最后的
正式答案。

项目内 `db-multitable-query` Skill 使用中文编写。系统提示词和 Skill 都要求新生成的模型思考、
工具说明及最终回答使用简体中文；工具名、表名、字段名和 SQL 关键字仍保持原文。已有历史思考
不会被自动翻译。

主要 Session API：

```text
POST   /api/sessions
GET    /api/sessions
GET    /api/sessions/{session_id}/messages
POST   /api/sessions/{session_id}/chat/stream
POST   /api/sessions/{session_id}/close
DELETE /api/sessions/{session_id}
GET    /api/memories
```

客户端调用这些接口必须携带 `X-XPD-Session-Key`；前端会自动生成和保存该键。

## 目录

- `src/xpd_report_agent/`：应用、MySQL-only Hermes 插件和运行时代码。
- `configs/`：可提交的配置模板；`local.env` 仅供本地使用。
- `scripts/`：数据库工具和服务管理入口。
- `skills/`：Hermes Skill。
- `tests/`：自动化测试。
- `docs/prds/`：产品需求文档。
- `docs/plans/`：设计和实施计划。
- `docs/archs/`：系统架构和代码说明。
