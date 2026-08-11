# 直播数据分析 Agent 内部测试接口说明

> 适用对象：Agent 开发、测试和运维人员  
> 禁止作为中台正式接入文档

中台正式接口请阅读 [middle-platform-agent-api.md](./middle-platform-agent-api.md)。本文记录服务内部、网页调试和运维测试接口，它们不承诺对中台保持兼容。

## 1. 测试原则

- 内部接口只允许在开发环境或受控内网调用。
- `/api/*` 在启用服务鉴权后需要 `Authorization: Bearer <service-key>`，
  唯一例外是 `/api/internal/scheduled-reports/{schedule_id}/run`：它不使用 Service Bearer，
  而使用请求体中的专用回调 `token`。
- 生产身份模式使用 `X-User-Id`；本地测试可根据配置使用 `X-XPD-Session-Key`。
- 测试数据与正式用户数据必须隔离。
- 不得把服务密钥、回调 `token`、数据库密码或 OSS 密钥写入测试报告、日志和代码仓库。

## 2. 运维检查

### 进程存活检查

```http
GET /live
```

只检查 FastAPI 进程是否能响应，不访问 Hermes、MySQL 或其他下游。进程存活时返回 `200`。

### 详细健康诊断

```http
GET /health
```

进程可响应时通常返回 `200`。测试仍需读取响应体中的 `ok` 和各项检查结果。

### 就绪检查

```http
GET /ready
```

Hermes runtime 和真实 MySQL `SELECT 1` 均成功时返回 `200`，否则返回 `503`。
该接口只证明数据库连接可用，不证明业务账号能够读取目标业务表。
该接口同时提供给中台网关、SLB 或发布系统作为服务就绪探针。

### Prometheus 指标

```http
GET /metrics
```

返回 Agent 并发容量、等待数及 Hermes 实例健康数等 Prometheus 文本指标。
该路径不在 `/api/*` 下，不受 Service Bearer 中间件保护；只能在集群内网、防火墙或鉴权代理后开放。

## 3. 网页会话接口

以下接口供内置网页和内部会话测试使用，不提供给中台：

| 方法与路径 | 用途 |
|---|---|
| `POST /api/sessions` | 创建网页会话 |
| `GET /api/sessions` | 查询会话列表 |
| `GET /api/sessions/{session_id}` | 查询会话详情 |
| `PATCH /api/sessions/{session_id}` | 修改会话信息 |
| `DELETE /api/sessions/{session_id}` | 删除会话 |
| `GET /api/sessions/{session_id}/messages` | 查询消息 |
| `GET /api/sessions/{session_id}/artifacts` | 查询生成文件 |
| `GET /api/sessions/{session_id}/artifacts/{artifact_id}/download` | 下载生成文件；该路径同时是第 9 节中台稳定接口之一 |
| `POST /api/sessions/{session_id}/chat/stream` | 网页流式对话 |
| `POST /api/sessions/{session_id}/clarifications/{clarification_id}/answer` | 回答网页流式对话中的澄清问题 |
| `POST /api/sessions/{session_id}/close` | 关闭会话 |

这些接口会随网页和 Hermes 会话实现调整，测试代码不得把它们当成稳定的中台协议。
内置网页入口是 `GET /`，静态资源位于 `/static/*`。

## 4. 低层 Agent Run 接口

以下是内部会话级 Run 入口，不是中台 v1 稳定协议：

| 方法与路径 | 用途 |
|---|---|
| `POST /api/sessions/{session_id}/runs` | 在已存在的会话上创建持久化 Run |
| `GET /api/runs/{run_id}` | 查询内部 Run 状态 |
| `POST /api/runs/{run_id}/input` | 提交内部 Run 的澄清回答 |

这些接口同样受用户范围隔离。创建 Run 和提交澄清答案都必须使用
`Idempotency-Key`，不得与另一个业务操作共用。

## 5. 分析与调试接口

| 方法与路径 | 用途 |
|---|---|
| `GET /api/analysis-presets` | 查询网页分析预设 |
| `POST /api/sessions/{session_id}/analyses` | 网页分析入口 |
| `GET /api/memories` | 查询 Agent 记忆 |
| `GET /api/reflections/{reflection_id}` | 查询反思任务 |
| `POST /api/reflections/{reflection_id}/retry` | 重试反思任务 |

## 6. 定时任务接口

| 方法与路径 | 用途 |
|---|---|
| `GET /api/schedules` | 查询定时任务 |
| `POST /api/schedules` | 创建定时任务 |
| `PUT /api/schedules/{schedule_id}` | 修改定时任务 |
| `POST /api/schedules/{schedule_id}/pause` | 暂停 |
| `POST /api/schedules/{schedule_id}/resume` | 恢复 |
| `POST /api/schedules/{schedule_id}/run` | 立即执行 |
| `DELETE /api/schedules/{schedule_id}` | 删除 |
| `POST /api/internal/scheduled-reports/{schedule_id}/run` | Hermes Cron 内部回调，请求体使用专用 `token` |

定时任务会创建内部会话和 Agent run。测试时应使用专用用户范围，避免污染正常分析历史。
内部回调不使用 Service Bearer，且在 OpenAPI 中隐藏；请求体为
`{"token":"<schedule-callback-token>"}`。非法 `schedule_id` 或已存在任务的错误 token 返回
`404`；任务已删除、缺失或停用时返回 `202`，但 `accepted=false`，调用方不得把 HTTP `202`
直接等同于已创建报告任务。

当前统一 `422` 校验错误的 `detail.errors[].input` 可能回显长度或格式不合法的请求值。回调调用方必须
始终发送合规 token，网关和日志系统不得记录该接口的请求体或 `422` 响应体；在错误契约完成敏感输入
脱敏前，该路由只能位于受控容器网络，不能通过网关或 Ingress 暴露。

## 7. 废弃接口

以下接口只用于旧版兼容，不应开发新调用方：

```text
POST /api/chat
POST /api/chat/stream
POST /api/sessions/{session_id}/chat
```

## 8. 内部测试重点

- 验证缺失或错误的 Service Bearer 统一返回 `401 SERVICE_AUTH_FAILED`；
  启用鉴权但未配置服务密钥时返回 `503 SERVICE_AUTH_MISCONFIGURED`。
- 验证定时报告内部回调不依赖 Service Bearer，正确 token 可接受，错误 token 返回 `404`；
  同时验证代理和应用访问日志不记录请求体。`422` 敏感输入脱敏完成前，不得断言所有错误响应都不会回显 token。
- 验证创建任务和澄清回答的幂等重放与冲突行为。
- 验证五种 run 状态及服务重启后的任务恢复。
- 验证 SSE 约 15 秒心跳、六类完整事件、`Last-Event-ID` 断线续传和缓存丢失后的权威终态。
- 验证同一会话串行执行，不交错提交多个未完成任务。
- 验证 MySQL 只读查询、空结果、零分母、小样本和数据不足提示。
- 验证商家版 Excel 的工作表契约：简单查询为“经营摘要、数据明细、口径与提示”；
  对比分析增加“趋势与对比”；诊断分析再增加“异常与建议”。
- 验证趋势横轴按可解析日期升序排列，小日期在左、大日期在右；所有图表锚点必须位于
  可见表格之后，不得覆盖表头或数据区。
- 验证百分比字段兼容原始小数（如 `0.3513`）和百分数（如 `35.13`）两种输入，显示结果不得放大
  或缩小 100 倍；商家版 Excel 不得包含 SQL、表名、原始字段名或内部查询审计。
- 验证本地文件下载、OSS `307` 跳转和 JSON 临时地址模式。
- 验证跳转到 OSS 时不会携带 Agent 的鉴权请求头。
- 验证 Hermes 不可用、MySQL 不可用和超时情况下的错误结构。

## 9. 中台接口回归

每次发布仍需回归 5 个中台稳定接口：

```text
POST /api/v1/agent/runs
GET  /api/v1/agent/runs/{run_id}
GET  /api/v1/agent/runs/{run_id}/stream
POST /api/v1/agent/runs/{run_id}/input
GET  /api/sessions/{session_id}/artifacts/{artifact_id}/download
```

自动接口页 `/docs` 和 `/openapi.json` 的 `paths` 只展示 5 个中台业务接口及 `GET /ready`。
OpenAPI 是先根据完整 FastAPI 应用生成再过滤 `paths`，因此 `components.schemas` 可能保留
不可从中台调用的内部模型。单独出现在 `components` 中不等于路由对外开放；
内部接口以本文和代码路由为准。
