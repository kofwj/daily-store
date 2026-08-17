#!/usr/bin/env bash
# 保留 VERSION 第一行的 V0.x.x，只刷新构建时间和 git 短哈希。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VER="V0.0.0"
if [[ -f VERSION ]]; then
  FIRST="$(head -n 1 VERSION | tr -d '\r')"
  if [[ "${FIRST}" =~ ^[Vv][0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    VER="V${FIRST#*[Vv]}"
  fi
fi
DATE="$(date '+%Y-%m-%d %H:%M')"
if git rev-parse --short HEAD >/dev/null 2>&1; then
  HASH="$(git rev-parse --short HEAD)"
  SUBJ="$(git log -1 --format='%s' | tr '\n' ' ' | cut -c1-48)"
else
  HASH=""
  SUBJ=""
fi
printf '%s\n%s\n%s\n%s\n' "${VER}" "${DATE}" "${SUBJ}" "${HASH}" > VERSION
echo "VERSION ${VER} ${DATE} ${HASH}"
