# 部署到局域网 VPS `192.168.100.5`

这台机器是内网地址，**不要绑 127.0.0.1**。绑 127.0.0.1 只有 VPS 自己打得开，手机和你的 Mac 都进不去。

8088 已有其他服务，日报默认用 **8099**。改端口只动 `.env` 里的 `CADDY_PORT`。

```text
店员手机 / 你的电脑
        --HTTP-->  192.168.100.5:8099
                        └─ Caddy (0.0.0.0:8099)
                              └─ store-daily 容器
```

和投资账本分开：独立目录、独立 Compose、独立库、独立口令。

## 从本机同步（推荐）

在 Mac 上，项目目录里执行：

```bash
cd /Users/jian/Downloads/store-daily
chmod +x scripts/sync_to_vps.sh scripts/deploy_vps.sh
./scripts/sync_to_vps.sh
```

默认：

| 项 | 值 |
|---|---|
| 地址 | `192.168.100.5` |
| 账号 | `root` |
| 远端目录 | `/opt/store-daily` |
| 端口 | `8099`（不抢 8088） |
| 绑定 | `0.0.0.0`（局域网可访问） |
| Cookie | `STORE_DAILY_SECURE=0`（HTTP 必须关） |

账号不是 root 时：

```bash
VPS_USER=ubuntu ./scripts/sync_to_vps.sh
```

第一次会要你输 SSH 密码。想免密，在本机做一次：

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id root@192.168.100.5
```

脚本会：

1. 本机没有 `.env` 时自动生成，并写入随机 `STORE_DAILY_SECRET`
2. `rsync` 代码、`.env`、以及本机 `data/store_daily.db`（含 8/13 回填）
3. SSH 到 VPS 跑 `docker compose up -d --build`

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

同一局域网里：

```text
http://192.168.100.5:8099
```

预置账号：`admin / 1234`，`ninghai / 0000`。进去立刻改口令。

VPS 上自检：

```bash
curl -s http://127.0.0.1:8099/health
# {"ok":true,"service":"store-daily"}
```

这里的 `127.0.0.1` 只用于 **VPS 自己查自己**。Caddy 对外绑的是 `0.0.0.0`。

## 远端没有 Docker 时

先 SSH 上去装：

```bash
ssh root@192.168.100.5
curl -fsSL https://get.docker.com | sh
```

再回本机跑 `./scripts/sync_to_vps.sh`。

## 以后上域名 / Cloudflare

把 `.env` 改成：

```env
STORE_DAILY_SECURE=1
CADDY_BIND=127.0.0.1
CADDYFILE=./caddy/Caddyfile.tunnel
APP_DOMAIN=daily.xxx.com
```

隧道 Public Hostname 回源 `http://127.0.0.1:8099`（这是 **VPS 本机回环**，和局域网直连不是一回事）。然后 `./scripts/sync_to_vps.sh --no-db`。

## 不要做的事

- 不要把 `CADDY_BIND` 设回 `127.0.0.1` 还想用手机打开
- 不要在纯 HTTP 下把 `STORE_DAILY_SECURE` 设成 1（登录会失败）
- 不要挂到投资账本的 GitHub 登录后面
- 不要把 8099 映射到公网网卡（这台如果只有 192.168.100.5，一般没这个问题）
- 不要再占用 8088
