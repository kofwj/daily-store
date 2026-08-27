#!/usr/bin/env bash
# 门店日报：从生产机拉一致性 SQLite 快照，推到局域网备份机 + 远端 VPS。
#
# 主机写在 scripts/backup.env（不进 git）。
#
# 用法：
#   ./scripts/backup_offsite.sh
#   LAN_BACKUP_SSH_PORT=22 LAN_BACKUP_USER=backup \
#     REMOTE_BACKUP=ubuntu@standby-host ./scripts/backup_offsite.sh
#   ./scripts/backup_offsite.sh --dry-run
#
# Termux 注意：
#   - 端口用 LAN_BACKUP_SSH_PORT
#   - 目录用相对家目录：store-daily-backups（不要写开发机的 $HOME）
#
# cron 示例（生产机每小时；推荐，开发机睡觉也不漏）：
#   5 * * * * /opt/store-daily/scripts/backup_offsite.sh \
#     >>/var/log/store-daily-offsite.log 2>&1
# 安装：ssh user@your-primary-host /opt/store-daily/scripts/install_backup_cron.sh

set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if command -v flock >/dev/null 2>&1; then
  exec 9>"${TMPDIR:-/tmp}/store-daily-backup.lock"
  if ! flock -n 9; then
    echo "另一份备份还在跑，跳过"
    exit 0
  fi
fi

# 可选本地配置：scripts/backup.env（勿提交密钥类内容；仅 host/user）
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
      sed -n '2,28p' "$0"
      exit 0
      ;;
    *)
      echo "未知参数: $arg" >&2
      exit 1
      ;;
  esac
done

PRIMARY_HOST="${PRIMARY_HOST:-}"
PRIMARY_USER="${PRIMARY_USER:-root}"
PRIMARY_DIR="${PRIMARY_DIR:-/opt/store-daily}"
PRIMARY_SSH_PORT="${PRIMARY_SSH_PORT:-22}"

LAN_BACKUP_HOST="${LAN_BACKUP_HOST:-}"
LAN_BACKUP_USER="${LAN_BACKUP_USER:-backup}"
LAN_BACKUP_DIR="${LAN_BACKUP_DIR:-store-daily-backups}"
LAN_BACKUP_SSH_PORT="${LAN_BACKUP_SSH_PORT:-22}"

# 公网灾备（可用 user@host，或只写 host + REMOTE_BACKUP_USER）
REMOTE_BACKUP="${REMOTE_BACKUP:-}"
REMOTE_BACKUP_USER="${REMOTE_BACKUP_USER:-root}"
REMOTE_BACKUP_DIR="${REMOTE_BACKUP_DIR:-store-daily-backups}"
REMOTE_BACKUP_SSH_PORT="${REMOTE_BACKUP_SSH_PORT:-22}"

KEEP_DAYS="${KEEP_DAYS:-30}"
if [[ -z "${PRIMARY_HOST}" && ! -f "${PRIMARY_DIR}/data/store_daily.db" ]]; then
  echo "请在 scripts/backup.env 或环境变量里设置 PRIMARY_HOST" >&2
  exit 1
fi
STAMP="$(TZ=Asia/Shanghai date +%Y%m%d_%H%M%S)"
TAG="offsite_${STAMP}"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/store-daily-backup.XXXXXX")"
cleanup() { rm -rf -- "${WORKDIR}"; }
trap cleanup EXIT

PRIMARY="${PRIMARY_USER}@${PRIMARY_HOST}"
LAN="${LAN_BACKUP_USER}@${LAN_BACKUP_HOST}"
# 规范化远端：允许 REMOTE_BACKUP=user@host 或 host + REMOTE_BACKUP_USER
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
REMOTE_SNAP_NAME="store_daily_${TAG}.db"
REMOTE_ENV_NAME="env_${TAG}.env"
REMOTE_UPLOADS_NAME="uploads_${TAG}.tar.gz"
REMOTE_SNAP="${PRIMARY_DIR}/data/backups/${REMOTE_SNAP_NAME}"
REMOTE_ENV="${PRIMARY_DIR}/data/backups/${REMOTE_ENV_NAME}"
REMOTE_UPLOADS="${PRIMARY_DIR}/data/backups/${REMOTE_UPLOADS_NAME}"

ssh_p() {
  local port="$1"; shift
  ssh -o ConnectTimeout=15 -o BatchMode=yes -p "${port}" "$@"
}
scp_p() {
  local port="$1"; shift
  scp -q -P "${port}" "$@"
}
rsync_p() {
  local port="$1"; shift
  rsync -az -e "ssh -o ConnectTimeout=15 -o BatchMode=yes -p ${port}" "$@"
}

ON_PRIMARY=0
if [[ -f "${PRIMARY_DIR}/data/store_daily.db" ]]; then
  ON_PRIMARY=1
fi

echo "== 备份开始 ${STAMP} =="
if [[ "${ON_PRIMARY}" == "1" ]]; then
  echo "生产: 本机 ${PRIMARY_DIR}"
else
  echo "生产: ${PRIMARY}:${PRIMARY_DIR} (ssh ${PRIMARY_SSH_PORT})"
fi

FP_PY="${ROOT}/scripts/offsite_fingerprint.py"
if [[ "${DRY_RUN}" != "1" ]]; then
  if [[ "${ON_PRIMARY}" == "1" ]]; then
    STATE="$(python3 "${FP_PY}" check --dir "${PRIMARY_DIR}")"
  else
    STATE="$(ssh_p "${PRIMARY_SSH_PORT}" "${PRIMARY}" \
      "python3 '${PRIMARY_DIR}/scripts/offsite_fingerprint.py' check --dir '${PRIMARY_DIR}'")"
  fi
  if [[ "${STATE}" == "SKIP" ]]; then
    echo "库未变化，跳过拷贝"
    exit 0
  fi
fi
echo "局域网: ${LAN}:${LAN_BACKUP_DIR} (ssh ${LAN_BACKUP_SSH_PORT})"
if [[ -n "${REMOTE_TARGET}" ]]; then
  echo "远端: ${REMOTE_TARGET}:${REMOTE_BACKUP_DIR} (ssh ${REMOTE_BACKUP_SSH_PORT})"
else
  echo "远端: (未设置 REMOTE_BACKUP，跳过)"
fi

snapshot_primary() {
  set -euo pipefail
  mkdir -p "${PRIMARY_DIR}/data/backups"
  local host_db="${PRIMARY_DIR}/data/store_daily.db"
  local host_snap="${PRIMARY_DIR}/data/backups/${REMOTE_SNAP_NAME}"
  local host_env="${PRIMARY_DIR}/data/backups/${REMOTE_ENV_NAME}"
  if [[ ! -f "${host_db}" ]]; then
    echo "找不到生产库: ${host_db}" >&2
    return 1
  fi
  if command -v docker >/dev/null 2>&1 && \
     docker compose -f "${PRIMARY_DIR}/docker-compose.yml" ps --status running 2>/dev/null | grep -q app; then
    # </dev/null 防止 exec 吞掉后续 stdin
    docker compose -f "${PRIMARY_DIR}/docker-compose.yml" exec -T app \
      python -c "import sqlite3; s=sqlite3.connect('/app/data/store_daily.db'); d=sqlite3.connect('/app/data/backups/${REMOTE_SNAP_NAME}'); s.backup(d); d.close(); s.close(); print('/app/data/backups/${REMOTE_SNAP_NAME}')" \
      </dev/null
  else
    python3 -c "import sqlite3; s=sqlite3.connect('${host_db}'); d=sqlite3.connect('${host_snap}'); s.backup(d); d.close(); s.close(); print('${host_snap}')"
  fi
  if [[ ! -f "${host_snap}" ]]; then
    echo "快照未生成: ${host_snap}" >&2
    return 1
  fi
  if [[ -f "${PRIMARY_DIR}/.env" ]]; then
    cp -f "${PRIMARY_DIR}/.env" "${host_env}"
    chmod 600 "${host_env}" || true
    echo "已附带 .env -> ${host_env}"
  else
    echo "警告: 生产机没有 .env" >&2
  fi
  if [[ -d "${PRIMARY_DIR}/data/uploads" ]]; then
    tar czf "${PRIMARY_DIR}/data/backups/${REMOTE_UPLOADS_NAME}" -C "${PRIMARY_DIR}/data" uploads
    echo "已附带 uploads -> ${REMOTE_UPLOADS_NAME}"
  fi
  ls -la "${host_snap}" "${host_env}" 2>/dev/null || ls -la "${host_snap}"
}

echo "→ 生产机打一致性快照"
if [[ "${DRY_RUN}" == "1" ]]; then
  echo "  [dry-run] snapshot on ${PRIMARY}"
elif [[ "${ON_PRIMARY}" == "1" ]]; then
  snapshot_primary
else
  ssh_p "${PRIMARY_SSH_PORT}" "${PRIMARY}" \
    "PRIMARY_DIR=$(printf %q "${PRIMARY_DIR}") SNAP_NAME=$(printf %q "${REMOTE_SNAP_NAME}") ENV_NAME=$(printf %q "${REMOTE_ENV_NAME}") UPLOADS_NAME=$(printf %q "${REMOTE_UPLOADS_NAME}") bash -s" <<'EOS'
set -euo pipefail
mkdir -p "${PRIMARY_DIR}/data/backups"
HOST_DB="${PRIMARY_DIR}/data/store_daily.db"
HOST_SNAP="${PRIMARY_DIR}/data/backups/${SNAP_NAME}"
HOST_ENV="${PRIMARY_DIR}/data/backups/${ENV_NAME}"

if [[ ! -f "${HOST_DB}" ]]; then
  echo "找不到生产库: ${HOST_DB}" >&2
  exit 1
fi

if command -v docker >/dev/null 2>&1 && \
   docker compose -f "${PRIMARY_DIR}/docker-compose.yml" ps --status running 2>/dev/null | grep -q app; then
  # </dev/null 防止 exec 吞掉 bash -s 剩余脚本（否则 .env 拷贝不会执行）
  docker compose -f "${PRIMARY_DIR}/docker-compose.yml" exec -T app \
    python -c "import sqlite3; s=sqlite3.connect('/app/data/store_daily.db'); d=sqlite3.connect('/app/data/backups/${SNAP_NAME}'); s.backup(d); d.close(); s.close(); print('/app/data/backups/${SNAP_NAME}')" \
    </dev/null
else
  python3 -c "import sqlite3; s=sqlite3.connect('${HOST_DB}'); d=sqlite3.connect('${HOST_SNAP}'); s.backup(d); d.close(); s.close(); print('${HOST_SNAP}')"
fi

if [[ ! -f "${HOST_SNAP}" ]]; then
  echo "快照未生成: ${HOST_SNAP}" >&2
  exit 1
fi
if [[ -f "${PRIMARY_DIR}/.env" ]]; then
  cp -f "${PRIMARY_DIR}/.env" "${HOST_ENV}"
  chmod 600 "${HOST_ENV}" || true
  echo "已附带 .env -> ${HOST_ENV}"
else
  echo "警告: 生产机没有 .env" >&2
fi
if [[ -d "${PRIMARY_DIR}/data/uploads" ]]; then
  tar czf "${PRIMARY_DIR}/data/backups/${UPLOADS_NAME}" -C "${PRIMARY_DIR}/data" uploads
  echo "已附带 uploads -> ${UPLOADS_NAME}"
fi
ls -la "${HOST_SNAP}" "${HOST_ENV}" 2>/dev/null || ls -la "${HOST_SNAP}"
EOS
fi

LOCAL_DB="${WORKDIR}/${REMOTE_SNAP_NAME}"
LOCAL_ENV="${WORKDIR}/${REMOTE_ENV_NAME}"
LOCAL_UPLOADS="${WORKDIR}/${REMOTE_UPLOADS_NAME}"

echo "→ 拉回临时目录并校验"
if [[ "${DRY_RUN}" == "1" ]]; then
  echo "  [dry-run] copy snapshot"
elif [[ "${ON_PRIMARY}" == "1" ]]; then
  cp -f "${REMOTE_SNAP}" "${LOCAL_DB}"
  [[ -f "${REMOTE_ENV}" ]] && cp -f "${REMOTE_ENV}" "${LOCAL_ENV}" || true
  [[ -f "${REMOTE_UPLOADS}" ]] && cp -f "${REMOTE_UPLOADS}" "${LOCAL_UPLOADS}" || true
else
  scp_p "${PRIMARY_SSH_PORT}" "${PRIMARY}:${REMOTE_SNAP}" "${LOCAL_DB}"
  scp_p "${PRIMARY_SSH_PORT}" "${PRIMARY}:${REMOTE_ENV}" "${LOCAL_ENV}" 2>/dev/null || true
  scp_p "${PRIMARY_SSH_PORT}" "${PRIMARY}:${REMOTE_UPLOADS}" "${LOCAL_UPLOADS}" 2>/dev/null || true
fi

if [[ "${DRY_RUN}" != "1" ]]; then
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
  local dest_port="$3"
  echo "→ 推送到 ${dest_host}:${dest_dir} (ssh ${dest_port})"
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "  [dry-run] rsync → ${dest_host}"
    return
  fi
  ssh_p "${dest_port}" "${dest_host}" "mkdir -p '${dest_dir}/db' '${dest_dir}/env' '${dest_dir}/uploads'"
  # Termux 常只有 scp、没有 rsync；统一用 scp，兼容安卓备份机
  scp_p "${dest_port}" "${LOCAL_DB}" "${dest_host}:${dest_dir}/db/"
  if [[ -f "${LOCAL_ENV}" ]]; then
    scp_p "${dest_port}" "${LOCAL_ENV}" "${dest_host}:${dest_dir}/env/"
  fi
  if [[ -f "${LOCAL_UPLOADS}" ]]; then
    scp_p "${dest_port}" "${LOCAL_UPLOADS}" "${dest_host}:${dest_dir}/uploads/"
  fi
  ssh_p "${dest_port}" "${dest_host}" \
    "DIR=$(printf %q "${dest_dir}") DAYS=$(printf %q "${KEEP_DAYS}") DB=$(printf %q "${REMOTE_SNAP_NAME}") ENV=$(printf %q "${REMOTE_ENV_NAME}") bash -s" <<'EOS'
set -euo pipefail
chmod 600 "${DIR}/db/${DB}" 2>/dev/null || true
chmod 600 "${DIR}/env/${ENV}" 2>/dev/null || true
find "${DIR}/db" -type f -name 'store_daily_offsite_*.db' -mtime +"${DAYS}" -delete 2>/dev/null || true
find "${DIR}/env" -type f -name 'env_offsite_*.env' -mtime +"${DAYS}" -delete 2>/dev/null || true
find "${DIR}/uploads" -type f -name 'uploads_offsite_*.tar.gz' -mtime +"${DAYS}" -delete 2>/dev/null || true
echo "  最近备份："
ls -lt "${DIR}/db" 2>/dev/null | head -5 || true
EOS
}

PUSH_OK=0
PUSH_FAIL=0
if [[ -n "${LAN_BACKUP_HOST}" ]]; then
  if push_one "${LAN}" "${LAN_BACKUP_DIR}" "${LAN_BACKUP_SSH_PORT}"; then
    PUSH_OK=1
  else
    echo "警告: 局域网备份失败（${LAN}），继续远端" >&2
    PUSH_FAIL=1
  fi
fi

if [[ -n "${REMOTE_TARGET}" ]]; then
  if push_one "${REMOTE_TARGET}" "${REMOTE_BACKUP_DIR}" "${REMOTE_BACKUP_SSH_PORT}"; then
    PUSH_OK=1
  else
    echo "警告: 远端备份失败（${REMOTE_TARGET})" >&2
    PUSH_FAIL=1
  fi
fi

MAX_KEEP="${MAX_KEEP:-20}"
prune_primary_count() {
  # 只留最新 MAX_KEEP 份，防止异机写进来的库无限堆积
  ls -1t "${PRIMARY_DIR}"/data/backups/store_daily_*.db 2>/dev/null | tail -n +$((MAX_KEEP + 1)) | while read -r f; do
    rm -f -- "$f" 2>/dev/null || true
  done || true
}
prune_local() {
  find "${PRIMARY_DIR}/data/backups" -type f \
    \( -name 'store_daily_offsite_*.db' -o -name 'env_offsite_*.env' -o -name 'uploads_offsite_*.tar.gz' \) \
    -mtime +"${KEEP_DAYS}" -delete 2>/dev/null || true
  prune_primary_count
}

if [[ "${DRY_RUN}" != "1" ]]; then
  if [[ "${ON_PRIMARY}" == "1" ]]; then
    prune_local
  else
    ssh_p "${PRIMARY_SSH_PORT}" "${PRIMARY}" \
      "MAX_KEEP=$(printf %q "${MAX_KEEP}") bash -s" <<'EOS'
set -euo pipefail
# 只留最新 MAX_KEEP 份，防止异机写进来的库无限堆积
ls -1t "${PRIMARY_DIR}"/data/backups/store_daily_*.db 2>/dev/null | tail -n +$((MAX_KEEP + 1)) | while read -r f; do
  rm -f -- "$f" 2>/dev/null || true
done || true
find "${PRIMARY_DIR}/data/backups" -type f \
  \( -name 'store_daily_offsite_*.db' -o -name 'env_offsite_*.env' \) \
  -mtime +"${KEEP_DAYS}" -delete 2>/dev/null || true
EOS
  fi
fi

if [[ "${DRY_RUN}" != "1" && ( "${PUSH_OK}" == "1" || ( -z "${LAN_BACKUP_HOST}" && -z "${REMOTE_TARGET}" ) ) ]]; then
  if [[ "${ON_PRIMARY}" == "1" ]]; then
    python3 "${FP_PY}" save --dir "${PRIMARY_DIR}" >/dev/null
  else
    ssh_p "${PRIMARY_SSH_PORT}" "${PRIMARY}" \
      "python3 '${PRIMARY_DIR}/scripts/offsite_fingerprint.py' save --dir '${PRIMARY_DIR}'" >/dev/null || true
  fi
fi

echo "== 备份完成 =="
echo "恢复步骤：docs/backup-dr.md"
# 至少推成功一处（通常是 pve）即成功；109 关机不让整次失败
if [[ "${PUSH_OK}" != "1" && ( -n "${LAN_BACKUP_HOST}" || -n "${REMOTE_TARGET}" ) ]]; then
  echo "错误: 机外备份全部失败" >&2
  exit 1
fi
if [[ "${PUSH_FAIL}" == "1" ]]; then
  echo "（部分目标失败，见上方警告）"
fi
