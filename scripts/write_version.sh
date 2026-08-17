#!/usr/bin/env bash
# 保留 VERSION 第一行的 V 0.x.x，只刷新构建时间和 git 短哈希。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VER="V 0.0.0"
if [[ -f VERSION ]]; then
  FIRST="$(head -n 1 VERSION | tr -d '\r')"
  NUM="$(echo "${FIRST}" | sed -E 's/^[Vv][[:space:]]*//')"
  if [[ "${NUM}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
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
