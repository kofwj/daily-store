#!/usr/bin/env bash
# 门店日报：从生产机拉一致性 SQLite 快照，推到局域网备份机 + 远端 VPS。
#
# 典型拓扑：
#   生产（写）：192.168.100.5:/opt/store-daily
#   局域网备份：192.168.100.109:~/store-daily-backups
#   远端 VPS：  user@x.x.x.x:~/store-daily-backups
#
# 用法：
#   ./scripts/backup_offsite.sh
#   PRIMARY_HOST=192.168.100.5 LAN_BACKUP_HOST=192.168.100.109 \
#     REMOTE_BACKUP=user@x.x.x.x ./scripts/backup_offsite.sh
#   ./scripts/backup_offsite.sh --dry-run
#
# cron 示例（Mac 或 109，每天 02:15）：
#   15 2 * * * cd /path/to/store-daily && \
#     REMOTE_BACKUP=ubuntu@公网IP ./scripts/backup_offsite.sh \
#     >>/tmp/store-daily-backup.log 2>&1

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,22p' "$0"
      exit 0
      ;;
    *)
      echo "未知参数: $arg" >&2
      exit 1
      ;;
  esac
done

PRIMARY_HOST="${PRIMARY_HOST:-192.168.100.5}"
PRIMARY_USER="${PRIMARY_USER:-root}"
PRIMARY_DIR="${PRIMARY_DIR:-/opt/store-daily}"

LAN_BACKUP_HOST="${LAN_BACKUP_HOST:-192.168.100.109}"
LAN_BACKUP_USER="${LAN_BACKUP_USER:-${USER}}"
LAN_BACKUP_DIR="${LAN_BACKUP_DIR:-${HOME}/store-daily-backups}"

# 例：ubuntu@1.2.3.4 ；空则跳过远端
REMOTE_BACKUP="${REMOTE_BACKUP:-}"
REMOTE_BACKUP_DIR="${REMOTE_BACKUP_DIR:-${HOME}/store-daily-backups}"

KEEP_DAYS="${KEEP_DAYS:-30}"
STAMP="$(TZ=Asia/Shanghai date +%Y%m%d_%H%M%S)"
TAG="offsite_${STAMP}"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/store-daily-backup.XXXXXX")"
cleanup() { rm -rf -- "${WORKDIR}"; }
trap cleanup EXIT

PRIMARY="${PRIMARY_USER}@${PRIMARY_HOST}"
LAN="${LAN_BACKUP_USER}@${LAN_BACKUP_HOST}"
REMOTE_SNAP_NAME="store_daily_${TAG}.db"
REMOTE_ENV_NAME="env_${TAG}.env"
REMOTE_SNAP="${PRIMARY_DIR}/data/backups/${REMOTE_SNAP_NAME}"
REMOTE_ENV="${PRIMARY_DIR}/data/backups/${REMOTE_ENV_NAME}"

echo "== 备份开始 ${STAMP} =="
echo "生产: ${PRIMARY}:${PRIMARY_DIR}"
echo "局域网: ${LAN}:${LAN_BACKUP_DIR}"
if [[ -n "${REMOTE_BACKUP}" ]]; then
  echo "远端: ${REMOTE_BACKUP}:${REMOTE_BACKUP_DIR}"
else
  echo "远端: (未设置 REMOTE_BACKUP，跳过)"
fi

echo "→ 生产机打一致性快照"
if [[ "${DRY_RUN}" == "1" ]]; then
  echo "  [dry-run] snapshot on ${PRIMARY}"
else
  ssh -o ConnectTimeout=12 "${PRIMARY}" \
    "PRIMARY_DIR=$(printf %q "${PRIMARY_DIR}") SNAP_NAME=$(printf %q "${REMOTE_SNAP_NAME}") ENV_NAME=$(printf %q "${REMOTE_ENV_NAME}") bash -s" <<'EOS'
set -euo pipefail
mkdir -p "${PRIMARY_DIR}/data/backups"
HOST_DB="${PRIMARY_DIR}/data/store_daily.db"
HOST_SNAP="${PRIMARY_DIR}/data/backups/${SNAP_NAME}"
HOST_ENV="${PRIMARY_DIR}/data/backups/${ENV_NAME}"

if [[ ! -f "${HOST_DB}" ]]; then
  echo "找不到生产库: ${HOST_DB}" >&2
  exit 1
fi

# 容器在跑就进容器备份（路径 /app/data）；否则宿主机 python3
if command -v docker >/dev/null 2>&1 && \
   docker compose -f "${PRIMARY_DIR}/docker-compose.yml" ps --status running 2>/dev/null | grep -q app; then
  docker compose -f "${PRIMARY_DIR}/docker-compose.yml" exec -T app \
    python -c "import sqlite3; s=sqlite3.connect('/app/data/store_daily.db'); d=sqlite3.connect('/app/data/backups/${SNAP_NAME}'); s.backup(d); d.close(); s.close(); print('/app/data/backups/${SNAP_NAME}')"
else
  python3 -c "import sqlite3; s=sqlite3.connect('${HOST_DB}'); d=sqlite3.connect('${HOST_SNAP}'); s.backup(d); d.close(); s.close(); print('${HOST_SNAP}')"
fi

if [[ -f "${PRIMARY_DIR}/.env" ]]; then
  cp -a "${PRIMARY_DIR}/.env" "${HOST_ENV}"
  chmod 600 "${HOST_ENV}"
fi
ls -la "${HOST_SNAP}"
EOS
fi

LOCAL_DB="${WORKDIR}/${REMOTE_SNAP_NAME}"
LOCAL_ENV="${WORKDIR}/${REMOTE_ENV_NAME}"

echo "→ 拉回临时目录并校验"
if [[ "${DRY_RUN}" == "1" ]]; then
  echo "  [dry-run] scp ${REMOTE_SNAP}"
else
  scp -q "${PRIMARY}:${REMOTE_SNAP}" "${LOCAL_DB}"
  scp -q "${PRIMARY}:${REMOTE_ENV}" "${LOCAL_ENV}" 2>/dev/null || true
  python3 - <<PY
import os, sqlite3, sys
p = "${LOCAL_DB}"
con = sqlite3.connect(p)
tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
need = {"stores", "users", "daily_reports", "daily_facts"}
miss = sorted(need - tables)
if miss:
    sys.exit(f"快照缺表: {miss}")
n = con.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
print(f"  校验 OK stores={n} size={os.path.getsize(p)} bytes")
con.close()
PY
fi

push_one() {
  local dest_host="$1"
  local dest_dir="$2"
  echo "→ 推送到 ${dest_host}:${dest_dir}"
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "  [dry-run] rsync → ${dest_host}"
    return
  fi
  ssh -o ConnectTimeout=15 "${dest_host}" "mkdir -p '${dest_dir}/db' '${dest_dir}/env'"
  rsync -az --chmod=F600 "${LOCAL_DB}" "${dest_host}:${dest_dir}/db/"
  if [[ -f "${LOCAL_ENV}" ]]; then
    rsync -az --chmod=F600 "${LOCAL_ENV}" "${dest_host}:${dest_dir}/env/"
  fi
  ssh "${dest_host}" \
    "DIR=$(printf %q "${dest_dir}") DAYS=$(printf %q "${KEEP_DAYS}") bash -s" <<'EOS'
set -euo pipefail
find "${DIR}/db" -type f -name 'store_daily_offsite_*.db' -mtime +"${DAYS}" -delete 2>/dev/null || true
find "${DIR}/env" -type f -name 'env_offsite_*.env' -mtime +"${DAYS}" -delete 2>/dev/null || true
echo "  最近备份："
ls -lt "${DIR}/db" 2>/dev/null | head -5 || true
EOS
}

if [[ -n "${LAN_BACKUP_HOST}" ]]; then
  push_one "${LAN}" "${LAN_BACKUP_DIR}"
fi

if [[ -n "${REMOTE_BACKUP}" ]]; then
  push_one "${REMOTE_BACKUP}" "${REMOTE_BACKUP_DIR}"
fi

if [[ "${DRY_RUN}" != "1" ]]; then
  ssh "${PRIMARY}" \
    "find $(printf %q "${PRIMARY_DIR}/data/backups") -type f \\( -name 'store_daily_offsite_*.db' -o -name 'env_offsite_*.env' \\) -mtime +${KEEP_DAYS} -delete 2>/dev/null || true"
fi

echo "== 备份完成 =="
echo "恢复步骤：docs/backup-dr.md"
