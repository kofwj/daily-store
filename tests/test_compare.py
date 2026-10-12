"""手机销量对比触客：口径、权限、改格、导出（`/compare`）。

口径（与 app/views_compare.py 顶部说明一致）：
- 触客条数 = 当天 deal_posts 条数（不分成交）；手机销量 = 当天日报 phone_sales
- 差 = 触客 − 销量（正=少报、负=多报）；未交日报的店留行但差不计算
- 合计只算已交日报的店 / 天；改手机销量只给 admin / city
"""
import re
from datetime import date, timedelta

import pytest
from conftest import store_id

from app import db
from app.views_compare import _diff_text, _rate_text, _side


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
    assert "另有 1 家未交日报：示例丁店" in body
    # 差 0 也列，显示「一致」
    assert "示例甲店" in body and "一致" in body
    # 地市小计：邻市 2 家已交 + 1 家未交
    assert "示例市 · 2 家" in body
    assert "邻市 · 2 家（未交 1）" in body
    # 静默店（无日报无触客）不占表行，只报个数
    assert "还有 1 家当天没交日报也没有触客记录" in body
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


def test_sort_switch_keeps_abs_order(admin_client, day_scene):
    """默认按 |差| 降序（地市分组内），目录顺序照旧。拿同城三家比：邻市 戊(+4) / 巳(+2) / 丁(未交)。"""
    day = day_scene.isoformat()
    by_abs = admin_client.get(f"/compare?date={day}").get_data(as_text=True)
    catalog = admin_client.get(f"/compare?date={day}&sort=catalog").get_data(as_text=True)
    assert by_abs.index("示例戊店") < by_abs.index("示例巳店") < by_abs.index("示例丁店")
    assert catalog.index("示例丁店") < catalog.index("示例戊店") < catalog.index("示例巳店")


def test_empty_day(admin_client):
    """那天既没日报也没触客：表空，不报错。"""
    day = (work_day() - timedelta(days=40)).isoformat()
    resp = admin_client.get(f"/compare?date={day}")
    assert resp.status_code == 200
    assert "这一天没有店交日报，也没有触客记录" in resp.get_data(as_text=True)


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


def test_export_needs_permission(filler_client):
    assert filler_client.get("/compare.xlsx?view=day").status_code == 302
