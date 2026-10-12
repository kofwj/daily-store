#!/usr/bin/env bash
# 门店日报：从生产机拉一致性 SQLite 快照，推到局域网备份机 + 异地备份机。
#
# 主机写在 scripts/backup.env（不进 git）。
#
# 用法：
#   ./scripts/backup_offsite.sh
#   LAN_BACKUP_SSH_PORT=22 LAN_BACKUP_USER=backup \
#     REMOTE_BACKUP=ubuntu@standby-host ./scripts/backup_offsite.sh
#   ./scripts/backup_offsite.sh --dry-run
#
# 告警（企业微信机器人）：
#   失败/恢复推送到群里；webhook 默认取生产库 app_meta.wecom_global，
#   也可用环境变量 ALERT_WEBHOOK 覆盖。
#   ok→fail 立即发；fail→fail 24 小时内静默（超 24 小时再提醒）；fail→ok 发恢复。
#   去重状态：${PRIMARY_DIR}/data/backups/.alert-state.json
#   演练（只打印、不真发）：ALERT_DRY_RUN=1 ./scripts/backup_offsite.sh --dry-run
#   只验通道：python3 scripts/notify_failure.py --probe
# 落点探活（库未变化的小时也查一遍）：
#   ssh 连得上且备份目录在 = 可用；全挂 → exit 1 并告警。
#   关掉：SKIP_PROBE=0
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
      sed -n '2,31p' "$0"
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
cleanup() {
  local rc=$?
  rm -rf -- "${WORKDIR}" 2>/dev/null || true
  alert_notify "${rc}"
}
trap cleanup EXIT

PRIMARY="${PRIMARY_USER}@${PRIMARY_HOST}"
LAN="${LAN_BACKUP_USER}@${LAN_BACKUP_HOST}"
# 备份机是手机时，「隐私 MAC」重连后会换 MAC；本机旧 ARP 表项会把包发往失效的
# 旧 MAC，表现为长时间连不上。连接前主动清一次 ARP（免 sudo），强制重新解析。
if [[ -n "${LAN_BACKUP_HOST}" ]]; then
  arp -d "${LAN_BACKUP_HOST}" >/dev/null 2>&1 || true
fi
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

# ── 备份告警（企业微信机器人）──────────────────────────────────────────
# 每次运行都调用一次 notify_failure.py，由它按 state 文件决定发不发：
#   ok→fail 立即发；fail→fail 24h 内静默；fail→ok 发一条恢复。
# webhook 复用生产库 app_meta.wecom_global；告警自身失败绝不影响备份，
# 只往日志里写。state 放在 data/backups/ 下（该目录不随代码同步、不被清理）。
NOTIFY_PY="${ROOT}/scripts/notify_failure.py"
ALERT_STATE="${ALERT_STATE:-${PRIMARY_DIR}/data/backups/.alert-state.json}"
FAIL_MSG=""

alert_notify() {
  local rc="$1" status detail
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "  [dry-run] 跳过备份告警"
    return 0
  fi
  if [[ ! -f "${NOTIFY_PY}" ]]; then
    echo "警告: 找不到 ${NOTIFY_PY}，本次不发备份告警" >&2
    return 0
  fi
  if [[ "${rc}" == "0" && -z "${FAIL_MSG:-}" ]]; then
    status=ok
    detail="门店日报备份成功 ${STAMP}（局域网 ${LAN_BACKUP_HOST:-未设} / 异地 ${REMOTE_BACKUP:-未设}）"
  else
    status=fail
    detail="门店日报备份失败 ${STAMP}: ${FAIL_MSG:-脚本退出码 ${rc}}；日志 /var/log/store-daily-offsite.log"
  fi
  python3 "${NOTIFY_PY}" --status "${status}" --message "${detail}" \
    --state-file "${ALERT_STATE}" --db "${PRIMARY_DIR}/data/store_daily.db" 2>&1 \
    | sed 's/^/  [告警] /' >&2 || true
}

# ── 落点探活（库未变化的小时也要能发现掉线）────────────────────────────
# 库没变时脚本会在拷贝前退出，落点是否还活着就没人管了（异地落点曾静默失效
# 53 天就是这么漏掉的）。这里补一次轻量探测：ssh 连得上、备份目录存在即算可用，
# 顺带报一下剩余空间。全挂 → exit 1 并告警；挂一半 → 保持 exit 0、由告警提示。
# 关掉：SKIP_PROBE=0
SKIP_PROBE="${SKIP_PROBE:-1}"
probe_one() {
  local label="$1" dest_host="$2" dest_dir="$3" dest_port="$4"
  local out rc
  if out="$(ssh_p "${dest_port}" "${dest_host}" "DIR=$(printf %q "${dest_dir}") bash -s" <<'EOS' 2>&1
set -u
if [[ ! -d "${DIR}" ]]; then
  echo "备份目录不存在: ${DIR}" >&2
  exit 3
fi
df -Pk "${DIR}" | awk 'NR==2 {printf "%.1fG", $4/1048576}'
EOS
  )"; then
    echo "  ${label} 可达（${dest_host}，剩余 ${out:-?}）"
    return 0
  fi
  rc=$?
  echo "警告: ${label} 不可达（${dest_host}）：${out:-ssh 退出码 ${rc}}" >&2
  return 1
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
      "python3 '${PRIMARY_DIR}/scripts/offsite_fingerprint.py' check --dir '${PRIMARY_DIR}'" </dev/null)"
  fi
  if [[ "${STATE}" == "SKIP" ]]; then
    echo "库未变化，跳过拷贝"
    if [[ "${SKIP_PROBE}" == "1" ]]; then
      PROBE_OK=0
      if [[ -n "${LAN_BACKUP_HOST}" ]]; then
        if probe_one "局域网备份机" "${LAN}" "${LAN_BACKUP_DIR}" "${LAN_BACKUP_SSH_PORT}"; then
          PROBE_OK=1
        else
          FAIL_MSG="局域网备份机不可达（${LAN}）"
        fi
      fi
      if [[ -n "${REMOTE_TARGET}" ]]; then
        if probe_one "异地备份机" "${REMOTE_TARGET}" "${REMOTE_BACKUP_DIR}" "${REMOTE_BACKUP_SSH_PORT}"; then
          PROBE_OK=1
        else
          FAIL_MSG="${FAIL_MSG:+${FAIL_MSG}；}异地备份机不可达（${REMOTE_TARGET}）"
        fi
      fi
      if [[ -n "${LAN_BACKUP_HOST}" || -n "${REMOTE_TARGET}" ]]; then
        if [[ "${PROBE_OK}" != "1" ]]; then
          echo "错误: 机外备份落点全部不可达（库未变化，本次无拷贝）" >&2
          exit 1
        fi
        if [[ -n "${FAIL_MSG}" ]]; then
          echo "（部分落点不可达，见上方警告；已按需告警）"
        fi
      fi
    fi
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
    chmod 600 "${PRIMARY_DIR}/data/backups/${REMOTE_UPLOADS_NAME}" 2>/dev/null || true
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
  chmod 600 "${PRIMARY_DIR}/data/backups/${UPLOADS_NAME}" 2>/dev/null || true
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
  # </dev/null：ssh 会把自己 stdin 转给远端，不加会把调用者（如 bash -s）的剩余脚本吃掉
  ssh_p "${dest_port}" "${dest_host}" "mkdir -p '${dest_dir}/db' '${dest_dir}/env' '${dest_dir}/uploads'" </dev/null
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
chmod 600 "${DIR}"/uploads/*.tar.gz 2>/dev/null || true
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
    FAIL_MSG="局域网备份失败（${LAN}）"
    PUSH_FAIL=1
  fi
fi

if [[ -n "${REMOTE_TARGET}" ]]; then
  if push_one "${REMOTE_TARGET}" "${REMOTE_BACKUP_DIR}" "${REMOTE_BACKUP_SSH_PORT}"; then
    PUSH_OK=1
  else
    echo "警告: 远端备份失败（${REMOTE_TARGET}）" >&2
    FAIL_MSG="${FAIL_MSG:+${FAIL_MSG}；}异地备份失败（${REMOTE_TARGET}）"
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
      "python3 '${PRIMARY_DIR}/scripts/offsite_fingerprint.py' save --dir '${PRIMARY_DIR}'" </dev/null >/dev/null || true
  fi
fi

echo "== 备份完成 =="
echo "恢复步骤：docs/backup-dr.md"
# 「至少一处成功」只说明还有副本，不等于健康；单点失败靠上面的告警提示。
# 两个落点全部失败才 exit 1（旧的「109 关机不让整次失败」语义保留）。
if [[ "${DRY_RUN}" != "1" && "${PUSH_OK}" != "1" && ( -n "${LAN_BACKUP_HOST}" || -n "${REMOTE_TARGET}" ) ]]; then
  echo "错误: 机外备份全部失败" >&2
  exit 1
fi
if [[ "${PUSH_FAIL}" == "1" ]]; then
  echo "（部分目标失败，见上方警告；已按需告警）"
fi
