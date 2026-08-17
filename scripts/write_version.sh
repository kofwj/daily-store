#!/usr/bin/env bash
# 把当前 git 短哈希写进 VERSION，部署后页头 /health 能对上。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if git rev-parse --short HEAD >/dev/null 2>&1; then
  HASH="$(git rev-parse --short HEAD)"
  DATE="$(date '+%Y-%m-%d %H:%M')"
  SUBJ="$(git log -1 --format='%s' | tr '\n' ' ' | cut -c1-48)"
else
  HASH="dev"
  DATE="$(date '+%Y-%m-%d %H:%M')"
  SUBJ=""
fi
printf '%s\n%s\n%s\n' "${HASH}" "${DATE}" "${SUBJ}" > VERSION
echo "VERSION ${HASH} ${DATE}"
