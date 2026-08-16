# 备份与容错（局域网 + 远端 VPS）

## 角色怎么分

| 角色 | 建议机器 | 干什么 |
|---|---|---|
| **生产** | 局域网 `192.168.100.5`（现网） | 店员每天写的那台，Docker 跑着 |
| **局域网备份** | `192.168.100.109`（或 NAS） | 每天收一份库，断网/坏盘时最快救 |
| **远端备份** | 公网 VPS | 机房断电、整网挂了时的底牌 |
| **开发机** | 你的 Mac | 改代码；**不要**拿开发库盖生产 |

原则：

1. **只在一处写库**（生产）。备份机、远端 VPS 默认只收文件，不接店员流量。  
2. **拷库必须用 SQLite backup API**，不要 `cp store_daily.db`（有 WAL 会拷残）。  
3. **代码** 在 git；**数据 + `.env`** 靠备份脚本，不进 git。

```text
店员手机 ──HTTP──► 192.168.100.5:8099   ← 唯一生产
                      │
                      │ 每天凌晨 backup_offsite.sh
                      ├──────► 192.168.100.109  ~/store-daily-backups/
                      └──────► 公网 VPS         ~/store-daily-backups/
```

---

## 一、现成能力（应用内）

设置 → 备份恢复：

- **立即备份**：`data/backups/store_daily_*.db`，最多留 20 份  
- **下载 / 上传恢复**：恢复前会再拍一份 `before_restore`  
- 校验：文件头 + 必要表（stores/users/…）

这只防「人手误删、想回滚」。**防不了整机挂掉**，所以还要机外备份。

---

## 二、机外备份（推荐）

脚本：`scripts/backup_offsite.sh`

### 1. 准备 SSH 免密

在跑备份的机器上（Mac 或 109）：

```bash
ssh-copy-id root@192.168.100.5          # 生产
ssh-copy-id 你的用户@192.168.100.109    # 局域网备份
ssh-copy-id ubuntu@你的公网VPS          # 远端
```

### 2. 手动跑一次

```bash
cd /path/to/store-daily

# 先看命令、不写盘
PRIMARY_HOST=192.168.100.5 \
LAN_BACKUP_HOST=192.168.100.109 \
LAN_BACKUP_USER=你的用户 \
REMOTE_BACKUP=ubuntu@公网IP \
./scripts/backup_offsite.sh --dry-run

# 真备份
PRIMARY_HOST=192.168.100.5 \
LAN_BACKUP_HOST=192.168.100.109 \
LAN_BACKUP_USER=你的用户 \
REMOTE_BACKUP=ubuntu@公网IP \
KEEP_DAYS=30 \
./scripts/backup_offsite.sh
```

会得到：

```text
~/store-daily-backups/
  db/store_daily_offsite_YYYYMMDD_HHMMSS.db
  env/env_offsite_YYYYMMDD_HHMMSS.env   # 权限 600
```

### 3. 定时（cron）

在 **109 或 Mac** 上：

```bash
crontab -e
```

```cron
# 每天 02:15，从生产拉库推到 109 + 远端
15 2 * * * cd /path/to/store-daily && \
  PRIMARY_HOST=192.168.100.5 \
  LAN_BACKUP_HOST=192.168.100.109 \
  LAN_BACKUP_USER=你的用户 \
  REMOTE_BACKUP=ubuntu@公网IP \
  ./scripts/backup_offsite.sh >>/tmp/store-daily-backup.log 2>&1
```

生产机自己也可以再留一份应用内备份（设置页或 cron 调容器）：

```bash
# 在 192.168.100.5 上，每天 02:00 容器内快照
0 2 * * * cd /opt/store-daily && docker compose exec -T app \
  python -c 'from app import backup; print(backup.snapshot("cron"))' \
  >>/var/log/store-daily-backup.log 2>&1
```

---

## 三、容错怎么做（由易到难）

### 级别 A：只备份，不双活（够用，推荐先做）

- 生产挂了 → 从 109 或远端把最新 `.db` 拷回，重启 Docker  
- RPO：最多丢「上次备份到故障」之间的数据（cron 每天 = 最多约 24h；改成每小时则更短）  
- RTO：半小时内能恢复（有文档 + SSH）

### 级别 B：局域网热备（109 当替补）

109 上同样部署一份代码 + compose，**平时不写库或停掉 app**：

```bash
# 109 首次
rsync -az root@192.168.100.5:/opt/store-daily/ /opt/store-daily/
# 改 109 的 .env：CADDY_PORT 可仍 8099，SECRET 与生产相同才能续会话（可选）
cd /opt/store-daily && docker compose up -d --build
```

故障切换：

1. 确认 5 号机已停写（关容器或拔网）  
2. 把最新备份库放到 109 的 `data/store_daily.db`  
3. `docker compose up -d`  
4. 路由器/内网 DNS 把「日报地址」指到 `192.168.100.109:8099`  
   （或让店员临时改收藏夹）

不要两台同时写同一个业务库。

### 级别 C：公网入口 + 远端灾备

- 日常仍写局域网生产（延迟低）  
- 公网 VPS 只做：备份落点，或 Cloudflare Tunnel 反代到家里  
- 整网挂了：在远端 VPS 起 docker，导入最近备份，改 Tunnel/DNS 指向远端  

TLS：生产若走公网，用 `STORE_DAILY_SECURE=1` + tunnel/Caddy HTTPS（见 `docs/deploy-vps.md`）。

---

## 四、恢复步骤（背下来）

### 1）只恢复数据库（生产机还在）

```bash
# 任选一份备份
scp 用户@192.168.100.109:~/store-daily-backups/db/store_daily_offsite_XXXX.db /tmp/restore.db

ssh root@192.168.100.5
cd /opt/store-daily
docker compose stop app
# 先留现场
cp -a data/store_daily.db data/backups/store_daily_before_manual_restore_$(date +%Y%m%d_%H%M%S).db
cp /tmp/restore.db data/store_daily.db
rm -f data/store_daily.db-wal data/store_daily.db-shm
docker compose start app
curl -s http://127.0.0.1:8099/health
```

或用网页：设置 → 备份恢复 → 上传该 `.db`。

### 2）整机重装 / 换机

```bash
# 新机器
mkdir -p /opt/store-daily && cd /opt/store-daily
# 同步代码（从 Mac）
./scripts/sync_to_vps.sh --no-db   # 或 git clone + 拷 .env

# 放入备份库
mkdir -p data
scp 用户@192.168.100.109:~/store-daily-backups/db/最新.db data/store_daily.db
scp 用户@192.168.100.109:~/store-daily-backups/env/最新.env .env
chmod 600 .env data/store_daily.db

./scripts/deploy_vps.sh
```

### 3）验证恢复是否完整

```bash
sqlite3 data/store_daily.db "SELECT COUNT(*) FROM stores; SELECT COUNT(*) FROM daily_reports; SELECT COUNT(*) FROM advance_posts;"
```

登录管理员，看：报表、成交记录、垫资、设置→组织 是否都在。

---

## 五、不要做的事

| 动作 | 为什么 |
|---|---|
| `./scripts/sync_to_vps.sh` **带库**盖生产 | 本机空库/旧库会冲掉线上 |
| 直接 `cp *.db` 当备份 | WAL 模式可能不完整 |
| 两台同时接店员写 | 数据分叉，无法合并 |
| 把 `.env` 提交 git / 发群 | 会话密钥泄露等于谁都能伪造登录 |
| 只靠应用内 `data/backups` | 盘一挂全没 |

---

## 六、建议你今天做的最小集

1. 109 上建目录：`mkdir -p ~/store-daily-backups/{db,env}`  
2. 配好到 `192.168.100.5` 和公网 VPS 的 SSH 免密  
3. 跑通一次：  
   `REMOTE_BACKUP=ubuntu@公网IP ./scripts/backup_offsite.sh`  
4. 写上 cron（每天一次；重要档期可改每小时）  
5. 故意在测试目录恢复一份，确认能登录、店数对  

做到这五步，局域网挂了有 109，整网挂了有远端 VPS。
