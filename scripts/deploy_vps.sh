#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "缺少 .env。先: cp .env.example .env 并改 STORE_DAILY_SECRET" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

if [[ "${STORE_DAILY_SECRET}" == "replace-with-random-secret" ]]; then
  echo "STORE_DAILY_SECRET 还是示例值，先换成随机串" >&2
  exit 1
fi

mkdir -p data
docker compose up -d --build
docker compose ps

PORT="${CADDY_PORT:-8099}"
echo
echo "容器内健康检查："
curl -fsS "http://127.0.0.1:${PORT}/health"
echo
echo "局域网打开：http://${APP_DOMAIN:-192.168.100.5}:${PORT}"
