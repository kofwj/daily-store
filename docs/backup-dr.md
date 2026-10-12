# 备份与容错（局域网 + 异地）

主机名写在 `scripts/backup.env`（不进 git）。下面用占位符。

## 角色怎么分

| 角色 | 建议机器 | 干什么 |
|---|---|---|
| **生产** | `PRIMARY_HOST` | 店员每天写的那台，Docker 跑着 |
| **局域网备份** | `LAN_BACKUP_HOST`（NAS / 安卓等） | 每天收一份库，断网/坏盘时最快救 |
| **异地备份** | `REMOTE_BACKUP` | 整网/机房出事时的底牌（放在另一处场地） |
| **开发机** | 本机 | 改代码；**不要**拿开发库盖生产 |
| **灾备热备**（可选） | `DR_STANDBY_HOST` | 预装一套代码+库，随时切过去；平时不接店员流量 |

原则：

1. **只在一处写库**（生产）。备份机、异地机默认只收文件，不接店员流量。
2. **拷库必须用 SQLite backup API**，不要 `cp store_daily.db`（有 WAL 会拷残）。
3. **代码** 在 git；**数据 + `.env`** 靠备份脚本，不进 git。

```text
店员手机 ──HTTP──► PRIMARY_HOST:8099   ← 唯一生产
                      │
                      │ 每小时 backup_offsite.sh（生产机 cron）
                      ├──────► LAN_BACKUP_HOST  ~/store-daily-backups/
                      └──────► REMOTE_BACKUP    ~/store-daily-backups/
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

脚本：`scripts/backup_offsite.sh`。库和 `.env` 没变会跳过拷贝，只打日志「库未变化，跳过拷贝」；
这一小时仍会探活两个落点（见 4. 失败/恢复告警）。

### 1. 准备 SSH 免密

在跑备份的机器上：

```bash
ssh-copy-id user@your-primary-host
ssh-copy-id user@your-lan-backup-host
ssh-copy-id user@your-standby-host
```

### 2. 手动跑一次

```bash
cd /path/to/store-daily
cp scripts/backup.env.example scripts/backup.env
# 改成真实主机后：

./scripts/backup_offsite.sh --dry-run
./scripts/backup_offsite.sh
```

会得到：

```text
~/store-daily-backups/
  db/store_daily_offsite_YYYYMMDD_HHMMSS.db
  env/env_offsite_YYYYMMDD_HHMMSS.env   # 权限 600
```

### 3. 定时（cron，推荐生产机每小时）

开发机睡觉会漏备份，所以 **cron 装在生产机上**。

```bash
ssh user@your-primary-host /opt/store-daily/scripts/install_backup_cron.sh
```

会写入：

```cron
5 * * * * /opt/store-daily/scripts/backup_offsite.sh >>/var/log/store-daily-offsite.log 2>&1
```

并生成本机 SSH 钥匙。把打印出的公钥加到局域网备份机和异地备份机。

局域网或异地暂时连不上时脚本会告警；只要还有一个落点收到，整次仍算有副本。

看日志：

```bash
ssh user@your-primary-host 'tail -50 /var/log/store-daily-offsite.log'
```

### 4. 失败/恢复告警（企业微信）

备份失败会主动推到企业微信群，不再只躺在日志里（2026-10 之前远端落点静默失效 53 天，
就是因为只写日志、exit code 仍是 0）：

```bash
# 通道自检（用非法 msgtype 探活：企微会拒绝，群里不会出现消息）
python3 scripts/notify_failure.py --probe

# 演练：只打印将要发送的内容，不真发
ALERT_DRY_RUN=1 ./scripts/backup_offsite.sh --dry-run
```

- webhook 默认读生产库 `app_meta.wecom_global`（设置页那个机器人），可用 `ALERT_WEBHOOK` 覆盖
- 通知规则：`ok→fail` 立即发；`fail→fail` 24 小时内静默（超 24 小时再提醒一次）；`fail→ok` 发一条恢复
- 去重状态：`data/backups/.alert-state.json`（不随代码同步、不被清理）
- 告警自己发不出去**不影响**备份，只往日志里写

「库未变化」这一小时的落点探活：库没变化时脚本本来会直接退出、不连备份机，落点掉线得等到
下次有数据变化才暴露（远端那次静默失效就是这么漏了很久）。现在这个小时也会做一次轻量探活：
ssh 连得上且备份目录在 = 可用（顺带打一行剩余空间）；全挂 → `exit 1` + 告警，挂一半 → 仍
`exit 0`、由告警提示。不想要这层检查可设 `SKIP_PROBE=0`。

生产机自己也还有应用内备份（设置页「立即备份」）。

---

## 三、容错怎么做（由易到难）

### 级别 A：只备份，不双活（够用，推荐先做）

- 生产挂了 → 从局域网或异地把最新 `.db` 拷回，重启 Docker
- RPO：最多丢「上次备份到故障」之间的数据
- RTO：半小时内能恢复（有文档 + SSH）

### 级别 B：局域网热备

备份机上同样部署一份代码 + compose，**平时不写库或停掉 app**：

```bash
rsync -az user@your-primary-host:/opt/store-daily/ /opt/store-daily/
cd /opt/store-daily && docker compose up -d --build
```

故障切换：

1. 确认生产机已停写（关容器或拔网）
2. 把最新备份库放到备份机的 `data/store_daily.db`
3. `docker compose up -d`
4. 路由器/内网 DNS 把「日报地址」指到备份机

不要两台同时写同一个业务库。

### 级别 C：公网入口 + 异地灾备

- 日常仍写局域网生产（延迟低）
- 公网机只做：备份落点，或 Cloudflare Tunnel 反代到家里
- 整网挂了：在异地把 docker 起起来，导入最近备份，改 Tunnel/DNS 指向它

TLS：生产若走公网，用 `STORE_DAILY_SECURE=1` + tunnel/Caddy HTTPS（见 `docs/deploy-vps.md`）。

---

## 四、恢复步骤（背下来）

### 1）只恢复数据库（生产机还在）

```bash
scp user@your-lan-backup-host:~/store-daily-backups/db/store_daily_offsite_XXXX.db /tmp/restore.db

ssh user@your-primary-host
cd /opt/store-daily
docker compose stop app
cp -a data/store_daily.db data/backups/store_daily_before_manual_restore_$(date +%Y%m%d_%H%M%S).db
cp /tmp/restore.db data/store_daily.db
rm -f data/store_daily.db-wal data/store_daily.db-shm
docker compose start app
curl -s http://127.0.0.1:8099/health
```

或用网页：设置 → 备份恢复 → 上传该 `.db`。

### 2）整机重装 / 换机

```bash
mkdir -p /opt/store-daily && cd /opt/store-daily
./scripts/sync_to_vps.sh --no-db   # 或 git clone + 拷 .env

mkdir -p data
scp user@your-lan-backup-host:~/store-daily-backups/db/最新.db data/store_daily.db
scp user@your-lan-backup-host:~/store-daily-backups/env/最新.env .env
chmod 600 .env data/store_daily.db

./scripts/deploy_vps.sh
```

### 3）验证恢复是否完整

```bash
sqlite3 data/store_daily.db "SELECT COUNT(*) FROM stores; SELECT COUNT(*) FROM daily_reports; SELECT COUNT(*) FROM advance_posts;"
```

登录管理员，看：报表、触客记录、垫资、设置→组织 是否都在。

---

## 五、不要做的事

| 动作 | 为什么 |
|---|---|
| `./scripts/sync_to_vps.sh --with-db` 用本机旧库盖生产 | 本机空库/测试库会冲掉线上。日常部署不要带 `--with-db` |
| 直接 `cp *.db` 当备份 | WAL 模式可能不完整 |
| 两台同时接店员写 | 数据分叉，无法合并 |
| 把 `.env` 提交 git / 发群 | 会话密钥泄露等于谁都能伪造登录 |
| 只靠应用内 `data/backups` | 盘一挂全没 |

---

## 六、建议先做的最小集

1. 备份机上建目录：`mkdir -p ~/store-daily-backups/{db,env,uploads}`
2. 配好到生产和异地备份机的 SSH 免密
3. 跑通一次：`./scripts/backup_offsite.sh`
4. 写上 cron（每天一次；重要档期可改每小时）
5. 故意在测试目录恢复一份，确认能登录、店数对
