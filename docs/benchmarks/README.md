# 实验与证据导航

本项目围绕一个问题逐步验证：**CPU 预测能否改善 Kubernetes 的扩缩容表现，哪些等待和资源开销来自算法以外的环节？**

目前的实验没有证明 PredictiveHPA 普遍优于原生 HPA。这里保留了不利结果、压测路径修正、同控制器消融和指标链路诊断，展示如何从现象提出问题，再用测试和实测缩小范围。它们是学习与工程实践的记录，适合结合控制器代码阅读。

## 先读这三份

1. [容量校准与控制器对照讲解](controlled-pilot-guide.zh-CN.md)：为什么先确认流量确实分发到各个 Pod，再选择单副本无法承载、扩容后可以承载的负载；为什么资源占用更少不一定更好。
2. [同控制器决策消融讲解](decision-mode-ablation-guide.zh-CN.md)：Current、Predictive、Hybrid 分别使用什么信号，怎样复现“较低预测抑制已有扩容需求”的路径，以及为什么规则修复不能直接推导成整体性能提升。
3. [指标采集链路讲解](metric-pipeline-guide.zh-CN.md)：区分源样本时间、查询可见时间、协调时机和 Scale 写入；解释为什么缩短 CPU 窗口可能同时丢失可用样本。

## 当前证据支持什么

- **压测负载经过容量校准，但动态扩容仍未满足服务标准。** 最近的独立 `kernel31` 批次中，25 RPS、90 秒、固定五副本达到 100% HTTP 200、全请求 p95 73.93ms、零丢弃；固定单副本不达标。这只说明该环境下测试负载可在扩容后被承载。三次动态 Current 运行仍未达到 99% HTTP 200 / 500ms p95 的诊断标准。见[最新报告](metric-pipeline-20260908.md)。
- **决策规则的保护作用已有证据，整体收益仍未成立。** 同一控制器三种模式各运行三次，各组首次扩容均值接近，未证明 Current / Hybrid 能稳定更早扩容或取得整体服务优势。Current 比 Predictive 少 7.1% 总 Pod-seconds 是小样本占用观察，不能写成节省成本。见[决策消融报告](decision-mode-ablation-20260907.md)。
- **更频繁协调和更短 CPU 窗口都有代价。** 30/15 秒协调对照每组两次，15 秒组观察到更早扩容、更高成功率，也增加了副本占用和查询；全请求 p95 仍约 10 秒。随后三次旁路观察的 59 个扩容前周期中，30 秒 CPU 表达式有 39 次空结果，60 秒表达式全部有效。因此保留默认 30 秒协调间隔和一分钟 CPU 窗口。见[协调周期报告](latency-cadence-followup-20260907.md)和[窗口观察报告](metric-pipeline-20260908.md)。

## 按研究问题查阅

| 研究问题 | 协议与实现范围 | 结果与讲解 |
|---|---|---|
| 早期稳定窗口对照能说明什么？ | [历史 v2 设计与报告](stabilization-window-ablation-v2.md) | 27 次历史实验；存在 Service port-forward 路径局限与部分缩容截尾，先读报告顶部更正说明 |
| 压测是否经过真实 Service 分流？ | [Service 路径校准协议](service-routing-validation.md) | [2026-09-05 结果](service-routing-validation-results-20260905.md) |
| 负载扩容后能否承载？匹配缩容窗口后如何比较？ | [容量与小规模对照协议](controlled-pilot.md) | [2026-09-06 结果](capacity-and-controlled-pilot-20260906.md) · [中文讲解](controlled-pilot-guide.zh-CN.md) |
| 预测值是否抑制了当前需求？ | [同控制器决策消融协议](decision-mode-ablation.md) | [2026-09-07 结果](decision-mode-ablation-20260907.md) · [中文讲解](decision-mode-ablation-guide.zh-CN.md) |
| 首次扩容前在等待哪个环节？ | [时序诊断协议](latency-diagnostic.md) | [六次诊断结果](latency-diagnostic-20260907.md) · [中文讲解](latency-diagnostic-guide.zh-CN.md) |
| 将协调间隔从 30 秒减为 15 秒会怎样？ | [单变量对照协议](latency-cadence-followup.md) | [四次匹配对照结果](latency-cadence-followup-20260907.md) · [中文讲解](latency-diagnostic-guide.zh-CN.md) |
| 历史记录是否及时过期？CPU 数据何时可见？ | [历史保留修复范围](history-metrics-followup.md) · [离线诊断协议](metric-visibility-diagnostic.md) | [十次旧运行的复算结果](metric-visibility-20260908.md) · [中文讲解](history-metrics-guide.zh-CN.md) |
| 源端采样、抓取状态与真实 30/60 秒表达式如何对应？ | [指标链路观察协议](metric-pipeline-followup.md) | [三次现场诊断结果](metric-pipeline-20260908.md) · [中文讲解](metric-pipeline-guide.zh-CN.md) |

## 如何理解这些结果

- **实验批次不能直接混合。** 历史 v2 的 port-forward 路径、后续集群内 Service 路径、不同源码与配置，以及内核升级后的 `kernel31` 批次分别保留身份。后续校准不能还原历史流量；各批次的均值不能拼接成同一对照组。
- **服务质量和资源占用要一起读。** HTTP 200 比例、含失败请求的全请求 p95、丢弃迭代、扩缩容时机与 Pod-seconds 各有含义。成功请求子集的低延迟不能代替整体结果；Pod-seconds 是采样副本数的时间积分，不是 CPU 消耗或账单。
- **区分观察、规则验证与因果结论。** 同控制器消融每组 n=3，协调周期对照每组 n=2；它们是共享主机、小样本、限定负载的描述性结果。旁路表达式不参与副本决策，59 个观察周期也不是 59 次独立实验，不能据此估计短窗控制器的服务收益。
- **保留负面结果与时间精度。** 校准失败、发压前预检停止、超时、丢弃和缩容截尾均有各自记录。API 写入、Pod Ready、收到请求及 15 秒监控采样不代表同一时刻，不把采样延迟直接解释成 Pod 启动耗时。

## 证据与复核入口

每份报告注明源码、配置、环境、纳入规则和校验结果。[assets/](assets/) 保存图表及可追溯输入 JSON，其中包括逐次指标、数据路径和校验摘要。实验与分析工具位于 [hack/](../../hack/) 和 [hack/analyze/](../../hack/analyze/)，具体复算命令见对应协议或中文讲解。

完整原始请求、指标与日志体积较大，主要封存在各报告指向的 `benchmark-runs/` 本地目录，该目录未纳入 Git。**仅克隆仓库即可阅读报告、代码与图表输入，但不包含后续批次完整原始数据。** 历史 v2 的发布物入口与清单见其[归档说明](stabilization-window-ablation-v2.md#9-final-release-artifacts)；该发布物包含提取结果，也不包含全部原始请求与监控文件。

新一轮实验应使用独立的 Kind 集群，从 [Service 路径校准](service-routing-validation.md)开始，确认当前环境与容量，再选择对应协议。旧报告中的通过点和命令是带环境条件的历史记录，不能直接当成新环境的容量保证。
