#!/usr/bin/env bash
# 把当前代码同步到灾备机 pve，并用最新 offsite 备份灌库后启动（仅本机 127.0.0.1:8099）。
# 不修改 Cloudflare；店员入口仍走 5 的 Tunnel。
#
# 用法：
#   ./scripts/deploy_pve_standby.sh
#   ./scripts/deploy_pve_standby.sh --skip-backup   # 不先跑 backup_offsite
#   ./scripts/deploy_pve_standby.sh --stop          # 只停止灾备容器
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PVE_HOST="${PVE_HOST:-pve.anemy.org}"
PVE_USER="${PVE_USER:-root}"
PVE_PORT="${PVE_PORT:-8022}"
PVE_DIR="${PVE_DIR:-/opt/store-daily}"
PRIMARY_HOST="${PRIMARY_HOST:-192.168.100.5}"
PRIMARY_USER="${PRIMARY_USER:-root}"

SKIP_BACKUP=0
STOP_ONLY=0
for a in "$@"; do
  case "$a" in
    --skip-backup) SKIP_BACKUP=1 ;;
    --stop) STOP_ONLY=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数 $a" >&2; exit 1 ;;
  esac
done

SSH_PVE=(ssh -o BatchMode=yes -o ConnectTimeout=15 -p "${PVE_PORT}" "${PVE_USER}@${PVE_HOST}")
SSH_PRI=(ssh -o BatchMode=yes -o ConnectTimeout=12 "${PRIMARY_USER}@${PRIMARY_HOST}")

if [[ "${STOP_ONLY}" == "1" ]]; then
  "${SSH_PVE[@]}" "cd '${PVE_DIR}' && docker compose stop"
  echo "灾备已停止"
  exit 0
fi

if [[ "${SKIP_BACKUP}" != "1" ]]; then
  echo "→ 先打三地备份"
  ./scripts/backup_offsite.sh
fi

echo "→ 同步代码到 pve（tar，不依赖 rsync）"
"${SSH_PVE[@]}" "mkdir -p '${PVE_DIR}/data' '${PVE_DIR}/scripts' '${PVE_DIR}/caddy'"
COPYFILE_DISABLE=1 tar czf - \
  --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='.ruff_cache' --exclude='.git' --exclude='data' --exclude='.pi' \
  --exclude='*.xlsx' --exclude='*.pdf' --exclude='.DS_Store' \
  --exclude='._*' --exclude='.AppleDouble' \
  app caddy scripts tests Dockerfile docker-compose.yml requirements.txt \
  wsgi.py README.md VERSION .env .gitignore docs pyproject.toml 2>/dev/null \
  | "${SSH_PVE[@]}" "cd '${PVE_DIR}' && tar xzf - && chmod +x scripts/*.sh"

echo "→ 灌最新备份库并写灾备 .env"
"${SSH_PVE[@]}" "PRIMARY_DIR='${PVE_DIR}' bash -s" <<'EOS'
set -euo pipefail
cd "${PRIMARY_DIR}"
LATEST_ENV=$(ls -t ~/store-daily-backups/env/env_offsite_*.env 2>/dev/null | head -1 || true)
[[ -n "${LATEST_ENV}" ]] && cp -f "${LATEST_ENV}" .env
grep -q '^CADDY_BIND=' .env && sed -i 's/^CADDY_BIND=.*/CADDY_BIND=127.0.0.1/' .env || echo 'CADDY_BIND=127.0.0.1' >> .env
grep -q '^STORE_DAILY_SECURE=' .env && sed -i 's/^STORE_DAILY_SECURE=.*/STORE_DAILY_SECURE=1/' .env || echo 'STORE_DAILY_SECURE=1' >> .env
grep -q '^STORE_DAILY_TRUST_PROXY=' .env && sed -i 's/^STORE_DAILY_TRUST_PROXY=.*/STORE_DAILY_TRUST_PROXY=1/' .env || echo 'STORE_DAILY_TRUST_PROXY=1' >> .env
grep -q '^CADDY_PORT=' .env && sed -i 's/^CADDY_PORT=.*/CADDY_PORT=8099/' .env || echo 'CADDY_PORT=8099' >> .env
LATEST_DB=$(ls -t ~/store-daily-backups/db/store_daily_offsite_*.db | head -1)
cp -f "${LATEST_DB}" data/store_daily.db
rm -f data/store_daily.db-wal data/store_daily.db-shm
echo "库: ${LATEST_DB}"
EOS

echo "→ 从生产机导出镜像到 pve（pve 常拉不了 Docker Hub）"
"${SSH_PRI[@]}" "docker save store-daily-app:latest caddy:2-alpine" \
  | "${SSH_PVE[@]}" "docker load"

echo "→ 启动灾备（仅 127.0.0.1:8099）"
"${SSH_PVE[@]}" "cd '${PVE_DIR}' && docker compose up -d --no-build && sleep 3 && docker compose ps && curl -sS http://127.0.0.1:8099/health && echo"

echo
echo "完成。店员入口未改，仍走 5 的 Cloudflare Tunnel。"
echo "故障切换见: docs/failover-cloudflare-tunnel.md"
