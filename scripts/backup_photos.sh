#!/usr/bin/env bash
# 门店日报：把照片目录 data/uploads/imei 每天镜像一份到局域网备份机（只落局域网，不推远端）。
#
# 为什么单独一个脚本：
#   照片是纯增量、体量大（一张 100–500KB，一年几千张）。原来混在「每小时全量 uploads 快照」里，
#   一张图会被复制成 14 天 × 24 份 = 336 份，生产机 8 天就盘满（2026-11-09 排查结论）。
#   现在 uploads 快照用 --exclude='uploads/imei'，照片由本脚本每天整目录覆盖镜像 1 份 —— 不按份数放大。
#
# 配置：自动读取 scripts/backup.env（LAN_BACKUP_HOST / LAN_BACKUP_USER / LAN_BACKUP_DIR / LAN_BACKUP_SSH_PORT），
#   也可用 PHOTO_DEST_HOST / PHOTO_DEST_PORT / PHOTO_DEST_FILE 单独覆盖。
# 告警：复用 scripts/notify_failure.py 推送企业微信群（独立状态文件 .photo-alert-state.json）。
# 落点：局域网备份机的 ${LAN_BACKUP_DIR}/imei_photos.tar（单文件，原子校验后覆盖写）
# 恢复：tar xf imei_photos.tar -C /opt/store-daily/data/uploads
#
# 用法（在生产机）：
#   /opt/store-daily/scripts/backup_photos.sh
#   /opt/store-daily/scripts/backup_photos.sh --dry-run
# cron（install_backup_cron.sh 会一并安装）：
#   20 3 * * * /opt/store-daily/scripts/backup_photos.sh >>/var/log/store-daily-photos.log 2>&1
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if command -v flock >/dev/null 2>&1; then
  exec 9>"${TMPDIR:-/tmp}/store-daily-photos.lock"
  if ! flock -n 9; then
    echo "另一份照片镜像还在跑，跳过"
    exit 0
  fi
fi

if [[ -f "${ROOT}/scripts/backup.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "${ROOT}/scripts/backup.env"
  set +a
fi

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,18p' "$0"
      exit 0
      ;;
    *)
      echo "未知参数: $arg" >&2
      exit 1
      ;;
  esac
done

PRIMARY_DIR="${PRIMARY_DIR:-/opt/store-daily}"
SRC="${PHOTO_SRC:-${PRIMARY_DIR}/data/uploads/imei}"
LAN_BACKUP_HOST="${LAN_BACKUP_HOST:-}"
LAN_BACKUP_USER="${LAN_BACKUP_USER:-backup}"
LAN_BACKUP_DIR="${LAN_BACKUP_DIR:-store-daily-backups}"
LAN_BACKUP_SSH_PORT="${LAN_BACKUP_SSH_PORT:-22}"

if [[ -n "${PHOTO_DEST_HOST:-}" ]]; then
  DEST_HOST="${PHOTO_DEST_HOST}"
elif [[ -n "${LAN_BACKUP_HOST}" ]]; then
  if [[ "${LAN_BACKUP_HOST}" == *@* ]]; then
    DEST_HOST="${LAN_BACKUP_HOST}"
  else
    DEST_HOST="${LAN_BACKUP_USER}@${LAN_BACKUP_HOST}"
  fi
else
  DEST_HOST=""
fi
DEST_PORT="${PHOTO_DEST_PORT:-${LAN_BACKUP_SSH_PORT}}"
DEST_FILE="${PHOTO_DEST_FILE:-${LAN_BACKUP_DIR}/imei_photos.tar}"

STAMP="$(TZ=Asia/Shanghai date +%Y%m%d_%H%M%S)"
NOTIFY_PY="${ROOT}/scripts/notify_failure.py"
PHOTO_ALERT_STATE="${PHOTO_ALERT_STATE:-${PRIMARY_DIR}/data/backups/.photo-alert-state.json}"
FAIL_MSG=""
DETAIL_OK=""

alert_notify() {
  local rc="$1" status detail
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "  [dry-run] 跳过照片备份告警"
    return 0
  fi
  if [[ ! -f "${NOTIFY_PY}" ]]; then
    echo "警告: 找不到 ${NOTIFY_PY}，本次不发照片备份告警" >&2
    return 0
  fi
  if [[ "${rc}" == "0" && -z "${FAIL_MSG}" ]]; then
    status=ok
    detail="${DETAIL_OK:-门店日报照片镜像成功 ${STAMP}（${DEST_HOST}:${DEST_FILE}）}"
  else
    status=fail
    detail="门店日报照片镜像失败 ${STAMP}: ${FAIL_MSG:-脚本退出码 ${rc}}；日志 /var/log/store-daily-photos.log"
  fi
  python3 "${NOTIFY_PY}" --status "${status}" --message "${detail}" \
    --state-file "${PHOTO_ALERT_STATE}" --db "${PRIMARY_DIR}/data/store_daily.db" 2>&1 \
    | sed 's/^/  [告警] /' >&2 || true
}
trap 'alert_notify "$?"' EXIT

if [[ -z "${DEST_HOST}" ]]; then
  FAIL_MSG="未设置 LAN_BACKUP_HOST（请在 scripts/backup.env 配置局域网备份机）"
  echo "错误: ${FAIL_MSG}" >&2
  exit 1
fi

# 局域网备份机是安卓 Termux 时，重连 Wi-Fi 换隐私 MAC 会导致旧 ARP 悬空
ARP_HOST="${DEST_HOST#*@}"
if [[ -n "${ARP_HOST}" ]]; then
  arp -d "${ARP_HOST}" >/dev/null 2>&1 || true
fi

if [[ ! -d "${SRC}" ]]; then
  DETAIL_OK="门店日报照片目录尚未生成（${SRC}），本次跳过 ${STAMP}"
  echo "$(TZ=Asia/Shanghai date '+%F %T') 没有照片目录 ${SRC}，跳过"
  exit 0
fi

N=$(find "${SRC}" -maxdepth 1 -type f | wc -l | tr -d ' ')
SIZE=$(du -sh "${SRC}" | cut -f1)
START=$(date +%s)
echo "$(TZ=Asia/Shanghai date '+%F %T') 开始镜像：${N} 个文件 / ${SIZE} -> ${DEST_HOST}:${DEST_FILE} (ssh ${DEST_PORT})"

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "  [dry-run] tar ${SRC} -> ${DEST_HOST}:${DEST_FILE}"
  exit 0
fi

# 不压缩（照片本身已压缩，gzip 只省 1–2% 却多跑几分钟；纯 tar 更快）
# 先写 .part 并用 tar tf 校验包尾完整再改名：断流/满盘绝不覆盖上一份完好镜像
REMOTE_FILE_Q=$(printf %q "${DEST_FILE}")
if ! tar cf - -C "$(dirname "${SRC}")" "$(basename "${SRC}")" \
  | ssh -p "${DEST_PORT}" -o BatchMode=yes -o ConnectTimeout=15 "${DEST_HOST}" \
      "set -eu; f=${REMOTE_FILE_Q}; mkdir -p \"\$(dirname \"\$f\")\"; cat > \"\$f.part\"; tar tf \"\$f.part\" >/dev/null; chmod 600 \"\$f.part\" 2>/dev/null || true; mv -f \"\$f.part\" \"\$f\"; ls -lh \"\$f\""; then
  FAIL_MSG="推送到局域网备份机失败（${DEST_HOST}:${DEST_FILE}，ssh ${DEST_PORT}）"
  echo "错误: ${FAIL_MSG}" >&2
  exit 1
fi

ELAPSED=$(( $(date +%s) - START ))
DETAIL_OK="门店日报照片镜像成功 ${STAMP}（${N} 张 / ${SIZE} -> ${DEST_HOST}:${DEST_FILE}，耗时 ${ELAPSED}s）"
echo "$(TZ=Asia/Shanghai date '+%F %T') 完成，耗时 ${ELAPSED}s"
