# 面试演示验证：2026-09-20

本轮围绕简历中的 PredictiveHPA 四项主张，修复配置保护与部署入口，并增加可独立携带的离线演示。基线提交为 `42ddf562ea9919b176d562a01b002bacbfe8c17e`，验证对象包含工作区改动，不代表已发布版本。

## 修复及其原因

| 问题 | 修复后的行为 | 验证 |
|---|---|---|
| `scaleTargetRef.apiVersion` 被忽略，其他 API group 的同名目标可能被当作 Deployment 伸缩 | 只接受明确的 `apps/v1 Deployment` 与合法名称 | Reconcile 回归确认非法配置不读取指标、不访问 Scale，修复配置后恢复 |
| 不支持的 kind 提前返回，可能残留上一代 `MetricsReady=True` | 统一写入当前 generation 的 `InvalidConfiguration`，清除旧 CPU 展示值 | 与 CPU、alpha、算法、模式、稳定窗口等共 18 类非法配置一起验证 |
| 旧 schema 存储的零 CPU 目标或负稳定窗口绕过 admission 边界 | 运行时拒绝，避免错误最大扩容或窗口操作异常 | 真实 envtest 先存旧对象，再升级严格 schema，验证状态更新与修复恢复 |
| Helm 只有容器启动状态，缺少 manager 探针 | chart `0.3.1` 提供 `/healthz`、`/readyz`，metrics 开关不影响健康端口 | 实际 Helm 渲染及双 manager 功能验收 |
| E2E 复用集群与当前 context，嵌套 make 存在误操作风险 | 全新集群、私有 kubeconfig 和源码快照；逐命令核对节点与集群身份；失败保留证据并清理 | 命令边界回归与独立入口验证 |
| 面试现场依赖 Go、仓库、集群与网络 | 提前构建带摘要清单的包，现场复用生产策略重放已提交证据 | Windows/Linux 脱离仓库、空 PATH、无 kubeconfig 均成功 |

## 离线回放

两端均核对完整 18 轮 step/ramp × 三模式 × 三次重复，607 条原模式决策全部匹配。其中 457 条通过完整 CPU 历史重算预测，150 条保留记录预测值，不填补未知初始 CPU。

三个负例分别验证：负预测时距退出 `2`、篡改期望决策退出 `1`、未知 JSON 字段退出 `2`。成功 JSON 不得与拒绝结果同时输出。包缺项、摘要变化、错误平台、子进程超时和中断均有失败回执，旧输出目录不会被覆盖。

报告同时展示观测副本、已请求副本、策略候选值和跳过原因。以 step Predictive r3 首轮为例：当前 CPU 约 `91.28%`、预测约 `50.65%`、目标 `50%`；Predictive 虽有候选副本 `2`，仍因 `WithinToleranceBand` 不写 Scale。Current/Hybrid 在同输入下支持扩容。候选值不能直接解读为成功写入。

## Linux 功能验收

公共入口 `hack/run_metrics_safety.py` 于 **15:21:32–15:32:47（UTC+08）** 完成一次独立 Kind 验收，耗时约 11 分 15 秒。Dockerfile 构建、镜像身份关联、Helm 双 manager、11 项检查、诊断及清理全部通过。

| 检查 | 本次结果 |
|---|---|
| 同名前缀指标归属 | 空闲 web 低于 20%，高负载 web-canary 超过 100%，保持隔离 |
| 缩容与滚动更新 | 保护到期后缩至 1；滚动更新后恢复完整新鲜指标，并继续与 canary 隔离 |
| 真实 CPU 扩容 | 出现成功 Scale 增长 |
| 切主保护 | 接替 leader 建立新保护，30 秒观察段保留 2 个副本，保护到期后允许缩容 |
| Prometheus 中断 | `MetricsReady=False`；35 秒观察段保持 Scale 并清除旧 CPU 字段 |
| 指标恢复 | 新鲜源恢复后继续协调和伸缩 |
| 清理 | `cluster_deleted=true`，运行、诊断、清理无错误；结束后仅剩原有 `hpa-dev` 集群 |

回执包含 109 份观察快照、273 条验收命令。取回本地后逐项核对了 80 个证据文件的 SHA256，并确认 32 个生产源码、依赖、Dockerfile 和 chart 文件与当前工作区一致。数量表示证据条目，不表示独立实验重复次数。

可提交的紧凑证据在 [interview-live-20260920.json](verification/interview-live-20260920.json)。完整对象和命令回执位于本地忽略目录 `benchmark-runs/interview-20260920/linux-evidence/benchmark-runs/interview-live/`；包含 kubeconfig 的私有执行目录已由入口清理，没有纳入归档。

```text
源快照 SHA256：2c52e38bab832865abe9ba9168283d820b11c0fc70953836f75433f98739f3b1
证据归档 SHA256：7c004d7cd59c482b294418bf2e17cf0a596a79fe478d72d1d26f5dce95508af6
```

## 验证范围与限制

- 最终 Linux 相关 Python/Helm 回归共 **48 项全部通过，无跳过**：演示包 12 项、E2E 入口 12 项、指标验收入口 23 项、Helm 渲染 1 项。
- Linux `GOFLAGS=-race make test` 通过，包含生成、vet、单元测试与真实 envtest；生成 CRD/DeepCopy 没有差异。
- 包含 logcheck 的 `make lint-fix` 最终为 `0 issues`。首次快照遗漏插件配置、随后插件下载失败，以及发现重复测试常量的失败日志均保留；补齐配置、恢复工具并修复常量后通过。
- Windows 的 Go 单元测试和 vet 通过。进程组与真实 Kubernetes 验证使用 Linux 结果，不能由 Windows 测试替代。
- 本次集群为 `linux/amd64`，固定 Prometheus/Busybox 镜像使用预载缓存，kube-state-metrics 由节点拉取；这不是全冷缓存或完全离线安装证明。
- 11 项检查证明有界窗口内的控制器功能与故障行为，不产生新的 HTTP 成功率、p95、提前扩容或成本收益。607 条匹配仍属于离线策略回放。
- 新增双平台演示 CI 和 E2E 证据上传配置；本地验证不等于 GitHub 托管 CI 已运行。本轮未推送或发布。

实际演示按[面试演示手册](interview-demo.zh-CN.md)执行，每次使用新的输出目录。

## E2E 实际部署烟测

第三次专用 Kind 运行于 **16:00:57–16:01:55（UTC+08）** 完成，两个原有 Ginkgo 场景全部通过，零失败、零跳过，另有 metrics 探针配置测试通过。场景覆盖 manager 部署以及带认证的 metrics 访问；整次运行、诊断、清理无错误，集群已删除。该约 58 秒耗时来自已有工具与镜像缓存的环境，不代表全冷启动时间。

前两次失败保留在独立目录，均确认自建集群已清理：

1. 默认安装的 cert-manager 镜像拉取接近五分钟，就绪等待到期，两个场景尚未开始。默认清单并不使用 cert-manager，现仅在显式 `CERT_MANAGER_INSTALL_SKIP=false` 时安装，不减少原有两个场景的断言。
2. manager 场景通过；浮动 curl 镜像拉取耗尽探针等待，且集群域名被环境 DNS 解析到外部代理地址。现在从单一文件读取固定镜像摘要，在创建集群前验证缓存或拉取，再按实际节点平台导入；探针从 Kubernetes API 获取 Service ClusterIP，以 `--resolve` 和 `--noproxy` 访问真实 Service。它验证 Service 与 metrics，不把本机外部 DNS 当集群行为。

探针使用 Pod 投射的 ServiceAccount token，在容器内通过 stdin 传给 curl；不把令牌嵌入命令参数、Pod spec 或 verbose 日志。保留自签名 TLS 测试边界，成功需同时有 `HTTP_STATUS=200` 与实际 Go metrics 内容。

固定镜像为 `docker.io/curlimages/curl@sha256:9a1ed35addb45476afa911696297f8e115993df459278ed036182dd2cd22b67b`（本次从 `8.14.1` 获取并核对）。[E2E 紧凑证据](verification/interview-e2e-20260920.json)包含三次摘要、最终源码摘要和归档哈希；完整成功回执在本地 `benchmark-runs/interview-20260920/e2e-final/`，32 个文件已逐项核验，9 个关键 E2E 源文件与工作区一致。早期失败原始回执仅保存在忽略目录，不发布其中的临时集群令牌。

## 2026-10-03 GitHub CI 兼容性修复

发布 [PR #15](https://github.com/TH1NKING/predictive-hpa/pull/15) 后，GitHub runner 的 Docker 归档暴露出另一条导入路径：`docker save` 生成的归档经 containerd 导入后具有新的 manifest 摘要，原 registry 的多平台索引引用不可见。首次 E2E 因此在固定摘要的 CRI 查询阶段失败，入口保留了回执并清理专用集群。

E2E 现在显式允许在归档导入成功、但原固定引用仍不可用时，由自建节点按相同 digest 和节点平台从 registry 拉取，再验证 CRI 身份。不会把重新生成的 manifest 强行标为原 digest，也不会在导入本身失败时掩盖错误。默认的指标安全预载入口保持原有行为。

这里的兼容性路径可能需要节点访问 registry；已有缓存不再被解释为所有 Docker 存储格式下都能离线导入。上文的源码摘要和验收数字记录的是 9 月 20 日快照，不替代本次变更后 PR head 的 GitHub 检查。
