# Stabilization Window Ablation Benchmark v2

> Final publication for the `stabilization-window-ablation-v2` campaign. The
> final matrix completed 27/27 experiments with 0 skipped runs in approximately
> 290 minutes. The implementation was merged in [PR #2](https://github.com/TH1NKING/predictive-hpa/pull/2)
> ([feature commit `09dbcf3`](https://github.com/TH1NKING/predictive-hpa/commit/09dbcf3f25f22c343550e1ef4b952c7ea1bf9064),
> [merge commit `bd47e486`](https://github.com/TH1NKING/predictive-hpa/commit/bd47e486c6d0840b1d9837e7e87ddc08dcde4d6f)).

The final report, environment snapshot, execution log, source snapshot,
analysis snapshot, and checksum manifest are preserved in the
[`benchmark-stabilization-window-ablation-v2` release](https://github.com/TH1NKING/predictive-hpa/releases/tag/benchmark-stabilization-window-ablation-v2).

**Post-publication note — 2026-09-05:** Source review identified a load-routing
limitation. The archived harness targets `http://localhost:8080` through
`kubectl port-forward svc/php-apache 8080:80`. Kubernetes documents that a
port-forward session selects one Pod, including when its target is a Service.
That path does not exercise Service distribution across the Deployment's
replicas. This confirms a limitation in the recorded harness; it does not
establish the actual per-Pod request distribution in all historical runs or
prove that routing caused their high failure rates. See the
[official port-forward reference](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_port-forward/)
and the [Service routing validation runbook](service-routing-validation.md).
The historical tables, archived files, checksums, release assets, and release
tag remain unchanged. This note reports no new experiment.

Subsequent [Service routing calibration results from 2026-09-05](service-routing-validation-results-20260905.md)
record separate fixed-replica observations on the new load path; they do not
reconstruct this campaign's historical traffic or revise its numerical data.

## 1. Goal and hypotheses

This ablation separates two comparisons that would otherwise be confounded:

1. Shortening the native HPA scale-down stabilization window from 300 seconds
   to 60 seconds should primarily reduce the post-load resource tail, without
   materially changing the first scale-up.
2. With both controllers using a 60-second window, PHPA can be compared with a
   matched native baseline for observed differences in request failures,
   scaling timing, peak capacity, and resource consumption.

These are engineering hypotheses, not pre-established conclusions. The
reported effects are descriptive differences between three-run group means.

## 2. Controllers and matrix design

| Published name | Matrix name | Controller and configuration | Scale-down window | Prediction variant |
|---|---|---|---:|---|
| Native-300 | `native_hpa_300` | Kubernetes HPA, `config/benchmark/native-hpa.yaml` | 300 s | none |
| Native-60 | `native_hpa_60` | Kubernetes HPA, `config/benchmark/native-hpa-60.yaml` | 60 s | none |
| PHPA-60 | `phpa` | Predictive HPA controller | 60 s | `ewma_damped_cap` |

The formal matrix contains three load patterns (`step`, `ramp`, and `spike`),
three controllers, and three repeats per pattern/controller pair:

`3 load patterns × 3 controllers × 3 repeats = 27 experiments`.

Controller order rotates within every load pattern to reduce run-order bias:

| Repeat | Controller order |
|---|---|
| r1 | Native-300 → Native-60 → PHPA-60 |
| r2 | Native-60 → PHPA-60 → Native-300 |
| r3 | PHPA-60 → Native-300 → Native-60 |

The load-pattern blocks ran in `step → ramp → spike` order. Each experiment
reset the Deployment to one replica, selected the controller, allowed a
30-second metric-accumulation period, ran k6, and then retained a 360-second
post-load observation tail before collection and extraction. Individual runs
lasted 612–674 seconds.

### Matrix completion record

The beginning of `matrix.log` records one prerequisite-check failure before any
experiment started. That invocation listed all 27 experiments as `PENDING` and
reported `0 already done`; it exited before the first `[1/27] ... starting`
entry. After the prerequisite was fixed, the wrapper again listed `0 already
done, 27 pending`, passed preflight, and ran the complete matrix from `[1/27]`
through `[27/27]`.

The final summary is therefore 27/27 successful, 0 skipped, and approximately
290 minutes elapsed. The initial preflight event is neither an experiment
failure nor a resumed partial matrix.

## 3. Effect definitions and reporting convention

- **Window effect** = `Native-60 - Native-300`. This comparison changes only
  the native HPA scale-down stabilization window.
- **Prediction effect** = `PHPA-60 - Native-60`. This compares the complete
  PHPA-60 controller with the matched 60-second native baseline.

Each controller value below is the mean ± sample standard deviation for `n=3`.
Each effect is an arithmetic difference between group means, followed by the
relative change in parentheses. A positive or negative sign is not inherently
better; interpretation depends on the metric. Failure-rate differences in the
text are stated in percentage points (pp).

## 4. Primary results

| Pattern | Metric | Native-300 | Native-60 | PHPA-60 | Window effect | Prediction effect |
|---|---|---:|---:|---:|---:|---:|
| ramp | First scale-up delay | 88 ± 1 s | 89 ± 1 s | 107 ± 9 s | +0.7 (+1%) | +18.0 (+20%) |
| ramp | Peak replicas | 5.0 ± 0.0 | 5.0 ± 0.0 | 10.0 ± 0.0 | +0.0 (+0%) | +5.0 (+100%) |
| ramp | Waste window after k6 stop | 368 ± 2 s † | 139 ± 9 s | 167 ± 9 s | -229.7 (-62%) † | +28.0 (+20%) |
| ramp | Failed rate | 75.88 ± 2.14% | 81.84 ± 5.79% | 78.56 ± 3.33% | +6.0 (+8%) | -3.3 (-4%) |
| spike | First scale-up delay | 73 ± 1 s | 74 ± 1 s | 97 ± 23 s | +0.3 (+0%) | +23.3 (+32%) |
| spike | Peak replicas | 5.0 ± 0.0 | 5.0 ± 0.0 | 10.0 ± 0.0 | +0.0 (+0%) | +5.0 (+100%) |
| spike | Waste window after k6 stop | 352 ± 9 s | 113 ± 8 s | 136 ± 9 s | -239.7 (-68%) | +23.3 (+21%) |
| spike | Failed rate | 61.65 ± 2.48% | 63.60 ± 6.90% | 59.22 ± 3.62% | +1.9 (+3%) | -4.4 (-7%) |
| step | First scale-up delay | 73 ± 1 s | 73 ± 1 s | 99 ± 3 s | +0.0 (+0%) | +25.7 (+35%) |
| step | Peak replicas | 5.0 ± 0.0 | 5.3 ± 0.6 | 10.0 ± 0.0 | +0.3 (+7%) | +4.7 (+88%) |
| step | Waste window after k6 stop | 370 ± 0 s † | 167 ± 18 s | 173 ± 13 s | -202.7 (-55%) † | +5.7 (+3%) |
| step | Failed rate | 95.58 ± 0.62% | 94.07 ± 2.44% | 91.96 ± 3.87% | -1.5 (-2%) | -2.1 (-2%) |

Resource consumption, measured across the full experiment:

| Pattern | Native-300 pod-seconds | Native-60 pod-seconds | PHPA-60 pod-seconds | Window effect | Prediction effect |
|---|---:|---:|---:|---:|---:|
| ramp | 2802 ± 11 | 1905 ± 15 | 2498 ± 249 | -897.5 (-32%) | +592.5 (+31%) |
| spike | 2685 ± 15 | 1705 ± 31 | 2875 ± 181 | -980.0 (-36%) | +1170.0 (+69%) |
| step | 2605 ± 9 | 1895 ± 74 | 2195 ± 154 | -710.0 (-27%) | +300.0 (+16%) |

## 5. Data-driven conclusions

For **Native-60 versus Native-300**:

- First scale-up changed by only 0.0–0.7 seconds and was effectively
  unchanged.
- Using the reported values, the post-k6 waste window decreased by
  approximately 203–240 seconds. The `step` and `ramp` reduction magnitudes are
  lower bounds because their Native-300 groups are censored.
- Total pod-seconds decreased by approximately 27%–36%.
- Mean failed rate changed by +6.0 pp for `ramp`, +1.9 pp for `spike`, and
  -1.5 pp for `step`. The direction is inconsistent, so this experiment does
  not support a claim that shortening the window improves request success.

For **PHPA-60 versus Native-60**:

- Mean failed rate was lower by approximately 2.1–4.4 pp across the three
  patterns.
- First scale-up was approximately 18–26 seconds later.
- Peak replicas increased by 88%–100%, or roughly doubled.
- Total pod-seconds increased by approximately 16%–69%.

The PHPA-60 failure-rate observation is therefore accompanied by later initial
scaling and substantially higher peak and cumulative capacity. Given the small
sample and overload regime, it should not be generalized beyond this matrix.

## 6. Censoring marker

`†` marks a **censored group**: at least one run ended while replica count was
still above `minReplicas`. A marked waste-window value is a lower bound, and a
marked scale-down mean omits runs for which the event could not be calculated.

All three Native-300 `step` runs ended without a detected scale-down. For
Native-300 `ramp`, two runs had no detected scale-down and the third ended above
`minReplicas`. Consequently, the Native-300 `step` and `ramp` waste windows are
lower bounds. Because the signed window effect is `Native-60 - Native-300`, its
reported value is an upper bound (the true value may be more negative), while
the corresponding reduction magnitude is a lower bound.

## 7. Environment and provenance

The archived environment snapshot records the following host and toolchain:

| Component | Recorded value |
|---|---|
| OS | Ubuntu 24.04.4 LTS, x86_64 |
| Kernel | `7.0.0-30-generic` |
| Docker | `29.7.2` |
| kind | `v0.31.0` |
| kubectl client | `v1.35.4` |
| Kubernetes server | `v1.35.0` |
| Kustomize | `v5.7.1` |
| Go | `1.26.2` |
| k6 | `v1.3.0` |
| Python | `3.12.3` |

The experiments recorded Git fixed point
`100db7254afe7056d80a0a0785ca15300a0b0f16` on branch
`bench/stabilization-window-ablation-v2`, with uncommitted and untracked
benchmark changes. The exact relevant source files are preserved in the final
source snapshot and checksummed in the final manifest. Those changes were later
committed as the benchmark implementation and merged through PR #2.

The environment file was generated at `2026-09-04T08:36:06Z`, after the
experiments. It is a retrospective snapshot, not a fully contemporaneous
capture. The matrix log independently corroborates only part of it, including
k6 `v1.3.0`, the `kind-hpa-dev` context, and successful cluster, monitoring,
CRD, Prometheus, and target-service prerequisite checks.

## 8. Known limitations

- The archived load harness uses a Service port-forward, which selects one
  Pod rather than exercising Service distribution across replicas. The current
  publication has no verified per-Pod request evidence sufficient to establish
  historical distribution or attribute failure rates to this routing issue.
  Capacity and request-success conclusions require a new routing calibration;
  see the dated post-publication note above.
- Each group has only `n=3`. Means, sample standard deviations, and effect
  differences are engineering summaries; no statistical significance is
  claimed.
- Group mean failure rates range from approximately 59% to 96%, and all-request
  p95 latency for nearly every group reaches the 10-second ceiling. This is a
  severe overload scenario, not representative of a normal SLA regime.
- Native-300 `step` and `ramp` results are censored as described above; their
  waste windows are lower bounds.
- Replica timelines use a 15-second Prometheus query step. `events.yaml` is
  retained only as a secondary signal because the three controllers do not emit
  identical event streams.
- The 27 runs used a dirty working tree. The final source archive and SHA256
  manifest preserve the relevant source snapshot, but the recorded Git commit
  alone does not reproduce that working tree.
- The environment inventory is a post-experiment snapshot. Only some versions
  and prerequisites are corroborated by contemporaneous matrix output.
- The prediction effect is a comparison of complete controllers after matching
  the 60-second window. It must not be over-interpreted as the pure causal effect
  of the prediction algorithm alone.
- The release analysis archive supports re-aggregation from `extract.json` and
  `metadata.yaml`; by design it does not contain the large raw `k6.json`,
  `prom.json`, `events.yaml`, or `controller.log` files, so raw-data extraction
  cannot be repeated from the release alone.
- PHPA v1alpha1 keeps stabilization history in memory, assumes a single
  controller replica, loses history on restart, supports only CPU metrics and a
  Deployment scale target, and coerces `minReplicas=0` to 1 at runtime.

## 9. Final release artifacts

Only the following six final artifacts are release assets. The integrity
manifest retains a broader packaging audit trail, but intermediate packages
mentioned in that trail are not release inputs or assets.

| Artifact | Size (bytes) | SHA256 |
|---|---:|---|
| [Final report](https://github.com/TH1NKING/predictive-hpa/releases/download/benchmark-stabilization-window-ablation-v2/predictive-hpa-ablation-v2-final-report-20260904.md) | 9,543 | `660D8609CF1BE7A1F9273CEFA4521E7CB7D01DEA5E6040C1A51EA86A1AF664C6` |
| [Environment snapshot](https://github.com/TH1NKING/predictive-hpa/releases/download/benchmark-stabilization-window-ablation-v2/predictive-hpa-ablation-v2-final-environment-20260904.txt) | 1,899 | `C59D44475857AA027A86B606B595FA171BB853C488BDDAD26B8D41849C08B971` |
| [Matrix log](https://github.com/TH1NKING/predictive-hpa/releases/download/benchmark-stabilization-window-ablation-v2/predictive-hpa-ablation-v2-final-matrix-20260904.log) | 1,027,137 | `D8B038BE21ADC05D0DE2B437B80B9B3EF5A4DBD7E589AAF9C335CC8D1C72AC97` |
| [Source snapshot](https://github.com/TH1NKING/predictive-hpa/releases/download/benchmark-stabilization-window-ablation-v2/predictive-hpa-ablation-v2-source-final-20260904.tar.gz) | 28,116 | `C455CF9AE0AC689282FFB4A9668A88BFF78664885ADCD7952182FC21CA165E4B` |
| [Analysis snapshot](https://github.com/TH1NKING/predictive-hpa/releases/download/benchmark-stabilization-window-ablation-v2/predictive-hpa-ablation-v2-analysis-final-20260904.tar.gz) | 149,683 | `DDB1383E4A1CB34E0EA7A76900DE910B75BBBFD74A8EDF47F903D76A7F216DC8` |
| [SHA256 manifest](https://github.com/TH1NKING/predictive-hpa/releases/download/benchmark-stabilization-window-ablation-v2/predictive-hpa-ablation-v2-final-manifest-20260904.sha256) | 9,055 | `5E4AE7E1B1C5F9D5F49184F1A2871532A7D07861D1A272FE3173EBB162C89922` |

The source archive contains nine benchmark-related repository files. The
analysis archive contains the final report, environment snapshot, matrix log,
and `extract.json` plus `metadata.yaml` for each of the 27 runs (57 files in
total). All archived members were checked against the final manifest.

## 10. Reproduction and verification

The commands below reconstruct the **historical v2 harness**, including its
port-forward load path, for source and analysis verification. They are not the
procedure for a new comparative benchmark. New runs must first pass
[Service routing validation](service-routing-validation.md) on a dedicated Kind
cluster. No new Kind, E2E, or benchmark execution was performed for the
post-publication update.

```bash
git clone https://github.com/TH1NKING/predictive-hpa.git
cd predictive-hpa
git checkout 100db7254afe7056d80a0a0785ca15300a0b0f16
tar -xzf ../predictive-hpa-ablation-v2-source-final-20260904.tar.gz -C .

python3 -m venv hack/analyze/.venv
source hack/analyze/.venv/bin/activate
python -m pip install -r hack/analyze/requirements.txt
python -m unittest discover -s hack/analyze -p 'test_*.py'
python -m unittest discover -s hack/tests -p 'test_*.py'

EXPERIMENTS_ROOT=experiments/ablation-v2 hack/run_matrix.sh --dry-run
# Historical execution command (reference only; do not reuse the v2 data root):
# EXPERIMENTS_ROOT=experiments/ablation-v2 hack/run_matrix.sh
```

To inspect the archived analysis and regenerate its aggregate presentation from
the 27 extracted records:

```bash
mkdir ablation-v2-analysis
tar -xzf predictive-hpa-ablation-v2-analysis-final-20260904.tar.gz \
  -C ablation-v2-analysis

rg -l '^  status: success' ablation-v2-analysis -g metadata.yaml | wc -l
rg 'Completed this session: 27|Skipped \(already done\): 0|Elapsed: 290 min|All planned experiments complete' \
  ablation-v2-analysis/matrix.log
python hack/analyze/aggregate.py ablation-v2-analysis --stdout \
  > regenerated-aggregate-report.md
```

The generated-at timestamp will change when the aggregate report is regenerated;
compare its tables and conclusions rather than expecting a byte-identical report.
To verify the downloaded release artifacts themselves, calculate all six
digests and compare them with the table above:

```bash
sha256sum \
  predictive-hpa-ablation-v2-final-report-20260904.md \
  predictive-hpa-ablation-v2-final-environment-20260904.txt \
  predictive-hpa-ablation-v2-final-matrix-20260904.log \
  predictive-hpa-ablation-v2-source-final-20260904.tar.gz \
  predictive-hpa-ablation-v2-analysis-final-20260904.tar.gz \
  predictive-hpa-ablation-v2-final-manifest-20260904.sha256
```
