# Docker Compose 单机备用部署指南（阿里云 ECS / Linux）

生产环境已改用“一条流水线 + ACK 动态 Hermes 实例池”，请优先阅读
[`deploy/kubernetes/README.md`](../kubernetes/README.md)。本文保留的固定三个 Hermes
拓扑只用于单台 ECS 回退或本地验证，不具备 HPA 动态扩缩能力。

本文用于把 `xpd-report-agent` 部署为中台可调用的 AI 微服务。部署采用一个 FastAPI
容器和三个固定 Hermes 实例容器：

```text
中台 -> xpd-report-agent（FastAPI，宿主机 8000）
                |-> hermes-1（Gateway，Compose 内网 8642，Cron leader）
                |-> hermes-2（Gateway，Compose 内网 8642）
                `-> hermes-3（Gateway，Compose 内网 8642）
                          -> db-query -> RDS 只读查询
                          -> report-file -> Excel/PDF 等报告并上传 OSS

四个容器共享同一个 hermes-state 和 report-files 本地卷
```

三个 Hermes 都使用自己网络命名空间内的 `8642`，不映射到宿主机；只有 FastAPI
对宿主机发布 `8000`。数据库和 OSS 不部署在容器内，分别连接阿里云 RDS 和 OSS。

## 1. 部署前检查

- ECS 已安装 Docker Engine 和 Docker Compose。
- ECS 与 RDS 位于可互通的 VPC，使用 RDS 内网地址。
- RDS 白名单或安全组允许 ECS 私网地址访问 `3306`。
- RDS 使用 MySQL 5.7.8 或更高版本，推荐 MySQL 8.0。
- 数据库账号对目标库和业务表至少有 `SELECT` 权限，不要使用 root 账号。
- ECS 安全组只向中台来源开放 `8000`，不要将 `8642` 暴露到公网。
- 当前拓扑固定为 `1 个 FastAPI + 3 个 Hermes`，不要使用
  `docker compose up --scale`。
- `hermes-state` 和 `report-files` 依赖单台 Linux 主机的 Docker local volume 与 POSIX 文件锁；
  不支持 NFS、NAS 共享目录、多台 ECS 或 Docker Swarm/Kubernetes 跨节点部署。

确认版本：

```bash
docker --version
docker compose version
```

## 2. 放置项目

推荐目录：

```text
/opt/xpd-report-agent/
```

进入部署目录：

```bash
cd /opt/xpd-report-agent/deploy/docker
```

应至少存在：

```text
Dockerfile
compose.yaml
entrypoint.sh
xpd-report-agent.env.example
```

## 3. 配置环境变量

创建只保存在服务器上的配置文件：

```bash
cp xpd-report-agent.env.example xpd-report-agent.env
chmod 600 xpd-report-agent.env
```

编辑 `xpd-report-agent.env`，填写以下配置：

- 模型：`HERMES_LLM_PROVIDER`、`HERMES_LLM_MODEL`、`HERMES_LLM_BASE_URL`、
  `HERMES_LLM_API_MODE`、`HERMES_LLM_API_KEY`。
- RDS：`XPD_DB_HOST`、`XPD_DB_PORT`、`XPD_DB_NAME`、`XPD_DB_USERNAME`、
  `XPD_DB_PASSWORD`；`XPD_MYSQL_QUERY_TIMEOUT_MS` 可设置单条 Agent 查询的服务端执行上限。
- 中台鉴权：`XPD_SERVICE_API_KEY`。
- 会话签名：`XPD_SESSION_SIGNING_SECRET`，生产环境必须长期固定且与服务密钥不同。
- 并发上限：`XPD_AGENT_MAX_CONCURRENCY=20` 是三节点池的初始值，应根据压测、模型限流、
  RDS 连接数和服务器内存向下调整。
- OSS：报告文件所需的 endpoint、bucket、prefix 和访问凭证。

四个容器共用这份生产配置。Compose 会覆盖服务发现参数：三个 Hermes 都监听
`0.0.0.0:8642`，FastAPI 使用 `HERMES_GATEWAY_NODES` 连接
`hermes-1`、`hermes-2` 和 `hermes-3`，并用 `XPD_HERMES_SCHEDULER_NODE=hermes-1`
固定调度节点。Compose 还会向每个 Hermes 注入唯一的 `XPD_HERMES_NODE_ID`，用于日志和运维审计。
不要在共享环境文件中添加或覆盖节点池、节点 ID、主机名、监听端口和内部回调地址。

定时报告默认关闭。若显式启用，`hermes-1` 从环境文件读取 Cron 开关并作为唯一
Cron leader；Compose 会强制关闭 `hermes-2` 和 `hermes-3` 的定时任务及 Cron 补丁，
防止同一任务重复触发。回调地址固定为 `http://xpd-report-agent:8000`。

`HERMES_GATEWAY_API_KEY`、`XPD_SERVICE_API_KEY`、`XPD_SESSION_SIGNING_SECRET` 必须是
三个不同的高熵密钥，每个至少 32 个字符。可以分别执行三次下列命令生成：

```bash
openssl rand -hex 32
```

中台生产配置还必须保持：

```text
XPD_IDENTITY_MODE=user_id
XPD_SERVICE_AUTH_ENABLED=true
XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED=false
FASTAPI_RELOAD=false
```

真实 `xpd-report-agent.env` 已被 Git 和 Docker 构建上下文忽略，禁止提交到仓库、日志或接口文档。
如果密钥曾出现在聊天、工单或终端录屏中，应及时轮换。
容器启动前会执行失败关闭的配置预检；缺少必填项、使用 `REPLACE_WITH_*`
占位符、关闭鉴权或复用密钥时，服务不会带病启动。

## 4. 验证 RDS 内网

只测试网络端口，不打印账号和密码：

```bash
RDS_HOST="$(sed -n 's/^XPD_DB_HOST=//p' xpd-report-agent.env)"
RDS_PORT="$(sed -n 's/^XPD_DB_PORT=//p' xpd-report-agent.env)"
timeout 5 bash -c "</dev/tcp/$RDS_HOST/${RDS_PORT:-3306}" \
  && echo 'RDS 内网端口可达' \
  || echo 'RDS 内网端口不可达'
```

端口不通时依次检查 VPC、RDS 白名单/安全组、内网域名和端口。ECS 通常不需要开放入方向
`3306`，因为容器是主动连接 RDS。

## 5. 构建镜像

Dockerfile 使用两个构建目标：`fastapi` 只包含项目的 Python 3.12 运行环境，`hermes` 额外包含
固定版本的 Python 3.11 Hermes Runtime、插件依赖和中文字体。三个 Hermes 服务共用同一个
`xpd-report-agent-hermes:dev` 镜像，不是三份镜像。两个构建目标共享公共依赖层；首次构建仍需
下载 Hermes Runtime 和依赖，后续构建会复用 BuildKit 缓存。

前台构建：

```bash
docker compose build --progress=plain
```

远程终端可能断开时，使用后台构建：

```bash
nohup docker compose build --progress=plain \
  > /opt/xpd-report-agent-build.log 2>&1 &
tail -f /opt/xpd-report-agent-build.log
```

按 `Ctrl+C` 只会退出日志查看，不会终止后台构建。成功后应能看到两个本地镜像：

```bash
docker image inspect \
  xpd-report-agent-fastapi:dev \
  xpd-report-agent-hermes:dev >/dev/null
```

验证镜像中的 Hermes：

```bash
docker run --rm --entrypoint sh xpd-report-agent-hermes:dev \
  -c '/opt/hermes-agent/venv/bin/hermes --version && \
      uv pip check --python /opt/hermes-agent/venv/bin/python'
```

Hermes 固定运行时仅存在于 `xpd-report-agent-hermes:dev` 的 `/opt/hermes-agent`；FastAPI 镜像
不包含该运行时。可变的会话状态仍保存在四个容器共享的 `$HOME/.hermes` 本地卷，
因此重建镜像不会丢失历史。

构建完成后，可在启动前单独检查配置：

```bash
docker compose run --rm --no-deps \
  --entrypoint /app/.venv/bin/python xpd-report-agent \
  -m xpd_report_agent.runtime.deployment_preflight
```

### 首次从单容器部署升级

旧的 `xpd-report-agent` 容器同时运行 FastAPI 和 Hermes。第一次切换时，绝不能在旧容器仍运行
Hermes 的同时启动新的三个 Hermes 容器，否则新旧进程会并发写同一个 SQLite/JSON 状态卷。

拉取新代码前，先保存旧 Compose 定义并保留正在运行的旧镜像：

```bash
cd /opt/xpd-report-agent/deploy/docker
cp compose.yaml /opt/xpd-report-agent-compose-single.yaml
docker image tag "$(docker inspect -f '{{.Image}}' xpd-report-agent)" \
  xpd-report-agent:rollback-before-split
```

可以在旧服务运行期间拉取代码并构建两个新镜像。构建完成后，等待所有 Agent 任务结束，再停服做
一致性备份：

```bash
curl -fsS http://127.0.0.1:8000/health | python3 -c '
import json, sys
active = json.load(sys.stdin)["agent_runs"]["active_tasks"]
print(f"active_tasks={active}")
raise SystemExit(0 if active == 0 else 1)
'

umask 077
XPD_SPLIT_BACKUP_DIR="/opt/xpd-before-split-$(date +%Y%m%d%H%M%S)"
install -d -m 700 "$XPD_SPLIT_BACKUP_DIR/hermes-state"
install -d -m 700 "$XPD_SPLIT_BACKUP_DIR/report-files"

docker stop --time 60 xpd-report-agent
docker cp xpd-report-agent:/var/lib/xpd-report-agent/.hermes/. \
  "$XPD_SPLIT_BACKUP_DIR/hermes-state/"
docker cp xpd-report-agent:/app/data/report-files/. \
  "$XPD_SPLIT_BACKUP_DIR/report-files/"
test -n "$(find "$XPD_SPLIT_BACKUP_DIR/hermes-state" -mindepth 1 -print -quit)" \
  || { echo 'Hermes 状态备份为空，已恢复旧服务，请停止发布' >&2; \
       docker start xpd-report-agent; false; }
```

只有输出 `active_tasks=0` 且备份成功时才能继续。保持原部署目录和 Compose project 名不变，新的
四个服务会继续使用原 `hermes-state` 与 `report-files` 本地卷：

```bash
docker compose up -d --force-recreate
docker compose ps
```

如果切换失败，先停止四容器拓扑，再恢复保留的单容器镜像和 Compose 定义：

```bash
docker compose down
docker image tag xpd-report-agent:rollback-before-split xpd-report-agent:dev
docker compose \
  --project-directory /opt/xpd-report-agent/deploy/docker \
  -f /opt/xpd-report-agent-compose-single.yaml \
  up -d --force-recreate --no-build
```

备份目录中可能包含会话、记忆和配置密钥，必须保持 `0700` 权限。纯新部署不执行本段。

### 从旧的 `1 FastAPI + 1 Hermes` 双容器升级

如果服务器已经使用旧版双容器 Compose，旧的 `xpd-report-agent-hermes` 不会因服务名改成
`hermes-1` 而自动停止。必须在活动任务为零后显式停止它，否则旧 Hermes 会与新节点同时写
`xpd-report-agent-hermes-state` 卷。

拉取新代码前备份旧 Compose，并保留两份旧镜像：

```bash
cd /opt/xpd-report-agent/deploy/docker
cp compose.yaml /opt/xpd-report-agent-compose-one-hermes.yaml
docker image tag "$(docker inspect -f '{{.Image}}' xpd-report-agent)" \
  xpd-report-agent-fastapi:rollback-before-pool
docker image tag "$(docker inspect -f '{{.Image}}' xpd-report-agent-hermes)" \
  xpd-report-agent-hermes:rollback-before-pool
```

新镜像构建完成后，先通过 `/health` 确认 `active_tasks=0`，再停止旧的两个容器并备份卷内数据：

```bash
docker stop --time 60 xpd-report-agent xpd-report-agent-hermes

umask 077
XPD_POOL_BACKUP_DIR="/opt/xpd-before-hermes-pool-$(date +%Y%m%d%H%M%S)"
install -d -m 700 "$XPD_POOL_BACKUP_DIR/hermes-state"
install -d -m 700 "$XPD_POOL_BACKUP_DIR/report-files"
docker cp xpd-report-agent-hermes:/var/lib/xpd-report-agent/.hermes/. \
  "$XPD_POOL_BACKUP_DIR/hermes-state/"
docker cp xpd-report-agent-hermes:/app/data/report-files/. \
  "$XPD_POOL_BACKUP_DIR/report-files/"
```

保持原部署目录、Compose project 名和卷名不变，启动新拓扑并移除已停止的旧
`hermes` orphan 容器：

```bash
docker compose up -d --force-recreate --remove-orphans
docker compose ps
```

升级失败时，先停止新拓扑，把保留镜像重新标记为旧 Compose 使用的镜像名，再恢复
双容器：

```bash
docker compose down
docker image tag xpd-report-agent-fastapi:rollback-before-pool \
  xpd-report-agent-fastapi:dev
docker image tag xpd-report-agent-hermes:rollback-before-pool \
  xpd-report-agent-hermes:dev
docker compose \
  --project-directory /opt/xpd-report-agent/deploy/docker \
  -f /opt/xpd-report-agent-compose-one-hermes.yaml \
  up -d --force-recreate --no-build
```

不要执行 `docker compose down -v`。

## 6. 启动服务

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=100 hermes-1 hermes-2 hermes-3 xpd-report-agent
```

三个 Hermes 容器先启动，FastAPI 在它们进入 `started` 后启动，不会因某一个 Hermes
暂时不健康而无法重新创建。FastAPI 运行时会自行探测节点池；初期显示 `health: starting`
属于正常现象。至少一个 Hermes 健康时 `/ready` 可以就绪，但完整部署验收仍建议确认
`hermes-1`、`hermes-2`、`hermes-3` 和 `xpd-report-agent` 四项都变为 `healthy`：

```text
Up ... (healthy)
```

四个容器全部 `healthy` 是完整部署的验收标准，但不等于模型推理和 OSS 业务链路已经
验收；还必须完成第 8 节的真实 Agent 查询和文件下载测试。

## 7. 就绪验收

```bash
curl -sS http://127.0.0.1:8000/health
echo
curl -sS http://127.0.0.1:8000/ready
echo
```

`/ready` 必须返回 HTTP `200`，并满足：

```json
{
  "ok": true,
  "status": "ready",
  "checks": {
    "runtime": true,
    "mysql": true
  }
}
```

`runtime=true` 表示 FastAPI 已通过容器内网连接至少一个 Hermes 节点，`mysql=true` 表示应用已实际
连接 RDS，而不只是容器进程存活。三个 Hermes 中的一个暂时不健康时，FastAPI 会停止向它
分配新 owner；所有 Hermes 都不健康时 `/ready` 返回 HTTP `503`。`/health` 是运维诊断接口，
中台业务方只需要使用 `/ready`。

## 8. 真实业务接口测试

服务密钥从容器环境读取，不在终端输出：

```bash
XPD_TEST_KEY="$(docker exec xpd-report-agent printenv XPD_SERVICE_API_KEY)"
XPD_TEST_REQUEST_ID="$(cat /proc/sys/kernel/random/uuid)"
```

创建一个实际读取 RDS Schema 的任务：

```bash
curl -sS -X POST 'http://127.0.0.1:8000/api/v1/agent/runs' \
  -H "Authorization: Bearer $XPD_TEST_KEY" \
  -H 'X-User-Id: deployment_test_user' \
  -H "Idempotency-Key: $XPD_TEST_REQUEST_ID" \
  -H 'Content-Type: application/json' \
  --data '{
    "message":"请通过数据库工具实际查询当前数据库，统计可访问的业务表数量，并列出前5个业务表名。不要根据记忆回答。",
    "session_id":null,
    "title":"RDS真实接口测试"
  }'
```

取得 `run_id` 后连接 SSE：

```bash
curl -N --max-time 300 \
  'http://127.0.0.1:8000/api/v1/agent/runs/<run_id>/stream' \
  -H "Authorization: Bearer $XPD_TEST_KEY" \
  -H 'X-User-Id: deployment_test_user' \
  -H 'Accept: text/event-stream'
```

正常会收到 `progress`、`answer.delta`，最后收到 `run.completed`。完整的 5 个中台接口、澄清续答
和文件下载流程见 [`../../docs/api/middle-platform-agent-api.md`](../../docs/api/middle-platform-agent-api.md)。

再创建一个明确要求“查询数据并生成 Excel”的任务，验收 OSS 链路。该任务必须同时
满足：

- `run.status=succeeded`，且结果不是空字符串；
- SSE 出现 `artifact.ready`，`run.result.artifacts` 至少有一项；
- 使用第 5 个中台接口实际下载成功，返回的 Excel 可以打开且中文正常。

上述三项全部通过，才说明模型、RDS、报告生成、OSS 上传和下载都已实际工作。

接口文档分工：

| 文档 | 使用对象 | 说明 |
|---|---|---|
| [`middle-platform-agent-api.md`](../../docs/api/middle-platform-agent-api.md) | 中台后端 | 正式稳定的 5 个业务接口及接入流程 |
| [`internal-test-api.md`](../../docs/api/internal-test-api.md) | Agent 开发、测试、运维 | 内部会话、网页调试和诊断接口，不提供给中台 |

部署完成后，交付给中台开发者的文档应为 `middle-platform-agent-api.md`，不要把内部测试文档
当作中台契约。

## 9. 中台调用地址

同 VPC 的中台服务使用：

```text
http://<ECS内网IP>:8000
```

中台后端必须携带 `Authorization: Bearer <service-key>` 和稳定的 `X-User-Id`。服务密钥只能保存
在中台后端，不得下发到浏览器。建议在安全组、内部负载均衡或 API 网关层进一步限制来源。

## 10. 更新、回滚与日常运维

### 从 Codeup 更新

发布前先确认没有运行中的长任务，仓库没有未处理的服务器本地改动。真实
`xpd-report-agent.env` 是 Git 忽略文件，不会被拉取覆盖。

```bash
cd /opt/xpd-report-agent
git status --short
git fetch origin master
git merge --ff-only origin/master

cd deploy/docker
docker image tag \
  xpd-report-agent-fastapi:dev \
  xpd-report-agent-fastapi:rollback-last
docker image tag \
  xpd-report-agent-hermes:dev \
  xpd-report-agent-hermes:rollback-last

docker compose build --progress=plain
docker compose run --rm --no-deps \
  --entrypoint /app/.venv/bin/python xpd-report-agent \
  -m xpd_report_agent.runtime.deployment_preflight
docker compose up -d --force-recreate
docker compose ps
```

如果 `git status --short` 有输出，先确认改动来源，不要在服务器上盲目执行
`git reset --hard`。`docker compose restart` 只会重启当前容器，不会加载新代码或新镜像；
代码或镜像更新必须使用 `docker compose up -d --force-recreate`。

如果只修复 FastAPI，可以只构建和替换 FastAPI 容器，三个 Hermes 不会被重建或重启：

```bash
docker compose build --progress=plain xpd-report-agent
docker compose up -d --no-deps --force-recreate xpd-report-agent
curl -fsS http://127.0.0.1:8000/ready
```

如果只修改 Hermes 运行时、插件或 Skill，只构建一次共享 Hermes 镜像，然后在确认没有运行中
任务后依次替换三个实例，Cron leader `hermes-1` 最后替换：

```bash
docker compose build --progress=plain hermes-1
docker compose up -d --no-deps --force-recreate hermes-3
docker compose up -d --no-deps --force-recreate hermes-2
docker compose up -d --no-deps --force-recreate hermes-1
docker compose ps
```

三个服务的 `image` 都是 `xpd-report-agent-hermes:dev`，因此上述命令不会产生三份镜像。
若 FastAPI 与 Hermes 之间的内部协议不兼容，不要分开发布，应使用前述全量流程。

发布后依次执行第 7 节的就绪检查和第 8 节的真实业务验收。在它们全部通过前，
不要删除回滚镜像和状态备份。

### 回滚镜像

如果新版启动失败或真实业务验收不通过，使用发布前保留的两份镜像一起回滚：

```bash
cd /opt/xpd-report-agent/deploy/docker
docker image inspect \
  xpd-report-agent-fastapi:rollback-last \
  xpd-report-agent-hermes:rollback-last >/dev/null
docker image tag \
  xpd-report-agent-fastapi:rollback-last \
  xpd-report-agent-fastapi:dev
docker image tag \
  xpd-report-agent-hermes:rollback-last \
  xpd-report-agent-hermes:dev
docker compose up -d --force-recreate --no-build
docker compose ps
curl -fsS http://127.0.0.1:8000/ready
```

首次从单容器拆分时使用第 5 节的专用回滚流程。日常的双镜像回滚只恢复运行服务，服务器 Git
工作树仍保留新版代码；确认原因后，通过新的 Codeup 修复提交再次发布，不在生产机上直接改代码。

### 日常命令

```bash
docker compose ps
docker compose logs -f --tail=200 hermes-1 hermes-2 hermes-3 xpd-report-agent
docker compose restart
```

只查看某个容器：

```bash
docker compose logs -f --tail=200 hermes-1
docker compose logs -f --tail=200 xpd-report-agent
```

查看最近错误：

```bash
docker compose logs --since=10m \
  | grep -E 'ERROR|Traceback|AuthError|RuntimeError'
```

Hermes 会话、记忆、任务和 FastAPI 节点粘性路由都由 `xpd-report-agent-hermes-state`
命名卷保存；Compose 的 `report-files` 卷由四个容器共同使用，并将报告上传 OSS。
`docker compose down` 不会删除命名卷；不要在生产环境执行 `docker compose down -v`。
应定期备份状态卷，并实际验证 OSS 上传和下载。这两个共享卷只能使用当前单机 Docker
local volume，不能将目录换成 NFS/NAS 后把 Hermes 分布到多台主机。

## 11. 常见问题

### 构建停在下载依赖

使用 `--progress=plain` 查看具体包。不要反复同时启动多个构建进程，可用以下命令确认：

```bash
pgrep -af 'docker compose build'
tail -f /opt/xpd-report-agent-build.log
```

### `Hermes executable was not found`

当前 Dockerfile 会在构建阶段检查 `pyproject.toml` 和 `venv/bin/hermes`，缺失时构建必须失败。
不要把缺少 Hermes 的镜像当作成功镜像启动；查看完整构建日志中最早出现的 Git 或依赖错误。

### `API Server: aiohttp not installed`

正式依赖已包含 `aiohttp==3.14.1`。出现该错误通常说明服务器仍在使用旧镜像，请更新代码并重新
构建，不要长期依赖进入容器手工安装。

### `No usable credentials found for provider 'alibaba-coding-plan'`

确认 `HERMES_LLM_API_KEY` 已填写。启动脚本会把该通用变量映射为 Hermes 需要的
`ALIBABA_CODING_PLAN_API_KEY` 和 `DASHSCOPE_API_KEY`；旧镜像没有该兼容映射，需要重新构建。

### `/ready` 中 `mysql=false`

端口可达不等于账号可用。检查数据库名、账号密码、RDS 白名单及账号的 `SELECT` 权限，并查看：

```bash
docker compose logs --tail=200
```

### FastAPI 无法连接 Hermes

先确认四个服务都在同一个 Compose project 内，并检查三个 Hermes 的健康状态与内部 DNS：

```bash
docker compose ps
docker compose logs --tail=200 hermes-1 hermes-2 hermes-3
docker compose exec xpd-report-agent \
  /app/.venv/bin/python -c \
  "import socket; print([socket.gethostbyname(name) for name in ('hermes-1', 'hermes-2', 'hermes-3')])"
```

不要在共享环境文件中添加 `HERMES_GATEWAY_HOST`、`HERMES_GATEWAY_NODES` 或
`XPD_HERMES_SCHEDULER_NODE`。服务级配置应保持：Hermes 监听 `0.0.0.0`，FastAPI 按 Compose
中的节点 ID 连接 `hermes-1`、`hermes-2` 和 `hermes-3`。

### 拆分前创建的定时任务回调失败

定时任务脚本会固化创建时的回调地址。旧脚本如果仍指向 `127.0.0.1:8000`，拆容器后不会自动
更新；请删除并重新创建对应定时任务，使脚本使用 `http://xpd-report-agent:8000`。

### SQLite WAL 警告

Hermes 会在检测到受影响的 SQLite 版本时自动使用 `journal_mode=DELETE`。这不会阻止 RDS 查询或
中台接口启动，但应在后续基础镜像升级时同步升级 SQLite/Python Runtime。
