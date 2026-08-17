#!/usr/bin/env bash
# 在生产机安装「每小时机外备份」cron，并确保本机有推送到备份机的 SSH 钥匙。
# 用法：在生产机上
#   /opt/store-daily/scripts/install_backup_cron.sh
# 从开发机：
#   ssh user@your-primary-host /opt/store-daily/scripts/install_backup_cron.sh
set -euo pipefail

DIR="${STORE_DAILY_DIR:-/opt/store-daily}"
SCRIPT="${DIR}/scripts/backup_offsite.sh"
LOG="${BACKUP_CRON_LOG:-/var/log/store-daily-offsite.log}"
KEY="${HOME}/.ssh/id_ed25519"
CRON_LINE="5 * * * * ${SCRIPT} >>${LOG} 2>&1"

if [[ ! -x "${SCRIPT}" && -f "${SCRIPT}" ]]; then
  chmod +x "${SCRIPT}"
fi
if [[ ! -f "${SCRIPT}" ]]; then
  echo "找不到 ${SCRIPT}" >&2
  exit 1
fi

mkdir -p "${HOME}/.ssh"
chmod 700 "${HOME}/.ssh"
if [[ ! -f "${KEY}" ]]; then
  ssh-keygen -t ed25519 -N "" -f "${KEY}" -C "store-daily-backup@$(hostname -s)"
  echo "已生成本机钥匙: ${KEY}.pub"
fi

echo "本机公钥（请加到 pve / 109 的 authorized_keys）："
cat "${KEY}.pub"
echo

if command -v crontab >/dev/null 2>&1; then
  tmp="$(mktemp)"
  crontab -l 2>/dev/null | grep -v 'store-daily/scripts/backup_offsite.sh' >"${tmp}" || true
  printf '%s\n' "${CRON_LINE}" >>"${tmp}"
  crontab "${tmp}"
  rm -f "${tmp}"
  echo "已写入 cron："
  crontab -l | grep backup_offsite || true
else
  echo "没有 crontab 命令" >&2
  exit 1
fi

touch "${LOG}"
chmod 640 "${LOG}" || true
echo "日志: ${LOG}"
echo "试跑: ${SCRIPT}"
