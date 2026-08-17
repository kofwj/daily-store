# Cloudflare Tunnel 故障切换清单（生产 ↔ 灾备）

主机名写在 `.env` / `scripts/backup.env`（不进 git）。下面用占位符。

## 拓扑

```text
店员 → Cloudflare 域名
         │
         ├─【正常】Tunnel（生产机）──► 127.0.0.1:8099     生产写库
         │
         └─【故障】Tunnel（灾备机）► 127.0.0.1:8099         灾备写库
```

- 生产穿透：Cloudflare Tunnel 指生产机
- 灾备 Caddy **只绑 127.0.0.1:8099**；店员不要收藏灾备 IP
- Tunnel 用 **Dashboard token 模式**：Public Hostname 在 Zero Trust 面板配
- 灾备机上若已有别的 hostname（路由器管理等），故障时只**新增**日报 hostname，不改原条目

---

## 一、平时

- [ ] 定时备份：`./scripts/backup_offsite.sh`（生产 → 局域网盘 + 远端）
- [ ] 灾备上 `/opt/store-daily` 存在，且 `docker compose ps` 可为 stopped 或仅本机 healthy
- [ ] Cloudflare Zero Trust 里记清：账号 / Tunnel 名称 / 店员用的域名 / Service URL
- [ ] 群公告只发 **Cloudflare 域名**，不发内网 IP

检查灾备本机是否正常（不经过 CF）：

```bash
ssh -p $REMOTE_BACKUP_SSH_PORT user@your-standby-host 'curl -sS http://127.0.0.1:8099/health'
# 期望 {"ok":true,"service":"store-daily"}
```

---

## 二、生产挂了 → 切到灾备（目标：域名不变）

### A. 起灾备服务并灌最新库

```bash
ssh user@your-standby-host
cd /opt/store-daily

LATEST=$(ls -t ~/store-daily-backups/db/store_daily_offsite_*.db | head -1)
echo "使用: $LATEST"
mkdir -p data/backups
cp -a data/store_daily.db "data/backups/before_failover_$(date +%Y%m%d_%H%M%S).db" 2>/dev/null || true
cp "$LATEST" data/store_daily.db
rm -f data/store_daily.db-wal data/store_daily.db-shm

docker compose up -d --build
curl -sS http://127.0.0.1:8099/health
```

### B. Cloudflare Tunnel 改指灾备

1. 打开 [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) → **Networks** → **Tunnels**
2. 找到 **灾备上正在跑的那条 Tunnel** → **Configure** → **Public Hostname**
3. **Add a public hostname**（与生产店员用的域名相同）：
   - Type: **HTTP**
   - URL: **`http://127.0.0.1:8099`**
4. 到 **生产机那条 Tunnel** 里，对**同一域名**：**Delete / 禁用**（避免两台抢流量）
5. 手机无痕打开该域名 → 登录 → 试填一笔

> 同一 hostname **同一时间只能有一个后端**。其它 hostname 原样保留。

**平时（未故障）**：灾备可以先不配日报 hostname；故障时再「灾备添加 + 生产删除」。

### C. 通知店员

> 系统已切到备份机，**网址不变**。请强刷或稍等 2 分钟再打开。

### D. 切换后自检

- [ ] 域名 `/health` 或打开登录页
- [ ] 管理员登录，看门店数、最近日报/触客是否接近故障前
- [ ] 店员试填一笔日报
- [ ] 确认 **只有灾备在写**，生产若还能开机先 `docker compose stop`

---

## 三、生产修好 → 切回

1. **灾备停写**
   ```bash
   ssh user@your-standby-host 'cd /opt/store-daily && docker compose stop'
   ```
2. **把故障期间的库从灾备拷回生产**（用 SQLite backup API，不要 `cp *.db`）
3. **Cloudflare**：hostname 改回生产 Tunnel；灾备上同一 hostname **关掉**
4. 手机验证原域名
5. 灾备：`docker compose stop`，继续只收备份

---

## 四、不要做的事

| 动作 | 原因 |
|---|---|
| 生产和灾备同时接同一 Tunnel hostname | 流量乱、数据双写 |
| 店员收藏灾备 IP:8099 | 绕过 CF，难切回、不安全 |
| 切过去后忘记停生产 | 双写无法合并 |
| 只靠安卓备份盘顶班 | 跑不了稳定 Docker 服务 |

---

## 五、相关命令速查

```bash
cd /path/to/store-daily
./scripts/backup_offsite.sh

# 同步代码到灾备（不覆盖库）
VPS_HOST=your-standby-host VPS_USER=root VPS_SSH_PORT=22 VPS_DIR=/opt/store-daily \
  ./scripts/sync_to_vps.sh --no-db

ssh user@your-standby-host 'curl -sS http://127.0.0.1:8099/health'
ssh user@your-primary-host 'curl -sS http://127.0.0.1:8099/health'
```
