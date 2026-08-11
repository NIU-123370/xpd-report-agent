# ACK 动态 Hermes 实例池部署

这套清单在阿里云容器服务 ACK 上部署一个 FastAPI Pod，以及由 HPA 从 2 个副本按需扩容、最多扩到 10 个副本的 Hermes StatefulSet。为保护每个 Hermes 的稳定实例状态，HPA 自动缩容已关闭。FastAPI 的 8000 端口通过 ACK 私网 LoadBalancer 在 VPC 内开放；Hermes 的 8642 只有无头 Service，NetworkPolicy 仅允许 FastAPI Pod 访问。

## 一、先理解边界

Hermes 当前把会话、记忆和任务状态保存在 SQLite/本地文件，并依赖本机文件锁。多个 Gateway 不能共用同一个 `HERMES_HOME`，否则 PID 和运行锁会冲突。本清单仍让 FastAPI 和全部 Hermes Pod 挂载同一个 `hermes-state` `ReadWriteOnce` 云盘 PVC，但目录职责不同：FastAPI 使用共享根目录，每个 Hermes Pod 使用 `instances/<POD_NAME>` 稳定子目录，所有角色仅通过共享根目录下的 `memories/` 读写长期记忆。`report-files` 使用第二个 RWO PVC。所有 Pod 固定到唯一一个带 `xpd-report-agent=true` 标签的节点。

因此，这个方案提供的是“单节点内的动态并发扩容”，不是跨节点高可用：

- 只能给一个专用 ACK 工作节点加 `xpd-report-agent=true` 标签；流水线会检查这一约束。Hermes 还使用强制 PodAffinity 跟随唯一的 FastAPI Pod，并以 `kubernetes.io/hostname` 约束拓扑，即使误标了额外节点也不能跨节点挂载共享数据。
- PVC 必须使用 ACK 块存储、`volumeBindingMode=WaitForFirstConsumer` 的 RWO StorageClass，保证两个盘按唯一工作节点的可用区延迟绑定；流水线会失败关闭地校验。
- 严禁改成 NAS、NFS、OSSFS 或其他跨节点 RWX 共享盘来共享 SQLite。
- 标记节点宕机时整个服务会暂停。若需要跨节点高可用，必须先把 SQLite、本地锁和报表文件迁移到支持分布式并发的数据库与对象存储。

“约 20 人并发”只能作为初始容量目标。清单把 FastAPI 全局并发上限设为 20，Hermes 最少 2 个、最多 10 个；真实上限仍取决于模型 API 限流、RDS 连接数、单次任务耗时以及节点 CPU/内存，投产前必须压测。

## 二、架构与调度

- `xpd-report-agent` Deployment 固定 1 个副本，使用 EndpointSlice 发现可用 Hermes Pod。
- `hermes` StatefulSet 使用稳定 Pod 名 `hermes-0`、`hermes-1`……；各 Pod 的 `HERMES_HOME` 分别为共享卷中的 `instances/hermes-0`、`instances/hermes-1`……，同名 Pod 重建后继续使用原实例目录。
- 节点粘性调度单位是经服务端签名的 `owner_scope`。在生产 `user_id` 模式下，同一个
  `X-User-Id` 的所有 `session_id` 都固定到同一个 Hermes Pod；一个 Hermes 可以同时服务多个
  用户。新用户按已绑定 owner 数量尽量均衡分配，而不是按单个用户的实时任务数重新平衡。
- HPA 扩容后的新 Pod 主要承接新用户；已绑定用户不会被自动迁移。因此一个重度用户的多个会话
  仍会集中在一个 Hermes，扩容不能把这个用户的已有负载拆分到新 Pod。用户长期记忆仍通过
  共享 `XPD_MEMORY_ROOT` 按 owner 隔离。
- `hermes-0` 是定时任务 leader。最小副本数为 2，HPA 只会扩容而不会自动删除已有 Pod。当前池初始化要求 scheduler node 可发现，因此 `hermes-0` 不 Ready 时不只是 Cron 暂停：FastAPI 的 Hermes 池构造、`/ready` 和普通业务请求也会失败，直到同名 Pod 恢复；系统不会切换 leader 或把 owner 路由改绑到其他节点。
- FastAPI ServiceAccount 只拥有当前命名空间内 EndpointSlice 的 `list` 权限，无工作负载或 Secret 写权限。
- HPA 以 `xpd_agent_demand=xpd_agent_active+xpd_agent_waiting` 为主要业务指标，按每个 Hermes 约 7 个任务计算副本数，并以 Hermes CPU 利用率为兜底。这样等待队列会参与容量计算，不使用会导致跳变的单独 `Value` 指标。
- HPA 的 `scaleDown.selectPolicy` 为 `Disabled`，同时 `XPD_HERMES_ROUTE_REBINDING_ENABLED=false`：负载下降后保留已创建的 Hermes Pod 和稳定实例目录，不自动缩容，也不把已有节点粘性路由静默改绑到另一个独立状态目录。当前项目没有可以自动排空、迁移会话状态并原子改写 owner 路由的缩容控制器，因此禁止降低 StatefulSet 副本数或永久移除已有 ordinal。因故障重建同名 Pod 会复用原实例目录，不属于缩容；计划重启前仍须停止新流量并确认任务结束。只有在另行实现并验收完整的 drain/状态迁移/路由迁移工具后，才能开放真正的缩容操作。
- FastAPI 使用 `Recreate` 发布策略，确保任何时刻只有一个调度器；代价是 FastAPI 版本发布时会有短暂维护窗口。

## 三、ACK 前置条件

1. 安装 ACK Metrics Server。
2. 开通阿里云 Prometheus 监控，并安装/配置 ACK Prometheus Adapter，把 FastAPI `/metrics` 的 `xpd_agent_demand` 映射到 `external.metrics.k8s.io`。Service 已带 Prometheus 抓取注解；Adapter 查询必须限定 `xpd-report-agent` 命名空间和 Service，避免把多条副本序列重复相加。没有完成 Adapter 映射时流水线会失败，不能把清单中的指标名称直接当成已经生效。
3. 确认所用 CNI 支持 Kubernetes NetworkPolicy。
4. 准备一个容量足够的 ACK 工作节点。按清单最大 10 个 Hermes 估算，节点至少要覆盖所有 Pod 的资源 request，并为模型调用、Excel/PDF 生成和系统组件保留余量。
5. 准备支持 `ReadWriteOnce` 的 ACK 云盘 StorageClass，例如集群实际提供的 ESSD StorageClass。不要照抄示例名，应先运行 `kubectl get storageclass`。

检查自定义指标 API（具体 API 版本由 Adapter 决定）：

```bash
kubectl get apiservice | grep external.metrics
kubectl get --raw '/apis/external.metrics.k8s.io/v1beta1/namespaces/xpd-report-agent/xpd_agent_demand'
```

如果自定义指标暂时不可用，CPU 过载仍可触发扩容。自动缩容无论指标是否正常都保持关闭，这是保护稳定实例状态的预期配置。容量过大时先保留 Pod 并告警；在上述迁移控制器实现之前，不得通过 `kubectl scale`、调低 HPA 上限、启用 `scaleDown` 或其他方式永久减少 ordinal。仅删除某个 Pod 会由 StatefulSet 重建同名 Pod，并不会完成缩容或路由迁移。

## 四、首次初始化

只给一个节点加标签：

```bash
kubectl label node <ACK_NODE_NAME> xpd-report-agent=true --overwrite
kubectl get nodes -l xpd-report-agent=true
```

先创建命名空间：

```bash
kubectl apply -f deploy/kubernetes/namespace.yaml
```

`secret.example.yaml` 仅说明必需字段，没有加入 `kustomization.yaml`。推荐用 External Secrets/阿里云密钥管理服务同步 `xpd-report-agent-secrets`。如果由 CI 创建，真实值只能来自 CI Secret，并写入权限为 0600 的临时 env 文件；不要提交该文件，也不要在日志中打印：

```bash
kubectl -n xpd-report-agent create secret generic xpd-report-agent-secrets \
  --from-env-file="$CI_SECRET_ENV_FILE" \
  --dry-run=client -o yaml | kubectl apply -f -
```

私有镜像仓库认证也应由 ACK 节点 RAM 权限、ACK免密组件或 CI 创建的 `imagePullSecret` 提供，不要把仓库密码写进脚本和 YAML。

## 五、一条流水线发布

本节可直接用于全新无历史数据的环境或已完成迁移的环境。若需要把旧 ECS/systemd 状态带入 ACK，
不要先运行本节流水线，因为它会立即创建并启动 FastAPI、Hermes 和 HPA；应先完成第六节的离线迁移，
再回到本节发布镜像和其余工作负载。

已有环境在发布前必须先停止接收新任务，并在构建和 apply 前紧邻执行下面的失败关闭门禁。
只有首次部署、集群中尚不存在 Deployment 时才跳过：

```bash
kubectl -n xpd-report-agent exec deployment/xpd-report-agent -- \
  /app/.venv/bin/python -c '
import json, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open("http://127.0.0.1:8000/health", timeout=5) as response:
    active = int(json.load(response)["agent_runs"]["active_tasks"])
print(f"active_tasks={active}")
raise SystemExit(0 if active == 0 else 1)
'
```

`pipeline-deploy.sh` 是平台无关的 Bash 入口，可以放进阿里云云效、GitHub Actions、GitLab CI 或其他 CI。CI 运行器需要预先完成镜像仓库登录并配置 kubeconfig。脚本会：

1. 校验干净 Git 工作区、完整提交号、Secret、WFFC StorageClass，以及唯一节点的 Ready/可调度状态。
2. 在集群中创建互斥发布锁，防止两条流水线交叉覆盖版本。
3. 以同一个 Git SHA 分别构建、推送 FastAPI 与 Hermes 镜像，并从 Buildx 结果读取不可变摘要。
4. 用镜像摘要渲染清单，先做 API Server dry-run，再通过一次实际 `kubectl apply` 发布全部资源。
5. 等待 Deployment、StatefulSet 和 Hermes Pod 就绪，通过端口转发执行 `/ready` 冒烟，并确认 `xpd_agent_demand` 已由 External Metrics API 提供。

这是“一次 apply”，不是 FastAPI 和 Hermes 的原子同时切换。Deployment 和 StatefulSet 由两个控制器
并发协调，发布期间可能短暂出现“新 FastAPI + 旧 Hermes”或相反组合。只有前后两个版本的内部协议、
状态格式和配置都双向兼容时，才能使用该流程在线更新。不兼容发布必须安排维护窗口，停止入口流量、
确认 `active_tasks=0`，并使用经单独测试的分阶段发布/回滚方案，不能直接运行本脚本。

Hermes Pod 的 `preStop` 保留 300 秒，StatefulSet 滚动更新会按 Pod 逐个等待。默认
`ROLLOUT_TIMEOUT=15m` 只适合小副本池，不能用于已扩到较多 Pod 的环境。发布前应按当前副本数预留
“每副本最多 10 分钟 + 10 分钟缓冲”，例如：

```bash
HERMES_REPLICAS="$(kubectl -n xpd-report-agent get statefulset hermes \
  -o jsonpath='{.spec.replicas}' 2>/dev/null || printf '2')"
export ROLLOUT_TIMEOUT="$((HERMES_REPLICAS * 10 + 10))m"
```

```bash
export IMAGE_REGISTRY=registry.cn-hangzhou.aliyuncs.com/<命名空间>
export ACK_STORAGE_CLASS=<ACK_RWO_STORAGE_CLASS>
export GIT_SHA="$CI_COMMIT_SHA"
./deploy/kubernetes/pipeline-deploy.sh
```

脚本推送 Git SHA 标签、不使用 `latest`，工作负载实际固定到镜像 digest；ACR 还应启用标签不可变策略。`IMAGE_REGISTRY`、`ACK_STORAGE_CLASS` 可以是普通 CI 变量；仓库密码、kubeconfig 和应用配置必须是受保护的 CI Secret。脚本不会创建或打印真实 Secret。CI 平台也应为本环境配置并发组；集群锁异常残留时，必须先确认没有发布任务运行，再人工删除 `xpd-report-agent-deploy-lock`。

脚本成功只代表基础设施发布、`/ready` 和 HPA 指标链路通过，不代表模型、真实 RDS 查询、澄清续答、
Excel 生成或 OSS 下载已验收。流水线应在该脚本之后追加第七节的真实业务验收 job；该 job 成功前不得
将新版本标记为可切流或删除上一版镜像和快照。

若不用流水线，应先复制整套清单到受控环境，替换 `REPLACE_FASTAPI_IMAGE:REPLACE_TAG`、`REPLACE_HERMES_IMAGE:REPLACE_TAG` 和 `REPLACE_ACK_RWO_STORAGE_CLASS`，然后按“Namespace → Secret → `kubectl apply -k deploy/kubernetes`”的顺序执行。

## 六、旧数据迁移与备份

迁移不是流水线的一部分，清单也不会自动复制、移动、改写或删除旧状态。必须根据来源拓扑选择下面
两套流程之一，不得把单实例流程用在当前 ECS 三实例卷上。

### 六.1 通用离线前置条件

1. 先停止入口新流量，通过旧 FastAPI `/health` 确认 `agent_runs.active_tasks=0`。
2. 停止旧 FastAPI 和全部 Hermes，并确认不再有进程写入 SQLite/JSON/报表目录。从这一步起，直到迁移校验
   完成，新旧两套服务都不得对这份状态提供写入流量。
3. 离线备份完整 Hermes 共享根目录和报表目录，包括 SQLite 主文件、`-wal`、`-shm`、路由状态、用户记忆和
   FastAPI 持久化任务状态。生成文件清单和校验值，备份保持只读。
4. 预先只创建两个空 PVC，不启动 FastAPI、Hermes 或 HPA。设置真实 StorageClass 后可单独执行：

   ```bash
   export ACK_STORAGE_CLASS=<ACK_RWO_STORAGE_CLASS>
   sed "s|REPLACE_ACK_RWO_STORAGE_CLASS|${ACK_STORAGE_CLASS}|g" \
     deploy/kubernetes/storage.yaml | kubectl apply -f -
   ```

   一次性迁移 Pod 应将来源备份以只读方式挂载，将目标 PVC 以读写方式挂载；目标不为空时必须拒绝覆盖。
   由于 PVC 是 WFFC/RWO，迁移 Pod 必须包含 `nodeSelector: {xpd-report-agent: "true"}`，并在启动前确认
   集群中只有一个节点带该标签；否则云盘可能绑定到错误节点或可用区。
5. 不复制 Gateway PID/锁/瞬时状态、Cron PID、`processes.json`、`gateway-starts.log`、旧 `.env`、日志、
   `.xpd-bootstrap.lock` 和 `state/gateway.heartbeat`。不得只复制 `.db` 主文件而遗漏已备份的 WAL/SHM。
6. 迁移完成后将文件属主/属组设为镜像使用的 `999:999`，目录和密钥权限保持最小化，再对新 PVC 创建切流前
   云盘快照。

### 六.2 旧单 Hermes 实例 → ACK

适用于旧单容器或 `1 FastAPI + 1 Hermes`，且会话/Cron 状态仍直接位于旧共享根目录、没有三个
`instances/hermes-N/` 的环境。

1. 将共享数据 `memories/`、`scripts/`、`xpd-report-agent/` 和报表目录分别复制到新共享根和
   `report-files` PVC。
2. 将旧根目录中的会话、Cron 和其他 Hermes 实例状态复制到空的 `instances/hermes-0/`，同时排除第
   六.1 节列出的瞬时文件和已单独复制的共享目录。旧 native Cron 的 `cron/` 状态必须跟随此步骤进入
   `hermes-0`，因为 ACK 中只有 `hermes-0` 是 scheduler leader。
3. 检查 `<shared-root>/xpd-report-agent/hermes-routes.json`。如果存在，其 `version` 必须为 `1`，`scopes` 和
   `sessions` 必须是对象，且所有旧路由值只能引用同一个旧节点。用 JSON 解析器把这两个对象的所有值原子改写为
   `hermes-0`；不得用 `sed` 直接改 JSON，发现多个旧节点时立即中止并改用下一节流程。
4. 启动 ACK 的最小两个 Hermes：`hermes-0` 承接旧用户与 Cron，`hermes-1` 从空实例目录启动并承接
   后续新用户。

### 六.3 当前 ECS `1 FastAPI + 3 Hermes` → ACK

当前 Compose 卷中已存在 `instances/hermes-1`、`instances/hermes-2`、`instances/hermes-3`，不能把共享根再整体复制到
`instances/hermes-0`。迁移必须使用以下一对一映射：

| ECS 节点 | ACK 节点 | 说明 |
|---|---|---|
| `hermes-1` | `hermes-0` | 连同原 Cron leader 状态一起迁移，ACK 中继续作为 leader |
| `hermes-2` | `hermes-1` | 保留该节点的会话和本地任务状态 |
| `hermes-3` | `hermes-2` | 保留该节点的会话和本地任务状态 |

1. 只复制一份共享 `memories/`、`scripts/`、`xpd-report-agent/` 和报表目录。
2. 从只读备份中把每个 ECS 实例目录复制到表中对应的空 ACK 实例目录，并对每个目录应用第六.1 节
   的瞬时文件排除规则。不得合并三份 `state.db`，也不得让两个来源覆盖同一个目标。
3. 使用 JSON 解析器原子改写 `<shared-root>/xpd-report-agent/hermes-routes.json`：对 `scopes` 和 `sessions` 两个
   对象的每个 value 应用上表映射。改写前必须确认 `version=1`；任何 value 不是 `hermes-1/2/3` 之一时
   中止迁移，不得猜测节点归属。先写同目录临时文件、`fsync`，再通过原子 rename 替换原文件。
4. 首次切流前必须让 `hermes-0`、`hermes-1`、`hermes-2` 三个 Pod 全部 Ready。当前 HPA 最小值是 2，因此在不开放
   入口流量的情况下显式向上扩到 3：

   ```bash
   kubectl -n xpd-report-agent scale statefulset/hermes --replicas=3
   kubectl -n xpd-report-agent rollout status statefulset/hermes --timeout=30m
   ```

   `scaleDown` 已禁用，HPA 不会在负载下降时自动删除这第三个 Pod。

### 六.4 切流校验与回滚边界

无论使用哪套流程，切流前都必须完成：

- 解析迁移后的 `hermes-routes.json`，确认 `version=1`、`scopes/sessions` 结构有效，所有路由 value 都属于实际
  Ready 的 ACK Hermes Pod。
- 核对每个实例目录的 SQLite 主文件、WAL/SHM、文件数量和备份校验值，确认 `999:999` 可读写；确认
  只有 `hermes-0` 加载 native Cron leader 状态。
- 确认所有 Pod 在同一个节点，`/ready` 返回 200，且使用每个来源 Hermes 上至少一个代表用户读取旧会话/消息成功。
- 执行第七节的真实 RDS 任务和 Excel/OSS 下载验收，再开放中台流量。

切流前回滚必须按顺序停止 ACK 写入：先删除或暂停 `hermes` HPA，再把 FastAPI Deployment 和 Hermes
StatefulSet 缩到 0，等待相关 Pod 全部退出，最后确认新 PVC 没有挂载写入者，才能重启仍保持停止且
未被修改的旧 ECS/systemd 环境。例如：

```bash
kubectl -n xpd-report-agent delete hpa hermes --ignore-not-found
kubectl -n xpd-report-agent scale deployment/xpd-report-agent --replicas=0
kubectl -n xpd-report-agent scale statefulset/hermes --replicas=0
kubectl -n xpd-report-agent wait --for=delete pod \
  -l app.kubernetes.io/part-of=xpd-report-agent --timeout=10m
test -z "$(kubectl -n xpd-report-agent get pod \
  -l app.kubernetes.io/part-of=xpd-report-agent -o name)"
```

不得先缩 StatefulSet 再处理 HPA，否则 HPA 可能立即重新拉起 Hermes。任何时候都不得让新旧 Hermes
同时写入同一份状态。

切流后如果 ACK 已产生新会话、记忆、Run 或报表，旧 ECS 卷已经落后，不能直接重启当作无损回滚。必须先停止
ACK 写入，再选择“接受丢弃切流后数据并恢复切流前快照”或“执行经测试的反向迁移”。旧卷、离线备份和新 PVC
快照至少保留一个完整业务回滚窗口。

## 七、部署验证

```bash
kubectl -n xpd-report-agent get deploy,statefulset,pod,hpa,pdb -o wide
kubectl -n xpd-report-agent get endpointslice -l kubernetes.io/service-name=hermes-headless
kubectl -n xpd-report-agent get networkpolicy
kubectl auth can-i list endpointslices.discovery.k8s.io \
  --as=system:serviceaccount:xpd-report-agent:xpd-report-agent \
  -n xpd-report-agent
kubectl auth can-i get secrets \
  --as=system:serviceaccount:xpd-report-agent:xpd-report-agent \
  -n xpd-report-agent
kubectl -n xpd-report-agent get pods \
  -l app.kubernetes.io/part-of=xpd-report-agent \
  -o custom-columns='NAME:.metadata.name,READY:.status.containerStatuses[0].ready,IMAGE:.spec.containers[0].image,IMAGE_ID:.status.containerStatuses[0].imageID,NODE:.spec.nodeName'
```

确认所有 FastAPI/Hermes Pod 的 `NODE` 列完全相同，第二条权限检查应返回 `yes`，Secret 检查应返回 `no`。`hermes-headless` 必须显示 `CLUSTER-IP: None`，且不存在 Hermes LoadBalancer/NodePort；只有 FastAPI 8000 拥有私网 LoadBalancer。若调用方跨越 VPC 或其他信任边界，应在前置 API 网关/Ingress 终止 TLS，不要把这个私网 HTTP Service 改成公网裸露。

镜像核对输出中不得再出现 `REPLACE_` 或可变的 `latest` 标签；FastAPI 和全部 Hermes 的 `IMAGE`
必须是本次流水线渲染的两个镜像 digest，同一类 Pod 的 `IMAGE_ID` 必须一致且 `READY=true`。
如果镜像仓库使用 manifest list，CRI 显示的 `IMAGE_ID` 可以是对应平台的子 manifest digest；这时应在 ACR
中核对它属于 `IMAGE` 指定的顶层 digest，不得只根据 Pod 创建时间猜测已切换新版。

`/ready=200` 只验证至少一个 Hermes runtime 和 MySQL 连接，不保证所有 owner 的绑定 Pod 可用，也不
调用模型或 OSS。正式切流前还必须按 [`../../docs/api/middle-platform-agent-api.md`](../../docs/api/middle-platform-agent-api.md)
完成一次真实五接口验收：创建 Run、查询状态、SSE 收到终态、至少一次澄清续答，以及生成并
下载可正常打开的 Excel。Run 必须实际查询 RDS，不得用纯模型问答代替。

压测时至少观察：`xpd_agent_active`、`xpd_agent_waiting`、HPA desired replicas、Hermes CPU/内存、FastAPI 等待时间、模型 429/超时、RDS 活跃连接和慢查询。只有等待队列能回落且错误率满足目标，才能认定承载约 20 人并发。

## 八、回滚

镜像使用 Git SHA，可直接回到上一版本：

```bash
kubectl -n xpd-report-agent rollout undo deployment/xpd-report-agent
kubectl -n xpd-report-agent rollout undo statefulset/hermes
kubectl -n xpd-report-agent rollout status deployment/xpd-report-agent \
  --timeout="${ROLLOUT_TIMEOUT:-30m}"
kubectl -n xpd-report-agent rollout status statefulset/hermes \
  --timeout="${ROLLOUT_TIMEOUT:-30m}"
```

回滚前同样必须停止新流量并确认 `active_tasks=0`；上述两个 `rollout undo` 也不是原子操作。回滚后必须
按第七节重新核对 FastAPI/Hermes 的镜像 digest、Pod 就绪状态和真实业务链路。若新版本已经迁移或修改数据格式，
应先停止写入并按对应版本的迁移说明恢复云盘快照。不要删除 PVC 来“回滚”，也不要在数据格式不兼容时仅回滚镜像。
