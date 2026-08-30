#!/usr/bin/env bash
# 把 store-daily 的完整 git 历史打成 bundle，推到局域网备份机 + 远端。
# 数据备份走 backup_offsite.sh（SQLite）；本脚本只备份代码（.git）。
# 只在有 .git 的机器上跑（开发机/部署机），部署后调用一次。
#
# 用法：
#   ./scripts/backup_code.sh
#   ./scripts/backup_code.sh --dry-run   # 只打 bundle 不推送
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .git ]]; then
  echo "本机没有 .git，跳过代码备份（灾备机/生产机无历史可打）" >&2
  exit 0
fi

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) echo "未知参数: $arg" >&2; exit 1 ;;
  esac
done

# 复用 offsite 配置
if [[ -f "${ROOT}/scripts/backup.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "${ROOT}/scripts/backup.env"
  set +a
fi
# .env 里也可能有远端目标，缺省时兜底
if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "${ROOT}/.env"
  set +a
fi

BUNDLE="store_daily_code.bundle"
BUNDLE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/store-daily-code.XXXXXX")"
BUNDLE_PATH="${BUNDLE_DIR}/${BUNDLE}"
trap 'rm -rf -- "${BUNDLE_DIR}"' EXIT

HEAD="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "== 打代码 bundle @ ${HEAD} -> ${BUNDLE} =="
# --all 收全部分支/标签；用指定位文件保证随意重建可用
git bundle create --quiet "${BUNDLE_PATH}" --all
SFX="$(TZ=Asia/Shanghai date +%Y%m%d_%H%M%S)"
REMOTE_BUNDLE="store_daily_code_${SFX}.bundle"

scp_p() {
  local port="$1"; shift
  scp -q -P "${port}" "$@"
}
ssh_p() {
  local port="$1"; shift
  ssh -o ConnectTimeout=15 -o BatchMode=yes -p "${port}" "$@"
}

# 代码 bundle 在备份机上保留的份数；每次部署推一份，旧的自动清理（0 = 不清理）
CODE_BUNDLE_KEEP="${CODE_BUNDLE_KEEP:-10}"

# 推送成功后清掉旧 bundle，只留最新 CODE_BUNDLE_KEEP 份（bundle 是全量历史，旧的冗余）
prune_bundles() {
  local port="$1" target="$2" dir="$3" keep left
  keep="${CODE_BUNDLE_KEEP:-10}"
  if [[ "${keep}" == "0" ]]; then
    return 0
  fi
  left="$(ssh_p "${port}" "${target}" \
    "cd '${dir}/code' && ls -1t store_daily_code_*.bundle 2>/dev/null | tail -n +$((keep + 1)) | while read -r f; do rm -f -- \"\$f\"; done; ls -1 store_daily_code_*.bundle 2>/dev/null | wc -l")"
  echo "  已清理旧 bundle，现存 ${left} 份（保留最新 ${keep} 份）"
}

PUSHED=0
if [[ -n "${LAN_BACKUP_HOST}" ]]; then
  LAN="${LAN_BACKUP_USER:-root}@${LAN_BACKUP_HOST}"
  # 备份机是手机时，「隐私 MAC」重连后会换 MAC；本机旧 ARP 表项会把包发往失效的
  # 旧 MAC，表现为长时间连不上。连接前主动清一次 ARP（免 sudo），强制重新解析。
  arp -d "${LAN_BACKUP_HOST}" >/dev/null 2>&1 || true
  echo "→ 推送到局域网备份机 ${LAN}:${LAN_BACKUP_DIR}/code/ (ssh ${LAN_BACKUP_SSH_PORT:-22})"
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "  [dry-run] scp ${BUNDLE}" 
  else
    ssh_p "${LAN_BACKUP_SSH_PORT:-22}" "${LAN}" "mkdir -p '${LAN_BACKUP_DIR}/code'"
    scp_p "${LAN_BACKUP_SSH_PORT:-22}" "${BUNDLE_PATH}" "${LAN}:${LAN_BACKUP_DIR}/code/${REMOTE_BUNDLE}"
    PUSHED=1
    prune_bundles "${LAN_BACKUP_SSH_PORT:-22}" "${LAN}" "${LAN_BACKUP_DIR}"
  fi
fi

# 规范化远端目标（与 backup_offsite.sh 相同）
REMOTE_TARGET=""
if [[ -n "${REMOTE_BACKUP}" ]]; then
  if [[ "${REMOTE_BACKUP}" == *@* ]]; then
    REMOTE_TARGET="${REMOTE_BACKUP}"
  elif [[ -n "${REMOTE_BACKUP_USER}" ]]; then
    REMOTE_TARGET="${REMOTE_BACKUP_USER}@${REMOTE_BACKUP}"
  else
    REMOTE_TARGET="${USER}@${REMOTE_BACKUP}"
  fi
fi
if [[ -n "${REMOTE_TARGET}" ]]; then
  echo "→ 推送到远端 ${REMOTE_TARGET}:${REMOTE_BACKUP_DIR}/code/ (ssh ${REMOTE_BACKUP_SSH_PORT:-22})"
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "  [dry-run] scp ${BUNDLE}"
  else
    ssh_p "${REMOTE_BACKUP_SSH_PORT:-22}" "${REMOTE_TARGET}" "mkdir -p '${REMOTE_BACKUP_DIR}/code'"
    scp_p "${REMOTE_BACKUP_SSH_PORT:-22}" "${BUNDLE_PATH}" "${REMOTE_TARGET}:${REMOTE_BACKUP_DIR}/code/${REMOTE_BUNDLE}"
    PUSHED=1
    prune_bundles "${REMOTE_BACKUP_SSH_PORT:-22}" "${REMOTE_TARGET}" "${REMOTE_BACKUP_DIR}"
  fi
fi

if [[ "${PUSHED}" == "1" ]]; then
  echo "代码 bundle 已推（含 HEAD ${HEAD} 之前全部历史）"
elif [[ "${DRY_RUN}" != "1" ]]; then
  echo "警告: 未配置任何 offsite 目标，代码 bundle 只留在本机" >&2
fi