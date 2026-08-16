# Cloudflare Tunnel 故障切换清单（生产 5 ↔ 灾备 pve）

## 拓扑（当前）

```text
店员 → Cloudflare 域名
         │
         ├─【正常】Tunnel（5 上）──► 192.168.100.5:8099     生产写库
         │
         └─【故障】Tunnel（pve 上）► 127.0.0.1:8099         灾备写库
                                      ↑
                         pve: store-daily + cloudflared(host 网络)
                         备份: ~/store-daily-backups +
                               109 安卓
```

- 生产穿透：Cloudflare Tunnel 指 5  
- pve 已跑 `store-daily-cloudflared`（**host 网络** + `restart unless-stopped`）  
- 灾备 Caddy **只绑 127.0.0.1:8099**；店员不要收藏 pve IP  
- Tunnel 用 **Dashboard token 模式**：Public Hostname 在 Zero Trust 面板配  

---

## 一、平时（已部署灾备后）

- [ ] 定时备份：`./scripts/backup_offsite.sh`（5 → 109 + pve）  
- [ ] pve 上 `/opt/store-daily` 存在，且 `docker compose ps` 可为 stopped 或仅本机 healthy  
- [ ] Cloudflare Zero Trust 里记清：  
  - 账号 / Team 名  
  - Tunnel 名称（跑在 5 上的那条）  
  - Public hostname（店员用的域名）  
  - Service 现在是 `http://127.0.0.1:8099` 或 `http://192.168.100.5:8099`（以你面板为准）  
- [ ] pve 上已装 `cloudflared`（可先不登录；或已 `cloudflared service install` 但 **hostname 未启用**）  
- [ ] 群公告只发 **Cloudflare 域名**，不发内网 IP / pve  

检查灾备本机是否正常（不经过 CF）：

```bash
ssh -p 8022 root@pve.anemy.org 'curl -sS http://127.0.0.1:8099/health'
# 期望 {"ok":true,"service":"store-daily"}
```

---

## 二、5 挂了 → 切到 pve（目标：域名不变）

### A. 起灾备服务并灌最新库

```bash
ssh -p 8022 root@pve.anemy.org
cd /opt/store-daily

# 1) 最新备份
LATEST=$(ls -t ~/store-daily-backups/db/store_daily_offsite_*.db | head -1)
echo "使用: $LATEST"
mkdir -p data/backups
cp -a data/store_daily.db "data/backups/before_failover_$(date +%Y%m%d_%H%M%S).db" 2>/dev/null || true
cp "$LATEST" data/store_daily.db
rm -f data/store_daily.db-wal data/store_daily.db-shm

# 2) 启动（本机 8099）
docker compose up -d --build
curl -sS http://127.0.0.1:8099/health
```

### B. Cloudflare Tunnel 改指 pve（推荐两种里选一）

#### 方式 1：在 pve 的 Tunnel 上挂「日报域名」（推荐）

pve 上 Tunnel 往往**已经有别的 hostname**（例如爱快 `ikuai.…` → 路由器）。  
**不要改掉原有条目**，只在同一 Tunnel 里**再加一条**日报域名。

1. 打开 [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) → **Networks** → **Tunnels**  
2. 找到 **pve 上正在跑的那条 Tunnel**（connector 在线）→ **Configure** → **Public Hostname**  
3. **Add a public hostname**（与生产店员用的域名相同，如 `daily.你的域`）：  
   - Type: **HTTP**  
   - URL: **`http://127.0.0.1:8099`**  
     （pve 上 cloudflared 已用 host 网络，能打到本机 Caddy）  
4. 到 **5 上那条 Tunnel** 里，对**同一域名**：**Delete / 禁用**（避免两台抢 DNS/流量）  
5. 若 5 整机已死：只保留 pve 上这一条即可  
6. 手机无痕打开该域名 → 登录 → 试填一笔  

> 同一 hostname **同一时间只能有一个后端**。  
> 爱快等其它 hostname 原样保留，互不影响。

**平时（未故障）**：pve 上可以**先不配**日报 hostname，或配了但 5 上仍占着该域名；  
故障时再「pve 添加/启用 + 5 删除」。

#### 方式 2：5 的 Tunnel 还活着，只改 Service URL（少见）

仅当 cloudflared 不在 5 本机、而在另一台能访问两台内网的机器上时：

1. 编辑该 Tunnel 的 Public Hostname  
2. Service 从 `http://192.168.100.5:8099` 改为 `http://pve可达地址:8099`  
3. 保存，等 1～2 分钟  

你当前是「Tunnel 打到家里 5」，5 整机挂了通常 **方式 1** 才有用。

#### 方式 3：临时用 pve 公网 IP（不推荐当常态）

1. 路由器放行 pve 侧 `8099`  
2. `.env` 里 `CADDY_BIND=0.0.0.0` 后 `docker compose up -d`  
3. Cloudflare DNS 将该域名临时改 A 记录到 pve 公网 IP，橙云代理  
4. 事后改回 Tunnel  

安全差，只作 Tunnel 都配不好时的急救。

### C. 通知店员

> 系统已切到备份机，**网址不变**。请强刷或稍等 2 分钟再打开。  
> 若进不去，把截图发群。

### D. 切换后自检

- [ ] `https://你的域名/health` 或打开登录页  
- [ ] 管理员登录，看门店数、最近日报/成交是否接近故障前  
- [ ] 店员试填一笔日报  
- [ ] 确认 **只有 pve 在写**，5 若还能开机先 `docker compose stop`  

---

## 三、5 修好 → 切回生产

1. **pve 停写**  
   ```bash
   ssh -p 8022 root@pve.anemy.org 'cd /opt/store-daily && docker compose stop'
   ```  
2. **把故障期间的库从 pve 拷回 5**（一致性快照）  
   ```bash
   ssh -p 8022 root@pve.anemy.org \
     "python3 -c \"import sqlite3;s=sqlite3.connect('/opt/store-daily/data/store_daily.db');d=sqlite3.connect('/tmp/back_to_5.db');s.backup(d);d.close();s.close()\""
   scp -P 8022 root@pve.anemy.org:/tmp/back_to_5.db /tmp/back_to_5.db
   scp /tmp/back_to_5.db root@192.168.100.5:/opt/store-daily/data/store_daily.db
   ssh root@192.168.100.5 'cd /opt/store-daily && rm -f data/*.db-wal data/*.db-shm && docker compose up -d'
   ```  
3. **Cloudflare**：hostname 改回 5 的 Tunnel；pve 上同一 hostname **关掉**  
4. 手机验证原域名  
5. pve：`docker compose stop`，继续只收备份  

---

## 四、pve 上 cloudflared 预备（可选，现在做省事）

在 pve 上（有浏览器登录 CF 时）：

```bash
# Debian 安装 cloudflared 见 Cloudflare 文档，然后：
cloudflared tunnel login
cloudflared tunnel create store-daily-dr
cloudflared tunnel route dns store-daily-dr 你的域名   # 若与生产抢 DNS 先别 route
# 配置 config.yml 里 ingress → http://127.0.0.1:8099
# 先不 install service，或 install 但 public hostname 留空直到故障
```

生产 hostname 仍绑在 5 的 tunnel 上；故障时按「方式 1」把 DNS/hostname 切到 `store-daily-dr`。

---

## 五、不要做的事

| 动作 | 原因 |
|---|---|
| 5 和 pve 同时接同一 Tunnel hostname | 流量乱、数据双写 |
| 店员收藏 `pve.anemy.org:8099` | 绕过 CF，难切回、不安全 |
| 切过去后忘记停 5 | 双写无法合并 |
| 只靠 109 安卓顶班 | 跑不了稳定 Docker 服务 |

---

## 六、相关命令速查

```bash
# 备份
cd /Users/jian/Downloads/store-daily && ./scripts/backup_offsite.sh

# 同步代码到 pve（不覆盖库）
VPS_HOST=pve.anemy.org VPS_USER=root VPS_SSH_PORT=8022 VPS_DIR=/opt/store-daily \
  ./scripts/sync_to_vps.sh --no-db

# 健康
ssh -p 8022 root@pve.anemy.org 'curl -sS http://127.0.0.1:8099/health'
ssh root@192.168.100.5 'curl -sS http://127.0.0.1:8099/health'
```
