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

# 部署门禁：lint + 类型 + 测试全过才推。约 75 秒。
# pytest 必须过；ruff/mypy 本地没装则跳过（与 pre-commit 同策略）
echo "→ 部署前检查"
python3 -m pytest -q
if python3 -m ruff --version >/dev/null 2>&1; then
  python3 -m ruff check . || exit 1
else
  echo "  (未装 ruff，跳过)"
fi
if python3 -m mypy --version >/dev/null 2>&1; then
  python3 -m mypy || exit 1
else
  echo "  (未装 mypy，跳过)"
fi

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

# 命令行/环境变量优先于 .env（否则 .env 里的 VPS_HOST 会盖掉灾备机地址）
_OVERR_VPS_HOST="${VPS_HOST-}"
_OVERR_VPS_USER="${VPS_USER-}"
_OVERR_VPS_DIR="${VPS_DIR-}"
_OVERR_VPS_SSH_PORT="${VPS_SSH_PORT-}"
_OVERR_CADDY_PORT="${CADDY_PORT-}"

set -a
# shellcheck disable=SC1091
. ./.env
set +a

VPS_HOST="${_OVERR_VPS_HOST:-${VPS_HOST:-}}"
VPS_USER="${_OVERR_VPS_USER:-${VPS_USER:-root}}"
VPS_DIR="${_OVERR_VPS_DIR:-${VPS_DIR:-/opt/store-daily}}"
VPS_SSH_PORT="${_OVERR_VPS_SSH_PORT:-${VPS_SSH_PORT:-22}}"
CADDY_PORT="${_OVERR_CADDY_PORT:-${CADDY_PORT:-8099}}"
if [[ -z "${VPS_HOST}" ]]; then
  echo "请在 .env 或环境变量里设置 VPS_HOST" >&2
  exit 1
fi

REMOTE="${VPS_USER}@${VPS_HOST}"
SSH=(ssh -o ConnectTimeout=12 -o BatchMode=yes -p "${VPS_SSH_PORT}")
RSYNC_SSH="ssh -o ConnectTimeout=12 -o BatchMode=yes -p ${VPS_SSH_PORT}"
echo "同步到 ${REMOTE}:${VPS_DIR} (ssh ${VPS_SSH_PORT})"
chmod +x scripts/write_version.sh
./scripts/write_version.sh

"${SSH[@]}" "${REMOTE}" "mkdir -p '${VPS_DIR}/data' '${VPS_DIR}/scripts' '${VPS_DIR}/caddy'"

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
  -e "${RSYNC_SSH}"
  --exclude '.venv/'
  --exclude '__pycache__/'
  --exclude '.pytest_cache/'
  --exclude '.ruff_cache/'
  --exclude '.git/'
  --exclude '.DS_Store'
  --exclude 'data/*.db'
  --exclude 'data/*.db-shm'
  --exclude 'data/*.db-wal'
  --exclude 'data/backups/'
  --exclude 'data/uploads/'
  --exclude 'app/stores_seed_local.py'
)

SYNC_SOURCES=(./app ./caddy ./scripts ./tests \
  Dockerfile docker-compose.yml requirements.txt wsgi.py README.md VERSION .env .gitignore)
"${RSYNC[@]}" \
  "${SYNC_SOURCES[@]}" \
  Dockerfile docker-compose.yml requirements.txt wsgi.py README.md VERSION .env .gitignore \
  "${REMOTE}:${VPS_DIR}/"

if [[ "${SYNC_DB}" == "1" ]]; then
  rsync -az -e "${RSYNC_SSH}" --chmod=F600 "${SNAPSHOT_DB}" "${REMOTE}:${VPS_DIR}/data/store_daily.db" \
    || rsync -az -e "${RSYNC_SSH}" "${SNAPSHOT_DB}" "${REMOTE}:${VPS_DIR}/data/store_daily.db"
fi

# docs 可选
if [[ -d docs ]]; then
  rsync -az -e "${RSYNC_SSH}" docs/ "${REMOTE}:${VPS_DIR}/docs/"
fi

if [[ "${SETUP_ONLY}" == "1" ]]; then
  echo "文件已拷完。到 VPS 上执行："
  echo "  ssh -p ${VPS_SSH_PORT} ${REMOTE}"
  echo "  cd ${VPS_DIR} && ./scripts/deploy_vps.sh"
  exit 0
fi

"${SSH[@]}" "${REMOTE}" "cd '${VPS_DIR}' && chmod +x scripts/*.sh && ./scripts/deploy_vps.sh"
echo
# 部署后把完整 git 历史推送到 offsite 备份机（代码灾备）
./scripts/backup_code.sh

# 部署成功后自动提交 VERSION 保留痕迹，保持工作区干净。
# 只提交 VERSION；代码改动仍需你手动提交（部署前）。
if git rev-parse --git-dir >/dev/null 2>&1 \
   && ! git diff --quiet VERSION \
   && git rev-parse --verify HEAD >/dev/null 2>&1; then
  VERLINE="$(sed -n '1p' VERSION | tr -d '\r')"
  git add VERSION
  if git -c user.name='pi-deploy' -c user.email='pi-deploy@local' \
       commit -q -m "升版本 ${VERLINE}（部署后自动提交）"; then
    echo "→ 已自动提交 VERSION: ${VERLINE}"
  else
    echo "  (VERSION 自动提交失败，见上)" >&2
    exit 1
  fi
else
  echo "  (VERSION 无改动或不在 git 仓库，跳过自动提交)"
fi
PORT="${CADDY_PORT:-8099}"
echo "目标机本机健康检查: ssh -p ${VPS_SSH_PORT} ${REMOTE} curl -s http://127.0.0.1:${PORT}/health"
echo "若 CADDY_BIND=0.0.0.0 可试: http://${VPS_HOST}:${PORT}"
