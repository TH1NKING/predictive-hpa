# PredictiveHPA (PHPA)

> 基于 EWMA 时序预测的 Kubernetes 自定义 HPA 控制器——在 CPU 负载到达之前扩容，在负载离开之后更快缩容。

![Go](https://img.shields.io/badge/Go-1.26-00ADD8?logo=go)
![Kubernetes](https://img.shields.io/badge/Kubernetes-v1.35-326CE5?logo=kubernetes)
![License](https://img.shields.io/badge/License-Apache%202.0-blue)

---

## 1. 核心价值主张

原生 Kubernetes HPA 是**被动响应式**的：观察到 `当前 CPU > 目标值` 才开始扩容。这意味着突发流量场景下，系统必须先过载、指标先恶化，扩容才会发生——实测中单 Pod 以 250% 利用率超载运行约 30 秒后副本数才开始变化。

PHPA 与原生 HPA 使用**完全相同的扩缩容公式**：

```
desiredReplicas = ceil(currentReplicas × cpu% / targetCPU%)
```

唯一差异是输入信号：原生 HPA 输入**当前观测值**，PHPA 输入 **`EWMA(history) + slope × horizon` 的预测值**。同公式、不同输入——这个设计让对照实验的差异可以干净地归因于"预测 vs 观测"本身，而不被公式差异污染。

## 2. 实测数据（PHPA vs 原生 HPA）

基于 18 次 k6 对照实验矩阵（3 种负载模式 × 2 种控制器 × 3 次重复，交替跑序保证集群状态可比），核心指标为**缩容资源浪费窗口**（负载归零后空载 Pod 持续运行的时间）：

| 负载模式 | PHPA | 原生 HPA | 改善 |
|---|---|---|---|
| step（阶跃） | 194 ± 15 s | 331 ± 69 s | **−41%** |
| ramp（渐变） | 224 ± 0 s | 429 ± 2 s | **−48%** |
| spike（脉冲） | 154 ± 17 s | 389 ± 0 s | **−60%** |

（mean ± stdev，n=3。样本量小，未做显著性检验，诚实标注。）

### 代价（诚实的反面数据）

预测不是免费的。同一组实验中 PHPA 的两个劣势：

- **首次扩容更慢 13%–31%**：EWMA 平滑 + Prometheus `rate()` 1 分钟窗口共同构成"平滑税"——信号要先穿过两层平均才能驱动决策。
- **稳态过度扩容 80%–100%**：一阶差分外推在上升段会持续高估，稳态副本数显著高于原生 HPA。

这两个数字与改善数字来自同一实验矩阵。PHPA 的适用场景是**缩容成本敏感、可容忍稳态冗余**的负载（如按量计费环境的波谷回收），不是无条件优于原生 HPA 的替代品。

## 3. 架构

```
kubelet cAdvisor ──► Prometheus (TSDB)
                          │  query_range: rate(container_cpu_usage_seconds_total[1m])
                          ▼
                 ┌─ metricsprovider ─┐     业务语义接口，PromQL 不泄漏到调用方
                 │                   │
                 │     predictor     │     纯函数 EWMA + 一阶差分外推，stateless
                 │                   │
                 │     controller    │     Reconcile: clamp → 稳定窗口 → tolerance → scale 子资源
                 └───────────────────┘
                          │
                          ▼
              Deployment/scale 子资源 + PHPA status 回写
```

四个解耦的包：`internal/predictor`（算法）、`internal/metricsprovider`（数据源抽象）、`internal/controller`（K8s 协调逻辑）、`api/v1alpha1`（CRD 类型）。每个包独立测试。

## 4. 快速开始

前置：可用的 K8s 集群（开发用 kind）、集群内 Prometheus（抓取 cAdvisor 指标）。

```bash
# 安装控制器 + CRD + RBAC
helm install phpa ./deploy/charts/predictive-hpa \
  --set prometheus.url=http://prometheus-server.monitoring.svc:80

# 创建一个 PHPA 实例
kubectl apply -f config/samples/autoscaling_v1alpha1_predictivehpa.yaml

# 观察预测值与扩缩容
kubectl get phpa -w
```

<!-- TODO: 以下为演示用示例输出，待控制器真实运行后替换为实测截图 -->
```
NAME                   REFERENCE    MINPODS  MAXPODS  REPLICAS  CURRENT%  PREDICTED%  AGE
predictivehpa-sample   php-apache   1        10       4         48        53          5m
```

`PREDICTED%` 列是核心卖点：当预测值领先当前值时，控制器已经在扩容路上。

## 5. CRD 字段

```
GVK: autoscaling.brian.io / v1alpha1 / PredictiveHPA   (shortName: phpa)
```

### spec

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `scaleTargetRef` | CrossVersionObjectReference | — | 目标 Deployment（与原生 HPA 同结构） |
| `minReplicas` | *int32 | 1 | 下限（0 保留但 v1alpha1 运行时 fallback 到 1） |
| `maxReplicas` | int32 | — | 上限 |
| `targetCPUUtilizationPercentage` | int32 | — | 目标利用率，1–100 |
| `prediction.algorithm` | enum | `EWMA` | 当前仅 EWMA |
| `prediction.alphaPercent` | int32 | — | EWMA 平滑系数 ×100，1–99 |
| `prediction.window` | Duration | — | 历史回溯窗口 |
| `prediction.horizon` | Duration | — | 预测视野 |
| `scaleDownStabilizationWindowSeconds` | *int32 | 60 | 缩容稳定窗口（原生 HPA 默认 300s） |

### status

`currentReplicas` / `desiredReplicas` / `currentCPUUtilizationPercentage` / `predictedCPUUtilizationPercentage` / `lastScaleTime` / `conditions`（含 `ScaleDownStabilized`）。

非法配置（如 `algorithm: ARIMA`、`alphaPercent: 200`）由 OpenAPI v3 schema 在 admission 阶段直接拒绝，控制器代码不重复校验。字段详情：`kubectl explain phpa.spec.prediction`。

## 6. 设计决策摘要

| 决策 | 选择 | 被否决方案与理由 |
|---|---|---|
| 预测算法 | Simple EWMA + 一阶差分外推 | ARIMA/LSTM：控制器要求 O(1) 增量计算、单参数调参、无外部推理依赖；EWMA 是工程上 fit 度最高的选择 |
| 扩缩容公式 | 与原生 HPA 完全一致 | 自定义公式会成为对照实验的 confounding variable，差异无法归因 |
| alpha 参数类型 | `alphaPercent int32` (1–99) | `float64`：K8s API 惯例回避浮点（JSON 精度、validation 复杂）；与 `targetCPUUtilizationPercentage` 命名风格一致 |
| 稳定窗口状态 | in-memory map（单副本） | annotation/status/ConfigMap 持久化：写冲突与语义错配；与原生 HPA 单实例 controller-manager 假设一致，limitation 显式写明 |
| 字段校验位置 | OpenAPI schema（kubebuilder markers） | 控制器内校验：错误应在 admission 前置拒绝，而非进了 etcd 再报；webhook：当前无跨资源校验需求，不引入证书管理复杂度 |
| 负数预测 clamp | Reconciler 层，predictor 不动 | predictor 内 clamp：一阶差分在下降信号出负数是算法的正确行为，业务约束属于业务层；保留原始值可观测性 |

## 7. 测试策略

双层证据，边界明确：

- **envtest（17 specs，Ginkgo）**：决策逻辑的自动化回归——扩缩容公式、min/max clamp、tolerance 带、稳定窗口压制（注入 FakeClock 做确定性时间控制）。覆盖"控制器对给定输入做出正确决策"。
- **k6 真集群对照实验（18 次矩阵）**：端到端行为证据——真实 Prometheus 延迟、真实 Pod 启动时间、真实指标噪声下的系统级表现。覆盖"决策在真实环境产生预期效果"。

envtest 证明逻辑正确，k6 证明效果真实。单元测试（predictor 9 个 + metricsprovider 3 个）覆盖算法边界与 PromQL 构造。

## 8. 已知 Limitation

1. **仅支持单副本部署**——稳定窗口历史在 in-memory map，不支持 HA / leader election
2. **控制器重启丢失稳定窗口历史**——重启窗口内可能发生未受保护的缩容
3. **不支持 scale-to-zero**——`minReplicas: 0` 被运行时 fallback 到 1（需要外部 activator 架构，见 Roadmap）
4. **仅支持 Deployment**——StatefulSet 等其他 workload 类型会被拒绝
5. **仅支持 CPU 指标**——无 memory / 自定义指标抽象

## 9. Roadmap

### v1beta1（计划中）
- 可配置 tolerance（`spec.tolerance`，当前写死 10%）
- 扩缩方向独立的稳定窗口
- ConfigMap 持久化稳定窗口历史（重启安全）
- 混合模式：扩容用当前值、缩容用预测值（针对"首次扩容慢"的定向修复）

### v2alpha1（架构级变更，超出当前范围）
- Scale-to-zero（需要 KEDA Activator 式外部唤醒机制，是架构问题而非参数问题）
- Holt 双重 EWMA（可配置 beta）
- 多指标源

## 10. 项目背景

30 天个人项目（2026-05 至 2026-06），前置项目为 mydocker（轻量容器运行时）。从容器运行时到调度弹性方向的自然演进。设计决策过程、基线实验、踩坑记录均有完整文档化。

License: Apache 2.0
