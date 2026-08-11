from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KUBERNETES_DIR = PROJECT_ROOT / "deploy" / "kubernetes"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _documents(filename: str) -> list[dict]:
    return [
        document
        for document in yaml.safe_load_all(_read(KUBERNETES_DIR / filename))
        if isinstance(document, dict)
    ]


def _document(filename: str, kind: str, name: str) -> dict:
    for document in _documents(filename):
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name:
            return document
    raise AssertionError(f"{filename} 中找不到 {kind}/{name}")


def _env_map(container: dict) -> dict[str, dict]:
    return {entry["name"]: entry for entry in container.get("env", [])}


def test_kustomization_contains_the_complete_ack_stack_but_not_secret_example():
    kustomization = yaml.safe_load(_read(KUBERNETES_DIR / "kustomization.yaml"))

    assert kustomization["namespace"] == "xpd-report-agent"
    assert set(kustomization["resources"]) == {
        "namespace.yaml",
        "configmap.yaml",
        "rbac.yaml",
        "storage.yaml",
        "services.yaml",
        "workloads.yaml",
        "hpa.yaml",
        "pdb.yaml",
        "networkpolicy.yaml",
    }
    assert "secret.example.yaml" not in kustomization["resources"]
    assert (KUBERNETES_DIR / "secret.example.yaml").is_file()
    secret_example = _document("secret.example.yaml", "Secret", "xpd-report-agent-secrets")
    assert all(
        not value
        or str(value).startswith("REPLACE_")
        or key
        in {
            "HERMES_LLM_PROVIDER",
            "HERMES_LLM_API_MODE",
            "HERMES_REQUIRE_LLM_API_KEY",
            "XPD_DB_PORT",
            "XPD_IDENTITY_MODE",
            "XPD_SERVICE_AUTH_ENABLED",
            "XPD_REPORT_OSS_ENABLED",
        }
        for key, value in secret_example["stringData"].items()
    )


def test_fastapi_is_one_public_replica_with_read_only_discovery_identity():
    deployment = _document("workloads.yaml", "Deployment", "xpd-report-agent")
    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    environment = _env_map(container)
    public_service = _document("services.yaml", "Service", "xpd-report-agent")

    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert pod_spec["serviceAccountName"] == "xpd-report-agent"
    assert pod_spec["nodeSelector"] == {"xpd-report-agent": "true"}
    assert container["image"] == "REPLACE_FASTAPI_IMAGE:REPLACE_TAG"
    assert container["ports"] == [{"name": "http", "containerPort": 8000, "protocol": "TCP"}]
    assert environment["XPD_CONTAINER_ROLE"]["value"] == "fastapi"
    assert environment["XPD_K8S_NAMESPACE"]["valueFrom"]["fieldRef"]["fieldPath"] == (
        "metadata.namespace"
    )
    assert public_service["spec"]["type"] == "LoadBalancer"
    assert public_service["spec"]["ports"][0]["port"] == 8000
    assert public_service["metadata"]["annotations"] == {
        "service.beta.kubernetes.io/alibaba-cloud-loadbalancer-address-type": "intranet",
        "prometheus.io/scrape": "true",
        "prometheus.io/path": "/metrics",
        "prometheus.io/port": "8000",
    }

    role = _document("rbac.yaml", "Role", "xpd-report-agent-endpointslice-reader")
    assert role["rules"] == [
        {
            "apiGroups": ["discovery.k8s.io"],
            "resources": ["endpointslices"],
            "verbs": ["list"],
        }
    ]
    binding = _document("rbac.yaml", "RoleBinding", "xpd-report-agent-endpointslice-reader")
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "xpd-report-agent",
            "namespace": "xpd-report-agent",
        }
    ]


def test_hermes_statefulset_has_stable_identity_and_forced_single_node_topology():
    statefulset = _document("workloads.yaml", "StatefulSet", "hermes")
    pod_spec = statefulset["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    environment = _env_map(container)
    required_affinity = pod_spec["affinity"]["podAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]

    assert statefulset["spec"]["serviceName"] == "hermes-headless"
    assert "replicas" not in statefulset["spec"]
    assert statefulset["spec"]["podManagementPolicy"] == "Parallel"
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["nodeSelector"] == {"xpd-report-agent": "true"}
    assert required_affinity == [
        {
            "labelSelector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "xpd-report-agent",
                    "app.kubernetes.io/part-of": "xpd-report-agent",
                }
            },
            "topologyKey": "kubernetes.io/hostname",
        }
    ]
    assert container["image"] == "REPLACE_HERMES_IMAGE:REPLACE_TAG"
    assert container["ports"] == [{"name": "http", "containerPort": 8642, "protocol": "TCP"}]
    assert environment["POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
    assert environment["XPD_HERMES_NODE_ID"]["valueFrom"]["fieldRef"]["fieldPath"] == (
        "metadata.name"
    )
    startup = container["args"][0]
    assert 'export HERMES_HOME="${XPD_HERMES_SHARED_HOME}/instances/${POD_NAME}"' in startup
    assert '"$POD_NAME" = "$XPD_HERMES_SCHEDULER_NODE"' in startup
    assert "export XPD_HERMES_CRON_PATCH=false" in startup
    assert pod_spec["terminationGracePeriodSeconds"] == 420
    assert container["lifecycle"]["preStop"]["exec"]["command"] == [
        "/bin/sh",
        "-c",
        "sleep 300",
    ]


def test_discovery_and_scheduler_configuration_uses_endpoint_slices_and_hermes_zero():
    config = _document("configmap.yaml", "ConfigMap", "xpd-report-agent-config")["data"]
    headless = _document("services.yaml", "Service", "hermes-headless")

    assert config["XPD_HERMES_DISCOVERY_MODE"] == "kubernetes"
    assert config["XPD_K8S_HERMES_SERVICE"] == "hermes-headless"
    assert config["XPD_K8S_HERMES_PORT"] == "8642"
    assert config["XPD_HERMES_SCHEDULER_NODE"] == "hermes-0"
    assert config["XPD_HERMES_ROUTE_REBINDING_ENABLED"] == "false"
    assert config["XPD_AGENT_MAX_CONCURRENCY"] == "20"
    assert headless["spec"]["clusterIP"] == "None"
    assert headless["spec"]["ports"][0]["port"] == 8642
    assert "type" not in headless["spec"]


def test_shared_storage_is_rwo_and_mounted_by_both_workloads():
    claims = _documents("storage.yaml")
    assert {claim["metadata"]["name"] for claim in claims} == {
        "hermes-state",
        "report-files",
    }
    assert all(claim["spec"]["accessModes"] == ["ReadWriteOnce"] for claim in claims)
    assert all(
        claim["spec"]["storageClassName"] == "REPLACE_ACK_RWO_STORAGE_CLASS" for claim in claims
    )
    assert all(
        claim["metadata"]["annotations"]["xpd-report-agent/storage-scope"] == "single-node-only"
        for claim in claims
    )

    workloads = _documents("workloads.yaml")
    for workload in workloads:
        pod_spec = workload["spec"]["template"]["spec"]
        assert pod_spec["securityContext"] == {
            "runAsNonRoot": True,
            "runAsUser": 999,
            "runAsGroup": 999,
            "fsGroup": 999,
            "fsGroupChangePolicy": "OnRootMismatch",
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        assert {volume["persistentVolumeClaim"]["claimName"] for volume in pod_spec["volumes"]} == {
            "hermes-state",
            "report-files",
        }


def test_container_images_use_the_same_numeric_non_root_identity_as_ack():
    dockerfile = _read(PROJECT_ROOT / "deploy" / "docker" / "Dockerfile")

    assert "groupadd --system --gid 999 xpd-agent" in dockerfile
    assert "useradd --system --uid 999 --gid 999" in dockerfile
    assert dockerfile.count("USER 999:999") == 2
    assert "USER xpd-agent" not in dockerfile


def test_hpa_scales_hermes_up_but_never_discards_state_automatically():
    hpa = _document("hpa.yaml", "HorizontalPodAutoscaler", "hermes")["spec"]

    assert hpa["scaleTargetRef"] == {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "name": "hermes",
    }
    assert hpa["minReplicas"] == 2
    assert hpa["maxReplicas"] == 10
    assert hpa["behavior"]["scaleDown"] == {"selectPolicy": "Disabled"}
    assert {metric["type"] for metric in hpa["metrics"]} == {"External", "Resource"}

    external_metrics = {
        metric["external"]["metric"]["name"]: metric["external"]["target"]
        for metric in hpa["metrics"]
        if metric["type"] == "External"
    }
    assert external_metrics == {
        "xpd_agent_demand": {"type": "AverageValue", "averageValue": "7"},
    }
    cpu = next(metric for metric in hpa["metrics"] if metric["type"] == "Resource")
    assert cpu["resource"]["name"] == "cpu"
    assert cpu["resource"]["target"] == {
        "type": "Utilization",
        "averageUtilization": 65,
    }


def test_network_policy_allows_hermes_only_from_fastapi_on_8642():
    policy = _document("networkpolicy.yaml", "NetworkPolicy", "hermes-ingress-from-fastapi-only")[
        "spec"
    ]

    assert policy["policyTypes"] == ["Ingress"]
    assert policy["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "hermes"
    assert policy["ingress"] == [
        {
            "from": [
                {
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/name": "xpd-report-agent",
                            "app.kubernetes.io/part-of": "xpd-report-agent",
                        }
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8642}],
        }
    ]


def test_pod_disruption_budgets_cover_fastapi_and_hermes():
    budgets = _documents("pdb.yaml")

    assert {budget["metadata"]["name"] for budget in budgets} == {
        "hermes",
        "xpd-report-agent",
    }
    assert all(budget["spec"]["minAvailable"] == 1 for budget in budgets)


def test_single_pipeline_uses_one_sha_one_apply_and_verifies_rollout_and_ready():
    script_path = KUBERNETES_DIR / "pipeline-deploy.sh"
    script = _read(script_path)

    assert script_path.stat().st_mode & 0o111
    assert "set -euo pipefail" in script
    assert "set -x" not in script
    assert 'SOURCE_SHA="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"' in script
    assert 'GIT_SHA="${GIT_SHA:-$SOURCE_SHA}"' in script
    assert 'FASTAPI_IMAGE="${FASTAPI_REPOSITORY}:${GIT_SHA}"' in script
    assert 'HERMES_IMAGE="${HERMES_REPOSITORY}:${GIT_SHA}"' in script
    assert script.count("--target fastapi") == 1
    assert script.count("--target hermes") == 1
    assert script.count("--push") == 2
    assert script.count("kubectl apply ") == 2
    assert script.count("--dry-run=server") == 1
    assert "containerimage.digest" in script
    assert "xpd-report-agent-deploy-lock" in script
    assert "WaitForFirstConsumer" in script
    assert "xpd_agent_demand" in script
    assert "get secret xpd-report-agent-secrets" in script
    assert "kubectl create secret" not in script
    assert "REPLACE_FASTAPI_IMAGE:REPLACE_TAG" in script
    assert "REPLACE_HERMES_IMAGE:REPLACE_TAG" in script
    assert "REPLACE_ACK_RWO_STORAGE_CLASS" in script
    assert "rollout status deployment/xpd-report-agent" in script
    assert "rollout status statefulset/hermes" in script
    assert '"http://127.0.0.1:${SMOKE_PORT}/ready"' in script
    assert "trap cleanup EXIT INT TERM" in script
    assert "xpd-report-agent=true" in script


def test_readme_calls_out_adapter_single_node_migration_verification_and_rollback():
    readme = _read(KUBERNETES_DIR / "README.md")

    for expected in (
        "ACK Prometheus Adapter",
        "xpd_agent_active",
        "xpd_agent_waiting",
        "ReadWriteOnce",
        "严禁改成 NAS、NFS、OSSFS",
        "只能给一个专用 ACK 工作节点",
        "同一个 Git SHA",
        "迁移与备份",
        "部署验证",
        "回滚",
        "不能把清单中的指标名称直接当成已经生效",
    ):
        assert expected in readme
