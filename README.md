# PredictiveHPA (PHPA)

> 基于 EWMA 时序预测的 Kubernetes 自定义 HPA 控制器，用于研究预测信号对扩缩容时机与资源成本的影响。

[![Tests](https://github.com/TH1NKING/predictive-hpa/actions/workflows/test.yml/badge.svg)](https://github.com/TH1NKING/predictive-hpa/actions/workflows/test.yml)
[![Lint](https://github.com/TH1NKING/predictive-hpa/actions/workflows/lint.yml/badge.svg)](https://github.com/TH1NKING/predictive-hpa/actions/workflows/lint.yml)
![Go](https://img.shields.io/badge/Go-1.26-00ADD8?logo=go)
![Kubernetes](https://img.shields.io/badge/Kubernetes-v1.35-326CE5?logo=kubernetes)
![License](https://img.shields.io/badge/License-Apache%202.0-blue)

---

## 1. 核心价值主张

PHPA 从 CPU 历史序列估计未来利用率，将预测信号用于副本决策。它能否更早扩容、降低请求失败或减少资源消耗，需要通过匹配配置的实验验证；当前数据不支持无条件的性能优势。

PHPA 使用与原生 HPA 相同形式的基础副本计算公式：

```
desiredReplicas = ceil(currentReplicas × cpu% / targetCPU%)
```

当前预测实现为 **EWMA 平滑 + 阻尼趋势外推**（阻尼系数 `0.85`），控制器将预测值限制在 `0` 到 `1.3 × 当前 CPU`，再执行副本上下限、稳定窗口和容差规则。相同形式的公式不等于相同的完整控制流程；指标来源、采样延迟与协调行为仍可能不同，因此控制器对比不能直接归因于预测算法本身。

## 2. 实测数据（PHPA vs 原生 HPA）

最新的 [容量校准与受控 step 对照](docs/benchmarks/capacity-and-controlled-pilot-20260906.md) 完成了 18 个固定副本探针，以及 Native-60 / PHPA-60 各 3 次、25 RPS 的匹配实验。通过集群内 Service 发压，并统一从负载开始到停止后 360s 的统计窗口。

| 描述性均值（每组 n=3） | Native-60 | PHPA-60 |
|---|---:|---:|
| HTTP 200 成功率 | 85.63% | 65.41% |
| 首次观察到扩容 | 40s | 70s |
| 总 Pod-seconds（同为 541s 窗口） | 2,306 | 2,101 |
| 停止负载后的 Pod-seconds | 1,037 | 1,161 |

本轮 PHPA 扩容更晚、成功率更低；总副本占用减少约 8.9%，但负载后的占用增加约 12.0%。较少副本伴随服务质量下降，不能据此宣称效率收益。两组全请求 p95 均接近 10s，合计有 5 次丢弃迭代；均未达到校准使用的 99% 成功率／500ms p95 标准。副本变化按 15s 采样，结论限于本轮小样本 step 场景，不宣称统计显著或预测算法的独立因果效果。方法与取舍见[中文讲解](docs/benchmarks/controlled-pilot-guide.zh-CN.md)。

历史 [稳定窗口消融 v2](docs/benchmarks/stabilization-window-ablation-v2.md) 包含 27 次实验：3 种负载模式 × 3 组控制器 × 3 次重复。它分别比较原生 HPA 的 300s/60s 窗口，以及同为 60s 窗口的 PHPA 与原生 HPA。两个批次的流量路径、并发配置和统计窗口不同，不能将数值变化归因于单一修改。

| 对比 | 首次扩容 | 负载停止后的资源拖尾 | 总 Pod-seconds | 请求失败率均值 |
|---|---|---|---|---|
| Native-60 相对 Native-300 | 基本不变（差 0.0–0.7s） | 缩短约 203–240s † | 减少约 27%–36% | 变化方向不一致 |
| PHPA-60 相对 Native-60 | 晚约 18–26s | 延长约 6–28s | 增加约 16%–69% | 降低约 2.1–4.4 个百分点 |

PHPA-60 的峰值副本增加约 88%–100%。这些是每组 `n=3` 的描述性均值差，未宣称统计显著。† Native-300 的 step/ramp 存在截尾，对应资源拖尾与缩短幅度为下界。原生 HPA 缩短稳定窗口的资源收益不能当作预测算法的收益。

**v2 证据限制（2026-09-05 补充）：** v2 失败率约 59%–96%，几乎所有组的全请求 p95 触及 10s 上限；归档脚本通过 `kubectl port-forward svc/php-apache` 压测，按 [Kubernetes 文档](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_port-forward/)，该会话选择一个 Pod，不能证明新增副本分担了请求。历史流量实际分布和高失败率根因尚未验证。

**2026-09-05 Service 路径校准：** [2026-09-05 校准报告](docs/benchmarks/service-routing-validation-results-20260905.md) 记录了 14 次有效固定副本探针，每个目标 Pod 均有请求证据。同为 25 RPS，5 副本在 90s 和反序 180s 探针中均为 100% HTTP 200、p95 约 80–87ms；1 副本两次均未达到预设成功率与延迟标准。这证明了本次环境与负载下从 1 到 5 副本的承载改善，不证明精确最大容量、5 到 10 副本的容量增益、统计显著性或预测算法收益。正式控制器对照仍需遵循 [校准与实验流程](docs/benchmarks/service-routing-validation.md)。

## 3. 架构

```
kubelet cAdvisor ──► Prometheus (TSDB)
                          │  query_range: rate(container_cpu_usage_seconds_total[1m])
                          ▼
                 ┌─ metricsprovider ─┐     业务语义接口，PromQL 不泄漏到调用方
                 │                   │
                 │     predictor     │     纯函数 EWMA + 阻尼趋势外推，stateless
                 │                   │
                 │     controller    │     Reconcile: 预测限幅 → 副本 clamp → 稳定窗口 → tolerance → scale
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


历史输出示例（kind 集群，php-apache 负载，target=50%；仅用于说明状态列）：

```
NAME                   REFERENCE    MINPODS   MAXPODS   REPLICAS   CURRENT%   PREDICTED%   AGE
predictivehpa-sample   php-apache   1         10        1          0          0            59m
predictivehpa-sample   php-apache   1         10        1          186        139          60m
predictivehpa-sample   php-apache   1         10        3          244        250          61m
predictivehpa-sample   php-apache   1         10        10         73         74           61m
predictivehpa-sample   php-apache   1         10        9          44         41           62m
predictivehpa-sample   php-apache   1         10        8          50         50           65m
```

`CURRENT%` 和 `PREDICTED%` 分别展示观测与限幅后的预测，`REPLICAS` 展示副本状态。单次输出不能证明提前扩容或容量收益；总体实验结果与限制见第 2 节。

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
| 预测算法 | Simple EWMA + 阻尼趋势外推（系数 0.85） | ARIMA/LSTM：当前实现优先保持计算轻量、无需外部推理依赖 |
| 扩缩容公式 | 与原生 HPA 采用相同形式的基础公式 | 减少公式差异，但完整控制器对比仍不能隔离预测算法的纯因果效应 |
| alpha 参数类型 | `alphaPercent int32` (1–99) | `float64`：K8s API 惯例回避浮点（JSON 精度、validation 复杂）；与 `targetCPUUtilizationPercentage` 命名风格一致 |
| 稳定窗口状态 | in-memory map（单副本） | annotation/status/ConfigMap 持久化：写冲突与语义错配；与原生 HPA 单实例 controller-manager 假设一致，limitation 显式写明 |
| 字段校验位置 | OpenAPI schema（kubebuilder markers） | 控制器内校验：错误应在 admission 前置拒绝，而非进了 etcd 再报；webhook：当前无跨资源校验需求，不引入证书管理复杂度 |
| 预测限幅 | 控制器限制为 0 到当前 CPU 的 1.3 倍，predictor 返回原始值 | predictor 内 clamp：业务约束属于控制器层；原始预测与限幅值的差异可通过日志观察 |

## 7. 测试策略

双层证据，边界明确：

- **envtest（Ginkgo）**：决策逻辑的自动化回归——扩缩容公式、min/max clamp、tolerance 带、稳定窗口压制（注入 FakeClock 做确定性时间控制）。覆盖"控制器对给定输入做出正确决策"。
- **k6 真集群对照实验（v2：27 次矩阵）**：记录指标延迟、Pod 启动与噪声共同作用下的观测结果；流量分配、过载、小样本及截尾限制见第 2 节和完整报告。

单元测试覆盖 predictor 算法边界与 metricsprovider 的 PromQL 构造；集群实验的结论范围取决于负载路径、配置匹配与数据质量。

## 8. 已知 Limitation

1. **仅支持单副本部署**——稳定窗口历史在 in-memory map，不支持 HA / leader election
2. **控制器重启丢失稳定窗口历史**——重启窗口内可能发生未受保护的缩容
3. **不支持 scale-to-zero**——`minReplicas: 0` 被运行时 fallback 到 1（需要外部 activator 架构，见 Roadmap）
4. **仅支持 Deployment**——StatefulSet 等其他 workload 类型会被拒绝
5. **仅支持 CPU 指标**——无 memory / 自定义指标抽象
6. **历史压测结论受流量路径限制**——新 Service 校准验证了当前请求分配，但不能追溯证明 v2 历史流量分布或预测收益

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
