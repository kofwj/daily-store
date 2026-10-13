"""触客通报（手机销量 vs 触客）：口径、权限、改格、导出（`/compare`）。

口径（与 app/views_compare.py 顶部说明一致）：
- 触客条数 = 当天 deal_posts 条数（不分成交）；手机销量 = 当天日报 phone_sales
- 触客/销量 = 触客 ÷ 手机销量（两位小数百分比）：理论要 >100.00%，低于 100.00% 都不好；
  销量 0 写「-」，没交日报写「未交日报」徽标（不参与排名）
- 合计只算已交日报的店 / 天；改手机销量只给 admin / city
- 业辅查询登记：每店每天一个整数，跟手机销量一样就地填 / 就地改（只给 admin / city）
- 门店顺序固定（启东 → 海门 → 开发区 → 港闸 → 通州 → 如皋 → 海安），没有排序开关
"""
import io
import re
from datetime import date, timedelta

import pytest
from conftest import store_id
from openpyxl import load_workbook

from app import db
from app.views_compare import _AREA_ORDER, _area_rank, _diff_text, _ratio_text, day_copy_text


def _uid(conn) -> int:
    return int(conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"])


def seed_report(code: str, day: date, phone: int) -> None:
    """造一条「已交日报」：daily_reports 一行 + daily_facts 里的 phone_sales。"""
    sid = store_id(code)
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO daily_reports(biz_date, store_id, submitted_by, submitted_at, compact, note)
            VALUES (?, ?, ?, ?, 0, '')
            ON CONFLICT(biz_date, store_id) DO NOTHING
            """,
            (day.isoformat(), sid, _uid(conn), f"{day.isoformat()} 09:00:00"),
        )
        conn.execute(
            """
            INSERT INTO daily_facts(biz_date, store_id, metric_code, day_value)
            VALUES (?, ?, 'phone_sales', ?)
            ON CONFLICT(biz_date, store_id, metric_code) DO UPDATE SET day_value=excluded.day_value
            """,
            (day.isoformat(), sid, phone),
        )


def seed_touch(code: str, day: date, n: int) -> None:
    """造 n 条触客记录（号码各不相同，免得被同号覆盖）。"""
    sid = store_id(code)
    with db.get_db() as conn:
        uid = _uid(conn)
        for i in range(n):
            db.record_deal_post(
                conn,
                store_id=sid,
                user_id=uid,
                closed=False,
                model="测试机",
                phone=f"139{abs(hash((code, day.isoformat(), i))) % 100000000:08d}",
                biz_date=day,
            )


def work_day() -> date:
    """管理员随便哪天都行；city 用例要用本月内的日子，所以取本月内的最近一天。"""
    today = db.today_local()
    if today.day > 1:
        return today - timedelta(days=1)
    return today

# ---------------------------------------------------------------- HTML 辅助


# 报表现已写门店全称：用例正文照旧用简称点店，辅助函数里翻一道。
SHORT_TO_FULL = {
    "示例甲店": "示例市甲街vivo体验店",
    "示例乙店": "示例市乙街vivo专卖店",
    "示例丙店": "示例市丙路vivo体验店",
    "示例丁店": "示例市丁路vivo体验店",
    "示例戊店": "邻市戊路vivo体验店",
    "示例巳店": "邻市巳街vivo专卖店",
}
FULL_TO_SHORT = {v: k for k, v in SHORT_TO_FULL.items()}


def _full(name: str) -> str:
    """简称 → 全称（本来就传全称就原样返回）。"""
    return SHORT_TO_FULL.get(name, name)


def row_of(body: str, name: str) -> str:
    """取表里某家店的那一行（<strong> 里的店名唯一；表里写的是全称）。"""
    m = re.search(r"<strong>" + re.escape(_full(name)) + r"</strong>.*?</tr>", body, re.S)
    assert m is not None, f"表里没有 {name} 这一行"
    return m.group(0)


def store_order(body: str, names) -> list[str]:
    """表里这几家店的出现顺序（只认 <strong> 里的店名，通报文案里的不算）。

    表里是全称，出去换成简称，用例正文照旧读得懂。
    """
    wanted = {_full(n) for n in names}
    found = re.findall(r"<strong>(.*?)</strong>", body)
    return [FULL_TO_SHORT.get(n, n) for n in found if n in wanted]

def shot_of(body: str) -> str:
    """截图容器（#compareShot）里的那段 HTML：贴群的图就是这一段。"""
    start = body.index('<div id="compareShot"')
    end = body.index('<div class="card" id="shotPreviewCard"')
    return body[start:end]


def foot_of(body: str, label: str) -> str:
    """tfoot 里含 label 的那一行（城市小计或合计），不跨行。"""
    m = re.search(
        r'<tr class="city-sub">(?:(?!</tr>).)*?' + re.escape(label) + r".*?</tr>", body, re.S
    )
    assert m is not None, f"tfoot 里没有含 {label} 的行"
    return m.group(0)


TOTAL_HEAD = '<td class="store" style="border-top: 2px solid #94a3b8">合计</td>'  # 合计行第一格

# ---------------------------------------------------------------- 文案（纯函数）


def test_diff_and_ratio_text():
    assert _diff_text(None) == "—"
    assert _diff_text(0) == "0"
    assert _diff_text(4) == "+4"
    assert _diff_text(-4) == "-4"
    # 触客 ÷ 手机销量，两位小数；理论要 >100.00%
    assert _ratio_text(23, 13) == "176.92%"
    assert _ratio_text(8, 8) == "100.00%"
    assert _ratio_text(4, 10) == "40.00%"
    assert _ratio_text(0, 5) == "0.00%"  # 触客一条没有也照算，不当成「没得比」
    assert _ratio_text(5, 0) == "-"  # 销量 0 台：当天才会出现，不进排名
    assert _ratio_text(0, 0) == "-"
    assert _ratio_text(None, 5) == "—"  # 没交日报 → 缺一边
    assert _ratio_text(5, None) == "—"


# ---------------------------------------------------------------- 主表口径


@pytest.fixture()
def day_scene(admin_client):
    """口径示例：乙店 180.00%、甲店 100.00%、巳店 / 戊店销量 0 写「-」、丁店未交日报、丙店静默。"""
    day = work_day()
    seed_report("store-beta", day, 5)
    seed_touch("store-beta", day, 9)
    seed_report("store-alpha", day, 8)
    seed_touch("store-alpha", day, 8)
    seed_report("store-zeta", day, 0)
    seed_touch("store-zeta", day, 2)
    seed_report("store-epsilon", day, 0)
    seed_touch("store-epsilon", day, 4)
    seed_touch("store-delta", day, 6)  # 只有触客，没日报
    return day


def test_day_table_numbers_and_unsubmitted(admin_client, day_scene):
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert "触客通报" in body
    # 已交 4 家：触客 9+8+2+4=23，销量 5+8+0+0=13 → 23 ÷ 13 = 176.92%
    assert TOTAL_HEAD in body  # 合计行只写「合计」，不再带「（已交 N 家）」
    assert ">23<" in body and ">13<" in body and "176.92%" in body
    # 未交日报的店：留行、那格写「未交日报」徽标；名单不再单独铺一段
    assert "未交日报" in row_of(body, "示例丁店")
    assert "未交日报 1 家" not in body
    # 比例 100.00% 就是「刚好持平」；少报 / 多报 / 一致这些字样全没了
    assert "100.00%" in row_of(body, "示例甲店")
    assert "少报" not in body and "多报" not in body and "一致" not in body
    # 地市小计：邻市 2 家已交 + 1 家未交
    assert "示例市 · 2 家" in body
    assert "邻市 · 2 家（未交 1）" in body
    # 静默店（无日报无触客）不占表行，只报个数
    assert "另有 1 家当天没交日报也没触客记录" in body
    assert "示例丙店" not in body
    # 门店格下面不再挂地市名（地市由分组小计行交代）
    assert '<span class="sub">' not in row_of(body, "示例甲店")



def test_day_table_shows_full_store_name(admin_client, day_scene):
    """表里门店写全称（表 / 文案 / 图片共用一份数据）；简称只留在 hover 提示里对号。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert "<strong>示例市甲街vivo体验店</strong>" in body
    assert '<td class="store" title="示例甲店">' in body  # 简称退到 tooltip
    assert "<strong>示例甲店</strong>" not in body


def test_store_view_header_uses_full_name(admin_client, tmp_db):
    """单店逐日抬头也写全称，跟单日表一个口径。"""
    sid = store_id("store-alpha")
    month = work_day().replace(day=1).isoformat()
    body = admin_client.get(
        f"/compare?view=store&store_id={sid}&month={month}"
    ).get_data(as_text=True)
    assert "· 示例市甲街vivo体验店 ·" in body
    assert "· 示例甲店 ·" not in body


def test_unsubmitted_store_shows_touch_but_no_diff(admin_client, day_scene):
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    row = row_of(body, "示例丁店")
    assert ">6<" in row  # 触客条数照记
    assert "未交日报" in row  # 差写「未交日报」而不是 0
    assert "+6" not in row and "-6" not in row


def test_touch_zero_keeps_row_and_zero_percent(admin_client):
    """触客 0 条：店还在表里（有日报），触客 ÷ 销量 照算 0.00%（不是「没得比」）。"""
    day = work_day()
    seed_report("store-gamma", day, 3)
    body = admin_client.get(f"/compare?date={day.isoformat()}").get_data(as_text=True)
    row = row_of(body, "示例丙店")
    assert "0.00%" in row  # 0 ÷ 3
    assert "差率" not in row and "少报" not in row  # 旧口径的字样一个都不留


def test_touch_cross_day_note_visible(admin_client, day_scene):
    """触客只算当天：页面写明没有补交一说。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert "没有补交一说" in body


# ---------------------------------------------------------------- 通报文案


def test_day_copy_text_lines():
    """纯函数：一行合计 + 「不足 100.00% N 家」点名（最差在前）；旧的三段不再出现。"""
    rows = [
        {"name": "示例乙店", "ratio": 1.8, "ratio_text": "180.00%"},
        {"name": "示例甲店", "ratio": 1.0, "ratio_text": "100.00%"},
        {"name": "示例戊店", "ratio": 0.4, "ratio_text": "40.00%"},
    ]
    totals = {"n": 3, "touch": 21, "sold": 13, "ratio_text": "161.54%"}
    text = day_copy_text(
        biz_date=date(2026, 10, 11),
        rows=rows,
        totals=totals,
        silent_n=1,
        scope_label="示例市",
    )
    assert text.splitlines() == [
        "【触客通报】10月11日（示例市）",
        "合计：触客 21 条 · 手机销量 13 台 · 触客/销量 161.54%",
        "不足 100.00% 1 家：示例戊店 40.00%",
        "另有 1 家没交日报也没触客记录",
    ]


def test_day_copy_text_empty_day():
    """空日不给文案（页面上那块直接不出现）。"""
    text = day_copy_text(
        biz_date=date(2026, 10, 11),
        rows=[],
        totals={"n": 0, "touch": 0, "sold": 0, "ratio_text": "—"},
    )
    assert text == ""
    assert text == ""


def test_copy_block_matches_page_numbers(admin_client, day_scene):
    """页面上的通报文案块，数字跟表格口径一致，且可一键复制。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert 'id="copyText"' in body and "复制通报" in body
    assert "合计：触客 23 条 · 手机销量 13 台 · 触客/销量 176.92%" in body
    assert "未交日报 1 家" not in body
    # 卖过货的只有甲（100.00%）、乙（180.00%）：没到 100.00% 的段不出现
    assert "不足 100.00%" not in body


def test_copy_block_hidden_on_empty_day(admin_client):
    day = (work_day() - timedelta(days=40)).isoformat()
    body = admin_client.get(f"/compare?date={day}").get_data(as_text=True)
    assert 'id="copyText"' not in body


def test_store_order_is_fixed(admin_client, day_scene):
    """没有排序开关：门店顺序固定（区县序，没命中就按目录序），地市仍按目录序分组。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert "按差排序" not in body and "sort=" not in body
    names = ("示例甲店", "示例乙店", "示例丁店", "示例戊店", "示例巳店")
    assert store_order(body, names) == list(names)


def test_area_order_rank_is_fixed():
    """固定门店顺序：启东 → 海门 → 开发区 → 港闸 → 通州 → 如皋 → 海安，没命中的排最后。"""
    assert list(_AREA_ORDER) == ["启东", "海门", "开发区", "港闸", "通州", "如皋", "海安"]
    assert [_area_rank(f"示例市{x}路vivo体验店") for x in ("海安", "启东", "通州")] == [6, 0, 4]
    assert _area_rank("示例市甲街vivo体验店") == len(_AREA_ORDER)  # 全称里没命中区县关键词
    assert _area_rank("示例市甲街vivo体验店", "启东店") == 0  # 退到简称再找一遍


def test_empty_day(admin_client):
    """那天既没日报也没触客：表空，不报错。"""
    day = (work_day() - timedelta(days=40)).isoformat()
    resp = admin_client.get(f"/compare?date={day}")
    assert resp.status_code == 200
    assert "这一天没有店交日报，也没有触客记录" in resp.get_data(as_text=True)


# ---------------------------------------------------------------- 通报图片


def test_shot_block_on_day_view(admin_client, day_scene):
    """单日视图有截图容器 +「复制图片 / 下载图片」+ 预览位 + 两个脚本。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert '<div id="compareShot"' in body
    assert "复制图片" in body and 'id="btnCopyShot"' in body
    assert 'id="btnDownShot"' in body
    assert 'id="shotPreview"' in body and 'id="shotPreviewCard"' in body
    assert "html2canvas.min.js" in body and "shot.js" in body
    # 配置键必须写在 window.__shotSetup 对象里：写到花括号外面会让整个内联脚本语法错，
    # __shotSetup 变成 undefined，按钮点下去只会把提示写错地方（真实踩过）。
    m = re.search(r"window\.__shotSetup = \{(.*?)\n  \};", body, re.S)
    assert m is not None, "没找到 window.__shotSetup"
    setup = m.group(1)
    assert "sourceId: 'compareShot'" in setup
    assert "text: '#compareShot .cell-plain'" in setup
    # 手机屏比表窄时靠这个开关把容器撑宽再拍，不然右边的业辅列会被 html2canvas 裁掉
    assert "fitContent: true" in setup


def test_shot_content_matches_table(admin_client, day_scene):
    """截图容器里就是这张表：抬头、两行表头、未交日报的店都在图里。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    shot = shot_of(body)
    assert "触客通报" in shot
    assert f"{day_scene.month}月{day_scene.day}日" in shot
    assert TOTAL_HEAD in shot  # 图里合计行也只写「合计」
    assert "176.92%" in shot  # 合计行的触客/销量
    assert "示例市丁路vivo体验店" in shot  # 未交日报的店还在表里
    assert "未交日报 1 家" not in shot  # 名单那段不再上屏
    assert "示例乙店" in shot


def test_day_shot_table_header_groups(admin_client, day_scene):
    """通报图两行表头：本月累计 4 列 + 当日 4 列；列名统一「触客/销量」，没有「差率」小字。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    shot = shot_of(body)
    head = shot[: shot.index("</thead>")]
    assert len(re.findall(r"<th[ >]", head)) == 12  # 3 个分组格 + 9 个列名
    assert '<th colspan="4" class="g-day">当日</th>' in head
    assert 'class="g-month"' in head and "本月累计（" in head
    assert "业辅查询登记" in head
    assert head.count("触客/销量") == 2  # 月 + 当日都用这个列名
    assert "差率" not in shot and "少报" not in shot and "多报" not in shot
    assert "dev-bar" not in shot
    assert "<th>日报</th>" not in shot


def test_ratio_column_shows_percent(admin_client, day_scene):
    """「差」列改成「触客/销量」百分比：乙店 180.00%、甲店 100.00%、销量 0 写「-」。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert "180.00%" in row_of(body, "示例乙店")
    assert "100.00%" in row_of(body, "示例甲店")
    assert '<td class="num">-</td>' in row_of(body, "示例戊店")  # 销量 0 台
    row = row_of(body, "示例丁店")
    assert "未交日报" in row and "%" not in row



def test_city_subtotal_uses_same_ratio(admin_client, day_scene):
    """城市小计跟店铺行同一套算法：合计触客 ÷ 合计销量；合计销量 0 写「-」。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    # 示例市：触客 8+9=17 ÷ 销量 8+5=13 = 130.77%
    assert "130.77%" in foot_of(body, "示例市 · 2 家")
    # 邻市：触客 2+4=6 ÷ 销量 0 台 → 「-」
    assert '<td class="num">-</td>' in foot_of(body, "邻市 · 2 家（未交 1）")


def test_worst_three_get_red_flag(admin_client, tmp_db):
    """排名最差 3 家：只给当日「触客/销量」那一格加红底，小计 / 合计行不参与。"""
    day = work_day()
    for code, sold, touch in (
        ("store-alpha", 10, 2),  # 20.00% ← 最差
        ("store-beta", 10, 5),  # 50.00%
        ("store-gamma", 10, 9),  # 90.00%
        ("store-zeta", 10, 20),  # 200.00% ← 最好，不标
    ):
        seed_report(code, day, sold)
        seed_touch(code, day, touch)
    body = admin_client.get(f"/compare?date={day.isoformat()}").get_data(as_text=True)
    assert body.count("color: #b91c1c") == 3  # 全表一个池子，只标最差 3 家
    for name in ("示例甲店", "示例乙店", "示例丙店"):
        assert "color: #b91c1c" in row_of(body, name)
    assert "color: #b91c1c" not in row_of(body, "示例巳店")
    assert "color: #b91c1c" not in foot_of(body, "合计")
    assert "20.00%" in row_of(body, "示例甲店")
    assert "color: #b91c1c" in shot_of(body)  # 贴群的图里也是同一套标色


def test_row_drops_city_subtitle(admin_client, day_scene):
    """门店格下面不再挂地市名；本月累计那一列也是百分比。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert '<span class="sub">示例市</span>' not in row_of(body, "示例甲店")
    assert month_of(body, "示例甲店") == ["8", "8", "100.00%", "—"]
    assert "差率" not in body

def test_shot_has_no_permission_footnote(admin_client, day_scene):
    """贴群的图不带页面说明（谁能改、去哪儿补录）：那是给登录进来的人看的。"""
    shot = shot_of(
        admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    )
    assert "管理员" not in shot
    assert "报表" not in shot


def test_edit_hint_on_page_but_not_in_image(admin_client, day_scene):
    """「格子能改 / 未交的去哪儿补录」得写在页面上（图外），别进贴群的图。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    head = body[: body.index('<div id="compareShot"')]
    assert "点销量格子就能改" in head
    assert "报表" in head


def test_shot_has_plain_number_for_editable_cells(admin_client, day_scene):
    """可编辑的格子旁边备着纯数字：截图时换掉 <input>，图里只出数字。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    row = row_of(body, "示例乙店")
    assert '<span class="cell-plain" style="display:none">5</span>' in row
    # 未交日报的格子本来就没有输入框，也就不需要备用数字
    miss = row_of(body, "示例丁店")
    assert "cell-plain" not in miss and "cell-edit" not in miss


def test_shot_scope_label_for_single_city(city_client, tmp_db):
    """地市负责人只辖一市：图片抬头带上城市名。"""
    day = work_day()
    seed_report("store-alpha", day, 8)
    seed_touch("store-alpha", day, 8)
    body = city_client.get(f"/compare?date={day.isoformat()}").get_data(as_text=True)
    assert f"{day.month}月{day.day}日 · 示例市" in body


def test_shot_hidden_on_empty_day(admin_client, day_scene):
    """空日没有图可截（按钮和脚本都不出）。"""
    empty = (work_day() - timedelta(days=40)).isoformat()
    body = admin_client.get(f"/compare?date={empty}").get_data(as_text=True)
    assert "复制图片" not in body and 'id="btnCopyShot"' not in body
    assert 'id="shotPreviewCard"' not in body


def test_store_view_shot_and_no_missing_note(admin_client, tmp_db):
    """单店逐日也能截图：容器 / 按钮 / 预览位 / 文件名带门店和月份；「另有 N 天未交日报」那句口径说明撤了。"""
    day = work_day()
    seed_report("store-alpha", day, 8)
    seed_touch("store-alpha", day, 8)
    sid = store_id("store-alpha")
    body = admin_client.get(
        f"/compare?view=store&store_id={sid}&month={day.replace(day=1).isoformat()}"
    ).get_data(as_text=True)
    assert '<div id="compareShot"' in body
    assert "复制图片" in body and 'id="btnCopyShot"' in body
    assert 'id="btnDownShot"' in body
    assert 'id="shotPreview"' in body and 'id="shotPreviewCard"' in body
    assert "html2canvas.min.js" in body and "shot.js" in body
    m = re.search(r"window\.__shotSetup = \{(.*?)\n  \};", body, re.S)
    assert m is not None, "没找到 window.__shotSetup"
    setup = m.group(1)
    assert "sourceId: 'compareShot'" in setup
    assert "fitContent: true" in setup
    assert "text: '#compareShot .cell-plain'" in setup
    assert f"触客通报_示例甲店_{day.strftime('%Y-%m')}.png" in setup
    shot = shot_of(body)
    assert "触客通报" in shot
    assert _full("示例甲店") in shot and f"{day.month}月" in shot  # 抬头：店名 + 月份
    assert TOTAL_HEAD in shot  # 图里合计行也只写「合计」
    assert "管理员" not in shot and "报表" not in shot  # 页面说明不进图
    assert len(re.findall(r"<th[ >]", shot)) == 5  # 标题不是 <th>，表还是五列
    # 可编辑格子旁备着纯数字：截图时拿数字换掉输入框，图里不出输入框外观
    assert '<span class="cell-plain" style="display:none">8</span>' in body
    # 「另有 N 天未交日报（触客照记，不进触客÷销量）」整句都撤了
    assert "另有" not in body and "不进触客÷销量" not in body


# ---------------------------------------------------------------- 单店逐日


def test_store_month_matrix_and_total(admin_client):
    day = work_day()
    seed_report("store-alpha", day, 8)
    seed_touch("store-alpha", day, 8)
    earlier = day.replace(day=1)
    if earlier != day:
        seed_report("store-alpha", earlier, 2)
        seed_touch("store-alpha", earlier, 5)  # 触客 5 ÷ 销量 2 = 250.00%
    sid = store_id("store-alpha")
    body = admin_client.get(
        f"/compare?view=store&store_id={sid}&month={day.replace(day=1).isoformat()}"
    ).get_data(as_text=True)
    assert "示例甲店" in body
    assert TOTAL_HEAD in body  # 合计行只写「合计」，不再带「（已交 N 天）」
    # 未来日期不铺行：最后一行不晚于今天
    assert (db.today_local() + timedelta(days=1)).isoformat() not in body


def test_store_month_view_has_five_columns(admin_client, tmp_db):
    """单店逐日跟单日同一套表：五列（多一列业辅查询登记），没有空进度条、没有「日报」列。"""
    day = work_day()
    seed_report("store-alpha", day, 8)
    sid = store_id("store-alpha")
    body = admin_client.get(
        f"/compare?view=store&store_id={sid}&month={day.replace(day=1).isoformat()}"
    ).get_data(as_text=True)
    assert len(re.findall(r"<th[ >]", body)) == 5
    assert "业辅查询登记" in body
    assert "dev-bar" not in body
    assert "<th>日报</th>" not in body


# ---------------------------------------------------------------- 权限


def test_filler_cannot_open_compare(filler_client):
    resp = filler_client.get("/compare", follow_redirects=False)
    assert resp.status_code == 302


def test_filler_cannot_post_cell(filler_client, tmp_db):
    resp = filler_client.post(
        "/compare/cell",
        data={
            "store_id": store_id("store-alpha"),
            "date": work_day().isoformat(),
            "value": "7",
        },
    )
    assert resp.status_code == 403


def test_city_can_fix_own_city_only(city_client, tmp_db):
    day = work_day()
    seed_report("store-alpha", day, 5)
    seed_touch("store-alpha", day, 9)
    seed_report("store-delta", day, 5)
    seed_touch("store-delta", day, 9)
    ok = city_client.post(
        "/compare/cell",
        data={
            "store_id": store_id("store-alpha"),
            "date": day.isoformat(),
            "value": "9",
            "view": "day",
            "date_hint": day.isoformat(),
        },
    )
    assert ok.status_code == 302
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT day_value FROM daily_facts WHERE store_id=? AND biz_date=? AND metric_code='phone_sales'",
            (store_id("store-alpha"), day.isoformat()),
        ).fetchone()
    assert int(row["day_value"]) == 9
    # 别的城市：403
    other = city_client.post(
        "/compare/cell",
        data={"store_id": store_id("store-delta"), "date": day.isoformat(), "value": "9"},
    )
    assert other.status_code == 403


def test_city_can_open_and_sees_own_city_only(city_client, tmp_db):
    """地市负责人进得去这页，且只看得到本市的门店。"""
    day = work_day()
    seed_report("store-alpha", day, 5)  # 示例市
    seed_touch("store-alpha", day, 5)
    seed_report("store-delta", day, 5)  # 邻市
    seed_touch("store-delta", day, 5)
    resp = city_client.get(f"/compare?date={day.isoformat()}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "示例甲店" in body
    assert "示例丁店" not in body


# ---------------------------------------------------------------- 改格


def test_admin_fix_cell_writes_edit_mark(admin_client, tmp_db):
    day = work_day()
    seed_report("store-alpha", day, 5)
    seed_touch("store-alpha", day, 9)
    sid = store_id("store-alpha")

    resp = admin_client.post(
        "/compare/cell",
        data={
            "store_id": sid,
            "date": day.isoformat(),
            "value": "9",
            "view": "day",
            "sort": "abs",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "已把 " in body and "的手机销量改成 9" in body
    assert "修正" in body  # 痕迹徽标

    with db.get_db() as conn:
        fact = conn.execute(
            "SELECT day_value FROM daily_facts WHERE store_id=? AND biz_date=? AND metric_code='phone_sales'",
            (sid, day.isoformat()),
        ).fetchone()
        edit = conn.execute(
            "SELECT before_json, after_json, note FROM report_edits WHERE store_id=? AND biz_date=?",
            (sid, day.isoformat()),
        ).fetchone()
    assert int(fact["day_value"]) == 9
    assert '"phone_sales": 5' in edit["before_json"] and '"phone_sales": 9' in edit["after_json"]
    assert "触客通报校准手机销量" in edit["note"]


def test_admin_fix_back_to_zero_clears_mark(admin_client, tmp_db):
    """改成 0 等于把当天的数抹掉：格子显示 0，不挂「修正」徽标。"""
    day = work_day()
    seed_report("store-alpha", day, 5)
    sid = store_id("store-alpha")
    admin_client.post(
        "/compare/cell",
        data={"store_id": sid, "date": day.isoformat(), "value": "0"},
    )
    body = admin_client.get(f"/compare?date={day.isoformat()}").get_data(as_text=True)
    assert "修正" not in body


def test_cell_bad_params_flash(admin_client, tmp_db):
    resp = admin_client.post(
        "/compare/cell", data={"store_id": "0", "date": "nope", "value": "1"}
    )
    assert resp.status_code == 302


def test_cannot_fix_cell_without_submitted_report(admin_client, tmp_db):
    """接口也必须拒绝未交日报的门店，不能经由校准操作悄悄创建日报。"""
    day = work_day()
    sid = store_id("store-delta")
    resp = admin_client.post(
        "/compare/cell",
        data={"store_id": sid, "date": day.isoformat(), "value": "7"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "该店当天未交日报" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, day) is None
        row = conn.execute(
            "SELECT 1 FROM daily_facts WHERE store_id=? AND biz_date=? AND metric_code='phone_sales'",
            (sid, day.isoformat()),
        ).fetchone()
    assert row is None



# ---------------------------------------------------------------- 导出


def test_day_export_xlsx(admin_client, day_scene):
    resp = admin_client.get(f"/compare.xlsx?view=day&date={day_scene.isoformat()}")
    assert resp.status_code == 200
    assert resp.mimetype.endswith("spreadsheetml.sheet")
    assert f"compare_{day_scene.isoformat()}.xlsx" in resp.headers["Content-Disposition"]
    assert resp.data[:2] == b"PK"


def test_store_export_xlsx(admin_client):
    month = work_day().replace(day=1).isoformat()
    sid = store_id("store-alpha")
    resp = admin_client.get(f"/compare.xlsx?view=store&store_id={sid}&month={month}")
    assert resp.status_code == 200
    assert f"compare_store-alpha_{month}.xlsx" in resp.headers["Content-Disposition"]
    assert resp.data[:2] == b"PK"


def test_store_export_includes_submitter_and_time(admin_client):
    day = work_day()
    seed_report("store-alpha", day, 8)
    sid = store_id("store-alpha")
    response = admin_client.get(
        f"/compare.xlsx?view=store&store_id={sid}&month={day.replace(day=1).isoformat()}"
    )
    assert response.status_code == 200
    ws = load_workbook(io.BytesIO(response.data)).active
    header = [cell.value for cell in ws[1]]
    assert "日报提交人" in header and "提交时间" in header
    with db.get_db() as conn:
        expected_name = conn.execute(
            "SELECT display_name FROM users WHERE username='alpha'"
        ).fetchone()["display_name"]
    values = list(ws.iter_rows(min_row=2, values_only=True))
    day_row = next(row for row in values if row[0] == day.isoformat())
    assert day_row[header.index("日报提交人")] == expected_name
    assert day_row[header.index("提交时间")].startswith(day.isoformat())


def test_export_needs_permission(filler_client):
    assert filler_client.get("/compare.xlsx?view=day").status_code == 302


# ---------------------------------------------------------------- 地市 / 区域经理筛选


def hrefs(body: str) -> list[str]:
    """页面里所有 href（挑链接用）。"""
    return re.findall(r'href="([^"]+)"', body)


def test_city_filter_keeps_only_that_city(admin_client, day_scene):
    """地市筛选：只留该市的店，合计跟着重算，只剩一个市就不插小计行。"""
    day = day_scene.isoformat()
    body = admin_client.get(f"/compare?date={day}&city=示例市").get_data(as_text=True)
    assert "示例甲店" in body and "示例乙店" in body
    assert "示例丁店" not in body and "示例戊店" not in body and "示例巳店" not in body
    assert "示例市 · 2 家" not in body  # 只剩一个地市，不插小计
    assert TOTAL_HEAD in body  # 合计行只写「合计」；触客 8+9=17 · 销量 8+5=13
    assert ">17<" in body and ">13<" in body


def test_area_filter_and_scope_label(admin_client, day_scene):
    """区域经理筛选：只留这位经理的店，图上抬头带出经理名。"""
    day = day_scene.isoformat()
    body = admin_client.get(f"/compare?date={day}&area=刘经理").get_data(as_text=True)
    assert "示例戊店" in body and "示例巳店" in body and "示例丁店" in body
    assert "示例甲店" not in body and "示例乙店" not in body
    assert f"{day_scene.month}月{day_scene.day}日 · 刘经理" in body
    assert "（刘经理）" in body  # 通报文案抬头也带


def test_city_and_area_together_scope_label(admin_client, day_scene):
    """地市 + 经理同时筛：抬头「地市 · 经理」，通报文案（…）一致。"""
    day = day_scene.isoformat()
    body = admin_client.get(f"/compare?date={day}&city=示例市&area=张管理").get_data(as_text=True)
    assert f"{day_scene.month}月{day_scene.day}日 · 示例市 · 张管理" in body
    assert "（示例市 · 张管理）" in body


def test_filter_controls_are_bulletin_style(admin_client, day_scene):
    """筛选控件跟通报表一套：日期 + 地市（首项「全部」）+ 区域经理。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert '<select name="city"' in body and '<select name="area"' in body
    assert '<option value="">全部</option>' in body
    assert '<option value="示例市"' in body and '<option value="邻市"' in body
    assert '<option value="张管理"' in body and '<option value="刘经理"' in body
    assert "前一天" in body


def test_filter_controls_stay_out_of_shot(admin_client, day_scene):
    """下拉不进贴群的图：图里只有表。"""
    shot = shot_of(
        admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    )
    assert "<select" not in shot and 'type="date"' not in shot
    assert "区域经理" not in shot and "前一天" not in shot


def test_front_and_export_keep_filter(admin_client, day_scene):
    """前一天 / 导出都带着当前筛选（没有排序开关）。"""
    day = day_scene.isoformat()
    prev = (day_scene - timedelta(days=1)).isoformat()
    body = admin_client.get(f"/compare?date={day}&city=示例市&area=张管理").get_data(as_text=True)
    links = hrefs(body)
    assert any(f"date={prev}" in h and "city=" in h and "area=" in h for h in links)
    assert not any("sort=" in h for h in links)  # 排序开关撤了
    xlsx = [h for h in links if h.startswith("/compare.xlsx")]
    assert xlsx and "city=" in xlsx[0] and "area=" in xlsx[0]


def test_unknown_filter_is_ignored(admin_client, day_scene):
    """传了不认识的地市 / 经理 / 顾问：当没传，别把表滤空。"""
    body = admin_client.get(
        f"/compare?date={day_scene.isoformat()}&city=不存在市&area=无此人&advisor=随便"
    ).get_data(as_text=True)
    assert "示例甲店" in body and "示例戊店" in body
    assert "示例市 · 2 家" in body  # 小计回来了 = 全店视野


def test_empty_day_wording_with_filter(admin_client, day_scene):
    """筛过的空日：文案点明是筛选范围内没数据。"""
    empty = (work_day() - timedelta(days=40)).isoformat()
    body = admin_client.get(f"/compare?date={empty}&city=示例市").get_data(as_text=True)
    assert "筛选范围内这一天没有店交日报" in body


def test_edit_cell_goes_back_to_same_filter(admin_client, day_scene):
    """改完一格回到同一个筛选视野（地市 / 经理 / 顾问都别丢）。"""
    resp = admin_client.post(
        "/compare/cell",
        data={
            "store_id": store_id("store-beta"),
            "date": day_scene.isoformat(),
            "value": "9",
            "view": "day",
            "city": "示例市",
            "area": "张管理",
            "advisor": "yes",
        },
    )
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert "date=" in loc and "city=" in loc and "area=" in loc and "advisor=yes" in loc


def test_day_export_respects_filter(admin_client, day_scene):
    """导出的 Excel 跟着筛选走、文件名不变；表尾只剩合计一行，月累计写成表头。"""
    resp = admin_client.get(f"/compare.xlsx?view=day&date={day_scene.isoformat()}&city=示例市")
    assert resp.status_code == 200
    assert f"compare_{day_scene.isoformat()}.xlsx" in resp.headers["Content-Disposition"]
    ws = load_workbook(io.BytesIO(resp.data)).active
    header = [cell.value for cell in ws[1]]
    assert len(header) == 15
    assert any(str(h).startswith("本月累计触客条数（") for h in header)
    labels = [str(row[1] or "") for row in ws.iter_rows(min_row=2, values_only=True)]
    names = [x for x in labels if x and x != "合计"]
    assert sorted(names) == sorted(["示例市甲街vivo体验店", "示例市乙街vivo专卖店"])
    assert labels.count("合计") == 1  # 表尾合计行只写「合计」，不再带「（已交 N 家）」

def test_table_wears_bulletin_style(admin_client, day_scene):
    """表格套通报表同款网格：1px 细线 + 表头浅灰加粗，合计行压一道深线。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert 'class="data dev-table bulletin"' in shot_of(body)
    assert "border-top: 2px solid #94a3b8" in foot_of(body, "合计")


def test_store_view_table_wears_bulletin_style(admin_client, tmp_db):
    """单店逐日同一套表样式。"""
    day = work_day()
    seed_report("store-alpha", day, 8)
    sid = store_id("store-alpha")
    body = admin_client.get(
        f"/compare?view=store&store_id={sid}&month={day.replace(day=1).isoformat()}"
    ).get_data(as_text=True)
    assert 'class="data dev-table bulletin"' in body
    assert "border-top: 2px solid #94a3b8" in foot_of(body, "合计")


# ---------------------------------------------------------------- 业辅 / 本月累计


def month_of(body: str, name: str) -> list[str]:
    """某家店那一行「门店」之后的 4 格：本月累计 触客 / 手机销量 / 触客÷销量 / 业辅。"""
    tds = re.findall(r"<td[^>]*>(.*?)</td>", row_of(body, name), re.S)
    return [re.sub(r"<[^>]+>", "", t).strip() for t in tds[0:4]]


def seed_aux(code: str, day: date, n: int, user_id=None) -> None:
    """直接写一格业辅查询登记。不传 user_id 就不带「谁改的」，hover 溯源留空。"""
    with db.get_db() as conn:
        db.set_aux(conn, store_id=store_id(code), biz_date=day, n=n, user_id=user_id)


def test_aux_column_editable_only_when_report_submitted(admin_client, day_scene):
    """第五列：已交日报的店行能就地填，未交日报的行根本没格子。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert "业辅查询登记" in shot_of(body)
    filled = row_of(body, "示例甲店")
    assert 'action="/compare/aux"' in filled
    assert 'name="value" value="0"' in filled  # 还没登记就是 0
    assert '<span class="cell-plain" style="display:none">0</span>' in filled
    missed = row_of(body, "示例丁店")
    assert "compare/aux" not in missed and "cell-plain" not in missed


def test_aux_only_row_shows_plain_number(admin_client, tmp_db):
    """只有业辅登记、当天没交日报的店也进表：格子出纯数字，但没有输入框。"""
    day = work_day()
    seed_aux("store-gamma", day, 4)
    body = admin_client.get(f"/compare?date={day.isoformat()}").get_data(as_text=True)
    row = row_of(body, "示例丙店")
    assert '<span style="font-weight: 650">4</span>' in row
    assert "compare/aux" not in row
    assert "未交日报" in row


def test_admin_writes_aux_and_sees_it(admin_client, tmp_db):
    """管理员填一格业辅查询登记：落库、记下谁什么时候写的、页面出数字。"""
    day = work_day()
    seed_report("store-alpha", day, 8)
    sid = store_id("store-alpha")
    resp = admin_client.post(
        "/compare/aux",
        data={"store_id": sid, "date": day.isoformat(), "value": "6", "view": "day"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f"已把 {day.isoformat()} 的业辅查询登记改成 6 条" in body
    assert '<span class="cell-plain" style="display:none">6</span>' in row_of(body, "示例甲店")
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT n, updated_by, updated_at FROM aux_queries WHERE store_id=? AND biz_date=?",
            (sid, day.isoformat()),
        ).fetchone()
        admin_id = int(conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"])
    assert int(row["n"]) == 6
    assert int(row["updated_by"]) == admin_id
    assert row["updated_at"]


def test_aux_zero_deletes_row(admin_client, tmp_db):
    """填 0 等于没登记：整行删掉，合计那行不挂「业辅查询登记 0 条」的尾巴。"""
    day = work_day()
    seed_report("store-alpha", day, 8)
    sid = store_id("store-alpha")
    seed_aux("store-alpha", day, 6)
    admin_client.post(
        "/compare/aux",
        data={"store_id": sid, "date": day.isoformat(), "value": "0", "view": "day"},
    )
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT n FROM aux_queries WHERE store_id=? AND biz_date=?",
            (sid, day.isoformat()),
        ).fetchone()
    assert row is None
    body = admin_client.get(f"/compare?date={day.isoformat()}").get_data(as_text=True)
    assert "合计：触客 0 条 · 手机销量 8 台 · 触客/销量 0.00%" in body
    # 当日合计那条尾巴只在有数时才挂（0 等于没登记）
    assert "合计：触客 0 条 · 手机销量 8 台 · 触客/销量 0.00% · 业辅" not in body


def test_aux_needs_submitted_report(admin_client, tmp_db):
    """未交日报的店不给填业辅：先去「报表」页补录，别经由这里悄悄建日报。"""
    day = work_day()
    sid = store_id("store-delta")
    resp = admin_client.post(
        "/compare/aux",
        data={"store_id": sid, "date": day.isoformat(), "value": "3"},
        follow_redirects=True,
    )
    assert "该店当天未交日报" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, day) is None
        row = conn.execute(
            "SELECT n FROM aux_queries WHERE store_id=? AND biz_date=?",
            (sid, day.isoformat()),
        ).fetchone()
    assert row is None


def test_filler_cannot_post_aux(filler_client, tmp_db):
    resp = filler_client.post(
        "/compare/aux",
        data={"store_id": store_id("store-alpha"), "date": work_day().isoformat(), "value": "5"},
    )
    assert resp.status_code == 403


def test_city_writes_aux_for_own_city_only(city_client, tmp_db):
    day = work_day()
    seed_report("store-alpha", day, 5)  # 示例市
    seed_report("store-delta", day, 5)  # 邻市
    ok = city_client.post(
        "/compare/aux",
        data={"store_id": store_id("store-alpha"), "date": day.isoformat(), "value": "4"},
    )
    assert ok.status_code == 302
    other = city_client.post(
        "/compare/aux",
        data={"store_id": store_id("store-delta"), "date": day.isoformat(), "value": "4"},
    )
    assert other.status_code == 403
    with db.get_db() as conn:
        assert db.aux_range_total(conn, [store_id("store-alpha")], day, day) == 4
        assert db.aux_range_total(conn, [store_id("store-delta")], day, day) == 0


def test_aux_cell_readonly_outside_current_month(city_client, tmp_db):
    """地市负责人看上月：格子出纯数字、没有输入框（跟手机销量同一套权限口径）。"""
    prev = db.today_local().replace(day=1) - timedelta(days=1)
    seed_report("store-alpha", prev, 4)
    seed_aux("store-alpha", prev, 2)
    body = city_client.get(f"/compare?date={prev.isoformat()}").get_data(as_text=True)
    row = row_of(body, "示例甲店")
    assert '<span style="font-weight: 650">2</span>' in row
    assert "compare/aux" not in row


def test_aux_feeds_subtotal_total_and_copy_text(admin_client, day_scene):
    """业辅进城市小计、进合计行尾巴（有数才挂）、进通报文案。"""
    for code, n in (("store-alpha", 3), ("store-beta", 5), ("store-zeta", 1), ("store-epsilon", 2)):
        seed_aux(code, day_scene, n)
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert "合计：触客 23 条 · 手机销量 13 台 · 触客/销量 176.92% · 业辅查询登记 11 条" in body
    assert '<td class="num">8</td>' in foot_of(body, "示例市 · 2 家")
    assert '<td class="num">3</td>' in foot_of(body, "邻市 · 2 家（未交 1）")
    total = re.findall(r"<td[^>]*>(.*?)</td>", foot_of(body, "合计"), re.S)
    assert len(total) == 9  # 门店 + 4 列本月累计 + 4 列当日
    assert total[8].strip() == "11"  # 当日业辅
    assert total[4].strip() == "11"  # 本月累计业辅（就这一天，跟当日同数）



def test_aux_range_total_normalizes_each_legacy_row(tmp_db):
    """汇总时逐条截断历史负数，避免负值抵消合法登记。"""
    day = work_day()
    day2 = day + timedelta(days=1)
    sid_a = store_id("store-alpha")
    sid_b = store_id("store-beta")
    with db.get_db() as conn:
        conn.executemany(
            "INSERT INTO aux_queries(biz_date, store_id, n) VALUES (?, ?, ?)",
            [
                (day.isoformat(), sid_a, -12),
                (day2.isoformat(), sid_a, 7),
                (day.isoformat(), sid_b, 4),
            ],
        )
        assert db.aux_range_total(conn, [sid_a], day, day2) == 7
        assert db.aux_range_total(conn, [sid_a, sid_b], day, day2) == 11


def test_month_columns_follow_filter(admin_client, day_scene):
    """本月累计是表里的 4 列（页头不再挂卡）：每家店一行，跟着地市筛选一起缩。"""
    day = day_scene.isoformat()
    body = admin_client.get(f"/compare?date={day}").get_data(as_text=True)
    span = r'<th colspan="4" class="g-month">本月累计（\d+月1日(–\d+月\d+日)?）</th>'
    assert re.search(span, shot_of(body))
    assert month_of(body, "示例乙店") == ["9", "5", "180.00%", "—"]
    assert "180.00%" in shot_of(body)  # 月累计同一口径（本月触客 ÷ 本月销量）
    assert month_of(body, "示例丁店") == ["6", "—", "—", "—"]  # 没交日报：销量 / 差不比
    scoped = admin_client.get(f"/compare?date={day}&city=示例市").get_data(as_text=True)
    assert "示例丁店" not in shot_of(scoped)
    foot = re.findall(r"<td[^>]*>(.*?)</td>", foot_of(scoped, "合计"), re.S)
    tail = [re.sub(r"<[^>]+>", "", t).strip() for t in foot[1:5]]
    assert tail == ["17", "13", "130.77%", "0"]  # 小计 / 合计行也是「合计触客 ÷ 合计销量」


def test_shot_has_month_group_header(admin_client, tmp_db):
    """图里表头多一层「本月累计（区间）」分组；这层不带「管理员 / 报表」这类给登录的人看的说明。"""
    day = work_day()
    seed_report("store-alpha", day, 8)
    seed_touch("store-alpha", day, 8)
    seed_aux("store-alpha", day, 6)  # 不传 user_id：溯源不带人名，进不了图
    shot = shot_of(
        admin_client.get(f"/compare?date={day.isoformat()}").get_data(as_text=True)
    )
    assert re.search(r'<th colspan="4" class="g-month">本月累计（[^）]+）</th>', shot)
    assert month_of(shot, "示例甲店") == ["8", "8", "100.00%", "6"]
    assert "管理员" not in shot and "报表" not in shot


def test_day_export_includes_aux(admin_client, day_scene):
    """导出 16 列：4 列本月累计在前、当日 9 列在后，合计行两段都有数。"""
    seed_aux("store-alpha", day_scene, 6)
    resp = admin_client.get(f"/compare.xlsx?view=day&date={day_scene.isoformat()}")
    ws = load_workbook(io.BytesIO(resp.data)).active
    header = [cell.value for cell in ws[1]]
    assert len(header) == 15  # 去掉了「日报」列
    assert str(header[3]).startswith("本月累计触客条数（")
    assert str(header[4]).startswith("本月累计手机销量（")
    assert str(header[5]).startswith("本月累计差（")
    assert str(header[6]).startswith("本月累计业辅查询登记（")
    assert header[7:11] == ["触客条数", "手机销量", "差", "差率"]
    assert header[11] == "业辅查询登记" and header[12] == "日报提交人"
    values = list(ws.iter_rows(min_row=2, values_only=True))
    alpha = next(row for row in values if row[1] == "示例市甲街vivo体验店")
    assert (alpha[3], alpha[4], alpha[5], alpha[6]) == (8, 8, "0", 6)  # 本月累计
    assert (alpha[7], alpha[8], alpha[9], alpha[11]) == (8, 8, "0", 6)  # 当日
    foot = next(row for row in values if str(row[1] or "") == "合计")
    # 差列带 ' 前缀：xlsx 的公式注入防护（_xlsx_safe）给 +/− 开头的文本加引号
    # 月累计差 = 各店差之和（未交日报的店当天不进这一列），所以不等于 29 − 13
    assert (foot[3], foot[4], foot[5], foot[6]) == (29, 13, "'+10", 6)
    assert (foot[7], foot[8], foot[9], foot[10], foot[11]) == (23, 13, "'+10", "176.92%", 6)
    assert not any(str(row[1] or "").startswith("本月累计（") for row in values)


def test_store_view_aux_cell_and_total(admin_client, tmp_db):
    """单店逐日也有业辅格子：能就地填，合计卡和合计行跟着走。"""
    day = work_day()
    seed_report("store-alpha", day, 8)
    sid = store_id("store-alpha")
    body = admin_client.get(
        f"/compare?view=store&store_id={sid}&month={day.replace(day=1).isoformat()}"
    ).get_data(as_text=True)
    assert 'action="/compare/aux"' in body
    assert "业辅查询登记合计" in body
    tds = re.findall(r"<td[^>]*>(.*?)</td>", foot_of(body, "合计"), re.S)
    assert len(tds) == 5
    assert tds[-1].strip() == "0"


def test_store_view_export_includes_aux(admin_client, tmp_db):
    """单店逐日导出：11 列，表尾合计行带业辅查询登记。"""
    day = work_day()
    seed_report("store-alpha", day, 8)
    sid = store_id("store-alpha")
    seed_aux("store-alpha", day, 5)
    resp = admin_client.get(
        f"/compare.xlsx?view=store&store_id={sid}&month={day.replace(day=1).isoformat()}"
    )
    ws = load_workbook(io.BytesIO(resp.data)).active
    header = [cell.value for cell in ws[1]]
    assert len(header) == 11 and header.index("业辅查询登记") == 6
    idx = 6
    values = list(ws.iter_rows(min_row=2, values_only=True))
    day_row = next(row for row in values if row[0] == day.isoformat())
    assert day_row[idx] == 5
    foot = next(row for row in values if str(row[0] or "") == "合计")
    assert foot[idx] == 5


# ---------------------------------------------------------------- 顾问筛选 + 改名


def test_advisor_filter_keeps_only_that_kind(admin_client, day_scene):
    """顾问筛选照洞察页：yes 只留设了顾问的店、no 只留没设的，抬头记一笔。"""
    with db.get_db() as conn:
        conn.execute(
            "UPDATE stores SET advisor_name='王顾问' WHERE code IN ('store-alpha', 'store-beta')"
        )
    day = day_scene.isoformat()
    yes = admin_client.get(f"/compare?date={day}&advisor=yes").get_data(as_text=True)
    assert "示例甲店" in yes and "示例乙店" in yes
    assert "示例戊店" not in yes and "示例丁店" not in yes
    assert f"{day_scene.month}月{day_scene.day}日 · 有顾问" in yes  # 图上抬头
    assert "（有顾问）" in yes  # 通报文案抬头也带
    no = admin_client.get(f"/compare?date={day}&advisor=no").get_data(as_text=True)
    assert "示例戊店" in no and "示例甲店" not in no
    assert f"{day_scene.month}月{day_scene.day}日 · 无顾问" in no


def test_advisor_filter_reaches_links_and_export(admin_client, day_scene):
    """顾问筛选跟着前一天 / 导出一起走（排序开关撤了）。"""
    day = day_scene.isoformat()
    prev = (day_scene - timedelta(days=1)).isoformat()
    body = admin_client.get(f"/compare?date={day}&advisor=yes").get_data(as_text=True)
    links = hrefs(body)
    assert any(f"date={prev}" in h and "advisor=yes" in h for h in links)
    assert not any("sort=" in h for h in links)  # 排序开关撤了，链接里不该再出现它
    xlsx = [h for h in links if h.startswith("/compare.xlsx")]
    assert xlsx and "advisor=yes" in xlsx[0]


def test_advisor_control_is_bulletin_style_and_out_of_shot(admin_client, day_scene):
    """顾问下拉跟地市 / 经理一个模子，且不进贴群的图。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert '<select name="advisor"' in body
    assert "只看有顾问" in body and "只看无顾问" in body
    shot = shot_of(body)
    assert '<select name="advisor"' not in shot and "顾问" not in shot


def test_page_renamed_to_touch_bulletin(admin_client, day_scene):
    """改名：抬头 / 导航 / 通报文案统一叫「触客通报」，旧名不再出现。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert "手机销量对比触客" not in body and "销量对比" not in body
    assert "<title>触客通报</title>" in body
    assert '<span class="full">触客通报</span>' in body  # 导航
    assert "【触客通报】" in body  # 通报文案抬头
    assert "触客通报" in shot_of(body)  # 图里抬头


def test_compare_title_constant():
    """改名落在常量上（模板 / 文案共用同一个名字）。"""
    from app.views_compare import COMPARE_TITLE

    assert COMPARE_TITLE == "触客通报"
