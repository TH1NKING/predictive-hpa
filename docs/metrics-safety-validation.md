# 指标安全改造的验证记录

本次验证检查输入归属、故障行为和重启保护，不衡量请求延迟或相对原生 HPA 的收益。设计与代码讲解见[中文讲解](metrics-safety-guide.zh-CN.md)，验收要求见[实现规范](metrics-safety-plan.md)。

## 环境与身份

- Windows 工作区：`G:\dev\predictive-hpa`，实现基线 `9156078f326446bf9e5413bc0c810776a78564ae`。
- Linux：Ubuntu VM，内核 `7.0.0-31-generic`，Go `1.26.2`。
- Kind：`phpa-metrics-safety-20260909`，Kubernetes `v1.35.0`，使用独立 kubeconfig。
- 集群 `kube-system` UID：`5c6a59ea-daa5-4f57-838b-84fe7844d35f`。原有 `hpa-dev` 的负载未被用作本次验收对象。
- Prometheus `v3.11.3`，本次功能验收抓取间隔为 5 秒；这不同于旧压测的 15 秒采集配置。
- Busybox fixture 使用固定镜像 digest；Prometheus/cAdvisor 的实际标签与 systemd cgroup 路径已现场核对。

## 代码与部署检查

修复版对应实现提交 `9b1a677`；对 28 个 Go/go.mod/go.sum 文件逐一复核，本地提交内容与实际构建输入零差异。

| 检查 | 结果 |
|---|---|
| Linux `make lint-fix` | 0 issues |
| Linux `make test`，包括生成、fmt、vet、单元测试及 envtest | 全部通过；controller 89.9%，metricsprovider 86.2%，predictor 96.2% 覆盖率 |
| Linux controller/provider `go test -race` | 两包均通过，包含 controller envtest |
| `helm lint` 与实际 Helm 安装/升级 | 通过；两个 manager Ready |
| ServiceAccount 权限 | Pod/ReplicaSet 读取、Scale、Lease 等 13 项检查通过 |
| 独立代码审查 | Standards 0 项；Spec 原 2 项 P2 已修复，复审剩余 0 项 |

实际构建与运行身份：

```text
Go source manifest SHA-256:
c372bdd6d40d14a38e589761c955d1c45b47e276a6ae48c3886a43ec2e0a4dbf

manager binary SHA-256:
37f3c60b3ad049c835437d30130897a718816dd34878a30dbe5d3c440133da26

Both manager Pods' runtime image digest:
sha256:3804e5a7e5ca17cee625f82accf416d6abc126e504701ddfdfb3fc8cba6e792a
```

验收镜像使用 `CGO_ENABLED=0 go build -trimpath` 的静态二进制，加 scratch 和 `USER 65532:65532` 包装；已从 OCI layer 中核对 `/manager` 与构建二进制 SHA 一致。仓库 Dockerfile 未修改，本轮没有把其完整打包路径或跨 Kubernetes 版本兼容性记为通过项。

## 最终集群结果

修复版 `acceptance-2` 在 2026-09-09 11:09:14 UTC 完成，**11/11 检查通过**，运行与清理均无错误。取回归档后，另行核对了 108 份对象快照和 276 条成功命令，结果见[可核对摘要](verification/metrics-safety-20260909.json)。

| 场景 | 实际观测 |
|---|---|
| 同名前缀隔离 | 首个满足验收条件的同轮观测：空闲 `web` 1%，高负载 `web-canary` 300% |
| 滚动更新 | 新 Pod 恢复完整指标，`web` 继续低于 20%，未混入 canary 负载 |
| 真实 CPU 扩容 | 目标由 1 个已请求副本增加到 3 个 |
| leader 删除与接替 | 新 leader 报告 `ColdStartProtection`；30 秒观察段内的 6 份快照均保留至少 3 个副本 |
| 保护到期 | 保护截止为 11:06:44.507594 UTC；首次采样到缩至 1 个副本在 11:06:46.210618 UTC |
| Prometheus 中断 | 35 秒观察段内的 8 份快照均保持 3 个已请求副本，CPU 展示值为空，MetricsReady=False，lastScaleTime 不变 |
| 指标恢复 | MetricsReady 恢复 True，目标能够重新缩至 1 个副本 |

这些是约 5 秒间隔的对象采样，不是 Kubernetes 审计日志，也不代表精确的 Pod 启动耗时。策略边界另外由受控时间回归验证。此前 `acceptance-1` 的 11 项也通过，但它使用审查修复前的镜像，只作为过程记录保留。

## 验证方法

Go 单元测试通过真实 Prometheus HTTP API 形状的响应和 Kubernetes API 边界验证 Provider 行为；Reconcile 测试检查 Scale/status，envtest 使用真实 API Server/etcd。Linux race 检查覆盖共享观测缓存、查询串行化和控制器历史访问。

集群验收使用 [fixture](../config/benchmark/metrics-safety-workloads.yaml) 和 [运行脚本](../hack/verify_metrics_safety.py)：

1. 同时运行空闲 `web` 和高 CPU 的 `web-canary`，要求前者低于 20%、后者高于 100%，并观察空闲目标最终缩至一个副本。
2. 对 `web` 执行真实滚动更新，确认新 Pod 能恢复完整指标，并继续与 canary 隔离。
3. 给 `web` 加载真实 CPU 工作，观察 Scale 增长；停止负载后删除 leader Pod，检查新 leader 建立冷启动保护、保留容量并最终允许缩容。
4. 停止专用 Prometheus，检查 `MetricsReady=False`、清除过期 CPU 展示值、Scale 保持及 lastScaleTime 不变；恢复后检查指标与缩容恢复。

脚本要求专用 Kind context、独立 kubeconfig、对应节点名称、两个 Ready manager 和单副本 Prometheus。它拒绝复用已存在的 fixture namespace，保留逐条命令和对象快照，并在结束时清理自己创建的 namespace、恢复停止的指标源。

## 重跑场景

先按 [Helm 指南](../deploy/charts/predictive-hpa/README.md)准备一个新的独立集群、Prometheus 和当前源码构建的镜像。将 manager 的 `replicaCount` 设为 2；集群名必须以 `phpa-metrics-safety-` 开头，并明确导出它的专用 kubeconfig。

下面对应本次验收的资源名称；使用其他 release/namespace 时，传入对应参数：

```bash
python3 hack/verify_metrics_safety.py \
  --context kind-phpa-metrics-safety-20260909 \
  --kubeconfig "$KUBECONFIG" \
  --manager-namespace metrics-safety-manager \
  --manager-deployment metrics-safety-predictive-hpa \
  --prometheus-namespace metrics-safety \
  --prometheus-deployment prometheus \
  --output /tmp/metrics-safety-new-run
```

输出目录必须不存在；失败结果也会保存，不应覆盖后重跑。脚本会暂停指定的单副本 Prometheus，因此它必须是该专用集群内用于验收的实例。

## 证据保存

完整命令、源码逐文件 SHA-256、镜像与集群身份、对象快照及失败记录保存在本地忽略目录 `benchmark-runs/metrics-safety-20260909/`。环境准备阶段的 45 份收据已取回校验，归档 SHA-256 为 `348ed3a65b1222172d1ba76b838440a392679afdbc437756890827558cb38226`。

环境准备也保留了失败尝试：原节点已回收镜像压缩层，不能完整导出；随后下载固定 digest 并使用单平台导入，最后重新核对采集目标和镜像身份。没有把准备失败记为功能通过。

修复版部署归档 SHA-256 为 `605ff0e1b073038383d7cfe8261408fdedeb5915e1c846b8bc50c8d682ad6c76`；两轮验收及测试日志归档 SHA-256 为 `41a435ae320c91564aa836aeb0bf25e101d3913448907b4ee673e812da9496b4`，其中 40 个文件已逐一复核。代码规范失败、早期 envtest 冲突与成功复测日志均保留。

证据取回并复核后，已删除本任务创建的专用 Kind 集群。原 `hpa-dev` 仍在，集群 UID 未改变；源码快照、镜像身份和运行收据继续保存在本地归档中。
