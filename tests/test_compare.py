"""手机销量对比触客：口径、权限、改格、导出（`/compare`）。

口径（与 app/views_compare.py 顶部说明一致）：
- 触客条数 = 当天 deal_posts 条数（不分成交）；手机销量 = 当天日报 phone_sales
- 差 = 触客 − 销量（正=少报、负=多报）；未交日报的店留行但差不计算
- 合计只算已交日报的店 / 天；改手机销量只给 admin / city
"""
import io
import re
from datetime import date, timedelta

import pytest
from conftest import store_id
from openpyxl import load_workbook

from app import db
from app.views_compare import _diff_text, _rate_text, _side, day_copy_text


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


def row_of(body: str, name: str) -> str:
    """取表里某家店的那一行（<strong> 里的店名唯一）。"""
    m = re.search(r"<strong>" + re.escape(name) + r"</strong>.*?</tr>", body, re.S)
    assert m is not None, f"表里没有 {name} 这一行"
    return m.group(0)

def store_order(body: str, names) -> list[str]:
    """表里这几家店的出现顺序（只认 <strong> 里的店名，通报文案里的不算）。"""
    found = re.findall(r"<strong>(.*?)</strong>", body)
    return [n for n in found if n in names]

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

# ---------------------------------------------------------------- 文案（纯函数）


def test_diff_and_rate_text():
    assert _diff_text(None) == "—"
    assert _diff_text(0) == "0"
    assert _diff_text(4) == "+4"
    assert _diff_text(-4) == "-4"
    assert _rate_text(None) == "—"
    assert _rate_text(0.0) == "0%"
    assert _rate_text(0.004) == "0%"  # 不到 0.5% 显示 0%
    assert _rate_text(0.44) == "+44%"
    assert _rate_text(-0.5) == "-50%"
    assert _side(None) == "none"
    assert _side(0) == "even"
    assert _side(3) == "under"  # 正差 = 少报
    assert _side(-3) == "over"


# ---------------------------------------------------------------- 主表口径


@pytest.fixture()
def day_scene(admin_client):
    """口径示例：乙店少报、甲店一致、巳店/戊店多报、丁店未交日报、丙店静默。"""
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
    assert "手机销量对比触客" in body
    # 已交 4 家：触客 9+8+2+4=23，销量 5+8+0+0=13，差 +10
    assert "合计（已交 4 家）" in body
    assert ">23<" in body and ">13<" in body and "+10" in body
    # 未交日报的店：留行、差不计算、单独点名
    assert "未交日报" in body
    assert "未交日报 1 家：示例丁店" in body
    # 差 0 也列，显示「一致」
    assert "示例甲店" in body and "一致" in body
    # 地市小计：邻市 2 家已交 + 1 家未交
    assert "示例市 · 2 家" in body
    assert "邻市 · 2 家（未交 1）" in body
    # 静默店（无日报无触客）不占表行，只报个数
    assert "另有 1 家当天没交日报也没触客记录" in body
    assert "示例丙店" not in body


def test_unsubmitted_store_shows_touch_but_no_diff(admin_client, day_scene):
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    row = row_of(body, "示例丁店")
    assert ">6<" in row  # 触客条数照记
    assert "未交日报" in row  # 差写「未交日报」而不是 0
    assert "+6" not in row and "-6" not in row


def test_touch_zero_keeps_row_and_no_rate(admin_client):
    """触客 0 条：店还在表里（有日报），差不写 0、差率算不出。"""
    day = work_day()
    seed_report("store-gamma", day, 3)
    body = admin_client.get(f"/compare?date={day.isoformat()}").get_data(as_text=True)
    row = row_of(body, "示例丙店")
    assert "-3" in row  # 差 = 0 − 3
    assert "—" in row


def test_touch_cross_day_note_visible(admin_client, day_scene):
    """触客只算当天：页面写明没有补交一说。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert "没有补交一说" in body


# ---------------------------------------------------------------- 通报文案


def test_day_copy_text_lines():
    """纯函数：一行合计 + 少报 / 多报 / 一致 / 未交 / 静默，缺的段不出现。"""
    rows = [
        {"name": "示例乙店", "side": "under", "diff_text": "+4"},
        {"name": "示例甲店", "side": "even", "diff_text": "0"},
        {"name": "示例戊店", "side": "over", "diff_text": "-4"},
    ]
    totals = {"n": 3, "touch": 21, "sold": 13, "diff_text": "+8"}
    text = day_copy_text(
        biz_date=date(2026, 10, 11),
        rows=rows,
        totals=totals,
        missing_names=["示例丁店"],
        silent_n=1,
        scope_label="示例市",
    )
    assert text.splitlines() == [
        "【手机销量对比触客】10月11日（示例市）",
        "合计（已交 3 家）：触客 21 条 · 手机销量 13 台 · 差 +8",
        "少报 1 家：示例乙店 +4",
        "多报 1 家：示例戊店 -4",
        "一致 1 家：示例甲店",
        "未交日报 1 家：示例丁店",
        "另有 1 家没交日报也没触客记录",
    ]


def test_day_copy_text_empty_day():
    """空日不给文案（页面上那块直接不出现）。"""
    text = day_copy_text(
        biz_date=date(2026, 10, 11),
        rows=[],
        totals={"n": 0, "touch": 0, "sold": 0, "diff_text": "0"},
        missing_names=[],
    )
    assert text == ""


def test_copy_block_matches_page_numbers(admin_client, day_scene):
    """页面上的通报文案块，数字跟表格口径一致，且可一键复制。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert 'id="copyText"' in body and "复制通报" in body
    assert "合计（已交 4 家）：触客 23 条 · 手机销量 13 台 · 差 +10" in body
    assert "少报 3 家：示例乙店 +4、示例戊店 +4、示例巳店 +2" in body
    assert "一致 1 家：示例甲店" in body
    assert "未交日报 1 家：示例丁店" in body


def test_copy_block_hidden_on_empty_day(admin_client):
    day = (work_day() - timedelta(days=40)).isoformat()
    body = admin_client.get(f"/compare?date={day}").get_data(as_text=True)
    assert 'id="copyText"' not in body


def test_sort_switch_keeps_abs_order(admin_client, day_scene):
    """默认按 |差| 降序（地市分组内），目录顺序照旧。拿同城三家比：邻市 戊(+4) / 巳(+2) / 丁(未交)。"""
    day = day_scene.isoformat()
    by_abs = admin_client.get(f"/compare?date={day}").get_data(as_text=True)
    catalog = admin_client.get(f"/compare?date={day}&sort=catalog").get_data(as_text=True)
    names = ("示例丁店", "示例戊店", "示例巳店")
    assert store_order(by_abs, names) == ["示例戊店", "示例巳店", "示例丁店"]
    assert store_order(catalog, names) == ["示例丁店", "示例戊店", "示例巳店"]


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


def test_shot_content_matches_table(admin_client, day_scene):
    """截图容器里就是这张表：抬头、四列表格、未交名单都在图里。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    shot = shot_of(body)
    assert "手机销量对比触客" in shot
    assert f"{day_scene.month}月{day_scene.day}日" in shot
    assert "合计（已交 4 家）" in shot
    assert "少报 +10" in shot  # 合计行的差（徽标写法）
    assert "未交日报 1 家：示例丁店" in shot
    assert "示例乙店" in shot


def test_shot_table_is_four_columns(admin_client, day_scene):
    """通报图只留 门店 / 触客条数 / 手机销量 / 差：差率并进差格，日报列和进度条去掉。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    shot = shot_of(body)
    head = shot[: shot.index("</thead>")]
    assert len(re.findall(r"<th[ >]", head)) == 4
    assert "差率" in shot  # 差格里的补充说明，不占一列
    assert "dev-bar" not in shot
    assert "<th>日报</th>" not in shot


def test_diff_column_uses_words_not_bare_numbers(admin_client, day_scene):
    """差列写「少报 +4 / 多报 -3 / 一致」：光看数字也知道往哪偏。"""
    seed_report("store-gamma", day_scene, 3)  # 触客 0 条 → 多报
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert "少报 +4" in row_of(body, "示例乙店")
    assert "多报 -3" in row_of(body, "示例丙店")
    assert "一致" in row_of(body, "示例甲店")
    row = row_of(body, "示例丁店")
    assert "未交日报" in row and "+6" not in row and "-6" not in row



def test_city_subtotal_uses_words_too(admin_client, day_scene):
    """城市小计跟店铺行同一套写法：一行是「少报 +4」，不是裸数字 +4。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert '<span class="dev-badge under">少报 +4</span>' in foot_of(body, "示例市 · 2 家")
    assert '<span class="dev-badge under">少报 +6</span>' in foot_of(body, "邻市 · 2 家")


def test_even_row_drops_zero_rate(admin_client, day_scene):
    """「一致」旁边不再挂「差率 0%」；真偏了的行照旧带差率。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    even = row_of(body, "示例甲店")
    assert "一致" in even and "差率" not in even
    assert "差率 +44%" in row_of(body, "示例乙店")

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


def test_shot_hidden_on_empty_day_and_store_view(admin_client, day_scene):
    """空日没有图可截（按钮和脚本都不出）；单店逐日视图压根没有截图容器。"""
    empty = (work_day() - timedelta(days=40)).isoformat()
    body = admin_client.get(f"/compare?date={empty}").get_data(as_text=True)
    assert "复制图片" not in body and 'id="btnCopyShot"' not in body
    assert 'id="shotPreviewCard"' not in body
    sid = store_id("store-alpha")
    body = admin_client.get(
        f"/compare?view=store&store_id={sid}&month={day_scene.replace(day=1).isoformat()}"
    ).get_data(as_text=True)
    assert '<div id="compareShot"' not in body
    assert "复制图片" not in body


# ---------------------------------------------------------------- 单店逐日


def test_store_month_matrix_and_total(admin_client):
    day = work_day()
    seed_report("store-alpha", day, 8)
    seed_touch("store-alpha", day, 8)
    earlier = day.replace(day=1)
    if earlier != day:
        seed_report("store-alpha", earlier, 2)
        seed_touch("store-alpha", earlier, 5)  # 多报 3
    sid = store_id("store-alpha")
    body = admin_client.get(
        f"/compare?view=store&store_id={sid}&month={day.replace(day=1).isoformat()}"
    ).get_data(as_text=True)
    assert "示例甲店" in body
    assert "合计（已交 2 天）" in body if earlier != day else "合计（已交 1 天）" in body
    # 未来日期不铺行：最后一行不晚于今天
    assert (db.today_local() + timedelta(days=1)).isoformat() not in body


def test_store_month_view_has_four_columns(admin_client, tmp_db):
    """单店逐日跟单日同一套表：四列，没有空进度条、没有「日报」列。"""
    day = work_day()
    seed_report("store-alpha", day, 8)
    sid = store_id("store-alpha")
    body = admin_client.get(
        f"/compare?view=store&store_id={sid}&month={day.replace(day=1).isoformat()}"
    ).get_data(as_text=True)
    assert len(re.findall(r"<th[ >]", body)) == 4
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
    assert "销量对比表校准手机销量" in edit["note"]


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
    assert "合计（已交 2 家）" in body  # 触客 8+9=17 · 销量 8+5=13
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


def test_front_sort_and_export_keep_filter(admin_client, day_scene):
    """前一天 / 排序 / 导出都带着当前筛选。"""
    day = day_scene.isoformat()
    prev = (day_scene - timedelta(days=1)).isoformat()
    body = admin_client.get(f"/compare?date={day}&city=示例市&area=张管理").get_data(as_text=True)
    links = hrefs(body)
    assert any(f"date={prev}" in h and "city=" in h and "area=" in h for h in links)
    assert any("sort=catalog" in h and "city=" in h for h in links)
    xlsx = [h for h in links if h.startswith("/compare.xlsx")]
    assert xlsx and "city=" in xlsx[0] and "area=" in xlsx[0]


def test_unknown_filter_is_ignored(admin_client, day_scene):
    """传了不认识的地市 / 经理：当没传，别把表滤空。"""
    body = admin_client.get(
        f"/compare?date={day_scene.isoformat()}&city=不存在市&area=无此人"
    ).get_data(as_text=True)
    assert "示例甲店" in body and "示例戊店" in body
    assert "示例市 · 2 家" in body  # 小计回来了 = 全店视野


def test_empty_day_wording_with_filter(admin_client, day_scene):
    """筛过的空日：文案点明是筛选范围内没数据。"""
    empty = (work_day() - timedelta(days=40)).isoformat()
    body = admin_client.get(f"/compare?date={empty}&city=示例市").get_data(as_text=True)
    assert "筛选范围内这一天没有店交日报" in body


def test_edit_cell_goes_back_to_same_filter(admin_client, day_scene):
    """改完一格回到同一个筛选视野。"""
    resp = admin_client.post(
        "/compare/cell",
        data={
            "store_id": store_id("store-beta"),
            "date": day_scene.isoformat(),
            "value": "9",
            "view": "day",
            "city": "示例市",
            "area": "张管理",
        },
    )
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert "date=" in loc and "city=" in loc and "area=" in loc


def test_day_export_respects_filter(admin_client, day_scene):
    """导出的 Excel 跟着筛选走，文件名不变。"""
    resp = admin_client.get(f"/compare.xlsx?view=day&date={day_scene.isoformat()}&city=示例市")
    assert resp.status_code == 200
    assert f"compare_{day_scene.isoformat()}.xlsx" in resp.headers["Content-Disposition"]
    ws = load_workbook(io.BytesIO(resp.data)).active
    names = [row[1] for row in ws.iter_rows(min_row=2, values_only=True)]
    assert sorted(names) == sorted(["示例甲店", "示例乙店"])


def test_table_wears_bulletin_style(admin_client, day_scene):
    """表格套通报表同款网格：1px 细线 + 表头浅灰加粗，合计行压一道深线。"""
    body = admin_client.get(f"/compare?date={day_scene.isoformat()}").get_data(as_text=True)
    assert 'class="data dev-table bulletin"' in shot_of(body)
    assert "border-top: 2px solid #94a3b8" in foot_of(body, "合计（已交 4 家）")


def test_store_view_table_wears_bulletin_style(admin_client, tmp_db):
    """单店逐日同一套表样式。"""
    day = work_day()
    seed_report("store-alpha", day, 8)
    sid = store_id("store-alpha")
    body = admin_client.get(
        f"/compare?view=store&store_id={sid}&month={day.replace(day=1).isoformat()}"
    ).get_data(as_text=True)
    assert 'class="data dev-table bulletin"' in body
    assert "border-top: 2px solid #94a3b8" in foot_of(body, "合计（已交")
