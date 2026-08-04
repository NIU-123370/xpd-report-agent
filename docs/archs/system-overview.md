# xpd-report-agent 系统架构

## 组件关系

```text
浏览器静态聊天页
  -> FastAPI Wrapper（session-key 校验、API 封装、结束反思队列）
  -> Hermes Gateway（Session、session_search、memory）
      -> db-query Plugin -> MySQL
      -> state.db        -> 会话历史
      -> memories/       -> MEMORY.md / USER.md
```

FastAPI 不直接生成或执行 SQL，也不建立第二套消息数据库。数据库理解、JOIN 路径发现、SQL
校验和执行由 Hermes Plugin 完成；原始会话消息以 Hermes `state.db` 为唯一事实源。

## 会话所有权

一期无账号系统，浏览器生成高熵 `X-XPD-Session-Key`。FastAPI 使用服务端签名密钥计算不可逆
owner scope，并把它编码到服务端生成的 session-id 中。历史、消息和会话操作先校验 scope；
不匹配统一返回 404，避免通过 session-id 探测其他会话。

传给 Hermes 的 `X-Hermes-Session-Key` 是 owner scope，不是浏览器原始键。该方案用于一期
本地/单用户部署；接入账号系统后应以认证 user-id 替代本地键。

## 反思与长期记忆

- `memory.nudge_interval=3` 复用 Hermes 原生后台 memory review。
- 会话关闭时 FastAPI 创建幂等最终反思任务，状态持久化到
  `~/.hermes/xpd-report-agent/reflections.json`，失败最多重试 3 次。
- 最终反思将脱敏后的会话交给临时 Hermes Session，系统提示明确限制其只形成结构化结论，
  并仅通过原生 `memory` 工具写入高置信长期记忆；临时 Session 完成后删除。
- 30 分钟无活动的会话由服务端扫描关闭并触发同一结束反思流程。
- 历史消息响应将 Hermes 的 `reasoning_content` 归一化为单一 `reasoning` 字段；前端与最终
  答案分区、默认折叠展示。流式响应先消费 `_thinking` 预览，再用 `run.completed.messages`
  中的完整思考替换。历史恢复时按用户轮次合并工具调用产生的多个 Assistant 思考片段，每轮
  只渲染一个折叠窗口。按当前产品要求，该字段原样返回，不做脱敏或内容过滤。
- FastAPI 的 `GET /api/memories` 只读返回 `MEMORY.md` 和 `USER.md` 的当前文件快照、容量和
  更新时间；前端“Agent 记忆”页面用于观察，不修改 Hermes 记忆。
- FastAPI 系统提示词与 `db-multitable-query` Skill 双重约束新生成的 reasoning、thinking、
  工具调用说明和最终回答使用简体中文；数据库技术标识保持原文。
- Hermes Session SSE 原生未连接 provider 的 `reasoning_callback`。启动脚本通过项目内兼容补丁
  将 `reasoning_content` 增量桥接为 `_thinking` 事件，使前端能在正式答案前实时展示思考；
  答案开始后的迟到 `_thinking` 副本会被忽略，`run.completed` 仍负责落定完整思考。

## 部署持久化

服务器或容器部署必须持久化完整 Hermes Home，至少包括：

- `state.db`：Session 和消息历史；
- `memories/MEMORY.md`、`memories/USER.md`：跨会话长期记忆；
- `xpd-report-agent/reflections.json`：最终反思任务与审计状态；
- Hermes 配置、插件和 Skill。

一期建议只运行一个 Hermes/FastAPI 实例。多实例部署前需要将 Session、反思队列和长期记忆
迁移到支持并发的一致共享存储，不能让多个实例各自维护不同的本地文件。

## 代码边界

- `src/xpd_report_agent/api/`：HTTP API、系统提示词和静态前端。
- `src/xpd_report_agent/hermes_plugin/db_query/`：独立 Hermes 插件源文件。
- `src/xpd_report_agent/runtime/`：配置归一化、Hermes 配置写入和进程生命周期。
- `scripts/`：面向开发者和运维的薄命令入口。
- `skills/`：安装给 Hermes 的数据库查询 Skill。

## 配置模型

配置优先级从高到低为：

1. 进程环境变量；
2. `configs/local.env`；
3. 兼容历史项目的根目录 `.env`；
4. 代码中的本地开发默认值。

`configs/local.env`、根目录 `.env`、日志和 PID 均不进入版本控制。

依赖安装、插件复制和 Hermes 配置写入只在显式执行 `prepare` 时发生。兼容旧行为的环境
可以将 `HERMES_BOOTSTRAP_ON_START` 设置为 `true`。

## Hermes 插件安装

插件源文件保留 `plugin.yaml` 和相对导入结构。`prepare` 将其复制到
`~/.hermes/plugins/db-query/`，将 Skill 复制到 `~/.hermes/skills/`，并使用 `uv`
向 Hermes Python 环境安装锁定的插件依赖。
