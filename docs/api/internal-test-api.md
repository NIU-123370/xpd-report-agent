# 直播数据分析 Agent 内部测试接口说明

> 适用对象：Agent 开发、测试和运维人员  
> 禁止作为中台正式接入文档

中台正式接口请阅读 [middle-platform-agent-api.md](./middle-platform-agent-api.md)。本文记录服务内部、网页调试和运维测试接口，它们不承诺对中台保持兼容。

## 1. 测试原则

- 内部接口只允许在开发环境或受控内网调用。
- `/api/*` 在启用服务鉴权后需要 `Authorization: Bearer <service-key>`。
- 生产身份模式使用 `X-User-Id`；本地测试可根据配置使用 `X-XPD-Session-Key`。
- 测试数据与正式用户数据必须隔离。
- 不得把服务密钥、数据库密码或 OSS 密钥写入测试报告和代码仓库。

## 2. 运维检查

### 存活检查

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
| `POST /api/sessions/{session_id}/chat/stream` | 网页流式对话 |
| `POST /api/sessions/{session_id}/close` | 关闭会话 |

这些接口会随网页和 Hermes 会话实现调整，测试代码不得把它们当成稳定的中台协议。

## 4. 分析与调试接口

| 方法与路径 | 用途 |
|---|---|
| `GET /api/analysis-presets` | 查询网页分析预设 |
| `POST /api/sessions/{session_id}/analyses` | 网页分析入口 |
| `GET /api/memories` | 查询 Agent 记忆 |
| `GET /api/reflections/{reflection_id}` | 查询反思任务 |
| `POST /api/reflections/{reflection_id}/retry` | 重试反思任务 |

## 5. 定时任务接口

| 方法与路径 | 用途 |
|---|---|
| `GET /api/schedules` | 查询定时任务 |
| `POST /api/schedules` | 创建定时任务 |
| `PUT /api/schedules/{schedule_id}` | 修改定时任务 |
| `POST /api/schedules/{schedule_id}/pause` | 暂停 |
| `POST /api/schedules/{schedule_id}/resume` | 恢复 |
| `POST /api/schedules/{schedule_id}/run` | 立即执行 |
| `DELETE /api/schedules/{schedule_id}` | 删除 |

定时任务会创建内部会话和 Agent run。测试时应使用专用用户范围，避免污染正常分析历史。

## 6. 废弃接口

以下接口只用于旧版兼容，不应开发新调用方：

```text
POST /api/chat
POST /api/chat/stream
POST /api/sessions/{session_id}/chat
```

## 7. 内部测试重点

- 验证服务鉴权失败统一返回 `401 SERVICE_AUTH_FAILED`。
- 验证创建任务和澄清回答的幂等重放与冲突行为。
- 验证五种 run 状态及服务重启后的任务恢复。
- 验证同一会话串行执行，不交错提交多个未完成任务。
- 验证 MySQL 只读查询、空结果、零分母、小样本和数据不足提示。
- 验证商家版 Excel 内容为中文，趋势、异常与建议、数据明细和口径提示清晰，且不包含 SQL、表名或内部审计字段。
- 验证本地文件下载、OSS `307` 跳转和 JSON 临时地址模式。
- 验证跳转到 OSS 时不会携带 Agent 的鉴权请求头。
- 验证 Hermes 不可用、MySQL 不可用和超时情况下的错误结构。

## 8. 中台接口回归

每次发布仍需回归 5 个中台稳定接口：

```text
POST /api/v1/agent/runs
GET  /api/v1/agent/runs/{run_id}
GET  /api/v1/agent/runs/{run_id}/stream
POST /api/v1/agent/runs/{run_id}/input
GET  /api/sessions/{session_id}/artifacts/{artifact_id}/download
```

自动接口页 `/docs` 和 `/openapi.json` 展示 5 个中台业务接口及 `GET /ready`；内部接口以本文和代码路由为准。
