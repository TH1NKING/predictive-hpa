# PredictiveHPA：实现与设计

本文说明当前代码如何完成一次扩缩容，以及这些设计的收益和边界。项目介绍见[首页](../README.md)，实测效果见[实验导航](benchmarks/README.md)。

## 从 CR 到 Scale 的代码路径

入口是 [`cmd/main.go`](../cmd/main.go)：创建 manager、注册 CRD scheme、用 uncached API Reader 构造 Prometheus Provider，并把它注入 [`PredictiveHPAReconciler`](../internal/controller/predictivehpa_controller.go)。控制器监听 PredictiveHPA 及目标 Deployment 的相关变化，正常协调后通过 `RequeueAfter` 再次检查。

一次协调按以下顺序执行：

1. 读取 PHPA 和同命名空间的目标 Deployment，读取 Scale 中已请求的副本数，并核验对象身份。PHPA 被删除后清理对应的建议历史。当前仅处理 `kind: Deployment`。
2. Provider 核验当前 Pod → ReplicaSet → Deployment 的 UID 链，查询每个普通容器的 CPU rate 和源样本时间，用同一集合的 CPU requests 归一化。查询前后的成员、容器实例或 requests 变化时拒绝本次观测；通过后加入有界 CPU 历史。无数据或预测样本不足时不写 Scale，并更新 `MetricsReady`。
3. 计算 EWMA 和阻尼趋势预测，再由控制器限幅；按 `decisionMode` 选择送入副本公式的信号。
4. 计算需求建议并应用 min/max 边界，记录未经稳定化的建议值。缩容时应用窗口最大值与冷启动保护；窗口最大值只能保留容量，不能主动触发扩容。动作判断区分已请求副本与滞后的观测副本。
5. 不需要调整或处于容差带时跳过写入；否则重新核验 PHPA/目标身份与配置，使用 Scale 的版本进行乐观并发更新。
6. 更新 PHPA status，包括 CPU、当前/期望副本、`MetricsReady` 与 `ScaleDownStabilized` 条件，并记录协调结果。输入不可用时清除过时 CPU 展示值，保留上次成功 Scale 时间。

重新读取 Scale 后更新，能使用该子资源的当前版本；API 写入失败或冲突会返回错误，后续协调重试。Scale 写入和 status 更新是两个请求，不能视为原子事务。代码在成功 Scale 写入后立即记录日志，因此随后 status 冲突不会抹去已发生的扩容证据。

事件和重排队共同驱动协调。因此 `30s` 配置不是严格的墙钟调度周期，也不是完整的端到端扩容延迟。状态回写不应触发无休止的自身协调；配置和用户显式触发的事件仍需要处理。

## 指标、预测与策略为何分开

[`metricsprovider.Provider`](../internal/metricsprovider/prometheus.go)暴露“查询 Deployment 的 CPU 利用率序列”的业务接口，调用方不拼接 PromQL。当前只有 Prometheus 实现；接口存在不等于已经支持多种数据源。

输入来自两条通道：

- cAdvisor 的 `container_cpu_usage_seconds_total`，用于计算 CPU 使用率；
- Kubernetes API 的 Pod/ReplicaSet/Deployment，提供 UID 归属、当前容器实例、Ready 状态与 CPU requests。

CPU 使用一分钟 `rate` 窗口，另查原始计数器的 `timestamp(...)`。源样本最大年龄默认 45 秒，查询过程有 10 秒期限。每个纳入容器必须有唯一的新鲜使用量、当前 runtime container ID 和正数 request；计算 `100 × 总 CPU 使用量 / 同一容器集合的总 CPU request`。这支持普通多容器的加权计算，不会把缺失 request 的容器悄悄从分母移除。

Provider 保存通过校验的实时观测，保留窗口支持 15 秒至 1 小时；用于预测的观测间隔至少 15 秒，默认协调周期约 30 秒。窗口必须容纳至少两个不同时间的有效观测。每个目标最多保留约 241 个锚点，目标缓存上限为 256；逐出或重启后重新积累样本。超过缓存容量的持续活跃目标可能反复预热，本版本没有相应的大规模可用性保证。正常滚动更新不会重新解释旧观测的成员，但不完整输入会清空该目标的 CPU 历史，恢复后重新预热。Pod 级资源与可重启 init sidecar 等语义被明确拒绝。

这是一次数据模型变化：以前向 Prometheus 回溯查询 15 秒步长的历史，现在积累自己验证过的观测。历史的已删除 Pod 不会被套用当前 Pod 名单重新计算，但启动前任意历史也不会被冒充为已验证数据。理由见 [ADR 0001](adr/0001-verified-live-cpu-observations.md)。旧 benchmark 的采样和启动行为不能当成当前版本的性能证据。

[`predictor`](../internal/predictor/ewma.go)保持无 Kubernetes 依赖的计算接口：

```text
S[0] = Y[0]
S[t] = alpha × Y[t] + (1 - alpha) × S[t-1]
预测值 = 最新平滑值 + 平滑尾部的趋势 × 阻尼后的外推步数
```

使用 EWMA 是为了保持实现轻量、计算路径可解释、无需训练和外部推理服务。当前每次协调重新遍历历史序列，时间和附加空间复杂度均为 O(n)。较小的 alpha 可以平滑噪声，也会增加趋势滞后；趋势外推可能在负载进入平台期后继续高估。因此实现采用 `0.85` 阻尼，并在控制器层加入 `0–1.3 × 当前 CPU` 的预测边界。这里没有做 ARIMA / LSTM 的实证选型对照，也没有实现完整的 Holt 双参数模型。

限幅属于扩缩容策略，放在控制器而不是预测包中，便于分别测试原始预测和业务约束。限制预测上界也限制了提前留出容量的幅度；它无法从尚未进入指标链路的负载中产生新信息。

## 三种决策模式与稳定窗口

| 模式 | 送入共同副本策略的 CPU 信号 |
|---|---|
| Predictive | 限幅预测值 |
| Current | 最新观测值 |
| Hybrid | `max(currentCPU, min(boundedPrediction, targetCPU))` |

Hybrid 在当前 CPU 高于目标时使用当前需求；当前值较低时，预测最多把决策信号抬到目标值，帮助保留副本。它不会仅因预测偏高而扩容，也不会让决策信号低于当前观测。最终是否扩缩容仍取决于副本取整、边界、容差和稳定窗口。

Current 仍执行预测计算并保留相同的样本就绪门槛。这样便于在相同执行链路中比较信号选择，但它不等于原生 HPA；原生 HPA 的指标来源和完整控制逻辑仍有差异。[模式回归与集群结果](benchmarks/decision-mode-ablation-guide.zh-CN.md)

缩容窗口记录每次计算得到的建议副本数，缩容时取最近窗口内的最大建议值。它与“每次扩容后固定等待 60 秒”不同。当前实现无论扩容、持平还是缩容都会裁剪过期记录，避免长期不缩容时历史持续增长。容差则检查 CPU 与目标值的相对偏差是否小于 `0.1`，减少阈值附近的反复动作。

建议历史以 PHPA 的命名空间/名称定位，同时校验 PHPA UID、目标 UID 和目标引用。新进程、对象替换或窗口配置改变后，会重新保护实时 Scale 已请求副本一个完整的新窗口。缩短正数窗口也会重新计时，因为旧时间桶已丢失桶内每个峰值的精确时间；设为零则立即禁用。当前副本边界仍有约束力。冷启动只阻止缩容；真实需求仍可触发扩容。

历史采用有界的保守时间桶，桶内保留最大建议值和最新观察时间，避免高频事件无限增加条目。最大值不会提前过期，代价是最多多保留一个桶宽。重启可能额外保留一个完整窗口，因此这是重建保护而不是持久化历史恢复。相比 status/ConfigMap 方案，它避免了历史写入与 Scale 写入之间的持久化协议，详见 [ADR 0002](adr/0002-cold-start-scale-down-protection.md)。

## 配置参考

API：`autoscaling.brian.io/v1alpha1`，Kind：`PredictiveHPA`，短名称：`phpa`。完整定义见 [Go 类型](../api/v1alpha1/predictivehpa_types.go)与[示例 CR](../config/samples/autoscaling_v1alpha1_predictivehpa.yaml)。

| 字段 | 默认或示例 | 当前语义 |
|---|---|---|
| `scaleTargetRef` | 必填 | 同命名空间 Deployment |
| `minReplicas` | 运行时默认 `1` | schema 允许 0；运行时小于 1 按 1 处理 |
| `maxReplicas` | 必填 | 副本上限，schema 最小值为 1 |
| `targetCPUUtilizationPercentage` | 必填，示例 `50` | 1–100，相对于 CPU request |
| `prediction.algorithm` | `EWMA` | 当前唯一算法 |
| `prediction.alphaPercent` | 必填，示例 `30` | 1–99；30 表示 alpha = 0.3 |
| `prediction.window` | 必填，示例 `5m` | 查询历史时序的回溯长度 |
| `prediction.horizon` | 必填，示例 `30s` | 外推时长 |
| `decisionMode` | `Predictive` | Predictive / Current / Hybrid |
| `scaleDownStabilizationWindowSeconds` | `60` | 缩容窗口；0 表示不保留此前窗口历史 |

枚举和已声明的数值范围由 CRD schema 在 admission 阶段校验；这不代表所有配置组合都经过完整校验。例如 `maxReplicas >= minReplicas` 的跨字段约束和 duration 的合理范围仍有完善空间。不要把类型注释中的约定等同于已实现的校验规则。

status 提供 `currentReplicas`、`desiredReplicas`、当前与预测 CPU、`lastScaleTime` 以及 `conditions`。`desiredReplicas` 是策略计算结果，不保证副本已经达到该值；容差也可能阻止写入。`lastScaleTime` 在成功 Scale 写入后更新，不宜单独用于统计真正的扩容次数。诊断工具使用成功写入日志中的 `previousDesiredReplicas` 和 `finalDesired` 判断目标是否实际提高。

manager 的 `--requeue-interval` 默认 `30s`，显式值不得小于 1 秒；它用于正常协调和目标/样本暂缺后的重试，不支持的目标类型使用 60 秒重试。它是启动参数，当前 Helm chart 未暴露对应 values 开关。部署参数与实例的预测参数分别配置，见 [Helm 指南](../deploy/charts/predictive-hpa/README.md)。

## 如何验证与讲解这项工作

| 想证明的事情 | 对应证据 | 不能由它推出的结论 |
|---|---|---|
| 公式、限幅、窗口在给定输入下正确 | 单元测试、FakeClock、envtest | 真实服务会更快或更省资源 |
| manager 能在 Kubernetes 中运行 | 独立 Kind 的部署和 metrics 烟测 | Helm 全流程或真实扩缩容性能已验证 |
| 指定负载下扩缩容表现如何 | 容量校准、匹配配置的 k6 实验 | 其他负载或生产环境的效果 |
| 等待发生在哪些时间段 | 查询、原始样本可见性、Scale、Pod 与请求时间记录 | 每个阶段已被独立识别为因果瓶颈 |

面试讲解可以从一次请求扩容的路径展开，再选择一个有代码和数据支撑的取舍：

- **为什么需要稳定窗口？** 展示短暂 CPU 回落时的建议值，再用 FakeClock 解释窗口内最大值何时过期，以及重启为何会失去保护。
- **为什么预测不一定更早？** 展示 EWMA 平滑滞后与当前值保护规则，再区分“这条规则在相同输入下有效”和“整个运行服务指标改善”。
- **为什么不能只看副本曲线？** 展示 Scale 成功时间与后续采样副本增长的差异；同时报告请求成功率、全请求 p95 和 Pod-seconds。
- **为什么发现无收益后继续做诊断？** 展示指标源、查询可见性和协调时序证据，解释为什么保留默认参数，而不是仅凭某次较好的曲线调参。

适合用于项目介绍的事实是“实现了什么、如何测试、观察到什么、还有哪些边界”。当前证据不支持把它表述为生产级 HPA 替代品，或声称预测模式已实现确定的性能提升。
