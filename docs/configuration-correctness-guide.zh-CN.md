# 配置与回放证据校验：实现和取舍

这次修复覆盖三个问题：非法时长能进入 API 存储、负时长让预测方向反转，以及不可能的回放时间线被当成已验证证据。默认预测模式、30 秒协调间隔和 60 秒 CPU rate 窗口保持原值。

## 为什么要在 API 入库前校验

`metav1.Duration` 在 JSON 里是字符串，解码时才调用 Go 的 `time.ParseDuration`。Go 类型本身不能替代 CRD schema：只有 `type: string` 时，`"not-a-duration"` 也可能被 API 接受，随后使客户端的 List 或 Watch 解码失败。Linux 红阶段 envtest 实际观察到了非法对象入库及 Watch 解码错误。

我在 Go 类型的 kubebuilder markers 中增加 CEL 规则，使用 `duration(self)` 解析时长并比较范围，然后用生成命令更新 CRD，再同步 Helm 的 CRD 副本。范围是：

| 字段 | 允许值 |
|---|---|
| `prediction.window` | 15 秒到 1 小时，含边界 |
| `prediction.horizon` | 大于 0、至多 1 小时 |
| 两个时长字符串 | 最多 64 个字符，支持 Go 复合单位、小数 |
| 副本上下界 | `minReplicas <= maxReplicas`；原有单字段范围继续生效 |

窗口范围沿用 Provider 已支持的边界。业务预测时长限制为一小时，让输入有明确范围；这是一项可解释的保护上限，并不表示已证明一小时预测有效。业务配置要求向未来预测，与既有回放输入的正时长要求一致。

相比手写正则，CEL 的时长转换不需要重新实现 Go 的小数和单位组合语法；相比 admission webhook，这些规则不需要额外服务、证书和可用性依赖。字符串长度约束也给 CEL 计算成本一个边界。类型 markers 是唯一维护入口，直接修改生成 YAML 会在下一次生成时丢失。

因此 chart 的 Kubernetes 下界提高到 1.33，chart 版本更新为 0.3.0。这里不仅需要 CEL，还依赖从 [Kubernetes 1.33 起稳定的 validation ratcheting](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definitions/#validation-ratcheting)：已有 spec 不变时，允许更新状态以报告旧配置错误。只要求 CEL 已稳定的 1.29 仍不够，旧非法字段可能阻止 Condition 回写。本次实际 envtest 使用 1.35，不声称逐版本验收。

## 为什么运行时还要再检查

新的 admission 规则不会重新验证旧对象。对于能解码、但范围不合法的旧配置，例如 `horizon: -1m`，Reconcile 在查询指标之前检查配置，保持当前副本请求，清除旧 CPU 展示值，并发布 `MetricsReady=False`、原因 `InvalidConfiguration`。修正配置后，正常事件和有界重试会恢复协调。

只在预测后把负数截成零是不够的。红阶段的例子是：两个 30 秒间隔的 CPU 观测从 20% 升到 100%，alpha 为 0.3，负一分钟 horizon 得到约 -8.24%。原代码把它截成零，关闭稳定窗口时就把 5 个副本缩到 1 个。现在该配置会被拒绝，而不是把错误预测解释成低负载。

通用预测器本身也拒绝负 horizon，保护其他调用者。它仍允许零 horizon 返回平滑后的末值；一小时上限留在业务层，避免把控制器部署约束塞进通用算法。

已经存储的 `"not-a-duration"` 无法进入 typed Reconcile，运行时保护对此无能为力。升级前要用非类型化客户端读取并修正对象，例如先保存 `kubectl get phpa -A -o json`，然后用 `kubectl patch ... --type=merge` 修正两个时长字段。修正后再启动控制器；不要把 schema 更新当作存量数据迁移。

## 回放为什么要校验时间下界

生产 Provider 的顺序是开始观测、读取容器清单、选定求值时间、发送查询。原分析器检查了求值不得晚于查询开始，却没有检查它不得早于本轮观测。将求值整体回拨到控制器启动之前，仍可能保持源样本新鲜、历史间距正常，从而通过原检查。

新规则将本轮观测开始时间截断到毫秒，并把它作为求值下界。必须保留这个精度差：生产 Go 代码使用 `Truncate(time.Millisecond)`，正常求值时间可能比高精度观测开始时间早不足一毫秒。测试分别覆盖同一毫秒内可接受、跨到上一毫秒应拒绝，以及整体回拨的不可能时间线。

这项修复提高离线证据校验的可信度，不能由此推断线上扩缩容性能改善，也不表示此前公开记录有异常。

## 测试能证明什么

- admission 测试通过非类型化 Create/Update 发送坏字符串，检查拒绝结果和 typed List/Get 是否仍可用，覆盖真正的客户端边界。
- Reconcile 测试检查 Scale、Condition、旧 CPU 展示值、最后成功扩缩容时间和配置修复后的恢复行为。
- 预测器测试检查负时长拒绝及零时长的数学含义；回放测试检查公共 CLI 的退出码与输出是否发布。
- Linux envtest 提供真实 API Server/etcd 的校验证据；它不运行真实业务 Pod，因此不能代替 Kind 负载实验。

红绿复现、历史录制兼容核对与最终验证结果见[验证记录](configuration-correctness-validation.md)。

短窗口即使通过 schema，也未必能完成暖机：窗口必须容纳两个实际观测。默认 30 秒协调间隔还有查询等耗时，推荐继续使用示例的 5 分钟窗口。后续性能实验采用独立的[协调周期配对方案](benchmarks/cadence-pilot-plan.md)。
