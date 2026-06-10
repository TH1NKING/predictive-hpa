#!/usr/bin/env bash
#
# sync-chart-crd.sh
#
# 把 `make manifests` 生成的 CRD 真产物同步到 Helm chart 内。
#
# 为什么需要这个脚本：
#   CRD 的完整 OpenAPI v3 schema 是 controller-gen 从 predictivehpa_types.go
#   生成的产物（config/crd/bases/*.yaml）。chart 若手抄一份 schema，会与生成
#   产物 drift——改了 types.go、跑了 make manifests，chart 里的副本就过期了。
#
#   所以 chart 不手维护 schema：本脚本把真产物 cp 进 chart 的 files/ 目录，
#   templates/crd.yaml 用 .Files.Get 读它 + 包一层 installCRD 条件渲染。
#   source of truth 始终是 config/crd/bases/，chart 副本可一键重生成。
#
# 用法（在仓库根执行）：
#   make manifests          # 先确保 CRD 产物是最新的
#   ./hack/sync-chart-crd.sh
#   git diff                # 检查 chart 内 CRD 副本的变化
#
set -euo pipefail

# 仓库根 = 本脚本所在目录的上一级
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SRC="${REPO_ROOT}/config/crd/bases/autoscaling.brian.io_predictivehpas.yaml"
DEST_DIR="${REPO_ROOT}/deploy/charts/predictive-hpa/files"
DEST="${DEST_DIR}/crd-predictivehpas.yaml"

# 前置检查：源产物存在
if [[ ! -f "${SRC}" ]]; then
  echo "ERROR: CRD 产物不存在：${SRC}" >&2
  echo "       请先在仓库根跑 'make manifests' 生成 CRD。" >&2
  exit 1
fi

mkdir -p "${DEST_DIR}"

# 复制并在头部加一行来源标记（提醒任何人不要手改这个副本）
{
  echo "# 本文件由 hack/sync-chart-crd.sh 从 config/crd/bases/ 同步生成，请勿手改。"
  echo "# source of truth: config/crd/bases/autoscaling.brian.io_predictivehpas.yaml"
  echo "# 重新同步：make manifests && ./hack/sync-chart-crd.sh"
  cat "${SRC}"
} > "${DEST}"

echo "OK: CRD 已同步"
echo "    ${SRC}"
echo " -> ${DEST}"
