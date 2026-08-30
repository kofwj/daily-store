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
./scripts/sync_to_vps.sh
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
2. `rsync` 代码、`.env`；加 `--no-db` 则不覆盖远端库
3. SSH 到生产机跑 `docker compose up -d --build`

之后改代码再跑同一条命令即可。远端已经有人填过数、不想被本机空库盖掉时：

```bash
./scripts/sync_to_vps.sh --no-db
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

隧道 Public Hostname 回源 `http://127.0.0.1:8099`（这是 **生产机本机回环**，和局域网直连不是一回事）。然后 `./scripts/sync_to_vps.sh --no-db`。

## 不要做的事

- 不要把 `CADDY_BIND` 设回 `127.0.0.1` 还想用手机打开
- 不要在纯 HTTP 下把 `STORE_DAILY_SECURE` 设成 1（登录会失败）
- 不要把 8099 映射到公网网卡
