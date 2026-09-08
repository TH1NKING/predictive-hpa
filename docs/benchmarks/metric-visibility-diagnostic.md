# CPU 指标可见性与一分钟窗口：离线诊断协议

本轮在实现分析器和系统复算前固定以下范围。设计已参考已有报告和原始数据的
只读抽查，因此不是盲法预注册。目标是解释已有时间线，保留无法识别的部分，
不改变控制器查询、默认配置或现场负载。

## 输入与纳入规则

全部纳入 2026-09-07 的十次已封存实验，保持两批独立：

- `offsets`：`benchmark-runs/latency-diagnostic-20260907/verified/pilot-current-offsets/runs`
  中六次，原顺序为 `185359`、`190546`、`191743`、`192950`、`194156`、`195424`。
- `cadence`：`benchmark-runs/latency-diagnostic-20260907/cadence-followup/verified/pilot-current-cadence/runs`
  中四次，原顺序为 `210233`、`211430`、`212612`、`213754`，间隔 30/15/15/30 秒。

不因阶段、结果或异常排除运行；缺失、错误、冲突须保留质量标记，不能补零。
正式复算前验证各批 `SHA256SUMS`；报告记录输入摘要。分析器只读取每次的
`k6.json`、`controller.log`、`latency-plan.json`、`latency-observations.ndjson`，
不覆盖封存文件。输出到标准输出，或显式指定输入目录之外的新文件。

## 时间边界

起点是 k6 `scenario.startTime + 30s`，不使用进程启动时间。终点是第一次
成功提高 Scale 的 `spec.replicas` 的写入开始时间 `scaleWriteStartedAt`；
必须满足 `finalDesired > previousDesiredReplicas`，不能只与当前就绪副本比较。
Scale 成功响应另列为里程碑。只分析结束时间严格早于写入开始的观察请求，
以免把写入期间已发生的副本变化解释成扩容前现象。没有该终点时标记缺失，
不把整轮数据冒充扩容前证据。

## 原始样本与可见性

按完整 labels 与样本 timestamp 标识 counter；重复值一致只计一次，冲突保留
标记并禁止相关样本对产生斜率。首次出现的 raw 响应结束时间是可见上界。
下界只能来自此前成功、查询窗口覆盖该 timestamp 且未返回该样本的 raw 查询，
使用该请求的开始时间。为保守起见，只使用在首个正观察请求开始前已经结束的
负观察请求。没有合格负观察时下界为 `null`，不能当成零延迟。

矩阵查询窗口左开右闭：`evaluation - window < timestamp <= evaluation`。
source timestamp、query evaluation time 和请求响应时间是三种不同时间；
可见区间不是精确 ingestion 时间，也不是置信区间。并行的 counter、request
gauge 与一分钟表达式查询不是原子快照。

## 增量、窗口与表达式

主统计使用同 labels 相邻样本对，右端 timestamp 必须晚于负载起点，左端可以
在负载前；两个样本都必须已被合格 raw 响应看到。记录样本间隔、counter 差值、
首次可见区间与 `Δcounter / Δtime`（cores）。负差值视为 reset，不作正增量
或零负载；异常非有限值也不能生成斜率。

相邻 counter 差分只表示两个样本间平均使用的 CPU 核数，**不是短窗口 PromQL
`rate`**。不把它作为新窗口的控制器建议。Prometheus `rate` 还涉及 counter reset
修正与窗口端点外推，不能用简单差分代替其语义。

对每次合格 raw 查询分别数其 30 秒和 60 秒窗口的每序列样本数，保留不足两个
样本的情况；这仅描述该次 raw 快照的可计算性，不声称旁路表达式同时看到了
完全相同的样本。request gauge 保留实际观测值集合；原始矩阵保留序列标签。
分母稳定时可在
报告手算换算近期 cores，但不能把两个独立请求的结果称为精确同步归一值。

一分钟表达式单独保留每次 evaluation、请求区间和值；错误、空向量、非单值、
非有限值分别标记，绝不按 0 CPU 填充。55% 为既有协议阈值，严格使用 `>55`。
列出第一次超阈值请求区间及 Controller 查询开始/返回、Scale 响应里程碑。
响应到响应的有符号时间差是观察差，不是隔离后的因果贡献。

## 验证与结论边界

公开分析 CLI 是用户确认的测试接口。逐条使用独立手算夹具验证可见性边界、
左开窗口、reset/冲突、错误与空值、扩容前截断和禁止覆盖输入。正式输出保留
全部十次运行；另一路不导入本分析器的原始审计检查重要数字。

本轮不追加现场实验，不估计改窗口后的 HTTP 成功率、p95 或资源收益。
当前证据不能隔离 cAdvisor 更新、scrape、采集失败、TSDB 提交各自贡献；
也不能证明短窗口闭环控制效果。若后续实验改变窗口，需要同一二进制、固定
协调周期、预先冻结的运行顺序，以及同时保留原始采集和多窗口旁路结果。

参考：[Prometheus range vector 边界](https://prometheus.io/docs/prometheus/latest/querying/basics/#range-vector-selectors)、
[rate 语义](https://prometheus.io/docs/prometheus/latest/querying/functions/#rate)。

## 复算接口与输出

在仓库根目录执行，分别为两个 batch 保留其原顺序。输出父目录须已存在，
输出文件必须为新文件；省略 `--output` 时打印 JSON。

```powershell
$offsetRuns = (Get-ChildItem 'benchmark-runs/latency-diagnostic-20260907/verified/pilot-current-offsets/runs' -Directory | Sort-Object Name).FullName
python hack/analyze/metric_visibility.py --batch offsets $offsetRuns --output <新输出目录>/offsets.json
$cadenceRuns = (Get-ChildItem 'benchmark-runs/latency-diagnostic-20260907/cadence-followup/verified/pilot-current-cadence/runs' -Directory | Sort-Object Name).FullName
python hack/analyze/metric_visibility.py --batch cadence $cadenceRuns --output <新输出目录>/cadence.json
python -m unittest hack.analyze.test_metric_visibility -v
```

输出版本为 `metric-visibility-v1`，`runs` 数组逐项保留调用者提供的输入与 batch。
每项包括 `load_onset_unix`、`cutoff_seconds`、`scale_response_seconds`，以及：

- `samples`：完整标签、相对样本时间、counter 值、冲突标记、可见区间。
- `pairs`：右端在负载后的相邻样本对、间隔、增量、cores 斜率和有效性状态。
- `window_samples`：每个 raw 快照、每个返回序列的 30s/60s 左开右闭窗口样本数。
- `empty_raw_observations`：成功但空的 CPU/request 原始查询及其请求区间，另加
  `prom_cpu_raw_empty`／`prom_requests_raw_empty` 标记；没有返回序列时不虚构
  某个序列的窗口计数，也不把空查询当成 CPU 为零。
- `request_values_cores`：合格 request 矩阵的有限值集合。
- `evaluations` / `first_above_threshold`：一分钟表达式的观察区间、状态、值与首次 >55%。
- `controller_queries`：独立控制器查询起止边界、对应决策 CPU 值与查询错误。
- `quality_flags`：reset、冲突、非有限值、表达式空值/错误/多值、缺失成功扩容等。

无法读取或解析输入时 CLI 返回非零，不写成功输出；没有真实扩容时保留运行，
终点设 `null` 并标记 `missing_successful_expansion`，不分析未界定的响应时段。
运行数量、批次封存校验和最终报告的输入摘要由本轮复算流程检查，CLI 不隐式
扫描或挑选其他运行。
