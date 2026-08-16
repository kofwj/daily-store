#!/usr/bin/env bash
# 从这台 Mac 把 store-daily 同步到局域网 VPS，并在那边重建容器。
# 用法：
#   ./scripts/sync_to_vps.sh
#   VPS_USER=ubuntu ./scripts/sync_to_vps.sh
#   ./scripts/sync_to_vps.sh --no-db          # 不覆盖远端已有日报库
#   ./scripts/sync_to_vps.sh --setup-only     # 只拷文件，不 docker compose

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SYNC_DB=1
SETUP_ONLY=0
SNAPSHOT_DB=""
cleanup_snapshot() {
  if [[ -n "${SNAPSHOT_DB}" && -f "${SNAPSHOT_DB}" ]]; then
    rm -f -- "${SNAPSHOT_DB}"
  fi
}
trap cleanup_snapshot EXIT

for arg in "$@"; do
  case "$arg" in
    --no-db) SYNC_DB=0 ;;
    --setup-only) SETUP_ONLY=1 ;;
    *)
      echo "未知参数: $arg" >&2
      exit 1
      ;;
  esac
done

if [[ ! -f .env ]]; then
  echo "本机还没有 .env，先从示例生成一个（请再改 STORE_DAILY_SECRET）：" >&2
  cp .env.example .env
  SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  if [[ "$(uname)" == "Darwin" ]]; then
    sed -i '' "s/STORE_DAILY_SECRET=replace-with-random-secret/STORE_DAILY_SECRET=${SECRET}/" .env
  else
    sed -i "s/STORE_DAILY_SECRET=replace-with-random-secret/STORE_DAILY_SECRET=${SECRET}/" .env
  fi
  echo "已写入 .env（密钥已随机生成）。" >&2
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

VPS_HOST="${VPS_HOST:-192.168.100.5}"
VPS_USER="${VPS_USER:-root}"
VPS_DIR="${VPS_DIR:-/opt/store-daily}"
CADDY_PORT="${CADDY_PORT:-8099}"

REMOTE="${VPS_USER}@${VPS_HOST}"
echo "同步到 ${REMOTE}:${VPS_DIR}"

ssh -o ConnectTimeout=8 "${REMOTE}" "mkdir -p '${VPS_DIR}/data' '${VPS_DIR}/scripts' '${VPS_DIR}/caddy'"

# A live WAL database must never be copied as just its main file.  Ask SQLite's
# backup API for a consistent temporary image, then rsync that image only.
if [[ "${SYNC_DB}" == "1" ]]; then
  SNAPSHOT_DB="$(mktemp "${ROOT}/data/.sync_snapshot.XXXXXX.db")"
  python3 - "${ROOT}/data/store_daily.db" "${SNAPSHOT_DB}" <<'PY'
import sqlite3, sys
source, target = sys.argv[1:]
with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
PY
  chmod 600 "${SNAPSHOT_DB}"
fi

RSYNC=(
  rsync -az --delete
  --exclude '.venv/'
  --exclude '__pycache__/'
  --exclude '.pytest_cache/'
  --exclude '.ruff_cache/'
  --exclude '.git/'
  --exclude '.DS_Store'
  --exclude 'data/*.db'
  --exclude 'data/*.db-shm'
  --exclude 'data/*.db-wal'
)

SYNC_SOURCES=(./app ./caddy ./scripts ./tests \
  Dockerfile docker-compose.yml requirements.txt wsgi.py README.md .env .gitignore)
"${RSYNC[@]}" \
  "${SYNC_SOURCES[@]}" \
  Dockerfile docker-compose.yml requirements.txt wsgi.py README.md .env .gitignore \
  "${REMOTE}:${VPS_DIR}/"

if [[ "${SYNC_DB}" == "1" ]]; then
  rsync -az --chmod=F600 "${SNAPSHOT_DB}" "${REMOTE}:${VPS_DIR}/data/store_daily.db"
fi

# docs 可选
if [[ -d docs ]]; then
  rsync -az docs/ "${REMOTE}:${VPS_DIR}/docs/"
fi

if [[ "${SETUP_ONLY}" == "1" ]]; then
  echo "文件已拷完。到 VPS 上执行："
  echo "  ssh ${REMOTE}"
  echo "  cd ${VPS_DIR} && ./scripts/deploy_vps.sh"
  exit 0
fi

ssh "${REMOTE}" "cd '${VPS_DIR}' && chmod +x scripts/*.sh && ./scripts/deploy_vps.sh"
echo
PORT="${CADDY_PORT:-8099}"
echo "本机浏览器打开：http://${VPS_HOST}:${PORT}"
