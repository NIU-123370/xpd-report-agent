# 直播中台 Agent API

本接口供直播中台后端调用。项目自带网页仅用于本地调试，不应作为生产身份或生产调用入口。

## 接入前提

- 生产环境使用 `XPD_IDENTITY_MODE=user_id`。
- 中台后端完成用户登录认证后，将稳定的用户标识放入 `X-User-Id`。
- 生产环境设置 `XPD_SERVICE_AUTH_ENABLED=true` 和独立的高强度
  `XPD_SERVICE_API_KEY`。中台后端调用所有公开 `/api/*` 接口时都必须携带
  `Authorization: Bearer <service-key>`。
- `X-User-Id` 只表示用户身份范围，不是调用凭证。服务密钥仍应配合内网或 API 网关，
  禁止浏览器和其他客户端直接伪造身份头。
- 生产环境必须长期固定 `XPD_SESSION_SIGNING_SECRET`。更换它会改变用户作用域，旧会话和旧任务将无法按原用户查询。
- FastAPI 必须保持单 worker、单实例运行，或先把 Agent run 状态迁移至支持多实例并发的存储。

上线时用 `GET /health` 做存活检查，用 `GET /ready` 做流量就绪检查。
`/ready` 会实际执行只读 `SELECT 1`；Hermes 或 MySQL 不可用时返回 `503`。

## 提交分析

```http
POST /api/v1/agent/runs
Authorization: Bearer <service-key>
X-User-Id: <中台登录用户ID>
Idempotency-Key: <本次业务请求的稳定唯一ID>
X-Request-Id: <可选；本次HTTP调用的链路追踪ID>
Content-Type: application/json
```

```json
{
  "session_id": "可选；继续历史对话时填写",
  "title": "可选；首次创建会话时使用",
  "message": "分析最近7天的商品表现"
}
```

未传 `session_id` 时，服务会根据用户范围和 `Idempotency-Key` 幂等创建会话。首次接受任务或任务仍在执行时返回 `202 Accepted`：

```json
{
  "ok": true,
  "run": {
    "run_id": "run_...",
    "request_id": "req_...",
    "idempotency_key": "...",
    "session_id": "xpd_...",
    "status": "pending",
    "attempt_count": 0,
    "clarification": null,
    "error": null,
    "result": null,
    "created_at": "2026-08-05T09:00:00+00:00",
    "updated_at": "2026-08-05T09:00:00+00:00",
    "started_at": null,
    "completed_at": null
  },
  "status_url": "/api/v1/agent/runs/run_..."
}
```

相同用户、相同会话和相同 `Idempotency-Key` 下：

- `message` 相同：返回原 `run_id` 和原状态，不重复执行。
- `message` 不同：返回 `409 IDEMPOTENCY_CONFLICT`。
- `title` 只是首次创建会话时的元数据，不参与分析请求的幂等比较；重复请求修改 `title` 不会创建新分析。

对已结束的任务重复提交时返回 `200 OK`，响应中仍是原 `run_id` 和终态结果。

## 查询状态和结果

```http
GET /api/v1/agent/runs/{run_id}
Authorization: Bearer <service-key>
X-User-Id: <与提交时相同的用户ID>
X-Request-Id: <可选；本次轮询调用的链路追踪ID>
```

状态为：

- `pending`：已持久化，等待执行。
- `running`：正在执行或服务正在核对 Hermes 中的执行结果。
- `waiting_input`：遇到会改变 SQL 或指标口径的关键歧义；读取
  `run.clarification`，提交回答后继续同一个 `run_id`。
- `succeeded`：成功终态，读取 `run.result`。
- `failed`：失败终态，读取 `run.error`。

`GET` 找到任务时始终返回 `200 OK` 和 `ok=true`。这只代表“状态查询成功”，不代表分析成功；中台必须以 `run.status` 判断业务结果。

成功时 `run.result` 包含：

- `session_id`
- `content`
- `analysis`：结构化分析对象
- `reasoning`
- `usage`
- `artifacts`
- `recovered`：结果是否由服务重启后的会话历史核对恢复

`analysis` 固定包含 `schema_version`、`structured`、`conclusion`、
`data_period`、`metrics`、`insights`、`assumptions` 和 `sql`。
`structured=false` 表示模型未返回可校验的结构化块，服务已降级为文本结论；
中台可继续展示 `content`，但不应把空指标解读为“业务指标为零”。

### 提交澄清回答

当状态为 `waiting_input` 时，`run.clarification` 包含 `clarification_id`、
`question`、最多 4 个 `choices` 和 `requested_at`。回答必须使用新的、稳定的幂等键：

```http
POST /api/v1/agent/runs/{run_id}/input
Authorization: Bearer <service-key>
X-User-Id: <与提交时相同的用户ID>
Idempotency-Key: <本次回答的稳定唯一ID>
Content-Type: application/json

{"answer":"按件数"}
```

首次接受回答返回 `202` 并将同一任务重新置为 `pending/running`；相同键和相同回答重放
返回当前任务且不会重复执行，同一键改用不同回答返回 `409 INPUT_IDEMPOTENCY_CONFLICT`。
非 `waiting_input` 状态提交新回答返回 `409 AGENT_RUN_NOT_WAITING_INPUT`。等待输入不会占用
Agent 并发槽，服务重启也不会自动重放原请求。

文件使用 `artifacts[].download_url` 下载。OSS 开启时返回有限时效的签名 URL，
客户端不需要在该 OSS 请求上携带 Agent 身份请求头；`download_url_expires_at`
表示过期时间。再次调用任务状态查询接口会刷新签名 URL。OSS 未开启的
本地开发模式仍返回 Agent 服务内的相对下载路径，此时需携带原身份请求头。
OSS 对象按北京时间存放为
`public/dev/agent-report-files/YYYYMMDD/uid-traceid-秒级Unix时间戳.扩展名`，其中
`uid` 来自 `X-User-Id`，`traceid` 来自提交请求的 `X-Request-Id`。
服务默认最多同时执行 3 个 Agent 任务；超出部分保持 `pending`，获得执行槽后才进入 `running`。

异步任务失败仍是一次成功的状态查询，例如：

```json
{
  "ok": true,
  "run": {
    "run_id": "run_...",
    "status": "failed",
    "attempt_count": 2,
    "result": null,
    "error": {
      "code": "HERMES_UNAVAILABLE",
      "message": "...",
      "retryable": true,
      "outcome_unknown": false,
      "request_id": "req_..."
    }
  }
}
```

### 建议轮询节奏

1. 收到 `202` 后等待 1 秒再首次查询。
2. `pending/running` 时按 1、2、4、5 秒退避，之后最多每 5 秒查询一次。
3. 单次轮询遇到网络错误或 `502/503/504` 时，仅重试同一个 `run_id`，按 2、4、8 秒退避，最长 30 秒。
4. 中台自身等待超时不会取消服务端任务。保留 `run_id`，稍后继续查询，不能因此生成新的幂等键重新分析。
5. 到达 `waiting_input` 时停止轮询并展示问题；提交回答成功后恢复轮询。
6. 到达 `succeeded/failed` 后停止轮询。

## HTTP 错误与重试

HTTP 请求本身失败时统一返回：

```json
{
  "ok": false,
  "error": {
    "code": "HERMES_UNAVAILABLE",
    "message": "...",
    "retryable": true,
    "outcome_unknown": false,
    "request_id": "req_..."
  },
  "detail": {}
}
```

中台只能根据 `error.code`、`retryable` 和 `outcome_unknown` 分支。`message` 用于展示，`detail` 是兼容诊断字段且结构不稳定，不应作为程序判断依据。

常见 HTTP 状态：

| HTTP | 典型错误码 | 处理方式 |
|---|---|---|
| `400` | `INVALID_IDEMPOTENCY_KEY` | 修正请求，不重试原错误请求 |
| `401` | `UNAUTHORIZED` | 检查身份头或服务鉴权 |
| `404` | `AGENT_RUN_NOT_FOUND` | 检查 `run_id` 与 `X-User-Id` 是否属于同一用户 |
| `409` | `IDEMPOTENCY_CONFLICT`、`INPUT_IDEMPOTENCY_CONFLICT`、`AGENT_RUN_NOT_WAITING_INPUT`、`SESSION_CLOSED` | 不自动重试；修正业务请求或刷新任务状态 |
| `422` | `VALIDATION_ERROR` | 修正请求体 |
| `502/503/504` | `HERMES_*`、`AGENT_RUN_STATE_UNAVAILABLE` | 按响应中的两个布尔字段处理 |

### `retryable` 的准确含义

- 对尚未得到 `run_id` 的 POST HTTP 错误，`retryable=true, outcome_unknown=false` 时，使用原请求和相同 `Idempotency-Key` 重试。
- 对 `run.status=failed`，`retryable=true, outcome_unknown=false` 表示失败原因可能是临时性的，但不保证该 run 仍有剩余尝试次数。服务会先执行配置允许的内部重试。
- 中台可以用相同幂等键再提交一次：若返回 `202/pending/running`，恢复轮询；若仍返回 `200/failed`，说明原 run 已是终态或次数已耗尽，必须停止自动重试并告警或转人工。
- 不得通过不断生成新幂等键绕过次数上限，否则可能重复分析、重复写记忆或重复生成文件。

### `outcome_unknown` 的准确含义

`outcome_unknown=true` 表示 Hermes 可能已经收到请求，自动重放存在重复执行风险：

- 已知 `run_id`：继续查询该 `run_id`。若最终仍为 `failed + outcome_unknown=true`，停止自动操作，展示人工确认状态。
- 初始 POST 没有返回 `run_id`：只能使用原请求和相同 `Idempotency-Key` 重新查询式提交；不能生成新 Key。
- 人工确认 Hermes 会话中没有对应结果后，才可以由操作者明确发起一个新的业务请求和新幂等键。

参数、身份、权限、SQL 语义和幂等冲突等确定性错误不应自动重试。

## Request ID 语义

- HTTP 响应头 `X-Request-Id` 对应当前这一次 HTTP 调用。调用方传入合法值时服务原样返回，否则服务生成新值。
- `run.request_id` 是首次创建该 run 时的请求 ID，重复提交相同幂等键时保持不变，用于追踪原始执行。
- `Idempotency-Key` 标识业务执行，`X-Request-Id` 标识一次传输尝试，两者不能互相替代。

## 本地与生产身份

本地 `session_key` 模式使用 `X-XPD-Session-Key`。生产 `user_id` 模式使用 `X-User-Id`。跨用户查询统一返回 `404`，避免泄露其他用户的 `run_id` 是否存在。

Agent 通过独立 Bearer 密钥校验中台服务身份，再使用 `X-User-Id`
划分用户数据。两者必须同时提供：Bearer 密钥不能代替用户作用域，
`X-User-Id` 也不能当成密码或鉴权凭证。未配置服务密钥时生产服务会拒绝启动。

FastAPI 在 `/docs` 和 `/openapi.json` 提供接口定义。正式中台客户端应只依赖
`/api/v1/agent/runs`、状态查询及 `/input` 契约；网页调试接口不属于稳定中台 API。
