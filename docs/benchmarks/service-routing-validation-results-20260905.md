# Service routing validation results — 2026-09-05

The initial nine fixed-replica probes verified that requests reached every
selected application Pod through the in-cluster Service. All nine met the
preselected diagnostic criterion: HTTP 200 responses at least 99%, all-request
p95 at most 500 ms, and zero dropped iterations.

At a common 25 offered RPS, the subsequent 90-second probes met the criterion
with five and ten replicas, while one replica failed it. A 180-second comparison
in reverse order repeated the five-replica pass and one-replica failure. These
observations show that five replicas served a tested load that one replica
could not meet under the chosen criterion on this VM. No exact maximum RPS,
gain from five to ten replicas, or statistical significance is established.

The completed diagnostic contains **14 valid probes**: nine initial probes,
three at 25 RPS, and two longer confirmation probes. All 14 have routing
evidence; 12 met the service criterion and the two overloaded one-replica
probes failed it. One earlier harness-validation failure is preserved
separately and excluded from these counts.

This is a routing and capacity diagnostic following the
[Service validation protocol](service-routing-validation.md), not a new
autoscaler ablation or evidence of predictive-algorithm benefit. The
[historical v2 publication](stabilization-window-ablation-v2.md), its numerical
tables, archived datasets, checksums and release tag remain unchanged.

## 1. Design and decision rules

The initial phase used `1 → 5 → 10` fixed replicas. At each replica count it ran
`1 → 3 → 5` offered RPS, for 90 seconds each:

`3 replica counts × 3 load levels × 1 observation = 9 probes`.

The target was `Deployment/default/php-apache`, reached by an in-cluster k6 Pod
at `http://php-apache.default.svc:80`. Every request used a fresh connection
(`noConnectionReuse: true`). The controller objects targeting the Deployment
were backed up and temporarily removed for fixed-replica control; the phase
wrapper restored them afterward.

The diagnostic service criterion was selected before the phase:

- HTTP **200** responses divided by all completed request records **≥99%**.
- All-request `http_req_duration` p95 **≤500 ms**.
- Dropped iterations **=0**.

These thresholds are operational criteria for this diagnostic, not a production
SLA promise. The analysis counts HTTP 200 explicitly; redirects do not qualify.
Status `0` client errors remain in the completed-request denominator, and
dropped iterations are reported separately.

Routing evidence requires identified requests for every expected Pod, matching
Pod/EndpointSlice identities before and after the probe, unchanged restart
counts and valid per-Pod CPU samples. Application logs identify the probe with
`User-Agent: phpa-routing/<token>`, excluding unrelated requests.

## 2. Initial sweep results

The table uses independently re-read raw k6 records from the verified download.
Each row is one observation, not a group mean. All requests in this phase were
HTTP 200, so the all-request and HTTP-200-only p95 values are equal.

| Replicas | Offered RPS | Completed requests | HTTP 200 % | All-request p95 (ms) | Dropped | HTTP 200 RPS | Pods receiving requests |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 91 | 100 | 40.126 | 0 | 1.011 | 1/1 |
| 1 | 3 | 270 | 100 | 38.441 | 0 | 3.000 | 1/1 |
| 1 | 5 | 450 | 100 | 39.171 | 0 | 5.000 | 1/1 |
| 5 | 1 | 91 | 100 | 43.240 | 0 | 1.011 | 5/5 |
| 5 | 3 | 270 | 100 | 37.908 | 0 | 3.000 | 5/5 |
| 5 | 5 | 451 | 100 | 39.255 | 0 | 5.011 | 5/5 |
| 10 | 1 | 91 | 100 | 39.027 | 0 | 1.011 | 10/10 |
| 10 | 3 | 271 | 100 | 37.018 | 0 | 3.011 | 10/10 |
| 10 | 5 | 450 | 100 | 37.114 | 0 | 5.000 | 10/10 |

Across the nine probes, 2,435 completed requests all returned HTTP 200. No
timeouts, dropped iterations or malformed raw records were observed. The p95
range was 37.018–43.240 ms. Access-log request totals matched the corresponding
k6 request totals for every probe.

The recorded k6 execution intervals were 90 seconds each. Rates divide the
completed count by that measured interval, whose endpoints have whole-second
precision and include script startup and request drain. A count of 91 at
1 offered RPS is therefore reported as 1.011 completed RPS; it is not evidence
that the configured arrival rate changed.

### Per-Pod request and CPU evidence

For `replicas-10_rps-5_JdPFMq`, all ten Pods received requests. The table below
combines the independently checked access-log counts with CPU values from the
repository's routing summary. Pod names share the prefix
`php-apache-7c4bc6fc97-`.

| Pod suffix | Identified requests | Request share % | Mean CPU cores |
|---|---:|---:|---:|
| `bds5f` | 39 | 8.667 | 0.013883 |
| `gfr94` | 41 | 9.111 | 0.013727 |
| `jjrsr` | 41 | 9.111 | 0.013686 |
| `rf9hr` | 46 | 10.222 | 0.015447 |
| `rr5bw` | 49 | 10.889 | 0.018179 |
| `t4jdx` | 52 | 11.556 | 0.016171 |
| `tnktt` | 46 | 10.222 | 0.018015 |
| `v8552` | 45 | 10.000 | 0.013758 |
| `vxrbm` | 44 | 9.778 | 0.016030 |
| `xrslp` | 47 | 10.444 | 0.014229 |

At five replicas and 5 RPS, each Pod received 75–102 of 451 requests. At ten
replicas and 5 RPS, each received 39–52 of 450 requests. These are observed
shares, not a requirement or proof of perfectly uniform load distribution.

CPU uses a per-Pod sum of
`rate(container_cpu_usage_seconds_total{container="php-apache"}[1m])`, queried
every 15 seconds. The summary averages valid samples from 30 seconds after
k6 starts through its end and reports CPU in cores. Every initial probe had
the required per-Pod samples. Because the rate window is one minute, the early
included samples still contain some pre-load idle time; these are observation
means, not steady-state CPU estimates.

The generator's per-probe sampled peak CPU values ranged from 0.008760 to
0.014364 cores; sampled peak memory ranged from 16.352 to 24.492 MiB. These
are sampled observations. The one-minute CPU rate and 15-second query step
can miss short peaks, and low sampled generator usage alone cannot rule out
all shared-node contention.

## 3. Capacity follow-up

The initial sweep supports only the statement that each replica count handled
the tested loads through 5 offered RPS under the diagnostic criterion. The
highest passing initial load is the same for 1, 5 and 10 replicas, so there is
no initial-sweep evidence that more replicas increased sustainable throughput.

The completed `phase-capacity-25` ran 1 → 5 → 10 replicas at a common 25 offered
RPS for 90 seconds each. The diagnostic criterion remained unchanged.

| Replicas | Completed requests | HTTP 200 % | Timeouts | p95 all / HTTP 200 (ms) | Dropped | Actual execution (s) | HTTP 200 RPS | Criterion |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 2,249 | 8.537 | 2,057 | 10001.027 / 9255.391 | 1 | 100 | 1.920 | Fail |
| 5 | 2,251 | 100.000 | 0 | 86.990 / 86.990 | 0 | 90 | 25.011 | Pass |
| 10 | 2,251 | 100.000 | 0 | 70.205 / 70.205 | 0 | 90 | 25.011 | Pass |

The one-replica probe recorded 192 HTTP 200 responses and 2,057 client timeout
records (status `0`, error code `1050`). Its 100-second measured interval
includes request drain after the 90-second offered-load period. The five- and
ten-replica probes each completed 2,251 HTTP 200 requests without a timeout or
dropped iteration. The independently recomputed raw status counts and p95
values agree with this distinction.

All three probes still passed the routing-evidence checks; routing observation
does not require that an overloaded service meet the latency/success criterion.

| Replicas | Identified server requests | Requests per Pod | Mean application CPU per Pod (cores) | Generator sampled peak cores / MiB |
|---:|---:|---:|---:|---:|
| 1 | 1,211 | 1,211 | 0.461285 | 0.030 / 81.285 |
| 5 | 2,251 | 427–481 | 0.141787–0.163606 | 0.032 / 65.461 |
| 10 | 2,251 | 202–249 | 0.074353–0.092344 | 0.037 / 64.609 |

The one-Pod server-log count differs from both client completions and HTTP 200
responses. Timed-out client requests can still produce server access entries;
server-side completion logs and client outcomes must not be treated as the same
counter. At five and ten replicas, the identified server requests matched the
client totals and were distributed across every selected Pod.

Thus five replicas served a tested load that one replica could not meet under
the chosen criterion. Both five and ten replicas passed at the highest tested
load, so this phase does **not** establish a capacity increase from five to ten.
The lower p95 at ten replicas is one descriptive observation.

### Confirmation in reverse order

`phase-confirm-25` repeated the common 25 RPS load for 180 seconds per probe,
running five replicas followed by one replica. It reproduced the distinction:

| Replicas, in execution order | Completed requests | HTTP 200 % | Timeouts | p95 all / HTTP 200 (ms) | Dropped | Actual execution (s) | HTTP 200 RPS | Criterion |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 5 | 4,501 | 100.000 | 0 | 80.436 / 80.436 | 0 | 180 | 25.006 | Pass |
| 1 | 4,500 | 4.244 | 4,309 | 10000.899 / 9561.422 | 1 | 190 | 1.005 | Fail |

The five-replica probe returned HTTP 200 for all 4,501 completed requests;
each Pod logged 856–933 identified requests. The one-replica probe returned
191 HTTP 200 responses and recorded 4,309 client timeouts (status `0`, error
code `1050`). Its application log had 2,515 identified server entries, again
a different observation from client outcomes.

Routing and per-Pod CPU evidence were present in both probes. Mean application
CPU was 0.142066–0.161454 cores per Pod with five replicas and 0.460452 cores
with one replica. Generator sampled peaks were 0.036 cores / 72.398 MiB for
five replicas and 0.031 cores / 89.828 MiB for one. These sampled values did
not show high generator CPU or memory use, subject to the sampling and
shared-node limitations below.

Across the original and reversed-order probes, five replicas met the chosen
criterion at 25 offered RPS and one replica did not. The repetition supports
the observed one-to-five improvement at that load; it is not a statistical
significance test. Ten replicas were tested at 25 RPS only once and were not
tested beyond the highest passing five-replica load.

| Replicas | Highest tested passing offered RPS | Higher tested failing point | Scope |
|---:|---:|---|---|
| 1 | 5 | 25 RPS at 90s and 180s | No intermediate loads were measured |
| 5 | 25 | Not tested | Pass observed at 90s and 180s; upper capacity limit unknown |
| 10 | 25 | Not tested | One 90s pass; upper capacity limit unknown |

These are observed test bounds, not exact maximum sustainable rates. In
particular, passing at 25 rather than 5 offered RPS does not prove a fivefold
capacity gain, because the one-Pod limit between those loads was not measured.

## 4. Source, environment and execution history

The successful phase is `phase-initial-fixed`, using repository commit
`b2b3b7099a0890729fd624281bdce3d828a0fd42` from branch
`codex/fix-benchmark-service-routing`. The VM used a separate fresh source
clone. Its recorded `git-status.txt` is empty, and the wrapper checked the
exact commit and clean status before running. The VM source was tagged locally
as `calibration-service-routing-b2b3b70`; no GitHub tag or release was published.
The portable source bundle records `HEAD` at the full commit SHA rather than
the local tag reference.

The wrapper started at `2026-09-05T07:48:54Z`, captured its environment at
`07:48:55Z`, and created the run manifest at `07:48:58Z`. The phase ended at
`08:15:11Z`, 26 minutes 17 seconds after wrapper start, including setup,
observation, collection and restoration.

The capacity phase used the same recorded repository commit and pinned images.
Its wrapper started at `2026-09-05T08:16:34Z` and ended at `08:27:38Z`, taking
11 minutes 4 seconds including restoration.

The confirmation phase used the same source and image references. It started
at `2026-09-05T08:28:32Z` and ended at `08:39:01Z`, taking 10 minutes 29 seconds
including restoration. The three successful phases span 50 minutes 7 seconds
from the initial wrapper start to final confirmation completion, including
the gaps between phases and excluding the earlier failed invocation.

| Component | Recorded value |
|---|---|
| VM OS / architecture | Ubuntu 24.04.4 LTS / x86_64 |
| Kernel | `7.0.0-30-generic` |
| VM allocation | 8 virtual CPUs; 8,272,625,664 bytes RAM (approximately 7.70 GiB) |
| Docker | `29.7.2` |
| Kind | `v0.31.0` |
| Cluster / context | `hpa-dev` / `kind-hpa-dev` |
| Kubernetes server / kubectl client | `v1.35.0` / `v1.35.4` |
| Node topology | One node, `hpa-dev-control-plane`; generator and application share it |
| Python | `3.12.3` |
| k6 version text | `v1.3.0+dirty` (`commit/5870e99ae8-dirty`, Go `1.25.1`, linux/amd64) |
| Application CPU | Request `200m`, limit `500m` per Pod |
| Generator resources | CPU request `500m`, no CPU limit; memory request `512Mi`, limit `1Gi` |
| Generator startup timeout | `180s` in the live wrapper |
| Application endpoint | `http://php-apache.default.svc:80` |
| Connection reuse | Disabled |

The image references were pinned before probes:

```text
grafana/k6:1.3.0@sha256:a90b459a3768c46ad1013da53af24189f735d7112273c6ac3212ca8ed0e18656
registry.k8s.io/hpa-example@sha256:581697a37f0e136db86d6b30392f0db40ce99c8248a7044c770012f4e8491544
```

### Earlier failed invocation

An earlier `phase-initial` invocation used commit `45d86bb` and started its
first probe at `07:38:41Z`. The pinned k6 image reported `v1.3.0+dirty`; the
runner's version-text check rejected that build-metadata suffix during
artifact validation, even though k6 itself exited zero. The phase exited with
calibration code `3` and restoration code `0` at `07:40:17Z`.

The `+dirty` suffix is recorded in the image's own version output. It does
not indicate that the successful phase used a dirty repository checkout.
Commit `b2b3b7099a0890729fd624281bdce3d828a0fd42` corrected the version check;
the successful nine-probe phase then ran from the start. The earlier probe is
preserved separately and excluded from the nine valid observations above.

## 5. Evidence and integrity

The evidence archives are retained under the VM campaign root
`/home/baiwenchen/phpa-service-routing-20260905/` and downloaded into the ignored
local directory `benchmark-runs/live-calibration-20260905/`. The archive names
below identify retained files; no public download endpoint is claimed.

| Archive | Bytes | SHA256 |
|---|---:|---|
| `phase-initial-fixed-evidence.tar.gz` | 499,655 | `26394B4722743BD5835051EBA4CED1719A4D56BDB6324D066B67C574E920A236` |
| `phase-initial-failed-evidence.tar.gz` | 71,618 | `03CC8229FFDFDF431DB29A3F885A028EEFA311EEE2E23BF18EB80E4ACCEF3289` |
| `phase-capacity-25-evidence.tar.gz` | 850,028 | `E28C8E3BF41559E4363B8834BF219E943F7901ED39F3A1A4C87D572C9F2CD98A` |
| `phase-confirm-25-evidence.tar.gz` | 1,072,388 | `ADD52B4A949C984E57958E68B4753D5EAD88EED13E53859D2FF26BE15F6E2A58` |
| `service-routing-source-support-final-20260905.tar.gz` | 292,579 | `49F55344D3E9CDCAA230493D51941F8CE793DAF32CDE9DE04E43F05C7C91CC34` |

The successful initial archive contains 324 members, including environment and
source records, Service/Pod/EndpointSlice snapshots, per-probe raw k6 records and logs,
application access logs, application CPU and generator CPU/memory observations,
the phase log, restoration records, routing summaries and strict HTTP 200
analysis. The archive digest was verified after download.

The successful run is
`phase-initial-fixed/runs/20260905T074858Z_50Q2Yg/`. The repository analyzer is
`hack/analyze/calibration.py` at the recorded source commit. A separate
`analyze_live.py` re-read downloaded raw records and produced local
`initial-local-analysis/sla-summary.json` and `sla-report.md` at
`2026-09-05T08:18:03Z`; its strict HTTP 200, routing and integrity checks passed
for all nine probes. The helper's SHA256 is
`C2643C5DFDBECC4CF6172735BF4BDE50E3529F9139914832078B16EFC8BD8EC9`.
It is a campaign analysis helper, not a file covered by the repository commit.

The capacity archive contains 135 members, and its downloaded SHA256 was also
verified. Its run is `phase-capacity-25/runs/20260905T081637Z_j33yRY/`.
Independent local analysis at `2026-09-05T08:30:11Z` produced
`capacity-local-analysis/sla-summary.json` and `sla-report.md`. Routing was
observed in all three probes; the strict service criterion passed for five and
ten replicas and failed for one. No probe was excluded because it overloaded.

The confirmation archive contains 99 members. Its run is
`phase-confirm-25/runs/20260905T082834Z_aQH1qd/`. Independent local analysis at
`2026-09-05T08:46:30Z` produced
`confirmation-local-analysis/sla-summary.json` and `sla-report.md`, confirming
the five-replica pass and one-replica failure with routing evidence in both.

The source-support archive contains five files: `predictive-hpa-b2b3b70.bundle`,
`run_phase.sh`, `analyze_live.py`, `monitor.py` and `protocol.json`. The protocol
records selected settings and adaptive-phase rationale; it is not a run-status
record. The full source bundle's SHA256 is
`B974E90C2A782EE29A0C23E962FEF63CE4D3333B33873CF04FE3FB378F915604`.
All five archive SHA256 digests were checked against their VM copies and the
downloaded files. `final-evidence.sha256` retains the consolidated manifest.

The initial phase's final `phase-status.json` records calibration exit code
`0` and restoration exit code `0`. Restoration logs record the original
application image/pull policy being reapplied and the backed-up HPA and PHPA
objects being recreated. The capacity phase also records calibration and
restoration exit codes `0`/`0`, as does the confirmation phase. Independent
before/after comparisons found the Deployment, HPA and PHPA specifications
equal for all three phases. Calibration exit code zero means its collection
and routing checks succeeded, not that every probe met the service criterion.

The final cluster snapshot shows the application at 1/1 replicas, the PHPA
manager at 0/0, the original autoscaler objects present and no k6 runner Pod.
The pre-existing repository's dirty-file list was unchanged. Temporary task
access was removed after retrieval, with a receipt recording one removed
authorized-key entry; the local temporary key pair was also removed. No
credential material is needed to inspect the retained archives.

### Offline source and analysis reproduction

From a directory containing the five archives and `final-evidence.sha256`,
verify and reconstruct the fixed source without contacting a cluster:

```bash
sha256sum -c final-evidence.sha256
mkdir service-routing-review
cd service-routing-review
tar -xzf ../service-routing-source-support-final-20260905.tar.gz
git init source
git -C source fetch ../predictive-hpa-b2b3b70.bundle HEAD
git -C source checkout --detach FETCH_HEAD
git -C source rev-parse HEAD

tar -xzf ../phase-initial-fixed-evidence.tar.gz
tar -xzf ../phase-capacity-25-evidence.tar.gz
tar -xzf ../phase-confirm-25-evidence.tar.gz
python3 analyze_live.py phase-initial-fixed/runs/20260905T074858Z_50Q2Yg \
  --output-dir review-initial
python3 analyze_live.py phase-capacity-25/runs/20260905T081637Z_j33yRY \
  --output-dir review-capacity
python3 analyze_live.py phase-confirm-25/runs/20260905T082834Z_aQH1qd \
  --output-dir review-confirmation
```

The bundle exposes `HEAD`, so the explicit fetch and detached checkout above
recover the recorded commit. The strict analyzer uses the Python standard
library and refuses existing output files. Its initial-phase exit code is
zero; capacity and confirmation return one because their one-replica probes
fail the diagnostic criterion. Inspect the reports to distinguish a measured
criterion failure from missing or invalid evidence. Generated timestamps and
local paths differ on re-analysis; compare counts and metrics.

## 6. Interpretation limits

- These are single observations per initial condition, with short probes and
  an adaptive follow-up. No statistical significance is claimed.
- Routing observations apply to the new Service path. They do not reconstruct
  historical v2 request distribution or prove the cause of v2's high failures.
- Fresh connections, workload/environment differences and the routing change
  prevent attribution of cross-campaign performance changes to any one factor.
- Pod and endpoint snapshots show the two sampled states, not continuous
  readiness throughout every instant of a probe. CPU and memory sampling miss
  short events, and node snapshots do not measure contention over time.
- Client request counts and server access-log totals can diverge under
  timeouts; inspect both rather than assuming their agreement in overload.
- The generator and target share a single VM/node. Higher loads require a
  separate headroom and capacity assessment.
- The repository analyzer keeps `capacity_validated: false` by design. A
  routing pass and a service-criterion pass alone do not establish capacity.
- No PHPA/native-HPA performance comparison or prediction-algorithm change
  was tested in this calibration.
