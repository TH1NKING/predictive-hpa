# Dockerfile／Helm 指标安全入口验收：2026-09-11

**第 9 次公共入口运行完成了当前 Dockerfile 构建、独立 Kind 创建、Helm 双 manager 部署和全部 11 项功能检查，运行、诊断、清理均无错误。** 时间为 **2026-09-11 00:02:19–00:12:41（UTC+08）**；原始回执保留微秒时间。前八次失败也完整保留，未用成功结果覆盖。

这次验证的是本地 `linux/amd64` 环境中的交付与故障行为。Prometheus 和 Busybox 使用已缓存的原始固定 digest，kube-state-metrics 在新节点正常拉取；Docker 构建也使用了已有缓存。因此，它不能作为全冷缓存、全离线安装、其他架构或 GitHub 托管 runner 已通过的证据，也不证明性能服务标准达标。性能结果见[18 次正式基线](benchmarks/live-baseline-20260910.md)。

可公开核对的来源见[紧凑证据与完整性清单](benchmarks/assets/metrics-safety-evidence-20260911.json)，入口和使用边界见[工作流说明](metrics-safety-workflow.md)。

**实际运行身份**

| 项目 | 记录 |
|---|---|
| 冻结提交 | `09b6a1dc6b90f329ef5538617de1c20a88c4ccc4` |
| 源码状态 | Git 工作区干净；272 个快照文件与该提交逐字节一致 |
| 源快照 SHA256 | `284e5b6348ffe0f699e943cc12475044eb29bfc8839a23cf5d54f876c8ac53a8` |
| Dockerfile SHA256 | `e3c8edb193f5ef14ebe73cda9036745dbbb8b69028e6f9ac21ea990c40b0414c` |
| 构建镜像 | `metrics-safety/predictive-hpa:source-284e5b6348ffe0f699e9` |
| 独立集群 | `phpa-metrics-safety-20260910-entry9`，namespace UID `a517301c-5659-43ab-92c1-ca22d246ed0a` |
| 验收配置 | Kubernetes 1.35.0、`linux/amd64`；Prometheus 抓取 5 秒；目标 PHPA 稳定窗口 90 秒 |
| Helm 部署 | `replicaCount=2`、manager 镜像 `pullPolicy=Never`；验收开始前两个 manager 均 Ready |

5 秒抓取和 90 秒稳定窗口属于本页功能验收配置，与性能基线的 15 秒抓取、60 秒稳定窗口分别解释。

**怎样确认部署的就是构建镜像。** `source.json` 固定输入，`image.json` 记录构建 ID；本次 Docker 返回 OCI manifest digest，CRI 和两个 manager Pod 返回 config digest：

```text
Docker manifest：sha256:7fcfb7caa80cdaa2b6d8b66efabf3e7c51ebd4794fdd26b70ab1c7d19e2a0915
CRI / Pod config：sha256:857eeeeb339afc878320caee6c7630eddb9b9319c2574a9662849c15d651d484
```

入口读取已加载 manifest 的原始字节，核对其 SHA256，再核对 config 引用、大小与 config 内容 SHA256。`image-identity.json` 记录这一关联；`049-manager-images.json` 中两 Pod 的 `imageID` 均为上述 config digest，`ready=true`。这解释了两种 ID 为什么可以不同，同时保留了对不相关别名或被替换内容的拒绝。

**缓存准备没有更换镜像。** 成功运行显式预载了下列完整引用：

```text
quay.io/prometheus/prometheus@sha256:c0b857aead0d5793aa566adb8f49a9983d6f6031652098759d521a330cfa050f
busybox@sha256:141c253bc4c3fd0a201d32dc1f493bcf3fff003b6df416dea4f41046e0f37d47
```

`fixture-images.json` 记录缓存引用与摘要。入口将 Docker 导出的私有归档直接作为 stdin，送入固定节点容器 ID 的 `ctr images import --local --platform linux/amd64 --digests --base-name … -`，随后用原始完整引用执行 `crictl inspecti`。原多平台索引引用保留，但只导入本节点需要的平台内容。

| 导入流 | 字节数 | SHA256 |
|---|---:|---|
| Prometheus | 152934912 | `192147ce16882261fcb058fd75ecb8a778ecc4fc9866b3811f8d099b92a18a2f` |
| Busybox | 736256 | `0d5a03a21a139839cc668af054ea2b8256e171ac5c8f4980ca4ac64ee2579ccf` |

两份 `fixture-import-N.json` 与对应命令回执的 stdin 摘要、大小一致。私有镜像 tar 已在收尾删除；这里核对的是保留下来的导入回执，不能声称再次读取了已删除的 tar。kube-state-metrics 不在预载列表中，事件记录其固定引用在新节点成功拉取，耗时 11.072 秒。

**11 项实际结果**

| 检查 | 观察结果 |
|---:|---|
| 1 | 空闲 `web` CPU 低于 20%，同名前缀 `web-canary` 高于 100%，保持隔离 |
| 2 | 空闲目标在初始保护结束后缩至 1 个副本 |
| 3 | 滚动更新后恢复完整、新鲜指标 |
| 4 | 滚动更新后仍与 canary CPU 保持隔离 |
| 5 | 真实目标 CPU 负载触发成功的 Scale 增长 |
| 6 | 接替 leader 建立新的冷启动缩容保护 |
| 7 | 覆盖 30 秒观察段及前后边界的 8 份快照中，已请求副本与 Ready 副本均为 5 |
| 8 | 保护截止之后出现成功的 5→1 Scale 写入 |
| 9 | Prometheus 中断后 `MetricsReady=False` |
| 10 | 35 秒观察窗口内的 7 次采样均保持已请求 3 副本，旧 CPU 字段缺失，`lastScaleTime` 不变；该窗口无 controller Scale 写入 |
| 11 | 新鲜数据恢复后继续协调，成功从 3 缩至 1 |

这些是有界观察窗口与结构化日志支持的功能结论。第 10 项保持的是副本**请求**：故障阶段的早期快照仍只有 1 个 Ready，不能写成全程具有 3 个 Ready 副本。35 秒是检查窗口，也不等于 Prometheus 的精确停机时长。第 8 项的成功写入时间晚于日志中的保护截止时间，不能把检查脚本下一次读到状态的时间当作精确缩容时刻。

**九次尝试都保留**

| 尝试 | 失败或完成位置 | 收尾结果 |
|---:|---|---|
| 1 | Dockerfile 的 `go mod download` 遇到 `unexpected EOF` | 尚未创建 Kind，保留构建回执 |
| 2 | Go 依赖下载再次出现 EOF | 尚未创建 Kind，保留失败回执 |
| 3 | 解析 distroless 基础镜像时 registry 连接重置 | 尚未创建 Kind |
| 4 | 解析 Go 基础镜像时 registry 连接重置 | 尚未创建 Kind |
| 5 | 构建和加载成功，但旧代码把 manifest／config ID 不同误判为镜像不符 | 独立 Kind 已删除 |
| 6 | 固定 Prometheus 引用拉取返回 NotFound，180 秒就绪等待超时 | 独立 Kind 已删除 |
| 7 | Kind 使用全平台导入，缺少原索引中的 ARM64 内容 | 独立 Kind 已删除 |
| 8 | `docker cp` 返回 0，但 ctr 打开节点归档路径时报告文件不存在 | 独立 Kind 已删除 |
| 9 | stdin 导入实际节点平台后，正常 Dockerfile→Kind→Helm→11 项检查完成 | 运行、诊断、清理均成功 |

这些尝试推动了身份映射、可选固定引用预载、平台选择和 stdin 传输的修复，没有更换 fixture 的固定 digest，也没有降低就绪门槛或跳过检查。九次是入口修复与环境恢复过程，不是九次成功重复；11 项检查只在第 9 次全部完成。早期缺失部署导致的诊断错误也保留在各自 summary 中。

**清理与复核范围。** 第 9 次运行的 `run_error=null`、`diagnostic_errors=[]`、`cleanup_error=null`、`cluster_deleted=true`。最后的 `fault-final-environment.json` 再次确认：九个故障尝试名称和此前性能基线名称，共十个专用集群及对应节点容器均不存在；九次入口的私有目录均已删除。原共享集群仍为 `hpa-dev`，namespace UID `5ef8888a-dfe3-4b9b-b247-731010abe051` 未变。

独立复核检查了 115 份观察与 290 条验收命令，命令退出码均为 0，11 项结果均对应此前的对象快照；另一路完成了 1,021 项一致性检查，未发现矛盾。上述数量是证据校验项，不能换算为更多次独立故障实验。

```text
归档：fault-evidence.tar.gz
字节数：506166
SHA256：60d82d1825309c9b9b093e16908dc7369765c94bf913245a1a416cc370392c2f
完整性清单 SHA256：ba330d4fa93e35ca094f52043720474e0adb34db8fc8922d84293570fb181c94
证据文件：346；另含完整性清单，归档成员总数为 347
```

本地解包后已逐项核对全部 346 个文件。原始归档及所有失败日志保留在忽略目录 `benchmark-runs/live-baseline-20260910/`；公开 JSON 提供紧凑结果和哈希链，不能代替原始文件内容。公共入口可按[工作流说明](metrics-safety-workflow.md)重跑；[GitHub workflow](../.github/workflows/metrics-safety.yml)已经提供相同入口，但本报告没有声称托管 CI 已实际通过。
