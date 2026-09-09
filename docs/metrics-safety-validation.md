# 指标安全改造的验证记录

本次验证检查输入归属、故障行为和重启保护，不衡量请求延迟或相对原生 HPA 的收益。设计与代码讲解见[中文讲解](metrics-safety-guide.zh-CN.md)，验收要求见[实现规范](metrics-safety-plan.md)。

## 环境与身份

- Windows 工作区：`G:\dev\predictive-hpa`，实现基线 `9156078f326446bf9e5413bc0c810776a78564ae`。
- Linux：Ubuntu VM，内核 `7.0.0-31-generic`，Go `1.26.2`。
- Kind：`phpa-metrics-safety-20260909`，Kubernetes `v1.35.0`，使用独立 kubeconfig。
- 集群 `kube-system` UID：`5c6a59ea-daa5-4f57-838b-84fe7844d35f`。原有 `hpa-dev` 的负载未被用作本次验收对象。
- Prometheus `v3.11.3`，本次功能验收抓取间隔为 5 秒；这不同于旧压测的 15 秒采集配置。
- Busybox fixture 使用固定镜像 digest；Prometheus/cAdvisor 的实际标签与 systemd cgroup 路径已现场核对。

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
