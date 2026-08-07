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
| `X-User-Id` | 是 | 中台内部稳定用户 ID，不要使用手机号或姓名 |
| `Idempotency-Key` | POST 必填 | 每个新业务操作使用新的 UUID；HTTP 重试必须复用原值 |
| `X-Request-Id` | 否 | 链路追踪 ID |

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
| `succeeded` | 展示 `run.result.content`，读取 `run.result.progress` 和 `run.result.artifacts` |
| `failed` | 展示错误并停止轮询 |

建议每 2～5 秒查询一次。HTTP `200` 只代表查询成功，最终业务结果必须看 `run.status`。

`run.result.progress` 是可展示给用户的中文分析步骤摘要，不包含模型原始思考过程。

## 7. 流式接收分析进度和答案

创建任务取得 `run_id` 后，建立 SSE 连接：

```http
GET /api/v1/agent/runs/{run_id}/stream
Accept: text/event-stream
Authorization: Bearer <service-key>
X-User-Id: user_123
```

事件类型如下：

| 事件 | 说明 |
|---|---|
| `progress` | 中文分析进度或步骤摘要，展示 `data.step` |
| `answer.delta` | 答案增量，将 `data.delta` 按顺序追加到页面 |
| `artifact.ready` | Excel 等结果文件已就绪 |
| `clarification.required` | Agent 需要用户补充口径，连接随后结束 |
| `run.completed` | 任务成功完成，连接随后结束 |
| `error` | 任务失败或状态不可用，连接随后结束 |

示例：

```text
event: progress
data: {"run_id":"run_xxx","status":"running","step":"正在获取并汇总数据"}

event: answer.delta
data: {"run_id":"run_xxx","delta":"最近30天直播场次退货率"}

event: run.completed
data: {"run_id":"run_xxx","status":"succeeded","session_id":"xpd_xxx"}
```

该接口不会发送模型原始 `reasoning`、`thinking` 或工具内部参数。中台服务端需要关闭
响应缓冲并保持长连接；收到终止事件后关闭连接。断线后可以重新连接，或调用状态查询接口
取得完整最终结果。

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

## 9. 下载结果文件

任务成功后，从 `run.result.artifacts` 读取 `download_url`，不要自行拼接下载路径。

```http
GET {BASE_URL}/api/sessions/{session_id}/artifacts/{artifact_id}/download
Authorization: Bearer <service-key>
X-User-Id: user_123
```

- 本地存储返回 `200` 文件流。
- OSS 存储可能返回 `307` 跳转。
- 跳转到 OSS 后，不要继续转发 `Authorization` 和 `X-User-Id`。

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

在线定义只展示本文列出的 5 个中台业务接口和 `GET /ready`。
