# 门店日报

门店日报系统：当天填数、本月自动累计、复制进微信群。同时管触客、垫资、通报表和月度考核。

个人微信没有官方机器人，所以现在是 **保存 → 复制 → 贴群**。数据按多店、多角色建好，加店换人不用推倒。

和投资账本 `invest-tracker` 分开，互不影响。

## 谁用

| 角色 | 典型人 | 能做什么 |
|---|---|---|
| 管理员 | 运营 | 全店、看板、考核、兑付、账户、备份 |
| 填报员 | 店员 | 本店日报 / 触客 / 垫资 |
| 只读 · 店长 | 店长 | 看本店报表、通报、垫资，不能改数 |
| 只读 · 区域经理 | 区域经理 | 看辖区上述内容，不能改数 |

店长靠账户绑店；区域经理的范围是门店档案里的「区域经理」姓名。

默认口令登录后必须先改密，不能改回 `1234` / `123456`。

## 页面

- **今日填报**：只改「日」，累计自动算；可复制群播报
- **触客播报 / 记录**：一单一单记开口；「成交」只是结果。成功率 = 已成交 ÷ 全部触客
- **垫资**：本月流水。门店记账必须带号码；已兑付锁定，管理员可取消兑付后再改
- **报表**：单店日报 / 周报 / 月报，可导出 Excel
- **通报表**：有移动编码的店一行一张；复盘文案点名今日单项有量的店
- **多店看板 / 月度考核 / 修改审计 / 登录日志**：仅管理员
- **设置**：管理员管账户、门店、目标、规则、备份；其他人只能改自己口令

管理员看成交记录默认今天全店，看垫资默认本月全店，可按地市、区域经理再筛。

## 口径（别改错）

- 只存「日」。累 = 本月 1 号到所选日期合计，月初自动清零
- 考核奖罚按系统规则；实际酬金 = 开票 + 房补 − 垫资
- 未交日报不考核（标「本月未交」，净额 0）
- 通报：没有 `mobile_code` 的店不进表
- 触客：同店 + 当日 + 同号码覆盖；当天可改可删，往日只读
- 号码页面默认打码

## 本地开发

```bash
cd /Users/jian/Downloads/store-daily
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export STORE_DAILY_SECRET='换成一段随机字符串'
.venv/bin/python wsgi.py
```

浏览器打开 http://127.0.0.1:5055。没设随机 `STORE_DAILY_SECRET` 或仍是示例值，生产模式会拒绝启动。

```bash
.venv/bin/pip install pytest
.venv/bin/pytest -q
```

门店目录：公开仓库只有示例店 `app/stores_seed.py`。真实店名、人员放 `app/stores_seed_local.py`（不进 git）。生产机有这份文件就会用真实目录；测试设 `STORE_DAILY_SAMPLE_SEED=1` 强制示例店。

## 生产

店员入口走 **Cloudflare 域名**（Tunnel）。家里生产机：

| 项 | 值 |
|---|---|
| 写库 | `192.168.100.5:/opt/store-daily` |
| 内网自检 | http://192.168.100.5:8099 |
| 灾备机 | `pve.anemy.org`（平时只收备份，不接店员） |
| 局域网盘 | `192.168.100.109` Termux（手机要开着） |

```bash
# 同步代码到 5，不覆盖库
./scripts/sync_to_vps.sh --no-db

# 每小时机外备份（已装在 5 的 cron）
# 5 → 109 + pve，SQLite backup API，禁止直接 cp *.db
```

文档：

- [局域网部署](docs/deploy-vps.md)
- [备份与恢复](docs/backup-dr.md)
- [Cloudflare Tunnel 切换](docs/failover-cloudflare-tunnel.md)
- [每日更新](docs/changelog.md)

原则：**只在一处写库**。5 和 pve 不要同时接店员。切灾备按 Tunnel 清单改 hostname，域名不变。

## 数据

默认库 `data/store_daily.db`，`.env` 不进 git。换路径：

```bash
export STORE_DAILY_DB=/path/to/store_daily.db
export STORE_DAILY_DATA=/path/to/data
export STORE_DAILY_SECRET='随机长串'
```
