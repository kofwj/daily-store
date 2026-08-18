#!/usr/bin/env bash
# 每次发布把补丁号 +1（V 0.2.3 → V 0.2.4），并刷新时间和 git 短哈希。
# 只改时间和哈希时：BUMP=0 ./scripts/write_version.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VER="V 0.0.0"
if [[ -f VERSION ]]; then
  FIRST="$(head -n 1 VERSION | tr -d '\r')"
  NUM="$(echo "${FIRST}" | sed -E 's/^[Vv][[:space:]]*//')"
  if [[ "${NUM}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    if [[ "${BUMP:-1}" != "0" ]]; then
      MAJOR="${NUM%%.*}"
      REST="${NUM#*.}"
      MINOR="${REST%%.*}"
      PATCH="${REST#*.}"
      PATCH=$((PATCH + 1))
      NUM="${MAJOR}.${MINOR}.${PATCH}"
    fi
    VER="V ${NUM}"
  fi
fi
DATE="$(date '+%Y-%m-%d %H:%M')"
HASH=""
if git rev-parse --short HEAD >/dev/null 2>&1; then
  HASH="$(git rev-parse --short HEAD)"
fi
printf '%s\n%s\n%s\n' "${VER}" "${DATE}" "${HASH}" > VERSION
echo "VERSION ${VER} ${DATE} ${HASH}"
