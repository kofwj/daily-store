#!/usr/bin/env bash
# 把这个仓库的 pre-commit 钩子装到 .git/hooks（git 不支持版本控制 hooks）。
set -eu
cd "$(git rev-parse --show-toplevel)"
ln -sf ../../scripts/pre-commit .git/hooks/pre-commit
chmod +x scripts/pre-commit
echo "已安装 pre-commit 钩子：.git/hooks/pre-commit -> scripts/pre-commit"
echo "试试手动跑：scripts/pre-commit"