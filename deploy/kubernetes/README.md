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
- `hermes-0` 是定时任务 leader。最小副本数为 2，HPA 只会扩容而不会自动删除已有 Pod；leader 不可用期间定时任务暂停，不会切换到另一个节点重复执行。
- FastAPI ServiceAccount 只拥有当前命名空间内 EndpointSlice 的 `get/list/watch` 权限，无工作负载或 Secret 写权限。
- HPA 以 `xpd_agent_demand=xpd_agent_active+xpd_agent_waiting` 为主要业务指标，按每个 Hermes 约 7 个任务计算副本数，并以 Hermes CPU 利用率为兜底。这样等待队列会参与容量计算，不使用会导致跳变的单独 `Value` 指标。
- HPA 的 `scaleDown.selectPolicy` 为 `Disabled`，同时 `XPD_HERMES_ROUTE_REBINDING_ENABLED=false`：负载下降后保留已创建的 Hermes Pod 和稳定实例目录，不自动缩容，也不把已有节点粘性路由静默改绑到另一个独立状态目录。确需人工缩容时，必须先排空目标 Pod，并完成节点粘性路由和实例状态迁移；未完成迁移不得删除副本。
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

如果自定义指标暂时不可用，CPU 过载仍可触发扩容。自动缩容无论指标是否正常都保持关闭，这是保护稳定实例状态的预期配置；应结合容量告警，由运维人员评估是否需要在排空和迁移后人工缩容。

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

`pipeline-deploy.sh` 是平台无关的 Bash 入口，可以放进阿里云云效、GitHub Actions、GitLab CI 或其他 CI。CI 运行器需要预先完成镜像仓库登录并配置 kubeconfig。脚本会：

1. 校验干净 Git 工作区、完整提交号、Secret、WFFC StorageClass，以及唯一节点的 Ready/可调度状态。
2. 在集群中创建互斥发布锁，防止两条流水线交叉覆盖版本。
3. 以同一个 Git SHA 分别构建、推送 FastAPI 与 Hermes 镜像，并从 Buildx 结果读取不可变摘要。
4. 用镜像摘要渲染清单，先做 API Server dry-run，再通过一次实际 `kubectl apply` 发布全部资源。
5. 等待 Deployment、StatefulSet 和 Hermes Pod 就绪，通过端口转发执行 `/ready` 冒烟，并确认 `xpd_agent_demand` 已由 External Metrics API 提供。

```bash
export IMAGE_REGISTRY=registry.cn-hangzhou.aliyuncs.com/<命名空间>
export ACK_STORAGE_CLASS=<ACK_RWO_STORAGE_CLASS>
export GIT_SHA="$CI_COMMIT_SHA"
./deploy/kubernetes/pipeline-deploy.sh
```

脚本推送 Git SHA 标签、不使用 `latest`，工作负载实际固定到镜像 digest；ACR 还应启用标签不可变策略。`IMAGE_REGISTRY`、`ACK_STORAGE_CLASS` 可以是普通 CI 变量；仓库密码、kubeconfig 和应用配置必须是受保护的 CI Secret。脚本不会创建或打印真实 Secret。CI 平台也应为本环境配置并发组；集群锁异常残留时，必须先确认没有发布任务运行，再人工删除 `xpd-report-agent-deploy-lock`。

若不用流水线，应先复制整套清单到受控环境，替换 `REPLACE_FASTAPI_IMAGE:REPLACE_TAG`、`REPLACE_HERMES_IMAGE:REPLACE_TAG` 和 `REPLACE_ACK_RWO_STORAGE_CLASS`，然后按“Namespace → Secret → `kubectl apply -k deploy/kubernetes`”的顺序执行。

## 六、旧数据迁移与备份

迁移前必须停止旧环境的 FastAPI 和全部 Hermes，确认没有任务写入后再做一致性备份：

1. 备份旧 `HERMES_HOME` 的完整目录，包括 SQLite 的 `-wal`、`-shm` 文件和路由状态。
2. 备份旧报表目录；若报表已全部上传 OSS，也要保留元数据和迁移清单。
3. 对两个新 PVC 创建云盘快照，然后用一次性迁移 Pod 把完整旧目录恢复到 `hermes-state` 共享根。
4. 在所有 FastAPI/Hermes 进程仍保持停止时，将旧共享根中的 Hermes 会话、Cron 和其他实例状态复制到空的 `instances/hermes-0`。复制时必须排除 `instances/`、`xpd-report-agent/`、`memories/`、`scripts/`、Gateway PID/锁/状态、Cron PID、`processes.json`、`gateway-starts.log`、旧 `.env`、日志和 `state/gateway.heartbeat`；共享的 `memories/`、`scripts/` 与 FastAPI 状态继续保留在根目录。
5. 校验文件属主可被镜像中的非 root 用户读写，并核对 SQLite 主文件及其 WAL/SHM，再启动工作负载。
6. 首次切流后保留旧卷和快照，至少覆盖一个业务回滚窗口。

清单不会自动复制、移动或删除旧状态；这项离线迁移必须由一次性迁移 Pod 人工执行并验证。禁止在旧服务仍运行时复制 SQLite，也不要只复制主 `.db` 文件而遗漏 WAL 文件。旧会话和 Cron 状态归入 `hermes-0`，其他 Hermes Pod 从各自的新实例目录启动。

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
```

确认所有 FastAPI/Hermes Pod 的 `NODE` 列完全相同，第二条权限检查应返回 `yes`，Secret 检查应返回 `no`。`hermes-headless` 必须显示 `CLUSTER-IP: None`，且不存在 Hermes LoadBalancer/NodePort；只有 FastAPI 8000 拥有私网 LoadBalancer。若调用方跨越 VPC 或其他信任边界，应在前置 API 网关/Ingress 终止 TLS，不要把这个私网 HTTP Service 改成公网裸露。

压测时至少观察：`xpd_agent_active`、`xpd_agent_waiting`、HPA desired replicas、Hermes CPU/内存、FastAPI 等待时间、模型 429/超时、RDS 活跃连接和慢查询。只有等待队列能回落且错误率满足目标，才能认定承载约 20 人并发。

## 八、回滚

镜像使用 Git SHA，可直接回到上一版本：

```bash
kubectl -n xpd-report-agent rollout undo deployment/xpd-report-agent
kubectl -n xpd-report-agent rollout undo statefulset/hermes
kubectl -n xpd-report-agent rollout status deployment/xpd-report-agent --timeout=15m
kubectl -n xpd-report-agent rollout status statefulset/hermes --timeout=15m
```

若新版本已经迁移或修改数据格式，应先停止写入并按对应版本的迁移说明恢复云盘快照。不要删除 PVC 来“回滚”，也不要在数据格式不兼容时仅回滚镜像。
