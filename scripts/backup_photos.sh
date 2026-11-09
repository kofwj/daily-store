#!/usr/bin/env bash
# 门店日报：把照片目录 data/uploads/imei 每天镜像一份到局域网备份机（Termux）。
#
# 为什么单独一个脚本：
#   照片是纯增量、体量大（一张 100–500KB，一年几千张）。原来它混在「每小时全量 uploads 快照」里，
#   一张图会被复制成 14 天 × 24 份 = 336 份，生产机 8 天就盘满（2026-11-09 排查结论）。
#   现在 uploads 快照用 --exclude='uploads/imei'，照片由本脚本每天整目录覆盖镜像 1 份 —— 不按份数放大。
#
# 落点：局域网备份机的 ~/store-daily-backups/imei_photos.tar（单文件，覆盖写）
# 恢复：tar xf imei_photos.tar -C /opt/store-daily/data/uploads
#
# 用法（在生产机）：
#   /opt/store-daily/scripts/backup_photos.sh
#   PHOTO_DEST_HOST=jian@host PHOTO_DEST_PORT=8022 ./scripts/backup_photos.sh
# cron（install_backup_cron.sh 会一并安装）：
#   20 3 * * * /opt/store-daily/scripts/backup_photos.sh >>/var/log/store-daily-photos.log 2>&1
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

SRC="${PHOTO_SRC:-/opt/store-daily/data/uploads/imei}"
DEST_HOST="${PHOTO_DEST_HOST:-jian@192.168.200.109}"
DEST_PORT="${PHOTO_DEST_PORT:-8022}"
DEST_FILE="${PHOTO_DEST_FILE:-store-daily-backups/imei_photos.tar}"

if [[ ! -d "${SRC}" ]]; then
  echo "$(date '+%F %T') 没有照片目录 ${SRC}，跳过"
  exit 0
fi

N=$(find "${SRC}" -maxdepth 1 -type f | wc -l)
SIZE=$(du -sh "${SRC}" | cut -f1)
START=$(date +%s)
echo "$(date '+%F %T') 开始镜像：${N} 个文件 / ${SIZE} -> ${DEST_HOST}:${DEST_FILE}"

# 不压缩（照片本身已压缩，gzip 只省 1–2% 却要多跑几分钟；纯 tar 更快）
# 先写 .part 再改名：传输中断不会留下半份「看起来可用」的包
tar cf - -C "$(dirname "${SRC}")" "$(basename "${SRC}")" \
  | ssh -p "${DEST_PORT}" -o BatchMode=yes -o ConnectTimeout=10 "${DEST_HOST}" \
      "mkdir -p \"\$(dirname ${DEST_FILE})\" && cat > ${DEST_FILE}.part && mv -f ${DEST_FILE}.part ${DEST_FILE}"

echo "$(date '+%F %T') 完成，耗时 $(( $(date +%s) - START ))s"
ssh -p "${DEST_PORT}" -o BatchMode=yes -o ConnectTimeout=10 "${DEST_HOST}" "ls -l ${DEST_FILE}" || true
