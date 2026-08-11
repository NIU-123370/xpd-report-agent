# 直播数据分析 Agent 中台接口文档

> 接口版本：v1<br>
> 字符编码：UTF-8<br>
> 适用对象：中台后端开发人员

中台需要接入 5 个业务接口，并可使用 1 个服务就绪检查接口。其他接口均为 Agent
内部接口，不应调用。

## 1. 接口地址

生产环境地址由运维提供，本文统一记为：

```text
{BASE_URL}
```

例如：`http://agent-api.internal.example.com:8000`。

## 2. 公共请求头

| 请求头 | 是否必填 | 说明 |
|---|---|---|
| `Authorization` | 是 | `Bearer <service-key>`，密钥只能保存在中台后端 |
| `X-User-Id` | 是 | 中台内部稳定用户 ID，1～128 个字符，不得包含空白或控制字符；不要使用手机号或姓名 |
| `Idempotency-Key` | POST 必填 | 不超过 255 个可见 ASCII 字符；推荐使用 UUID；每个新业务操作使用新值，HTTP 重试必须复用原值 |
| `X-Request-Id` | 否 | 链路追踪 ID，最多 128 个字符，第一个字符必须是字母或数字，其余位置只允许字母、数字、`.`、`_`、`:`、`-`；无效或缺失时服务端会生成新值 |

服务端会在每个 HTTP 响应的 `X-Request-Id` 响应头中返回本次请求的跟踪 ID。
`Authorization`、`X-User-Id` 和业务数据不得写入前端日志、埋点或错误页。

### 2.1 请求字段校验

| 字段 | 限制 |
|---|---|
| `message` | 1～100000 个字符 |
| `session_id` | 可省略或传 `null`；非空时最多 200 个字符，且必须属于当前 `X-User-Id` |
| `title` | 可省略或传 `null`；非空时最多 120 个字符 |
| `answer` | 1～2000 个字符；去除首尾空白后不得为空 |
| `Last-Event-ID` | 可省略；非负十进制整数，不得大于 `2^53-1` |

### 2.2 统一错误结构与安全重试

业务接口的非 `2xx` HTTP 响应使用统一结构：

```json
{
  "ok": false,
  "error": {
    "code": "AGENT_RUN_STATE_UNAVAILABLE",
    "message": "Agent run state is unavailable.",
    "retryable": false,
    "outcome_unknown": true,
    "request_id": "req_xxx"
  },
  "detail": {
    "code": "AGENT_RUN_STATE_UNAVAILABLE",
    "message": "Agent run state is unavailable.",
    "retryable": false,
    "outcome_unknown": true
  }
}
```

- 中台只根据 `error.code`、`error.retryable` 和 `error.outcome_unknown` 做程序分支；
  `message` 用于人工排查，`detail` 形状不稳定，不得作为协议依赖。
- HTTP 超时、断网或 `error.retryable=true` 且 `outcome_unknown=false` 时，可以指数退避重试，
  但必须复用原请求与原 `Idempotency-Key`。
- `outcome_unknown=true` 表示上游可能已产生副作用；不得生成新幂等键自动重提。
  已取得 `run_id` 时先查询原任务；未取得时使用 `request_id` 和原幂等键进行人工对账。
- `401`、`409`、`422` 通常需要先修正凭证、冲突或请求内容，不得无条件自动重试。
- HTTP `200` 不代表 Agent 任务成功。`run.status=failed` 是业务终态，应停止轮询并读取
  `run.error.code`、`retryable`、`outcome_unknown` 和 `request_id`；`attempts` 和 `retry_exhausted` 在相关失败类型中才会出现。

## 3. 接口清单

| # | 用途 | 方法与路径 |
|---|---|---|
| 1 | 创建分析任务 | `POST /api/v1/agent/runs` |
| 2 | 查询任务状态和结果 | `GET /api/v1/agent/runs/{run_id}` |
| 3 | 流式接收分析进度和答案 | `GET /api/v1/agent/runs/{run_id}/stream` |
| 4 | 回答 Agent 澄清问题 | `POST /api/v1/agent/runs/{run_id}/input` |
| 5 | 下载 Excel 等结果文件 | `GET /api/sessions/{session_id}/artifacts/{artifact_id}/download` |

## 4. 服务就绪检查

```http
GET /ready
```

- 返回 `200`：Agent runtime 和 MySQL 连接均已就绪，可以接收业务请求。
- 返回 `503`：服务尚未就绪，中台网关或发布系统应暂时停止转发业务流量。
- 此接口用于中台网关、SLB 或发布系统探测，不需要携带业务用户身份头。
- `/health` 包含更多内部诊断信息，仅供 Agent 运维使用，中台不要调用。

## 5. 创建分析任务

```http
POST /api/v1/agent/runs
Authorization: Bearer <service-key>
X-User-Id: user_123
Idempotency-Key: 5c92e26c-56ed-4d16-a7f2-ef3efbfabc01
Content-Type: application/json
```

```json
{
  "message": "分析最近30天直播场次退货情况并生成Excel",
  "session_id": null,
  "title": "直播退货分析"
}
```

首次提交通常返回 `202 Accepted`：

```json
{
  "ok": true,
  "run": {
    "run_id": "run_xxx",
    "session_id": "xpd_xxx",
    "status": "pending"
  },
  "status_url": "/api/v1/agent/runs/run_xxx"
}
```

第一轮可以省略 `session_id`。继续同一段对话时，传回上一轮的 `session_id`，并使用新的
`Idempotency-Key`。

## 6. 查询任务

```http
GET /api/v1/agent/runs/{run_id}
Authorization: Bearer <service-key>
X-User-Id: user_123
```

中台根据 `run.status` 处理：

| 状态 | 处理方式 |
|---|---|
| `pending` | 等待后继续轮询 |
| `running` | 继续轮询 |
| `waiting_input` | 停止轮询，展示 `run.clarification` 并调用澄清接口 |
| `succeeded` | 使用 `run.result.content` 作为最终答案，读取 `progress`、`artifacts` 和后端机器可读的 `analysis` |
| `failed` | 停止轮询，按 `run.error` 的稳定字段处理，不要只展示原始 JSON |

建议每 2～5 秒查询一次。HTTP `200` 只代表查询成功，最终业务结果必须看 `run.status`。

`run.result.progress` 是可展示给用户的中文分析步骤摘要，不包含模型原始思考过程。

### 6.1 结果安全展示边界

| 字段 | 用途 | 是否可直接展示给最终用户 |
|---|---|---|
| `run.result.content` | 已移除内部协议块的模型输出文本 | 经安全转义或受限 Markdown 渲染后才可展示 |
| `run.result.progress` | 中文步骤摘要 | 按不可信文本处理，转义后展示 |
| `run.result.artifacts` | 文件元数据和稳定下载入口 | 可以，但不得暴露 OSS 内部 URI 或日志中的签名地址 |
| `run.result.analysis` | v1.2 机器可读分析结构 | 否，只供受信任的中台后端解析 |
| `run.error` | 失败类型和重试语义 | 只展示经过产品化处理的提示，不直接展示原始对象 |

`analysis` 可能包含 `data_scope.source_tables`、`executed_queries[].sql` 和兼容字段 `sql`。
这些是技术审计信息，不得转发给终端用户，也不得出现在报表、前端日志、埋点或下载页面中。
中台若不需要结构化分析，应忽略 `analysis`，而不是将整个 `run.result` 原样返回给前端。
`content` 仍属于模型生成内容，服务端不会替前端完成 HTML 消毒。前端不得使用未经清理的
`innerHTML`，应进行上下文转义，或使用禁用原始 HTML、脚本、事件属性和危险链接协议的 Markdown 渲染器。

## 7. 流式接收分析进度和答案

创建任务取得 `run_id` 后，建立 SSE 连接：

```http
GET /api/v1/agent/runs/{run_id}/stream
Accept: text/event-stream
Authorization: Bearer <service-key>
X-User-Id: user_123
# 断线重连时附加：Last-Event-ID: <上次收到的 id>
```

事件类型如下：

| 事件 | 主要 `data` 字段 | 说明 |
|---|---|---|
| `progress` | `run_id`、`status`、`attempt_count`、`step` | 中文分析进度或步骤摘要，展示 `step` |
| `answer.delta` | `run_id`、`attempt_count`、`delta` | 答案增量，将 `delta` 按顺序追加到页面 |
| `artifact.ready` | `run_id`、`attempt_count`、`artifact_id`、`session_id`、`filename`、`format`、`media_type`、`size_bytes`、`created_at`、`download_url`、`storage` | Excel 等结果文件已就绪 |
| `clarification.required` | `run_id`、`status`、`attempt_count`、`clarification` | Agent 需要用户补充口径，连接随后结束 |
| `run.completed` | `run_id`、`status`、`attempt_count`、`session_id`、`content`、`analysis`、`usage` | 任务成功完成，连接随后结束 |
| `error` | `run_id`、`code`、`message`，可能还有 `status`、`attempt_count` | 任务失败或状态不可用，连接随后结束 |

示例：

```text
event: progress
data: {"run_id":"run_xxx","status":"running","attempt_count":1,"step":"正在获取并汇总数据"}

event: answer.delta
data: {"run_id":"run_xxx","attempt_count":1,"delta":"最近30天直播场次退货率"}

event: artifact.ready
data: {"run_id":"run_xxx","attempt_count":1,"artifact_id":"art_xxx","session_id":"xpd_xxx","filename":"直播退货分析.xlsx","format":"xlsx","media_type":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","size_bytes":12345,"created_at":1780000000.0,"download_url":"/api/sessions/xpd_xxx/artifacts/art_xxx/download","storage":"oss"}

event: run.completed
data: {"run_id":"run_xxx","status":"succeeded","attempt_count":1,"session_id":"xpd_xxx","content":"最终完整答案","analysis":{},"usage":{}}
```

该接口不会发送模型原始 `reasoning`、`thinking` 或工具内部参数。中台服务端需要关闭
响应缓冲并保持长连接。没有业务事件时，服务约每 15 秒发送一次 `: keep-alive`
注释心跳；客户端忽略该注释，网关和代理的空闲超时应大于心跳周期。收到终止事件后关闭连接。

每个数据事件都有稳定的 `id`。断线重连时将最后
收到的 `id` 通过 `Last-Event-ID` 请求头传回，服务只续发之后的事件，不得重复追加旧的
`answer.delta`。收到 `run.completed` 时，必须使用 `data.content` 替换页面上累积的增量文本，
它是最终权威答案；也可调用状态查询接口取得完整结果。若服务重启或缓存淘汰导致旧事件日志
不可用，服务会跳过历史答案增量并发送当前状态及权威终态，事件 `id` 可能进入新的编号序列。

SSE 建立连接前返回的非 `2xx` HTTP 错误使用 2.2 节的统一错误结构；连接建立后的
`event: error` 是 SSE 事件负载，不是 `ApiErrorResponse` 包装。两者需要分开解析。

## 8. 回答澄清问题

仅当状态为 `waiting_input` 时调用：

```http
POST /api/v1/agent/runs/{run_id}/input
Authorization: Bearer <service-key>
X-User-Id: user_123
Idempotency-Key: <本次回答的新UUID>
Content-Type: application/json
```

```json
{
  "answer": "按退货件数统计"
}
```

接口继续使用原来的 `run_id`。返回后恢复查询任务状态。
首次接受澄清回答通常返回 `202`；对完全相同的 `Idempotency-Key` 和 `answer` 进行幂等重放时可能返回 `200`。
相同幂等键若改变 `answer` 会返回 `409 INPUT_IDEMPOTENCY_CONFLICT`。

## 9. 下载结果文件

任务成功后，从 `run.result.artifacts` 读取 `download_url`，不要自行拼接下载路径。

```http
GET {BASE_URL}/api/sessions/{session_id}/artifacts/{artifact_id}/download
Authorization: Bearer <service-key>
X-User-Id: user_123
```

下载接口有三种正常响应模式：

| 存储与请求方式 | 响应 | 处理方式 |
|---|---|---|
| 本地存储 | `200` 文件流 | 按 `Content-Disposition` 和 `Content-Type` 保存文件 |
| OSS，普通请求 | `307` 跳转 | 访问 `Location` 中的 OSS 签名地址 |
| OSS，`Accept: application/json` | `200` JSON | 从响应中取得当次有效的 OSS 签名地址 |

中台希望自行管理跳转和过期时，可以使用 JSON 模式：

```http
GET {BASE_URL}/api/sessions/{session_id}/artifacts/{artifact_id}/download
Accept: application/json
Authorization: Bearer <service-key>
X-User-Id: user_123
```

OSS 文件的示例响应：

```json
{
  "ok": true,
  "filename": "直播经营对比报告.xlsx",
  "download_url": "https://example.oss-cn-beijing.aliyuncs.com/...?signature=...",
  "download_url_expires_at": "2026-08-11T08:00:00+00:00"
}
```

- `run.result.artifacts[].download_url` 是稳定、需要 Agent 鉴权的服务地址；不要自行拼接路径。
- OSS 签名 `download_url` 是短时临时地址，不得长期缓存；过期后重新请求稳定服务地址即可获取新地址。
- 请求 OSS 签名地址时，无论是自动跟随 `307` 还是使用 JSON 中的地址，都不得转发
  Agent 的 `Authorization`、`X-User-Id`、`Idempotency-Key`、`X-Request-Id` 或 Cookie。
- 对于本地存储，即使发送 `Accept: application/json`，返回值仍是 `200` 文件流。

## 10. 最简调用流程

```text
创建任务
  ↓
保存 run_id、session_id、status_url
  ↓
连接 SSE 接收分析进度和答案
  ├─ clarification.required → 提交回答 → 重新连接 SSE
  ├─ run.completed          → 展示结果 → 下载文件
  └─ error                  → 查询任务状态并展示错误
```

## 11. 重要规则

- 同一个 `session_id` 的任务必须串行提交。
- HTTP 超时重试必须复用原 `Idempotency-Key`，否则可能重复执行。
- `baseline_value`、`absolute_change` 或 `relative_change` 为 `null` 不代表数值为 0，通常表示数据不足或无法形成有效基准。
- Excel、结论和用户可见内容使用中文；JSON 协议字段保持英文。
- 中台不要调用 `/api/chat`、其他 `/api/sessions/*`、`/api/schedules/*` 或网页调试接口。

## 12. 在线接口定义

部署完成后可访问：

```text
{BASE_URL}/docs
{BASE_URL}/openapi.json
```

在线定义的 `paths` 只展示本文列出的 5 个中台业务接口和 `GET /ready`。
由于 OpenAPI 先根据完整 FastAPI 应用生成再过滤 `paths`，`components.schemas` 中可能保留
中台不使用的内部模型。这不表示内部接口对中台开放或承诺兼容。

中台生成 SDK 时必须以上述 6 个 `paths` 为白名单，只保留它们实际引用的 schema；
不得根据单独出现在 `components` 中的模型推断可调用路由。当前下载 operation 的在线定义仍不完整：
它可能把 `X-User-Id` 标成可选、暴露旧的 `X-XPD-Session-Key`，并且没有完整表达 `307/401/404`
以及 JSON/文件流双响应模式。因此下载接口不能直接依赖当前 OpenAPI 生成客户端，必须按本文第 9 节
手工覆盖请求头和响应处理。若在线定义与本文存在差异，
以本文的中台鉴权、下载和安全展示约束为准，并向 Agent 团队报告 OpenAPI 偏差。
