# 实验与证据导航

本项目围绕一个问题逐步验证：**CPU 预测能否改善 Kubernetes 的扩缩容表现，哪些等待和资源开销来自算法以外的环节？**

**最新结果是 [2026-09-10 当前版本性能基线](live-baseline-20260910.md)。** 我在独立 Kind 集群中重新校准了容量，完成 step / ramp 各九次正式 warm 对照，并复核全部原始证据。18 次均完整收集，但都未达到预先确定的服务标准。

在这批原始证据上，我又完成了 [2026-09-11 同输入决策回放](decision-replay-20260911.md)：607 次原模式决策全部匹配，其中 457 次完整历史重新运行 Go 预测代码，150 次明确沿用记录的预测值。[教学与复现](decision-replay-guide.zh-CN.md)解释了规则差异、未知历史和有界 Ready 时间；这项离线分析不构成新的性能实验。

**版本边界：** 这份新报告对应已完成 Kubernetes UID 校验、实时观测历史与冷启动缩容保护的实现。2026-09-05 至 09-08 的报告来自[指标安全改造](../metrics-safety-plan.md)之前的代码，查询步长和启动行为不同；下面将其单列为历史证据，不与新基线合并计算。

目前仍没有证据支持预测模式具有普遍的服务优势。这里保留了不利结果、压测路径修正、同控制器消融和指标链路诊断，展示我怎样从现象提出问题，再用测试和实测缩小范围。原生 HPA 对照也来自较早批次，不能用来推导当前版本与原生 HPA 的排名。

当前指标链路使用独立的 `live-baseline-v1` 实验入口：从控制器实际接受的观测核对输入，明确区分 warm/cold 启动，并以 k6 实际场景时钟计算完整观察窗口。复现方法和方案取舍见[当前版本基线讲解](live-baseline-guide.zh-CN.md)，验收范围见[本轮规范](live-baseline-followup.md)，工具检查见[回归与审查记录](live-baseline-validation.md)。Dockerfile、Helm 和故障场景的独立验证入口见[故障验收工作流](../metrics-safety-workflow.md)。

普通镜像与 Helm 路径已另行完成全部 11 项真实功能检查，详见[2026-09-11 入口验收结果](../metrics-safety-entry-validation-20260911.md)。该结果保留了失败尝试与缓存使用边界，不能与本页性能数值合并解释。

## 先读这三份

1. [当前版本 18 次正式基线报告](live-baseline-20260910.md)：容量校准、全部逐次结果、服务与副本时间的取舍，以及为什么暂不修改默认策略。
2. [当前版本基线实现讲解](live-baseline-guide.zh-CN.md)：如何核对控制器接受的输入、warm/cold 启动、k6 实际场景时钟、Scale 写入和 Ready 观察区间，并处理缺失证据与清理失败。
3. [回归与审查记录](live-baseline-validation.md)：哪些公共 CLI 边界已经验证，红绿测试修复了什么，以及离线验证和真实性能结果的区别。

## 当前证据支持什么

- **当前负载可以被五副本承载，自动扩容仍未达到服务标准。** 本次 25 RPS、90 秒校准中，单副本仅 165/2250 个请求返回 HTTP 200；五副本为 2250/2250、全请求 p95 81.78ms、零丢弃。正式 18 次运行的全请求 p95 均约 10 秒；step 每次丢弃一次迭代，ramp 为零，没有一次达到 99% HTTP 200 / 500ms p95 / 零丢弃的组合标准。
- **本次 step 有规则抑制证据，ramp 没有显示预测提前扩容。** step 的 Current / Predictive / Hybrid 成功率均值分别为 77.36% / 62.32% / 66.37%，首次 Scale 增长均值为 28.44s / 58.49s / 48.47s。Predictive 首轮已有较高当前 CPU，但预测值较低；Hybrid 两次较晚运行的首轮当前输入本身较低，不能作同样归因。ramp 三模式均约 58.5s 首次扩容。
- **服务与副本时间仍存在取舍，没有已验证的默认参数优化。** ramp 的 Hybrid 成功率均值为 92.64%，较 Current 的 90.15% 高，同时请求副本时间增加约 9.79%；每组仅 n=3，不能据此作普遍排名或费用结论。同输入回放已完成，下一步检验相位配对条件下协调周期的单变量假设；目前保留默认 30 秒协调周期、60 秒 rate 窗口和 Predictive 模式。

以上均来自[当前版本报告](live-baseline-20260910.md)。一轮独立探索和一轮 cold 观察另列：cold 负载到来前已观察到有效历史，不能把其结果差异解释成“空历史发压”或重启代价。正式组请求／Ready 副本积分均完整覆盖，step 为 541 秒、ramp 为 600 秒。

## 历史证据：2026-09-05 至 09-08

- **早期容量与输入链路诊断。** 2026-09-08 的独立 `kernel31` 批次中，25 RPS、90 秒、固定五副本达到 100% HTTP 200、全请求 p95 73.93ms、零丢弃；三次动态 Current 运行仍未达到服务标准。这些是该环境与旧实现的结果，见[该批次报告](metric-pipeline-20260908.md)。
- **早期决策消融。** 2026-09-07 同一控制器三种模式各运行三次，各组首次扩容均值接近；Current 比 Predictive 少 7.1% 总 Pod-seconds 是小样本占用观察，不能写成节省成本。见[决策消融报告](decision-mode-ablation-20260907.md)。
- **更频繁协调和更短 CPU 窗口的历史代价。** 30/15 秒协调对照每组两次，15 秒组观察到更早扩容、更高成功率，也增加了副本占用和查询；全请求 p95 仍约 10 秒。随后三次旁路观察的 59 个扩容前周期中，30 秒 CPU 表达式有 39 次空结果，60 秒表达式全部有效。见[协调周期报告](latency-cadence-followup-20260907.md)和[窗口观察报告](metric-pipeline-20260908.md)。

## 按研究问题查阅

| 研究问题 | 协议与实现范围 | 结果与讲解 |
|---|---|---|
| 指标安全改造后，预测信号和副本时间表现如何？ | [当前基线规范](live-baseline-followup.md) · [回归与审查](live-baseline-validation.md) | **[2026-09-10：18 次正式结果](live-baseline-20260910.md)** · [当前实现讲解](live-baseline-guide.zh-CN.md) |
| 早期稳定窗口对照能说明什么？ | [历史 v2 设计与报告](stabilization-window-ablation-v2.md) | 27 次历史实验；存在 Service port-forward 路径局限与部分缩容截尾，先读报告顶部更正说明 |
| 压测是否经过真实 Service 分流？ | [Service 路径校准协议](service-routing-validation.md) | [2026-09-05 结果](service-routing-validation-results-20260905.md) |
| 负载扩容后能否承载？匹配缩容窗口后如何比较？ | [容量与小规模对照协议](controlled-pilot.md) | [2026-09-06 结果](capacity-and-controlled-pilot-20260906.md) · [中文讲解](controlled-pilot-guide.zh-CN.md) |
| 预测值是否抑制了当前需求？ | [同控制器决策消融协议](decision-mode-ablation.md) | [2026-09-07 结果](decision-mode-ablation-20260907.md) · [中文讲解](decision-mode-ablation-guide.zh-CN.md) |
| 首次扩容前在等待哪个环节？ | [时序诊断协议](latency-diagnostic.md) | [六次诊断结果](latency-diagnostic-20260907.md) · [中文讲解](latency-diagnostic-guide.zh-CN.md) |
| 将协调间隔从 30 秒减为 15 秒会怎样？ | [单变量对照协议](latency-cadence-followup.md) | [四次匹配对照结果](latency-cadence-followup-20260907.md) · [中文讲解](latency-diagnostic-guide.zh-CN.md) |
| 历史记录是否及时过期？CPU 数据何时可见？ | [历史保留修复范围](history-metrics-followup.md) · [离线诊断协议](metric-visibility-diagnostic.md) | [十次旧运行的复算结果](metric-visibility-20260908.md) · [中文讲解](history-metrics-guide.zh-CN.md) |
| 源端采样、抓取状态与真实 30/60 秒表达式如何对应？ | [指标链路观察协议](metric-pipeline-followup.md) | [三次现场诊断结果](metric-pipeline-20260908.md) · [中文讲解](metric-pipeline-guide.zh-CN.md) |

## 如何理解这些结果

- **实验批次不能直接混合。** 当前 UID 校验实时观测基线、历史 v2 的 port-forward 路径、后续集群内 Service 路径，以及旧 `kernel31` 批次分别保留源码和环境身份。当前性能抓取间隔为 15 秒，功能故障验收使用 5 秒，也分别解释。后续校准不能还原历史流量；各批次的均值不能拼接成同一对照组。
- **服务质量和资源占用要一起读。** HTTP 200 比例、含失败请求的全请求 p95、丢弃迭代、扩缩容时机与 Pod-seconds 各有含义。成功请求子集的低延迟不能代替整体结果；Pod-seconds 是采样副本数的时间积分，不是 CPU 消耗或账单。
- **区分观察、规则验证与因果结论。** 同控制器消融每组 n=3，协调周期对照每组 n=2；它们是共享主机、小样本、限定负载的描述性结果。旁路表达式不参与副本决策，59 个观察周期也不是 59 次独立实验，不能据此估计短窗控制器的服务收益。
- **保留负面结果与时间精度。** 校准失败、发压前预检停止、超时、丢弃和缩容截尾均有各自记录。API 写入、Pod Ready、收到请求及 15 秒监控采样不代表同一时刻，不把采样延迟直接解释成 Pod 启动耗时。

## 证据与复核入口

每份报告注明源码、配置、环境、纳入规则和校验结果。[assets/](assets/) 保存图表及可追溯输入 JSON，其中包括逐次指标、数据路径和校验摘要。实验与分析工具位于 [hack/](../../hack/) 和 [hack/analyze/](../../hack/analyze/)，具体复算命令见对应协议或中文讲解。

当前报告的[紧凑输入](assets/live-baseline-inputs-20260910.json)和[公开绘图 CLI](../../hack/analyze/plot_live_baseline.py)可重建图表与逐次结果表。[完整性清单](assets/live-baseline-integrity-20260910.json)公布了 1,718 个归档文件的相对路径、字节数和 SHA256。18 次原始请求 p95、对象 UID 和日志边界的独立复算还需要完整本地归档；报告记录了该归档的 SHA256 和字节数。

完整原始请求、指标与日志体积较大，主要封存在各报告指向的 `benchmark-runs/` 本地目录，该目录未纳入 Git。**仅克隆仓库即可阅读报告、代码与图表输入，但不包含后续批次完整原始数据。** 历史 v2 的发布物入口与清单见其[归档说明](stabilization-window-ablation-v2.md#9-final-release-artifacts)；该发布物包含提取结果，也不包含全部原始请求与监控文件。

新一轮实验应使用独立 Kind，按[当前基线讲解](live-baseline-guide.zh-CN.md)完成环境准备和容量校准；分流检查的背景见 [Service 路径校准](service-routing-validation.md)。已经完成的校准只适用于其记录环境，不能直接当成另一个环境的容量保证。
