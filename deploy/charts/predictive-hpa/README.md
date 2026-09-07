# predictive-hpa Helm chart

部署 PredictiveHPA 控制器（含 CRD、RBAC、ServiceAccount）。

## 安装

```bash
helm install predictive-hpa ./deploy/charts/predictive-hpa \
  --namespace predictive-hpa-system --create-namespace \
  --set prometheus.url=http://prometheus-server.monitoring.svc:80
```

## values 参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `image.repository` | 控制器镜像仓库 | `ghcr.io/th1nking/predictive-hpa` |
| `image.tag` | 镜像 tag（空则用 `appVersion`） | `""` |
| `image.pullPolicy` | 拉取策略 | `IfNotPresent` |
| `replicaCount` | 控制器副本数（固定 1，不支持 HA） | `1` |
| `resources` | 控制器资源 requests/limits | 见 values.yaml |
| `prometheus.url` | Prometheus 查询端点 | `http://prometheus-server.monitoring.svc:80` |
| `installCRD` | 是否由本 chart 管理 CRD | `true` |
| `rbac.create` | 是否创建 ClusterRole/Binding | `true` |
| `serviceAccount.create` | 是否创建 ServiceAccount | `true` |
| `serviceAccount.name` | SA 名称（空则用 fullname） | `""` |
| `metricsService.enabled` | 是否暴露控制器 /metrics | `false` |
| `metricsService.port` | metrics 端口 | `8080` |

> **不在 values 里的参数**：EWMA 调参（`alphaPercent` / `window` / `horizon`）属于
> PredictiveHPA *实例* 的 spec 字段，写在你创建的 PredictiveHPA 资源里，不是 chart
> 部署参数。一个 chart 可服务多个不同调参的 PHPA 实例。

`spec.decisionMode` 也属于实例配置：可选 `Predictive`（默认）、`Current`、`Hybrid`。
Hybrid 用当前 CPU 触发扩容，预测只辅助保守缩容；省略字段保留旧行为。
使用新模式前需要更新 CRD 和控制器镜像，不能仅修改实例 YAML。

## CRD 来源（重要）

CRD 的 OpenAPI schema 是 `make manifests` 的生成产物，chart **不手维护** schema。
同步流程：

```bash
make manifests                 # controller-gen 生成 config/crd/bases/*.yaml
./hack/sync-chart-crd.sh       # 复制到 deploy/charts/predictive-hpa/files/
```

`templates/crd.yaml` 用 `.Files.Get` 读 `files/crd-predictivehpas.yaml` + `installCRD` 条件渲染。
打包/发布 chart 前务必先跑同步脚本，否则 `files/crd-predictivehpas.yaml` 缺失会导致 install 失败。

### 为什么 CRD 放 templates/ 而非 crds/ 目录

Helm 约定的 `crds/` 目录只在 `install` 时装 CRD，`upgrade` 完全不管 CRD——CRD schema
演进无法通过 `helm upgrade` 推送，是 Helm 著名痛点。放 `templates/` + `installCRD` 开关
换取完整生命周期与条件渲染。代价：`uninstall` 会删 CRD（连带删所有 PHPA 实例），生产慎用。

## RBAC 同步（注意）

本 chart 的 RBAC rules 是**手写**在 `templates/rbac.yaml` 里的（rules 短且稳定）。
生产代码里 rules 的 source of truth 是控制器顶部的 `+kubebuilder:rbac` markers。
**若未来新增 marker（如加 memory 指标要读 pods），需手动同步 `templates/rbac.yaml` 的 rules。**

## 已知约束

- 控制器单副本运行（in-memory 稳定窗口历史不支持 HA）
- 仅支持目标为 Deployment 的 scaleTargetRef
- 仅支持 CPU 指标
