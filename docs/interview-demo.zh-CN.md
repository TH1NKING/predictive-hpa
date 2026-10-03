# PredictiveHPA 面试演示

演示目标是把简历中的四条主张落到代码、可重复输入和可核对结果。现场主线约 10 分钟，使用提前构建的离线包；真实 Kubernetes 部署、CPU 负载、指标中断与切主在面试前用独立 Kind 完整验收。两者分别证明规则行为和集群行为。

当前实现仅支持同命名空间 Deployment、CPU 和 EWMA。18 轮对照没有证明预测模式更早扩容或改善整体服务质量；这一点是实验结论的一部分。演示通过也不意味着任意电脑、网络和负载下都不会失败。

本轮 Windows/Linux 离线演示、Go/envtest、Helm 与独立 Kind 的结果见[2026-09-20 验证记录](interview-validation-20260920.md)。

## 面试前准备

从仓库根目录执行。`prepare` 需要与 [go.mod](../go.mod) 对应的 Go 工具链及依赖；提前完成构建，现场 `run` 只需要 Python 和匹配操作系统/架构的包内可执行文件。每次选择新的输出目录，保留此前成功与失败记录。

```powershell
python hack/interview_demo.py doctor --output benchmark-runs/interview-doctor.json
python hack/interview_demo.py prepare --output benchmark-runs/interview-bundle
python hack/interview_demo.py run --bundle benchmark-runs/interview-bundle --output benchmark-runs/interview-rehearsal
```

`prepare` 固定当前回放实现、18 份已提交的回放输入和 SHA256 清单，并先完成一次 `self-check`。带到面试电脑的是完整包；不要只拷贝可执行文件。更换操作系统或架构时重新准备相应的包，不把 Windows 可执行文件交给 Linux 运行。

`doctor` 在源码仓库中检查准备环境，因此会检查 Go/Git 和原始输入是否存在；它不适合拿来判断只运行离线包的电脑是否合格。离线包的实际 `run` 是现场验证入口，不要求 Go/Git、Docker、kubeconfig 或网络。

排练时检查退出码以及生成的 JSON/Markdown 报告。预期是 18 份输入、607 条原模式决策全部匹配，457 条通过完整观测历史重算预测，150 条明确沿用记录预测值；非法输入与证据篡改的拒绝检查也必须通过。数字变化时先定位输入/代码差异，不改预期来让演示变绿。

SHA256 清单用于检测准备后的意外损坏或篡改，不是第三方签名，也不能代替对原实验采集过程的验证。首次接收包时应使用自己准备并保存的清单；攻击者同时替换整个包和清单不在这种完整性检查的能力范围内。

## 10 分钟现场主线

| 时间 | 操作 | 讲清楚的事实 |
|---|---|---|
| 0–1 分钟 | 展示下面的简历映射和控制链路 | 从声明式 CR 到 Scale 写入；预测能否改善时机是待检验的问题 |
| 1–3 分钟 | 运行离线回放，打开本次报告 | 当前构建复用生产策略，匹配 18 份输入的 607 条原模式决策 |
| 3–5 分钟 | 对照 step Predictive 的等待案例 | 同输入下，预测/容差可以阻止当前需求已经支持的扩容 |
| 5–7 分钟 | 展示非法输入/篡改拒绝结果 | 无法验证的输入不能进入成功报告；缺失历史不会补零 |
| 7–9 分钟 | 打开提前完成的 Kind 验收回执 | UID 隔离、真实扩容、切主保护、指标中断与恢复 |
| 9–10 分钟 | 回到代码和边界，回答取舍 | 保留不利实验结果；区分规则反事实与真实服务性能 |

现场从仓库根目录运行时，输出目录使用新的名称：

```powershell
python hack/interview_demo.py run --bundle benchmark-runs/interview-bundle --output benchmark-runs/interview-session
```

脱离仓库时，把完整目录复制为面试电脑上的 `interview-bundle`，在其父目录直接调用包内脚本：

```powershell
python interview-bundle/interview_demo.py run --bundle interview-bundle --output interview-session
```

Linux 对应使用 `python3`。报告为 `interview-session/report.md` 和 `report.json`，逐轮输出也保存在该目录。Linux/macOS 传输时保留包内 `replay` 的执行权限；可用 tar 归档完整目录。新的一次演示请更换 `--output`，入口会拒绝覆盖已有成功或失败回执。

不要现场下载安装 Go 依赖、Docker 镜像或重跑 18 轮性能实验。若面试场景要求现场真实集群演示，先完成下文 Linux 排练，再留出独立的集群验收时间；已有成功回执不能代替本次执行结果。

## 简历主张与可展示证据

### 1. CRD → Prometheus → EWMA → Deployment/scale

**简历主张：** 设计 CRD，打通配置、指标与 Scale 子资源更新；采用无需训练的 EWMA 平滑和阻尼趋势外推，检验历史趋势是否有用。

**实现代码：** [API 字段与校验](../api/v1alpha1/predictivehpa_types.go)、[Reconcile](../internal/controller/predictivehpa_controller.go)、[Prometheus provider](../internal/metricsprovider/prometheus.go)、[EWMA](../internal/predictor/ewma.go)。

**现场操作：** 打开示例 [PHPA](../config/samples/autoscaling_v1alpha1_predictivehpa.yaml)，指出 target、CPU 目标、观测窗口、预测时距；再在报告中找到 `computed-history` 决策，对照 `rawPrediction`、`boundedPrediction`、`decisionCPU` 和最终目标。

**预期结果：** 457 条决策有完整历史可重算预测；150 条是 `recorded-forecast`，继续验证该预测值之后的规则。副本公式是 `ceil(observedReplicas × decisionCPU / targetCPU)`，之后还有边界、方向保护、稳定窗口和容差。

**边界：** `observedReplicas`、`requestedReplicas`、Ready 副本是不同量；`status.desiredReplicas` 不等于发生了 Scale 写入。使用 `Scaled Deployment` 日志或 Scale/Deployment 的实际请求值确认写入。离线报告不创建 Kubernetes 客户端，不证明此刻集群正在扩容。

### 2. Current / Predictive / Hybrid 消融

**简历主张：** 三种模式共享指标、样本门槛与副本规则；Hybrid 以当前需求触发扩容，预测仅帮助保留容量。

**实现代码：** [信号选择与容差](../internal/controller/scaling_decision.go)、[复用生产策略的回放](../internal/controller/replay.go)。

**现场操作：** 打开 [step Predictive r3 输入](benchmarks/assets/decision-replay-20260911/20260910_195936_step_phpa_r3/replay-input.json) 和本次对应结果，展示当前 CPU 约 91.28%、记录预测约 50.65%、目标 CPU 50% 的首轮。具体数值以文件与本次输出为准。

**预期结果：** Predictive 的公式取整可得到 2，但 50.65% 相对 50% 仍在 10% 容差内，因此 `WithinToleranceBand` 阻止写入。Current 与 Hybrid 使用约 91.28% 的需求，支持扩到 2。Hybrid 的信号是 `max(current, min(boundedPrediction, target))`，不会仅凭高预测扩容。

**边界：** 同输入回放固定已记录的观测、时间和实际副本，不把假想目标反馈成下一轮真实副本。它解释策略差异，不会生成新 Pod、请求成功率或延迟结果。Current 仍保留共同预测样本门槛，也不等于 Kubernetes 原生 HPA。

### 3. UID 归属、指标故障和缩容保护

**简历主张：** 沿 UID 链验证 CPU 指标归属，以源样本时间检查新鲜度；缺失或陈旧时保持副本；采用有界窗口最大建议，重启或切主后重建保护。

**实现代码：** [指标实现目录](../internal/metricsprovider)、[不可用状态处理](../internal/controller/metrics_safety.go)、[有界推荐历史](../internal/controller/scale_history.go)、[UID 设计决策](adr/0001-verified-live-cpu-observations.md)、[冷启动设计决策](adr/0002-cold-start-scale-down-protection.md)。

**现场操作：** 展示提前验收输出中的 `acceptance/summary.json`、`observations.ndjson` 和 manager 日志。核对空闲 `web` 与高 CPU `web-canary` 保持隔离、切主后的 `ColdStartProtection`、Prometheus 中断后的 `MetricsReady=False`。

**预期结果：** 中断时已请求副本不降低，旧 CPU 展示字段被清除；新 leader 重新积累观测，并保护已请求容量一个完整稳定窗口。功能验收配置使用 90 秒稳定窗口及 5 秒抓取；不要把它说成默认 60 秒窗口或性能实验的 15 秒抓取配置。

**边界：** 源样本时间不同于表达式求值时间，也不能证明 cAdvisor 内部缓存刷新时间。缺失指标不是零负载。窗口保存原始建议而非被稳定化抬高的输出；窗口最大值只保留容量，不主动扩容。内存历史没有持久化，保守重建可能多保留一段容量，频繁重启可能不断延长保护。

### 4. 集群内 Service 发压、18 轮实验和 607 条回放

**简历主张：** 建立容量校准、时序采集和回放工具，完成阶跃/渐变对照，定位预测/容差抑制和当前观测未达阈值两类等待。

**实现代码：** [集群内 k6 runner](../hack/lib/k6_runner.sh)、[容量校准](../hack/run_calibration.sh)、[观测采集](../hack/observe_live_baseline.py)、[证据解析](../hack/analyze/decision_replay.py)、[18 轮报告](benchmarks/live-baseline-20260910.md)、[回放报告](benchmarks/decision-replay-20260911.md)。

**现场操作：** 先展示本次报告中 607 条匹配的结果，再打开已有正式报告中的服务结果和逐轮数据，说明回放输入来源。必要时展示 runner 的固定 `service-clusterip` 路径与请求分布证据，而不是用 `port-forward svc/...` 作为多副本分流证明。

**预期结果：** 18 轮原模式决策一致；回放可以区分“已有足够当前 CPU，但策略选择了低信号”和“已观察的当前 CPU 本身还没有越过阈值”。

**边界：** 18 轮均未达到预设服务门槛，不能把 607 条规则匹配改写成 607 次独立实验或生产性能收益。Pod-seconds 是副本数对时间的积分，不是 CPU 消耗或云账单。公开紧凑输入不包含全部原始运行记录；重新回放公开输入与重新采集完整集群证据是不同步骤。

## Linux 提前完整验收

使用可访问 Docker 的 Linux 环境。已记录的验证环境为 cgroup v2、Python 3.12+、Kind v0.31.0、Helm v3.20.2、kubectl v1.35.0；节点镜像和 fixture digest 由入口固定。先执行只读预检：

```bash
python3 hack/interview_demo.py doctor --live --output benchmark-runs/interview-linux-doctor.json
docker info --format '{{.OSType}} {{.CgroupVersion}}'
kind version
helm version --short
kubectl version --client -o json
```

确认 Docker daemon 可访问、OS 是 `linux`、cgroup 为 `2`。预检只确认它实际检查的依赖和环境，不承诺 registry 连通、镜像缓存完整或后续验收一定成功。

为降低现场网络不确定性，在排练前准备 fixture 缓存。下列操作会拉取固定镜像，但不修改 Kubernetes 资源；网络失败时保留错误并停止，不换成浮动 tag：

```bash
docker pull kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f
docker pull quay.io/prometheus/prometheus@sha256:c0b857aead0d5793aa566adb8f49a9983d6f6031652098759d521a330cfa050f
docker pull registry.k8s.io/kube-state-metrics/kube-state-metrics@sha256:1545919b72e3ae035454fc054131e8d0f14b42ef6fc5b2ad5c751cafa6b2130e
docker pull busybox@sha256:141c253bc4c3fd0a201d32dc1f493bcf3fff003b6df416dea4f41046e0f37d47
```

运行公共入口，显式预载三个已缓存 fixture。每次运行创建新集群并在末尾删除；它不使用当前 kubectl context。这里故意使用 11 项完整功能验收，不能用跳过检查缩短计时：

```bash
RUN_ID=$(date -u +%Y%m%d-%H%M%S)
python3 hack/run_metrics_safety.py \
  --cluster-name "phpa-metrics-safety-${RUN_ID}" \
  --output "benchmark-runs/interview-live-${RUN_ID}" \
  --preload-fixture-image quay.io/prometheus/prometheus@sha256:c0b857aead0d5793aa566adb8f49a9983d6f6031652098759d521a330cfa050f \
  --preload-fixture-image registry.k8s.io/kube-state-metrics/kube-state-metrics@sha256:1545919b72e3ae035454fc054131e8d0f14b42ef6fc5b2ad5c751cafa6b2130e \
  --preload-fixture-image busybox@sha256:141c253bc4c3fd0a201d32dc1f493bcf3fff003b6df416dea4f41046e0f37d47
```

已记录的 2026-09-11 缓存环境成功运行约 10 分钟，入口默认运行预算 45 分钟；这不是新环境的耗时保证。Dockerfile 基础镜像和 Go 依赖仍可能需要网络，预载 fixture 不等于完全离线安装。

完成后核对两层结果：顶层 `summary.json` 的 `passed=true`、`run_error=null`、`diagnostic_errors=[]`、`cleanup_error=null`、`cluster_deleted=true`，以及 `acceptance/summary.json` 的 11 条检查全部成功。`source.json`、`image.json` 和 `image-identity.json` 连接源码快照、构建镜像与运行镜像；顶层 `artifact-manifest.json` 用于核验输出文件传输后的完整性。

验收覆盖同名前缀指标隔离、空闲缩容、滚动替换恢复、真实 CPU 扩容、新 leader 保护、保护到期后的缩容、指标中断时保持 Scale 与恢复后的协调。它不施加 HTTP 服务性能对照负载，不能代替 18 轮实验。

## 故障时按证据定位

| 现象 | 先看哪里 | 下一步 |
|---|---|---|
| 离线包完整性失败 | 报告中的具体路径/摘要错误 | 从排练通过的原包重新复制；不要重写清单掩盖文件变化 |
| 回放结果不匹配 | 失败的 run、cycle 和 expected/actual 字段 | 对照当前源码与冻结输入；保留失败输出 |
| Docker 不可访问 | `doctor --live`、`docker info` | 在演示前启动正确的 Linux Docker daemon；现场仍可运行已排练的离线包 |
| 镜像拉取/构建失败 | 编号命令回执的 stderr、`summary.json` | 修复网络或准备固定缓存，以新名称重试；不复用失败输出目录 |
| Helm 等待超时 | manager Pod 事件、日志与探针状态 | 检查镜像、资源、RBAC、端口；健康就绪不等于 `MetricsReady=True` |
| `MetricsReady=False` | condition reason/message、Prometheus targets、manager 日志 | 先区分暖机、源陈旧、requests 缺失、身份歧义和查询失败；缺失数据不补零 |
| CPU 很低且没有扩容 | `decisionCPU`、容差、实际请求副本 | 先确认有效负载与指标，再解释规则；静态空闲业务保持最小副本正常 |
| 清理失败 | `cleanup_error`、`ownership.json`、`private_workspace` | 保留恢复目录，核对集群/容器身份后再处理；不批量删除所有 Kind 集群 |

公共验收入口会收集诊断并做身份受限的清理。私有目录含 kubeconfig，不能当公开报告上传。宿主机崩溃或强制结束可能中断清理，必须检查最终状态。

常规部署烟测使用 `make test-e2e`，也会创建全新 Kind、使用私有 kubeconfig 和源码副本。默认部署没有 webhook，metrics 使用自动生成的自签名证书，因此不安装无关的 cert-manager。启用依赖 cert-manager 的清单后，显式执行 `CERT_MANAGER_INSTALL_SKIP=false make test-e2e`。该烟测检查 manager 与受保护的 metrics 访问，不代替上面的 11 项 CPU/故障验收。

## 常见追问

- **为什么用 EWMA？** 无需训练，参数少，便于把平滑、趋势和策略边界逐步解释。当前每次遍历有界历史，时间和空间为 O(n)；小 alpha 会增加滞后，阻尼与限幅不能弥补尚未观察到的负载。没有做 ARIMA/LSTM 的实证优劣对比。
- **预测未证明收益，项目价值是什么？** 实现了真实控制器完整链路，并用匹配实验与回放定位负面结果；结论约束下一步优化，避免把规则直觉当性能证据。
- **为什么指标按 UID 而不是 Pod 名前缀？** 名称会复用，前缀会误收 canary。UID 链确定对象归属，容器实例标识和对齐 requests 的集合避免把旧容器或缺项平均进来。
- **为什么重启后不立刻缩容？** 进程内历史丢失，不能伪造已有完整建议。以 live requested replicas 重建一个完整保护窗口，允许高需求扩容，代价是额外保留容量。
- **为什么不把历史每次写 status？** Scale 与历史是分开的写入，需要额外一致性、冲突与版本演进协议。当前选择有界内存加保守恢复，边界写在 ADR 中。
- **为什么 Helm Ready 还可能没有业务指标？** manager 探针证明健康端点响应；CPU 来源、样本门槛和归属有效性是每个 PHPA 的运行状态，由 `MetricsReady` 表达。
- **如果要继续优化？** 先解决源样本年龄与负载起点的配对问题，再按冻结协议比较周期或策略；已有六轮周期 pilot 没有有效配对，不能宣称缩短周期已带来收益。

继续深入时使用[实现设计](design.md)、[指标安全讲解](metrics-safety-guide.zh-CN.md)、[同输入回放讲解](benchmarks/decision-replay-guide.zh-CN.md)和[公共验收工作流](metrics-safety-workflow.md)。
