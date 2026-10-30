# 《V5 落库与 store-daily 改造对接清单》（V4 终审无死角执行版 · 生产/CI/测试全闭环）

> **版本说明（V4 终审定论版）**：本版已全线对齐领导终审定论：**① 顺差让利单店算自己的**（彻底摒弃母子店跟跳与跨店分摊，每家店严格按本店当月申报笔数独立分档，小店自己不做满 10 笔无达量加价）；**② 移动优选会员 0 元义务项与 1:1 强绑定**（不发钱，一笔购机让利必须同开一笔优选会员，纯作为合规准入校验）；**③ 彻底修复 3 条可能导致测试与 CI 失败的代码隐患**（测试夹具脱离本地私有依赖、`render_broadcast` 纯函数签名平稳兼容、`init_db` 启动自检降级为非阻塞软警告）；**④ 顺差让利垫资追回款独立列示**（不进负向扣罚 ③、绝不污染 3A 扣款率）；**⑤ 前端与通报表闭环修复**（通报表 AI 列自动聚合、设置页顾问字段只读防覆盖、唯一约束去除伪条件），代码与文档全闭环交付。
>
> **重要执行期与数据隔离声明（核心定心丸）**：
> 1. **完全不影响 10 月及历史数据**：V5 方案正式执行月份为 **2026 年 11 月或 12 月**（以正式下文为准）。在系统内执行迁移 17（Migration 17）**纯属新增表结构（表 1–6）与向后兼容补列**，绝不修改、覆盖或重新计算 2026 年 10 月及之前的历史既有报表与算薪数据。
> 2. **双轨平滑并行**：10 月份及以前月份继续按原口径展示与归档；只有在管理员正式在后台为指定月份（如 2026-11）点击“生成 V5 月结表”时，V5 计算引擎才会对该月份触发。

---

## 一、库内 34 家门店真实主键、V5 模式与安全落库状态表

`data/store_daily.db` 的 `stores` 表当前共 **34 行（`id = 1..34`）**。**严禁在任何 SQL 或业务逻辑中按 `id BETWEEN 29 AND 34` 硬编码更新**（解决硬伤：测试库 `STORE_DAILY_SAMPLE_SEED=1` 时示例店是 `id 1..6`，硬编码会导致测试命中 0 行、生产真库裸奔），一律使用 **`WHERE code = ?`** 定位：

| `stores.id` | `stores.code` *(主键)* | `login` *(账号)* | `short_name` | `stores.name` | `city` | `active` | `v5_mode` | `carrier_scope` | `mobile_code` | `mobile_code_active` | `affiliated_to` *(母店id)* | 核定净酬金基线(元) | 目标搭载率 | 2026-10起在任顾问 (`advisor_name`) | 基础系数 (`base_coef`) | 当期平衡点(元) | 95%缓冲线(元) | 超额分成比 |
|---:|---|---|---|---|:---:|:---:|---|---|---|:---:|---|---:|:---:|---|---:|---:|---:|---:|
| **7** | `rg-ninghai` | `rgning` | 如皋宁海路 | TZ南通市如皋市如城镇宁海路体验店 | 南通市 | **`1`** | `ADVISOR_A` | `MOBILE` | `20001744` | `1` | `NULL` | **8,967** | **20%** | **李海燕** | **1.20** | **7,487** | 7,113 | **40%** |
| **6** | `haian-wanda` | `hawanda` | 海安万达 | TZ南通市海安万达广场vivo体验店 | 南通市 | **`1`** | `ADVISOR_A` | `MOBILE` | `20389153` | `1` | `NULL` | **13,753** | **20%** | **徐琳琳** | **1.20** | **7,167** | 6,809 | **40%** |
| **2** | `qidong-renmin` | `qidong` | 启东人民 | TZ南通市启东汇龙镇人民中路专卖店 | 南通市 | **`1`** | `ADVISOR_B_NEAR` | `MOBILE` | `20006824` | `1` | `NULL` | **4,649** | **20%** | **赵凯博** | **1.00** | **5,745** | 5,458 | **70%** |
| **3** | `tongzhou-jinsha` | `jinsha` | 通州金沙 | TZ南通市通州区金沙专卖店 | 南通市 | **`1`** | `ADVISOR_B_NEAR` | `MOBILE` | `20009630` | `1` | `NULL` | **5,222** | **20%** | **张庆庆** | **1.00** | **5,483** | 5,209 | **70%** |
| **9** | `gangzha-wanda` | `gzwanda` | 港闸万达 | TZ南通港闸区万达vivo体验店 | 南通市 | **`1`** | `NO_ADVISOR_MAIN` | `MOBILE` | `20122957` | `1` | `NULL` | **4,230** | **20%** | `''` *(任阳9月止)* | — | — | — | — |
| **4** | `tongzhou-wanda` | `tzwanda` | 通州万达 | TZ南通市通州区万达vivo体验店 | 南通市 | **`1`** | `NO_ADVISOR_MAIN` | `MOBILE` | `20694946` | `1` | `NULL` | **5,917** | **20%** | `''` *(蔡睿9月止)* | — | — | — | — |
| **1** | `haimen-jinhua` | `jinhua` | 海门金花 | TZ南通市海门金花vivo体验店 | 南通市 | **`1`** | `NO_ADVISOR_MAIN` | `MOBILE` | `20284034` | `1` | `NULL` | **3,116** | **20%** | `''` *(陈丽兰9月止)* | — | — | — | — |
| **5** | `haian-renmin` | `haian` | 海安人民 | TZ南通市海安人民中路vivo专卖店 | 南通市 | **`1`** | `NO_ADVISOR_MAIN` | `MOBILE` | `20003350` | `1` | `NULL` | **4,718** | **20%** | `''` *(朱冬芹9月止)* | — | — | — | — |
| **10** | `nt-kaifaqu` | `kaifaqu` | 开发区上海路 | TZ南通开发区vivo体验店 | 南通市 | **`1`** | `NO_ADVISOR_MAIN` | `MOBILE` | `20832069` | `1` | `NULL` | **1,000** | **20%** | `''` *(陈玉巧8月止)* | — | — | — | — |
| **24** | `qidong-wuyue` | `qdwy` | 启东吾悦 | TZ南通启东吾悦vivo体验店 | 南通市 | **`1`** | `NO_ADVISOR_MAIN` | `MOBILE` | `（独立机构号）` | `1` | `NULL` | **1,000** | **20%** | `''` *(有独立机构号·独立主力店)* | — | — | — | — |
| **13** | `yuanrong` | `yuanrong` | 南通圆融 | TZ南通圆融vivo体验店 | 南通市 | **`1`** | `TELECOM_SINGLE` | `TELECOM` | `''`*(留空防误入)* | **`0`** | `NULL` | **1,000** | **12%** | `''` *(电信调低·后台可调)* | — | — | — | — |
| **11** | `chongchuan-changqiao` | `changq` | 崇川长桥 | TZ南通市崇川区长桥vivo专卖店 | 南通市 | **`1`** | `AFFILIATED` | `MOBILE` | `''`*(停用散办号)* | **`0`** | `9` (`gangzha-wanda`) | *随母店* | *随母店* | `''` | — | — | — | — |
| **12** | `kfq-yongwang` | `yongwang` | 开发区永旺 | TZ南通开发区永旺vivo体验店 | 南通市 | **`1`** | `AFFILIATED` | `MOBILE` | `''` | `0` | `10` (`nt-kaifaqu`) | *随母店* | *随母店* | `''` | — | — | — | — |
| **8** | `nt-dayoujing` | `dayou` | 崇川大有镜 | TZ南通大有镜vivo体验店 | 南通市 | **`0`** | `FRANCHISE_DISABLED` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **14** | `jingjiang-wuyue` | `jjwy` | 靖江吾悦 | TZ泰州靖江吾悦vivo体验店 | 泰州市 | **`1`** | `TAIZHOU_ADVANCE_ONLY` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **15** | `jingjiang-yinxiang` | `jjyx` | 靖江印象城 | TZ泰州靖江印象城vivo体验店 | 泰州市 | **`1`** | `TAIZHOU_ADVANCE_ONLY` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **16** | `taixing-gulou` | `txgl` | 泰兴鼓楼 | TZ泰兴市鼓楼广场体验店 | 泰州市 | **`1`** | `TAIZHOU_ADVANCE_ONLY` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **17** | `jiangyan-luotang` | `jylt` | 姜堰罗塘 | TZ泰州市姜堰区罗塘vivo体验店 | 泰州市 | **`1`** | `TAIZHOU_ADVANCE_ONLY` | `MOBILE` | **`21114643`** | **`1`** | `NULL` | — | — | `''` | — | — | — | — |
| **18** | `hailing-beilu` | `hlbl` | 海陵北路 | TZ泰州海陵区海陵北路体验2店 | 泰州市 | **`1`** | `TAIZHOU_ADVANCE_ONLY` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **19** | `taixing-wuyue` | `txwy` | 泰兴吾悦 | TZ泰兴吾悦vivo体验店 | 泰州市 | **`1`** | `TAIZHOU_ADVANCE_ONLY` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **20** | `hailing-wanda` | `hlwd` | 海陵万达 | TZ泰州市海陵万达vivo体验店 | 泰州市 | **`1`** | `TAIZHOU_ADVANCE_ONLY` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **21** | `taizhou-wanxiang` | `tzwx` | 泰州万象 | TZ泰州万象vivo体验店 | 泰州市 | **`1`** | `TAIZHOU_ADVANCE_ONLY` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **22** | `xinghua-wuyue` | `xhwy` | 兴化吾悦 | TZ泰州兴化吾悦vivo体验店 | 泰州市 | **`1`** | `TAIZHOU_ADVANCE_ONLY` | `MOBILE` | **`21005538`** | **`1`** | `NULL` | — | — | `''` | — | — | — | — |
| **23** | `jiangyan-sanshui` | `jyss` | 姜堰三水 | TZ泰州姜堰三水广场vivo体验店 | 泰州市 | **`1`** | `TAIZHOU_ADVANCE_ONLY` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **25** | `haian-xirun` | `haxrc` | 海安喜润城 | TZ南通海安喜润城vivo体验店 | 南通市 | **`0`** | `FRANCHISE_DISABLED` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **26** | `rugao-wanda` | `rgwd` | 如皋万达 | TZ南通如皋万达vivo体验店 | 南通市 | **`0`** | `FRANCHISE_DISABLED` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **27** | `rugao-wuyue` | `rgwy` | 如皋吾悦 | TZ南通如皋吾悦vivo体验店 | 南通市 | **`0`** | `FRANCHISE_DISABLED` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **28** | `chongchuan-yinhe` | `yinhe` | 崇川银河 | TZ南通市崇川区银河大厦专卖店 | 南通市 | **`0`** | `FRANCHISE_DISABLED` | `MOBILE` | `''` | `0` | `NULL` | — | — | `''` | — | — | — | — |
| **29–34** | `store-alpha` .. `store-zeta` | `alpha`..`zeta` | 示例甲~巳店 | 示例店（共 6 家） | 示例/邻市 | **`0`** | `SAMPLE_DISABLED` | `MOBILE` | `''`*(清空假号)* | `0` | `NULL` | — | — | `''` | — | — | — | — |
> **“大有镜”两店口径修订（2026-10 二次核实，替换原“1 家档案”结论）**：`nt-dayoujing` 的 `short_name` 即**崇川大有镜**，系**加盟店**，业务非主控 → 本表该行由 `AFFILIATED` 改为 `FRANCHISE_DISABLED`、`active=0`、`affiliated_to=NULL`，并列入 13 家非主控名单，不进任何提成与考核计算；**中南大有镜**（`TZ南通中南大有镜vivo体验店`）系**自营门店**，1–9 月运营商六项全为 `0`、无独立渠道号，按第四类低量无号挂靠 `nt-kaifaqu`，因当期无业务发生暂不单建 `stores` 档案（日后一旦产生业务，按现有命名规范新建档案并置 `v5_mode='AFFILIATED'`、`affiliated_to=10`）。
>

---

## 二、运行期架构与测试环境彻底修复（必改项 A1–A4、C10）

### 1. A1：退单与冲销支持 —— `advance_posts` 真实有效索引
* 现库冲销行（`sesame=-958, ext_id=..._R`）与真实业务中同一 IMEI 退单重售必会触发无条件唯一约束。
* **正确索引（删去死条件 `source!='reversal'`，仅以金额大于 0 过滤正向有效行）**：
  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS idx_advance_posts_positive_imei
  ON advance_posts(advance_type, imei)
  WHERE imei != '' AND advance_amount > 0;
  ```
* **正确回填 SQL（安全转换为整数分，避免 REAL 列浮点毛刺）**：
  ```sql
  UPDATE advance_posts
  SET advance_amount = CAST(ROUND(COALESCE(broadband, 0) + COALESCE(rebate, 0) + COALESCE(other, 0) + COALESCE(sesame, 0)) AS INTEGER),
      batch_month = CASE WHEN batch_month = '' THEN substr(biz_date, 1, 7) ELSE batch_month END
  WHERE advance_type = 'LEGACY' AND (broadband != 0 OR rebate != 0 OR other != 0 OR sesame != 0);
  ```

### 2. A2：测试可复现与 CI 兼容 —— 固化 JSON 夹具
* `app/stores_seed_local.py` 位于 `.gitignore:14`，CI 与干净 clone 均不存在此文件；若测试直接 import 会报 `ImportError`；
* **落地解决法**：
  1. 将 34 家基线元数据固化为提交到 git 的静态夹具：`tests/fixtures/stores_34.json`；
  2. 新建 `tests/test_v5_migration.py`：测试使用 `stores_34.json` 初始化 sqlite 测试库并跑一遍迁移 17，断言迁移幂等、字段齐备、24 家 `active=1`、10 家 `active=0`；
  3. **迁移 17 内部隔离机制**：
     * DDL（建表、补列）与基础数据灌入**在任何环境均 100% 执行**；
     * 仅当 `os.environ.get("STORE_DAILY_SAMPLE_SEED") != "1"`（非示例测试环境）时，才执行对 6 家示例店的 `active=0` 停用与账号下线，确保现存使用 `alpha` 默认登录的数十个单元测试不受任何干扰！

### 3. A3：纯函数向后兼容 —— `render_broadcast()` 优雅加参
* `render_broadcast(store_name, biz_date, values, compact=...)` 在 7 个单元测试中作为**纯函数**直接调用，严禁绑定 `conn/store_id`；
* **正确改造**：保持原函数签名骨架，以可选关键字参数扩展：
  ```python
  def render_broadcast(store_name, biz_date, values, *, compact=False, sections=None):
      # 若未传入 sections，默认回落到全局 SECTIONS（7 个旧单测原样 100% 通过！）
      active_sections = sections if sections is not None else SECTIONS
      ...
  ```
* **调用点改造**：在知道门店身份的三处调用点传入绑定分组：
  * `app/views_daily.py:114/140`：`broadcast.render_broadcast(store["name"], biz_date, values, sections=sections_for_store(store, as_of))`
  * `app/views_daily.py:148` 与 `app/views_report.py:152`：`for section in sections_for_store(store, as_of):`

### 4. A4：消除顾问双源 —— 设置页顾问字段设为只读
* `stores.advisor_name / has_advisor` 明确定义为当前在任顾问的**只读快照缓存**；
* 在 `templates/settings/stores.html` 中，将“专职顾问”输入框改为**只读展示标签**（“当前顾问：李海燕 ｜ [去顾问任期表调整]”）；
* 在 `app/db_stores.py:update_store_profile()` 中，**禁止从普通设置表单 POST 覆盖 `advisor_name` 与 `has_advisor`**；所有顾问任职变更统一在 `advisor_tenure` 维护，保存后自动派生同步回 `stores`。

### 5. C10：启动自检降级为软告警，杜绝打死新店录入
* 在 `init_db()` 中，**严禁写死 `assert store_count == 24`**（否则测试库 6 家店炸、新库 0 家店炸、后台录入第 25 家新店后重启炸）；
* **正确自检**：
  ```python
  # 软检查：仅在库内已有实际规模（例如 active >= 15）时记录 warning，绝不抛出异常阻断启动
  active_stores = conn.execute("SELECT v5_mode, COUNT(*) FROM stores WHERE active = 1 GROUP BY v5_mode").fetchall()
  # 仅在后台系统设置页以一条顶部信息条提示模式分布情况（如：当前南通移动 13 / 电信 1 / 泰州 10 / 未配置 0）
  ```

---

## 三、算账核心与风控闸门精确定论（B5–B7、C8–C9、D11–D14）

### 1. B5：顺差让利单店独立核算分档与母子店收益定论（小店拿提成 · 大店得酬金）

* **正式定论原则**：彻底摒弃“工号达量全跟跳”或“跨店按比例分摊”，**严格执行“单店算自己的”独立核算机制**！无论母店工号全渠道累计做了多少笔，每家门店（母店、独立主力店、挂靠小店）**均严格按照本店当月实际申报/核定的顺差让利笔数（`v5_m_rebate`）独立划分档位**。
* **分档与单价规则**：
  * **当月 1–10 笔（基础档）**：主力店/独立店按 **10 元/笔** 计提；专职顾问店/挂靠小店合办按 **5 元/笔** 计提；
  * **当月第 11 笔起（达量档，超出 10 笔部分）**：主力店/独立店按 **20 元/笔** 计提；专职顾问店/挂靠小店合办按 **10 元/笔** 计提。
* **小店自己不做满 10 笔就没有多提成**：
  * 挂靠小店（崇川长桥、开发区永旺、中南大有镜）虽然在同区主力店工号下集中落单，但**提成分档严格只认小店本店当月经办笔数**。
  * **实测举例**：某月挂靠小店经办 4 笔顺差让利，母店工号全渠道累计达 15 笔。母店自办部分享受超 10 笔达量加价；而挂靠小店当月仅完成 4 笔，未突破 10 笔门槛，**4 笔全部严格按 5 元/笔核定（小店总包共 20 元），绝不跟随母店工号跳点到 10 元**！小店只有自己当月累计做到第 11 笔起，超出部分才能享受 10 元/笔的达量合办价。
* **母子店收益分配终审定论（挂靠的小店拿提成，大店得酬金没有额外提成）**：
  * **挂靠小店拿提成**：按 50% 合办价（5元/10元）核定本店提成总包，由小店店长统筹分配；
  * **母店得酬金，无额外单笔提成**：挂靠小店落单产生的全部运营商毛酬金与净酬金**直接计入母店月度结算大盘**，直接增厚母店净酬金达成率（考核指标 1 得分，满分 60 分）、提升跨越 70% 保底线概率、并降低母店 3A 扣款率。母店代办人**不再单独计提 50% 额外提成**（提成真正实现“50% 合办价”打折留存，公司与母店净赚！彻底移除 `PARENT_ASSIST_50` 枚举）。
* **系统计算公式（写入表 4 自动核算逻辑）**：
  设门店当月核定顺差让利笔数为 $N$：
  * **无顾问独立主力店（100% 提成）**：
    $$\text{rebate\_comm} = \min(N, 10) \times 10 + \max(0, N - 10) \times 20$$
  * **有顾问店 / 挂靠小店（50% 合办提成）**：
    $$\text{rebate\_comm} = \min(N, 10) \times 5 + \max(0, N - 10) \times 10$$

---

### 2. B6：移动优选会员定位为 0 元义务项与事后抽查留痕（不发钱 · 不作系统自动核减互锁）

* **正式定论原则**：**移动优选会员一笔购机让利就一笔优选会员，这个不发钱（个人/门店提成一律为 0 元）！** 它是运营商要求办理顺差让利购机直降时的**强制搭售/同开合规条件**。导购和门店不从中抽取会员单笔提成，无需也不应将其作为独立提成项目分配。
* **务实系统落地边界（务实不承诺系统做不到的事）**：
  1. **数据源客观限制**：运营商月度结算单仅按渠道工号汇总出账，无法逐笔将客户手机号/让利流水与优选会员开通记录做系统级字段配对；且真实月结单中会员类月均超 150 笔，远大于让利 10 笔，工号级总量比对恒真无拦截意义；
  2. **系统定位**：表 4 记录结算单优选会员量（`settled_cat4_rebate_member`）**仅作为合规留痕展示与事后审计对比，不作为月末系统自动核减顺差让利笔数的刚性互锁闸门**；
  3. **差错追缴通道**：若运营商事后专项稽核通报某笔让利未开会员属于违规件扣减款项，走线下店员差错追责或通过 `advance_posts` 垫资批次追回，不阻塞系统月末自动核算锁月。

---

### 3. 第一道闸（结算单交叉比对）：挂靠工号合并比对与总量封顶机制（解决挂靠穿透漏洞）

* **核心规则**：挂靠小店没有独立的移动结算单，运营商结算单只挂在母店工号所在行（`source_store_id` 为母店）。
* **比对与核减算法**：
  1. **工号级申报总量汇总**：
     $$\text{工号申报总量} = \text{母店日报申报量} + \sum \text{该工号下全部挂靠小店申报量}$$
  2. **比对母店结算单**：将“工号申报总量”与母店结算单对应大类实结量比对：
     - 若 $\text{工号申报总量} \le \text{结算单实结量} \times 1.2$，母店及挂靠子店全量通过比对；
     - 若工号申报总量超出结算单实结量（偏差 > 20%）：
       **母店与各挂靠子店按各自申报量比例同比例核减**！
  3. **挂靠子店状态联动**：挂靠子店在计算时联动读取母店工号行的比对状态与核减比例，子店不存伪结算单数据，彻底杜绝“子店分别比导致 3 倍漏洞”或“子店留 0 导致全部挂起”的问题！

---

### 4. 刚性计算先后顺序：核减 → 分档 → 追回（绝不双重惩罚）

当某店顺差让利涉及结算单核减与批次追回时，系统严格按以下三步串行执行：
1. **第一步（笔数核减）**：先按第一道风控闸门比对，若申报超标则同比例核减，得出该店当月最终有效让利笔数 $N$；
2. **第二步（重新分档）**：以核减后的有效笔数 $N$ 重新划分档位（前 10 笔按基础价，第 11 笔起按达量价），算出基础提成金额；
3. **第三步（垫资追回）**：若同一业务月退单导致有效笔数变动，取“核减差额”与“批次追回”两者**较低值**作为当期 `rebate_clawback_cents`，同一笔业务同月绝不重复追扣！

---

### 5. B7：顺差让利追回款单列，绝不污染指标 3A 扣罚率

* **严禁将让利追回款塞入 `total_deduction_cents`**！
  * `total_deduction_cents` 是运营商月结单上的违规罚款，直接用于计算店长 100 分考核中的 **指标 3A 扣款率**（$\text{扣款率} = \text{total\_deduction} / \text{gross\_comm}$）；
  * 顺差让利追回是**公司内部对垫资未回款导购提成的冲销**，属于内部风控，一旦放进 ③ 会导致扣款率虚高，让店长无辜扣除 2–10 分！
* **独立列示与结算链路**：
  1. 在表 4 单设 `rebate_clawback_cents INTEGER NOT NULL DEFAULT 0`；
  2. 仅在计算最终提成总包时扣减：
     $$\text{shop\_final\_pool\_cents} = \max\left(0, \text{shop\_raw\_pool\_cents} \times \text{shop\_payout\_ratio} - \text{rebate\_clawback\_cents}\right)$$
  3. **上线前 3 个月（2026-10 ～ 2026-12）历史豁免**：由于 2026 年 7–9 月顺差让利 100 元垫资未在 `advance_posts` 逐笔记账，新系统对 2026-10 至 2026-12 期间结算自动将 `rebate_recovery_exempt = 1`，不触发历史陈欠追回；自 2026-10 起新产生的批次正常滚动计算。
  4. **唯一事实源**：以 `advance_posts`（实际让利出资与回款流水）为唯一底层事实源，表 4 仅存聚合结果。

---

### 4. C9：移动通报表 AI 手机列平滑接轨

* `app/views_admin.py:95-96` 与 `:135` 从 `pairs.get("ai_contract")` 取数发给移动地市通报表；
* **平滑兼容逻辑**：
  ```python
  # 移动通报表取数：若为 V5 门店，AI 手机数平滑读取 v5_m5 + v5_m6（同开换新 + 未同开换新）
  if store.get("v5_mode") in ("ADVISOR_A", "ADVISOR_B_NEAR", "NO_ADVISOR_MAIN", "AFFILIATED"):
      day_ai = pairs.get("v5_m5", (0, 0))[0] + pairs.get("v5_m6", (0, 0))[0]
      month_ai = pairs.get("v5_m5", (0, 0))[1] + pairs.get("v5_m6", (0, 0))[1]
  else:
      day_ai, month_ai = pairs.get("ai_contract", (0, 0))
  ```
* 既不需要前台店员重复填报旧科目，又保证发给运营商的通报表数据准确完整、不出现 0 值断档。

---

### 5. D11–D14：数据模型与字段精度最终确认

* **D11. 顾问工资单（Sheet 3）按任职段输出**：支持同一个月同一门店发生顾问交接（如 8 月南通开发区陈玉巧离任）时，按 `advisor_tenure` 行分别折算工作日天数、基础底薪与平衡点；
* **D12. 笔数调整审计追踪**：`store_month_biz` 增加 `updated_at TEXT NOT NULL DEFAULT ''` 与 `updated_by INTEGER REFERENCES users(id)`；
* **D13. 二次分配唯一性与重名防御**：`store_month_split` 采用自增主键 `id INTEGER PRIMARY KEY AUTOINCREMENT`，不设纯字符串数据库唯一约束，防止两名同名导购（如两个“王芳”且工号为空）撞车崩溃；在业务层强制要求补填员工工号/系统账号，同店且工号相同时才拦截重复录入；
* **D14. 浮点向分转换**：`advance_posts` 回填统一用 `CAST(ROUND(...) AS INTEGER)`。

---

## 四、核心表结构完整 DDL（迁移 17 执行标准）

```sql
-- 表 1：门店考核基线与搭载率配置表
CREATE TABLE IF NOT EXISTS store_baseline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    from_month TEXT NOT NULL,                      -- 生效起始月，如 '2026-10'
    to_month TEXT NOT NULL DEFAULT '',             -- 生效截止月，'' 表示长期有效
    v5_mode TEXT NOT NULL,                         -- 当期模式快照
    affiliated_to INTEGER REFERENCES stores(id),   -- 挂靠母店 ID
    net_baseline_cents INTEGER NOT NULL DEFAULT 0, -- 历史月均净酬金基线（分）
    target_carry_rate REAL NOT NULL DEFAULT 0.20,  -- 目标满分搭载率 (移动0.20 / 电信0.12)
    carry_tier_25 REAL NOT NULL DEFAULT 0.20,      -- 25分满分阈值 (移动0.20 / 电信0.12)
    carry_tier_18 REAL NOT NULL DEFAULT 0.15,      -- 18分及格阈值 (移动0.15 / 电信0.09)
    carry_tier_10 REAL NOT NULL DEFAULT 0.10,      -- 10分底线阈值 (移动0.10 / 电信0.06)
    cap_ratio REAL NOT NULL DEFAULT 0.35,          -- 单笔提成总包占净酬金封顶比
    cap_abs_cents INTEGER NOT NULL DEFAULT 0,      -- 绝对封顶金额（分，有顾问店为 65000）
    created_at TEXT NOT NULL,
    UNIQUE(store_id, from_month)
);

-- 表 2：专职顾问任期与成本平衡点表（支持换人分段与折算，解决 D11）
CREATE TABLE IF NOT EXISTS advisor_tenure (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    advisor_name TEXT NOT NULL,
    from_month TEXT NOT NULL,                      -- 任职起始月，如 '2026-10'
    to_month TEXT NOT NULL DEFAULT '',             -- 任职截止月，'' 表示当前在任
    tier_grade TEXT NOT NULL DEFAULT 'A',          -- 'A' / 'B_NEAR' / 'LEGACY'
    base_pay_cents INTEGER NOT NULL DEFAULT 400000, -- 基础底薪基数（分）
    base_coef REAL NOT NULL DEFAULT 1.20,          -- 基础系数（A档1.20 / B近1.00）
    breakeven_cents INTEGER NOT NULL DEFAULT 700000, -- 满月全口径人力成本平衡点（分）
    excess_rate REAL NOT NULL DEFAULT 0.40,        -- 净超额分成比例（A档0.40 / B近0.70）
    work_days INTEGER NOT NULL DEFAULT 0,          -- 0表示整月；>0表示实际天数（按比例折算）
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(store_id, advisor_name, from_month)
);

-- 表 3：门店月度单笔提成档位核算表（补齐审计列，解决 D12）
CREATE TABLE IF NOT EXISTS store_month_biz (
    month TEXT NOT NULL,                           -- '2026-10'
    store_id INTEGER NOT NULL REFERENCES stores(id),        -- 领钱门店 ID
    source_store_id INTEGER NOT NULL REFERENCES stores(id), -- 发生业务门店 ID
    role_share TEXT NOT NULL DEFAULT 'SHOP_FULL_100',   -- 'SHOP_FULL_100' (无顾问主力店100%) / 'ADVISOR_SHOP_50' (有顾问店导购50%) / 'AFFILIATED_SHOP_50' (挂靠小店50%)
    biz_code TEXT NOT NULL,                        -- 10个移动码 或 8个电信码
    daily_sum_count INTEGER NOT NULL DEFAULT 0,    -- 日报自动累计笔数
    settled_count INTEGER NOT NULL DEFAULT 0,      -- 核定有效笔数
    unit_price_cents INTEGER NOT NULL DEFAULT 0,   -- 适用单价（分）
    raw_amount_cents INTEGER NOT NULL DEFAULT 0,   -- 折前金额（分）
    updated_at TEXT NOT NULL DEFAULT '',           -- 变更时间戳
    updated_by INTEGER REFERENCES users(id),       -- 变更人
    PRIMARY KEY (month, store_id, source_store_id, role_share, biz_code)
);

-- 表 4：门店月度结算、店长100分考核、专职顾问薪酬总表（追回款独立，解决 B6/B7）
CREATE TABLE IF NOT EXISTS store_month_settlement (
    month TEXT NOT NULL,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    -- 运营商结算单输入项：
    gross_commission_cents INTEGER NOT NULL DEFAULT 0,     -- ① 正向毛酬金（含调测费）
    commissioning_fee_cents INTEGER NOT NULL DEFAULT 0,    -- ② 调测费垫资
    total_deduction_cents INTEGER NOT NULL DEFAULT 0,      -- ③ 运营商负向扣款合计（仅结算单惩罚！）
    -- 3B 四项违规独立拆列：
    deduct_3b_quality_cents INTEGER NOT NULL DEFAULT 0,    -- ④-1 品管服务支撑扣款
    deduct_3b_dispute_cents INTEGER NOT NULL DEFAULT 0,    -- ④-2 订购争议扣款
    deduct_3b_workorder_cents INTEGER NOT NULL DEFAULT 0,  -- ④-3 不规范工单扣款
    deduct_3b_luckynum_cents INTEGER NOT NULL DEFAULT 0,   -- ④-4 吉祥号码违规扣款
    broadband_star_penalty INTEGER NOT NULL DEFAULT 0,     -- ⑤ 宽带扣0.1星级系数 (0/1)
    net_commission_cents INTEGER NOT NULL DEFAULT 0,       -- ⑥ 实发净酬金 = ① - ② - ③
    -- 结算单大类结酬量与会员核验项：
    settled_cat1_fusion INTEGER NOT NULL DEFAULT 0,        -- 新入网主卡结酬量
    settled_cat2_contract INTEGER NOT NULL DEFAULT 0,      -- 终端信用购/金币合约量
    settled_cat3_ai INTEGER NOT NULL DEFAULT 0,            -- AI手机服务费台数
    settled_cat4_rebate INTEGER NOT NULL DEFAULT 0,        -- 顺差让利合约量
    settled_cat4_rebate_member INTEGER NOT NULL DEFAULT 0, -- 优选会员结酬量（0元义务项，用于1:1强绑定合规校验）
    core_biz_count INTEGER NOT NULL DEFAULT 0,             -- ⑦ 核定核心主业总笔数
    phone_sales_snap INTEGER NOT NULL DEFAULT 0,           -- ⑧ 当月日报累计手机销量 (分母)
    actual_carry_rate REAL NOT NULL DEFAULT 0.0,           -- ⑨ 实际核心搭载率 = core_biz_count / phone_sales_snap
    -- 顺差让利 100 元垫资与滚动回款跟踪（不进 ③、不进指标 3A）：
    rebate_advance_cents INTEGER NOT NULL DEFAULT 0,       -- 让利垫资总额 (分)
    rebate_recovered_cents INTEGER NOT NULL DEFAULT 0,     -- 实际回款金额 (分)
    rebate_recovery_ratio_3m REAL NOT NULL DEFAULT 1.0,    -- 滚动 3 个月回款率
    rebate_recovery_exempt INTEGER NOT NULL DEFAULT 0,     -- 10-12月上线前3月历史豁免 (0/1)
    rebate_clawback_cents INTEGER NOT NULL DEFAULT 0,      -- 内部追回扣款（仅冲减提成总包！）
    -- 风控状态：
    exempt_cross_month INTEGER NOT NULL DEFAULT 0,         -- 跨期异常追扣豁免 (0/1)
    audit_flag TEXT NOT NULL DEFAULT 'OK',                 -- 'OK' / 'COUNT_MISMATCH' / 'CAP_EXCEEDED'
    override_remark TEXT NOT NULL DEFAULT '',
    -- 快照字段：
    v5_mode_snap TEXT NOT NULL DEFAULT '',
    net_baseline_snap_cents INTEGER NOT NULL DEFAULT 0,
    target_carry_rate_snap REAL NOT NULL DEFAULT 0.20,
    advisor_name_snap TEXT NOT NULL DEFAULT '',
    base_coef_snap REAL NOT NULL DEFAULT 0,
    breakeven_snap_cents INTEGER NOT NULL DEFAULT 0,
    excess_rate_snap REAL NOT NULL DEFAULT 0,
    -- 计算输出字段：
    shop_raw_pool_cents INTEGER NOT NULL DEFAULT 0,        -- 折前单笔提成总包
    shop_payout_ratio REAL NOT NULL DEFAULT 1.0,           -- 保底发放系数 (1.0 / 0.8 / 0.0)
    shop_final_pool_cents INTEGER NOT NULL DEFAULT 0,      -- 实发提成总包（扣除 rebate_clawback）
    co_pool_deducted_cents INTEGER NOT NULL DEFAULT 0,     -- 顾问店扣除的50%合办提成包
    advisor_retained_net_cents INTEGER NOT NULL DEFAULT 0, -- 顾问考核留存净酬金
    score_1_net REAL NOT NULL DEFAULT 0,                   -- 指标1得分 (60分)
    score_2_biz REAL NOT NULL DEFAULT 0,                   -- 指标2得分 (25分 · 搭载率方案A)
    score_3a_deduct REAL NOT NULL DEFAULT 0,               -- 指标3A得分 (10分，仅受 ③ 影响)
    score_3b_redline REAL NOT NULL DEFAULT 0,              -- 指标3B得分 (5分)
    total_score_100 REAL NOT NULL DEFAULT 0,               -- 100分系统总分
    mgr_score_20 REAL NOT NULL DEFAULT 0,                  -- 店长20分版折算
    mgr_score_15 REAL NOT NULL DEFAULT 0,                  -- 店长15分版折算
    advisor_sys_coef REAL NOT NULL DEFAULT 0,              -- 顾问考核系数
    advisor_base_pay_cents INTEGER NOT NULL DEFAULT 0,     -- 顾问考核底薪
    advisor_excess_bonus_cents INTEGER NOT NULL DEFAULT 0, -- 顾问净超额分成
    advisor_3b_penalty_cents INTEGER NOT NULL DEFAULT 0,   -- 顾问3B违规连带扣款
    advisor_final_pay_cents INTEGER NOT NULL DEFAULT 0,    -- 顾问应发工资合计
    status TEXT NOT NULL DEFAULT 'DRAFT',                  -- 'DRAFT' / 'CONFIRMED' / 'LOCKED'
    locked_by INTEGER REFERENCES users(id),
    locked_at TEXT NOT NULL DEFAULT '',
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (month, store_id)
);

-- 表 5：店长二次分配备案留痕表（自增主键，消除脆弱唯一约束）
CREATE TABLE IF NOT EXISTS store_month_split (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    emp_code TEXT NOT NULL DEFAULT '',             -- 员工工号/ERP编码
    emp_name TEXT NOT NULL,                        -- 员工姓名
    role_tag TEXT NOT NULL DEFAULT '导购',         -- '店长' / '导购' / '协办落单'
    amount_cents INTEGER NOT NULL DEFAULT 0,       -- 分配金额（分），SUM 必须等于 shop_final_pool_cents
    remark TEXT NOT NULL DEFAULT '',
    confirmed_by INTEGER REFERENCES users(id),
    confirmed_at TEXT NOT NULL DEFAULT ''
);

-- 表 6：月结锁月与配置审计日志表
CREATE TABLE IF NOT EXISTS settlement_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    action TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '',
    after_json TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);
```
