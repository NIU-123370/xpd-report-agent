# xpd-report-agent 初始化迁移 PRD

> 状态：已完成并归档。本文仅记录项目初始化迁移时的目标和约束，不代表当前生产架构；
> 现行说明请阅读根目录 `README.md` 与 `docs/archs/system-overview.md`。

## 背景

历史项目 `/Users/gjh/claude-code/hermes-sqlite-demo` 已完成 Hermes Agent、SQLite
查询插件、FastAPI Wrapper、静态聊天页和本地进程管理验证。当前仓库需要在保留既有
行为的前提下完成一次性迁移，并建立可维护的工程结构。

## 目标

- 项目和 Python 分发名称统一为 `xpd-report-agent`。
- 服务代码位于 `src/`，配置模板位于 `configs/`，脚本位于 `scripts/`。
- 使用 `uv`、`pyproject.toml` 和 `uv.lock` 管理环境。
- 保持现有 HTTP API、Hermes 六个数据库工具、SQL 只读约束及启动生命周期语义。
- 保留历史产品、设计和实施文档，新增当前架构文档。

## 非目标

- 不在本次迁移中重写数据库查询逻辑。
- 不增加账号、多租户、生产部署、权限系统或新的前端框架。
- 不建立新旧目录之间的持续或双向同步。

## 安全要求

- 不迁移 `.env`、真实 API Key、日志、缓存、IDE 配置和生成的 SQLite 数据库。
- 配置模板中的密钥必须为空或使用明确的本地占位符。
- Hermes 插件继续以只读方式连接 SQLite，并在执行前校验 SQL。

## 验收标准

- 干净环境可执行 `uv sync`。
- 历史项目的 37 个自动化测试迁移后全部通过。
- `uv run ruff check .` 通过。
- 可生成并检查包含 7 张业务表的 SQLite 示例库。
- FastAPI 首页、健康检查、非流式和流式代理接口保持兼容。
- `start|stop|restart|status [all|hermes|fastapi]` 生命周期语义保持兼容。
