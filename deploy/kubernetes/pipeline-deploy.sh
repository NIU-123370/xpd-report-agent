#!/usr/bin/env bash
set -euo pipefail

# 一条流水线完成：同一 Git SHA 构建并推送两个镜像、渲染清单、一次 apply、
# 等待发布以及 /ready 冒烟。镜像仓库登录和应用 Secret 必须由 CI 预先提供。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
KUBE_NAMESPACE="${KUBE_NAMESPACE:-xpd-report-agent}"
PLATFORMS="${PLATFORMS:-linux/amd64}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-15m}"
SMOKE_PORT="${SMOKE_PORT:-18000}"

fail() {
  echo "错误：$*" >&2
  exit 1
}

for command_name in docker git kubectl sed grep curl mktemp python3 wc tr tail sleep; do
  command -v "$command_name" >/dev/null 2>&1 || fail "缺少命令：$command_name"
done

: "${IMAGE_REGISTRY:?请设置 IMAGE_REGISTRY，例如 registry.cn-hangzhou.aliyuncs.com/your-namespace}"
: "${ACK_STORAGE_CLASS:?请设置 ACK_STORAGE_CLASS，例如 alicloud-disk-essd}"

case "$IMAGE_REGISTRY" in
  *[!a-zA-Z0-9._:/-]*|"") fail "IMAGE_REGISTRY 含有不安全字符" ;;
esac
case "$ACK_STORAGE_CLASS" in
  *[!a-z0-9.-]*|"") fail "ACK_STORAGE_CLASS 不是合法的 StorageClass 名称" ;;
esac
case "$SMOKE_PORT" in
  *[!0-9]*|"") fail "SMOKE_PORT 必须是数字" ;;
esac

SOURCE_SHA="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
GIT_SHA="${GIT_SHA:-$SOURCE_SHA}"
case "$GIT_SHA" in
  *[!0-9a-f]*|"") fail "GIT_SHA 必须是十六进制 Git 提交号" ;;
esac
if [ "${#GIT_SHA}" -ne 40 ] || [ "$GIT_SHA" != "$SOURCE_SHA" ]; then
  fail "GIT_SHA 必须是当前检出代码的完整 40 位提交号"
fi
if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain --untracked-files=all)" ]; then
  fail "Git 工作区不干净；流水线只能从无额外文件的提交构建"
fi

FASTAPI_REPOSITORY="${IMAGE_REGISTRY%/}/xpd-report-agent-fastapi"
HERMES_REPOSITORY="${IMAGE_REGISTRY%/}/xpd-report-agent-hermes"
FASTAPI_IMAGE="${FASTAPI_REPOSITORY}:${GIT_SHA}"
HERMES_IMAGE="${HERMES_REPOSITORY}:${GIT_SHA}"

if [ "$KUBE_NAMESPACE" != "xpd-report-agent" ]; then
  fail "当前清单固定使用命名空间 xpd-report-agent"
fi

kubectl get namespace "$KUBE_NAMESPACE" >/dev/null 2>&1 \
  || fail "命名空间不存在；请先应用 namespace.yaml"
kubectl --namespace "$KUBE_NAMESPACE" get secret xpd-report-agent-secrets >/dev/null 2>&1 \
  || fail "缺少 xpd-report-agent-secrets；请由 CI Secret 或 External Secrets 预先创建"
kubectl get storageclass "$ACK_STORAGE_CLASS" >/dev/null 2>&1 \
  || fail "StorageClass 不存在：$ACK_STORAGE_CLASS"
binding_mode="$(kubectl get storageclass "$ACK_STORAGE_CLASS" -o jsonpath='{.volumeBindingMode}')"
if [ "$binding_mode" != "WaitForFirstConsumer" ]; then
  fail "StorageClass 必须使用 WaitForFirstConsumer，当前为 ${binding_mode:-未设置}"
fi

eligible_nodes="$(kubectl get nodes -l xpd-report-agent=true -o name)"
eligible_count="$(printf '%s\n' "$eligible_nodes" | sed '/^$/d' | wc -l | tr -d ' ')"
if [ "$eligible_count" -ne 1 ]; then
  fail "必须且只能有一个节点带有 xpd-report-agent=true 标签，当前为 ${eligible_count} 个"
fi
eligible_node="${eligible_nodes#node/}"
node_ready="$(kubectl get node "$eligible_node" -o jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status}{end}')"
node_unschedulable="$(kubectl get node "$eligible_node" -o jsonpath='{.spec.unschedulable}')"
if [ "$node_ready" != "True" ] || [ "$node_unschedulable" = "true" ]; then
  fail "唯一标记节点必须处于 Ready 且可调度状态：${eligible_node}"
fi

rendered_manifest="$(mktemp -t xpd-report-agent-ack.XXXXXX.yaml)"
port_forward_log="$(mktemp -t xpd-report-agent-port-forward.XXXXXX.log)"
fastapi_metadata="$(mktemp -t xpd-report-agent-fastapi.XXXXXX.json)"
hermes_metadata="$(mktemp -t xpd-report-agent-hermes.XXXXXX.json)"
port_forward_pid=""
deploy_lock_acquired="false"

cleanup() {
  if [ -n "$port_forward_pid" ]; then
    kill "$port_forward_pid" >/dev/null 2>&1 || true
    wait "$port_forward_pid" >/dev/null 2>&1 || true
  fi
  if [ "$deploy_lock_acquired" = "true" ]; then
    kubectl --namespace "$KUBE_NAMESPACE" delete configmap \
      xpd-report-agent-deploy-lock --ignore-not-found >/dev/null 2>&1 || true
  fi
  rm -f "$rendered_manifest" "$port_forward_log" "$fastapi_metadata" "$hermes_metadata"
}
trap cleanup EXIT INT TERM

kubectl --namespace "$KUBE_NAMESPACE" create configmap xpd-report-agent-deploy-lock \
  --from-literal="git_sha=$GIT_SHA" >/dev/null 2>&1 \
  || fail "已有另一条 xpd-report-agent 发布流水线在运行；如确认是残留锁，请人工检查后删除"
deploy_lock_acquired="true"

docker buildx build \
  --platform "$PLATFORMS" \
  --file "$PROJECT_ROOT/deploy/docker/Dockerfile" \
  --target fastapi \
  --tag "$FASTAPI_IMAGE" \
  --metadata-file "$fastapi_metadata" \
  --push \
  "$PROJECT_ROOT"

docker buildx build \
  --platform "$PLATFORMS" \
  --file "$PROJECT_ROOT/deploy/docker/Dockerfile" \
  --target hermes \
  --build-context "hermes_seed=$PROJECT_ROOT/deploy/docker/hermes-seed" \
  --tag "$HERMES_IMAGE" \
  --metadata-file "$hermes_metadata" \
  --push \
  "$PROJECT_ROOT"

FASTAPI_DIGEST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$fastapi_metadata")"
HERMES_DIGEST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$hermes_metadata")"
case "$FASTAPI_DIGEST:$HERMES_DIGEST" in
  sha256:*:sha256:*) ;;
  *) fail "无法从 Buildx 元数据读取两个镜像摘要" ;;
esac
FASTAPI_DEPLOY_IMAGE="${FASTAPI_REPOSITORY}@${FASTAPI_DIGEST}"
HERMES_DEPLOY_IMAGE="${HERMES_REPOSITORY}@${HERMES_DIGEST}"

kubectl kustomize "$SCRIPT_DIR" \
  | sed \
      -e "s|REPLACE_FASTAPI_IMAGE:REPLACE_TAG|$FASTAPI_DEPLOY_IMAGE|g" \
      -e "s|REPLACE_HERMES_IMAGE:REPLACE_TAG|$HERMES_DEPLOY_IMAGE|g" \
      -e "s|REPLACE_ACK_RWO_STORAGE_CLASS|$ACK_STORAGE_CLASS|g" \
  > "$rendered_manifest"

if grep -q "REPLACE_" "$rendered_manifest"; then
  fail "渲染后的清单仍含有 REPLACE_ 占位符"
fi

# 先让 API Server 完成准入校验；随后保持单次实际 apply。
kubectl apply --server-side --dry-run=server \
  --field-manager=xpd-report-agent-ci -f "$rendered_manifest" >/dev/null
kubectl apply --server-side --field-manager=xpd-report-agent-ci -f "$rendered_manifest"

kubectl --namespace "$KUBE_NAMESPACE" rollout status deployment/xpd-report-agent \
  --timeout="$ROLLOUT_TIMEOUT"
kubectl --namespace "$KUBE_NAMESPACE" rollout status statefulset/hermes \
  --timeout="$ROLLOUT_TIMEOUT"
kubectl --namespace "$KUBE_NAMESPACE" wait \
  --for=condition=Ready pod \
  -l app.kubernetes.io/name=hermes \
  --timeout="$ROLLOUT_TIMEOUT"

kubectl --namespace "$KUBE_NAMESPACE" port-forward \
  --address 127.0.0.1 service/xpd-report-agent "${SMOKE_PORT}:8000" \
  > "$port_forward_log" 2>&1 &
port_forward_pid="$!"

ready="false"
for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
  if curl --fail --silent --show-error --max-time 5 \
    "http://127.0.0.1:${SMOKE_PORT}/ready" >/dev/null; then
    ready="true"
    break
  fi
  kill -0 "$port_forward_pid" >/dev/null 2>&1 \
    || fail "端口转发提前退出：$(tail -n 20 "$port_forward_log")"
  sleep 2
done

if [ "$ready" != "true" ]; then
  fail "FastAPI /ready 冒烟检查超时"
fi

metric_ready="false"
metric_path="/apis/external.metrics.k8s.io/v1beta1/namespaces/${KUBE_NAMESPACE}/xpd_agent_demand"
for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
  if metric_payload="$(kubectl get --raw "$metric_path" 2>/dev/null)" \
    && printf '%s' "$metric_payload" | grep -q '"value"'; then
    metric_ready="true"
    break
  fi
  sleep 2
done
if [ "$metric_ready" != "true" ]; then
  fail "ACK Prometheus Adapter 尚未提供 xpd_agent_demand，HPA 业务指标未闭环"
fi

echo "部署完成：FastAPI 与 Hermes 均来自提交 ${GIT_SHA}，工作负载已固定到镜像摘要"
