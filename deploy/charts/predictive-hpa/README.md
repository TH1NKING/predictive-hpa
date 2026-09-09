# PredictiveHPA Helm chart

这个 chart 将项目的控制器、CRD、RBAC 和 ServiceAccount 部署到 Kubernetes。它用于本项目的学习、演示与实验；不会自动安装 Prometheus、业务 Deployment 或 PredictiveHPA 实例。

项目背景、控制器设计与实验结论见[项目首页](../../../README.md)。下面的命令从仓库根目录执行，使用 Bash；Windows 可在具备 Docker 访问能力的 WSL/Linux 环境执行。

## 在独立 Kind 集群中运行

需要 Docker、Kind、kubectl 和 Helm。以下示例使用 Kubernetes `v1.35.0`，与项目已有实验环境一致。`Chart.yaml` 中的 `kubeVersion` 下界不代表项目测试过所有较早的 Kubernetes 版本。

### 1. 创建集群并安装指标来源

```bash
kind create cluster --name predictive-hpa-demo --image kindest/node:v1.35.0
kubectl config use-context kind-predictive-hpa-demo

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install prometheus prometheus-community/prometheus \
  --version 29.5.0 \
  --kube-context kind-predictive-hpa-demo \
  --namespace monitoring --create-namespace \
  --values config/benchmark/prometheus-values.yaml \
  --wait --timeout 5m
```

这里沿用项目环境记录中的 Prometheus chart 版本。配置启用 kube-state-metrics，抓取间隔为 15 秒，关闭实验不需要的组件；Prometheus 使用临时存储。

控制器需要能够从 Prometheus 查询到以下两类指标：

- cAdvisor 的 `container_cpu_usage_seconds_total`，用于计算 CPU 使用量。
- kube-state-metrics 的 `kube_pod_container_resource_requests{resource="cpu"}`，用于将使用量换算为 CPU request 百分比。

因此目标容器必须配置 CPU requests。仅安装 metrics-server 不能满足当前 provider 的查询需求。如果使用已有 Prometheus，请确认它采集了上述指标，并在安装控制器时替换 `prometheus.url`。

### 2. 构建并部署当前代码

```bash
docker build -t predictive-hpa:dev .
kind load docker-image predictive-hpa:dev --name predictive-hpa-demo

helm upgrade --install predictive-hpa ./deploy/charts/predictive-hpa \
  --kube-context kind-predictive-hpa-demo \
  --namespace predictive-hpa-system --create-namespace \
  --set image.repository=predictive-hpa \
  --set image.tag=dev \
  --set image.pullPolicy=Never \
  --set prometheus.url=http://prometheus-server.monitoring.svc:80 \
  --wait --timeout 2m
```

明确指定本地镜像，是为了让运行版本对应当前源码。chart 默认会引用 `ghcr.io/th1nking/predictive-hpa:0.1.0`；仓库目前没有镜像发布 workflow，不能仅凭这个默认值认定镜像已发布或包含当前功能。在远端集群部署时，应先推送自己的镜像，再设置对应仓库、tag 和拉取策略。

上面的 release 名为 `predictive-hpa`，对应的控制器 Deployment 也叫 `predictive-hpa`。若更改 release 名，后续排查命令中的 Deployment 名需要一起调整。

### 3. 创建示例业务与伸缩策略

```bash
kubectl --context kind-predictive-hpa-demo apply -n default \
  -f config/benchmark/php-apache.yaml
kubectl --context kind-predictive-hpa-demo rollout status -n default \
  deployment/php-apache --timeout=2m

kubectl --context kind-predictive-hpa-demo apply \
  -f config/samples/autoscaling_v1alpha1_predictivehpa.yaml
kubectl --context kind-predictive-hpa-demo get phpa -n default
```

示例 PHPA 的 namespace 是 `default`，目标也是该 namespace 的 `php-apache` Deployment。控制器自身部署在 `predictive-hpa-system`，不要求与业务同 namespace。不要让原生 HPA 或另一个 PHPA 同时控制该 Deployment。

示例使用 `Predictive` 模式、50% CPU 目标、1–10 副本、5 分钟查询窗口、30 秒预测时距和 60 秒缩容稳定窗口。没有施加负载时，观察到低 CPU 或保持 1 副本是正常现象。正式压测请使用[仓库中的实验方案](../../../docs/benchmarks/controlled-pilot.md)。

### 4. 检查控制器是否进入决策流程

```bash
kubectl --context kind-predictive-hpa-demo logs -n predictive-hpa-system \
  deployment/predictive-hpa --tail=100
kubectl --context kind-predictive-hpa-demo get phpa predictivehpa-sample \
  -n default -o yaml
```

日志中的 `Queried CPU utilization`、`Evaluated PredictiveHPA scaling decision` 和 `Reconciled PredictiveHPA` 可用于核对查询与决策；成功写入目标副本数时会记录 `Scaled Deployment`。PHPA status 包含当前/预测 CPU、当前/期望副本数与 `ScaleDownStabilized` condition。

`Metrics not yet available` 或 `Insufficient samples for prediction` 表示当前轮次不会写入伸缩目标，控制器会重试。若持续出现，检查 Prometheus 地址、抓取目标及 CPU requests。Pod 为 Running、Helm `--wait` 完成，只能说明部署层面的状态，不能单独证明指标链路和伸缩决策正常。

## 部署参数与实例参数

完整默认值见 [values.yaml](values.yaml)。

| 参数 | 用途 | 默认值 |
|---|---|---|
| `image.repository` | 控制器镜像仓库 | `ghcr.io/th1nking/predictive-hpa` |
| `image.tag` | 镜像 tag；空值取 `Chart.yaml` 的 `appVersion` | `""`，当前解析为 `0.1.0` |
| `image.pullPolicy` | 镜像拉取策略 | `IfNotPresent` |
| `replicaCount` | 控制器副本数；当前应保持单副本 | `1` |
| `resources` | 控制器 requests/limits | CPU `50m`/`200m`，内存 `64Mi`/`256Mi` |
| `prometheus.url` | 控制器访问的 Prometheus 查询端点 | `http://prometheus-server.monitoring.svc:80` |
| `installCRD` | 是否由本 release 管理 CRD | `true` |
| `rbac.create` | 是否创建 ClusterRole/ClusterRoleBinding | `true` |
| `serviceAccount.create` | 是否创建 ServiceAccount | `true` |
| `serviceAccount.name` | 自定义 ServiceAccount 名称 | `""` |
| `metricsService.enabled` | 为控制器自身的 metrics 启用监听与 Service | `false` |
| `metricsService.port` | 控制器 metrics 端口 | `8080` |

`prediction.alphaPercent`、`prediction.window`、`prediction.horizon`、`decisionMode` 和缩容稳定窗口属于每个 PredictiveHPA 实例的 spec，不是 Helm values。一个控制器可以服务多个参数不同的实例。

| `spec.decisionMode` | 使用的决策信号 |
|---|---|
| `Predictive`（默认） | 经过边界约束的 EWMA 预测值 |
| `Current` | 最近一次查询得到的 CPU 值 |
| `Hybrid` | 当前 CPU 驱动扩容，预测值辅助保守缩容 |

三个模式共用指标查询、预测样本门槛、副本上下界、容忍区间与稳定窗口。`Current` 仍需要完整的 `prediction` 配置。更新实例使用新字段前，需要同时使用支持该字段的 CRD 和控制器镜像。

## 当前部署边界

- **保持单副本。** chart 没有启用 `--leader-elect`，也没有配置 Lease 所需的 RBAC。模板允许修改 `replicaCount`，但提高副本数会让多个控制器同时工作。二进制虽有 leader election 选项，缩容建议历史仍保存在进程内存中，重启或领导者切换不能恢复先前窗口；第一次缩容可能缺少历史保护。
- **Deployment + CPU + EWMA。** 目标必须与 PHPA 同 namespace。当前不支持 StatefulSet、内存/自定义指标或 scale-to-zero；`minReplicas: 0` 在计算时按 1 处理。
- **指标匹配有假设。** 查询使用 `pod=~"<deployment>-.*"` 匹配 Pod，当前没有按 owner reference 精确关联。已有实验以单容器、明确 CPU request 的业务为主；多容器业务与同名前缀工作负载需要额外验证。
- **metrics 开关不等于监控接入完成。** 二进制默认通过 HTTPS 和鉴权暴露自身 metrics，即使配置端口为 8080 也不会自动变成 HTTP。chart 尚未提供完整的 TokenReview/SubjectAccessReview 权限、metrics 读取授权、证书配置与 ServiceMonitor；默认演示流程保持关闭。`prometheus.url` 则是控制器读取业务指标的入口，两者用途不同。
- **Helm 与 Kustomize 尚未完全对齐。** chart 当前没有配置健康探针；仓库的 Kind E2E 使用 Kustomize，验证 manager 运行与 metrics 端点，不能作为 chart 完整部署流程或真实扩缩容效果的验收证据。

## CRD 更新与维护

CRD schema 的源头是 [API 类型与 markers](../../../api/v1alpha1/predictivehpa_types.go)，由 `make manifests` 生成。chart 内的副本通过脚本同步，不手工修改：

```bash
make manifests generate
bash hack/sync-chart-crd.sh
helm lint ./deploy/charts/predictive-hpa
helm template predictive-hpa ./deploy/charts/predictive-hpa \
  --namespace predictive-hpa-system > /tmp/predictive-hpa-rendered.yaml
```

`templates/crd.yaml` 读取 `files/crd-predictivehpas.yaml`，因此 CRD 随 release 的 install/upgrade/uninstall 生命周期管理。需要让 CRD 独立于 release 时，应在首次安装前单独安装 CRD，并为 chart 设置 `installCRD=false`：

```bash
kubectl apply -f config/crd/bases/autoscaling.brian.io_predictivehpas.yaml
# 在首次 helm install / upgrade --install 命令中加入 --set installCRD=false
```

chart 的 [RBAC](templates/rbac.yaml) 是手写模板。修改控制器 RBAC markers 后，除了运行 `make manifests`，还应对照生成的 [config/rbac/role.yaml](../../../config/rbac/role.yaml) 检查 chart 权限。CRD 同步脚本不会同步 RBAC。

## 清理演示环境

默认 `installCRD=true` 时，下面的卸载操作会删除 CRD，以及集群中该类型的所有 PHPA 实例；业务 Deployment 不属于本 release，会保留。

```bash
helm uninstall predictive-hpa --namespace predictive-hpa-system \
  --kube-context kind-predictive-hpa-demo
```

如果只为本次演示创建了 `predictive-hpa-demo` 集群，也可以直接删除整个演示集群；这会一并清理示例业务与 Prometheus：

```bash
kind delete cluster --name predictive-hpa-demo
```
