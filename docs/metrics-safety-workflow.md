# Reproducible metrics-safety acceptance

The public runner builds the current repository Dockerfile, creates a dedicated
Kind cluster, installs a dedicated Prometheus and kube-state-metrics instance,
installs two managers through the repository Helm chart, and runs the existing
11 functional checks. It checks metrics isolation, rolling replacement, real CPU
scale-up, leader succession protection, and source outage/recovery. These checks
do not measure service latency or prove a performance advantage.

## Run locally on Linux

Run from a Git checkout. Use Docker with a cgroup v2 Linux host, Python 3.13,
Kind v0.31.0, Helm v3.20.2,
and kubectl v1.35.0. Kind and Helm match the verified local environment. The
[Kind v0.31.0 release](https://github.com/kubernetes-sigs/kind/releases/tag/v0.31.0)
publishes the pinned Kubernetes v1.35.0 node image used by the runner. Install
these tools before invoking the runner; it does not install host software.

```bash
python3 hack/run_metrics_safety.py \
  --cluster-name phpa-metrics-safety-my-run \
  --output benchmark-runs/metrics-safety-my-run
```

Choose a new cluster name and a nonexistent output directory for every attempt.
An existing cluster, conflicting node container, or existing output directory is
an error. The runner never uses the current kubectl context. Every Kubernetes and
Helm command uses its private kubeconfig and explicit Kind context.

The monitoring fixture is
`config/benchmark/metrics-safety-monitoring.yaml`. It pins Prometheus v3.11.3 and
kube-state-metrics v2.18.0 by digest and uses a 5-second scrape interval, matching
the functional acceptance environment. The workload fixture also pins Busybox by
digest. Network access is needed for images and the Dockerfile's Go dependencies.

## Evidence and cleanup

The runner snapshots existing tracked and nonignored untracked repository files
into a private build directory. `source.json` records file hashes, Git HEAD and
dirty status; `image.json` records the Dockerfile hash and built image identity.
It verifies the image loaded by containerd and the images of both Ready manager
Pods before starting acceptance. Docker base tags remain those declared by the
current Dockerfile; this records a particular build, rather than claiming those
tags always resolve to the same bytes.

Every command has a timeout and a numbered JSON receipt, including failed
commands. The run phase also has a 45-minute total budget, configurable with
`--timeout-seconds`; bounded diagnostics and cleanup still run after it expires.
On POSIX, each command has its own process group. A timeout or interruption sends
TERM to that group, allows 3 seconds for exit, then uses KILL if necessary with a
5-second bounded wait. Output files retain stdout/stderr even when descendants
outlive their direct parent. Once final diagnostics and cleanup begin, SIGTERM
and SIGINT no longer interrupt them or the summary/manifest writes; the entry
restores the original signal handlers when it returns.
`acceptance/` contains the existing script's observations and checks.
Diagnostics collect workload objects, manager logs, Lease state, events and
Prometheus targets/build information/configuration. `summary.json` distinguishes
run, diagnostic and cleanup failures; any of them makes the runner exit nonzero.
`artifact-manifest.json` hashes every preceding artifact for transfer checking.

An exclusive private owner file is mounted into the Kind node. Before deleting
the cluster, the runner checks that file's mount, the Kind label, the original
node container ID and, when the API responds, the cluster namespace UID. It
attempts cleanup even after partial creation or interruption. An unavailable API
permits cleanup only through the owned node container identity; a changed
identity prevents deletion. The final receipt confirms that the named cluster
and container are absent.

The kubeconfig stays outside the evidence directory. If guarded cleanup fails,
`private_workspace` identifies retained local recovery state, which includes the
kubeconfig and owner file; do not upload that directory. Inspect the recorded
container identity before manually removing any remaining resources. A force
kill, host crash or runner termination can interrupt cleanup and artifact upload.

## GitHub Actions

The **Metrics Safety Acceptance** workflow supports manual dispatch and relevant
pull-request or main-branch changes. It installs pinned Kind, Helm and kubectl
binaries with checksum verification, runs the offline runner tests, then invokes
the same public entry. The final upload step runs on success and failure and
uploads only the evidence directory. It does not publish controller images.

Workflow configuration being present is distinct from an observed hosted run.
Use the uploaded `summary.json` and check the workflow conclusion when citing
GitHub Actions acceptance evidence.
