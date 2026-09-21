# 部署到局域网生产机

生产机是内网地址，**不要绑 127.0.0.1**。绑 127.0.0.1 只有机器自己打得开，手机和电脑都进不去。

默认端口 **8099**。改端口只动 `.env` 里的 `CADDY_PORT`。

```text
店员手机 / 电脑
        --HTTP-->  your-primary-host:8099
                        └─ Caddy (0.0.0.0:8099)
                              └─ store-daily 容器
```

独立目录、独立 Compose、独立库、独立口令。

## 从本机同步（推荐）

在项目目录里执行：

```bash
cd /path/to/store-daily
chmod +x scripts/sync_to_vps.sh scripts/deploy_vps.sh
./scripts/sync_to_vps.sh          # 默认不覆盖远端库
```

`.env` 里填：

| 项 | 示例 |
|---|---|
| `VPS_HOST` | 生产机地址 |
| `VPS_USER` | SSH 用户 |
| `VPS_DIR` | `/opt/store-daily` |
| `CADDY_PORT` | `8099` |
| `CADDY_BIND` | 默认 `127.0.0.1`；仅局域网明文 HTTP 才设 `0.0.0.0` |
| Cookie | TLS/Cloudflare 推荐 `STORE_DAILY_SECURE=1`；LAN HTTP 的 `0` 仅为显式 opt-in |

账号不是 root 时：

```bash
VPS_USER=ubuntu ./scripts/sync_to_vps.sh
```

第一次会要你输 SSH 密码。想免密，在本机做一次：

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id user@your-primary-host
```

脚本会：

1. 本机没有 `.env` 时自动生成，并写入随机 `STORE_DAILY_SECRET`
2. `rsync` 代码；`.env` **只在远端没有时初始化一次，之后以服务端为准**（免得把服务端手改的设置盖回本机）；**默认不覆盖远端库**
3. SSH 到生产机跑 `docker compose up -d --build`

之后改代码再跑同一条命令即可。本机 `data/store_daily.db` 是过期测试库，**禁止**用它盖生产。真要推库必须显式：

```bash
./scripts/sync_to_vps.sh --with-db   # 仅当本机库就是最新生产快照
```

8099 也被占了就改 `.env`：

```env
CADDY_PORT=8100
```

再同步一次。

## 打开

LAN HTTP 仅在确认内网可信、且接受明文凭据风险时使用：

```env
STORE_DAILY_SECURE=0
STORE_DAILY_TRUST_PROXY=1
CADDYFILE=./caddy/Caddyfile.lan
```

更安全的默认方式是受控 TLS 或 Cloudflare Tunnel：

```env
STORE_DAILY_SECURE=1
STORE_DAILY_TRUST_PROXY=1
CADDY_BIND=127.0.0.1
CADDYFILE=./caddy/Caddyfile.tunnel
```

预置账号：`admin / 123456`，进去立刻改成至少 8 位、非连续/重复数字的口令。示例店员见 `app/stores_seed.py`。生产必须使用随机 `STORE_DAILY_SECRET`，且不能使用示例值。

生产机上自检：

```bash
curl -s http://127.0.0.1:8099/health
# {"ok":true,"service":"store-daily"}
```

这里的 `127.0.0.1` 只用于 **机器自己查自己**。Caddy 对外绑的是 `0.0.0.0`。

## 远端没有 Docker 时

先 SSH 上去装：

```bash
ssh user@your-primary-host
curl -fsSL https://get.docker.com | sh
```

再回本机跑 `./scripts/sync_to_vps.sh`。

## 以后上域名 / Cloudflare

把 `.env` 改成：

```env
STORE_DAILY_SECURE=1
CADDY_BIND=127.0.0.1
CADDYFILE=./caddy/Caddyfile.tunnel
APP_DOMAIN=daily.example.com
```

隧道 Public Hostname 回源 `http://127.0.0.1:8099`（这是 **生产机本机回环**，和局域网直连不是一回事）。然后 `./scripts/sync_to_vps.sh`（默认不覆盖库）。

## 不要做的事

- 不要把 `CADDY_BIND` 设回 `127.0.0.1` 还想用手机打开
- 不要在纯 HTTP 下把 `STORE_DAILY_SECURE` 设成 1（登录会失败）
- 不要把 8099 映射到公网网卡

## 内网访问域名很卡（2026-09-21 实测）

现象：外网（手机流量）打开域名正常，局域网内用域名反而又慢又容易卡住。

原因不是服务器，而是**内网请求被网关代理绕出国再绕回来**：

```text
内网设备 → 网关代理（假 IP 198.18.x.x）→ 境外节点 → Cloudflare 边缘（cf-ray 显示 LAX）
                                              → Cloudflare Tunnel → 回到同一局域网 → Caddy → 应用
直连局域网地址：内网设备 → Caddy → 应用
```

实测（本机 192.168.100.200）：

| 请求 | 耗时 |
|---|---|
| `http://192.168.100.5:8099/login`（局域网直连） | **0.010s**（静态资源 0.006–0.007s） |
| `https://ai.anemy.org/login`（走域名） | TLS 0.45s、TTFB 0.97s、总计 1.8s，**且常常直接超时** |
| 强制连真实 Cloudflare IP（绕过假 IP） | 0.22–0.27s，稳定 |
| 网关 192.168.100.2 / 服务器 192.168.100.5 | 0.7/0.8ms |
| 1.1.1.1 / 8.8.8.8（境外） | 258ms / 214–267ms |

证据：`dig ai.anemy.org` 在内网返回假 IP（公网是 Cloudflare 的 172.67.179.93 / 104.21.88.124），
响应头 `cf-ray: ...-LAX` 说明流量是从洛杉矶边缘进来的；静态资源 `cf-cache-status: DYNAMIC`（没缓存），
所以每次打开页面都要跨太平洋跑好几个来回。

### 怎么办

1. **内网直接用局域网地址** `http://192.168.100.5:8099`（现已可在明文 HTTP 下登录）。最快，实测快 100 倍以上
2. 想让**域名**在内网也快：在网关代理里给自家域名加直连规则（如 `DOMAIN-SUFFIX,anemy.org,DIRECT`），
   并在内网 DNS 里把 `ai.anemy.org` 指向 `192.168.100.5`。注意内网只有 HTTP（Caddy `auto_https off`、端口 8099），
   所以内网要用 `http://ai.anemy.org:8099`；要用不带端口的 https 就得另配内网证书
3. 无论走哪条路，建议在 Cloudflare 给 `/static/*` 加缓存规则（现在全是 `DYNAMIC`），外网也能少跨几次太平洋

注意：`ai.anmey.org`（mey）公网是 NXDOMAIN，是拼错的写法；内网因为代理对任意域名都发假 IP，
所以拼错也"看起来能解析"，但一定打不开。群公告别写错。
