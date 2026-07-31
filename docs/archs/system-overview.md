# xpd-report-agent 系统架构

## 组件关系

```text
浏览器静态聊天页
  -> FastAPI Wrapper
  -> Hermes Gateway
  -> db-query Plugin
  -> SQLite
```

FastAPI 只代理 OpenAI 兼容的 Hermes 接口，不直接生成或执行 SQL。数据库理解、JOIN
路径发现、SQL 校验和执行由 Hermes Plugin 完成。

## 代码边界

- `src/xpd_report_agent/api/`：HTTP API、系统提示词和静态前端。
- `src/xpd_report_agent/hermes_plugin/db_query/`：独立 Hermes 插件源文件。
- `src/xpd_report_agent/runtime/`：配置归一化、Hermes 配置写入和进程生命周期。
- `src/xpd_report_agent/demo/`：示例数据库生成和检查逻辑。
- `scripts/`：面向开发者和运维的薄命令入口。
- `skills/`：安装给 Hermes 的数据库查询 Skill。

## 配置模型

配置优先级从高到低为：

1. 进程环境变量；
2. `configs/local.env`；
3. 兼容历史项目的根目录 `.env`；
4. 代码中的本地开发默认值。

`configs/local.env`、根目录 `.env`、日志、PID 和生成数据库均不进入版本控制。

依赖安装、插件复制和 Hermes 配置写入只在显式执行 `prepare` 时发生。兼容旧行为的环境
可以将 `HERMES_BOOTSTRAP_ON_START` 设置为 `true`。

## Hermes 插件安装

插件源文件保留 `plugin.yaml` 和相对导入结构。`prepare` 将其复制到
`~/.hermes/plugins/db-query/`，将 Skill 复制到 `~/.hermes/skills/`，并使用 `uv`
向 Hermes Python 环境安装锁定的插件依赖。
