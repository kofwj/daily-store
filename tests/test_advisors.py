"""运营商顾问月度打分：加权系数、角色列、保存范围。"""

from datetime import date

from app import db
from app.helpers import (
    advisor_coeffs,
    advisor_edit_column,
    advisor_score_month,
    advisor_score_open,
)


def _set_advisor(code="store-alpha", name="任阳"):
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM stores WHERE code=?", (code,)).fetchone()
        db.update_store_profile(
            conn,
            row["id"],
            mobile_code=row["mobile_code"] or "",
            area_manager=row["area_manager"] or "",
            store_manager=row["store_manager"] or "",
            advisor_name=name,
        )
        return row["id"]


def test_advisor_coeffs_need_all_three_weighted():
    assert advisor_coeffs(1.2, 0, [None, 10, 10]) == {"rate": None, "final": None}
    # (10*0.4 + 9*0.3 + 10*0.3) / 10 = 0.97；最终 (1.2+0)*0.97
    got = advisor_coeffs(1.2, 0, [10, 9, 10])
    assert got["rate"] == 0.97
    assert got["final"] == 1.164
    got = advisor_coeffs(1.2, 200 / 4000, [10, 10, 10])
    assert got["rate"] == 1.0
    assert got["final"] == 1.25


def test_advisor_edit_column_by_role():
    class U(dict):
        def __getitem__(self, k):
            return dict.__getitem__(self, k)

        def keys(self):
            return dict.keys(self)

    assert advisor_edit_column(U(role="admin", scope="")) == "all"
    assert advisor_edit_column(U(role="city", scope="示例市")) == "score_city"
    assert advisor_edit_column(U(role="readonly", scope="张管理")) == "score_area"
    assert advisor_edit_column(U(role="readonly", scope="")) == "score_manager"
    assert advisor_edit_column(U(role="filler", scope="")) == ""


def test_score_window_is_next_month_days_1_to_5():
    july = date(2026, 7, 1)
    assert advisor_score_month(date(2026, 8, 3)) == july
    assert advisor_score_open(date(2026, 8, 1), july)
    assert advisor_score_open(date(2026, 8, 5), july)
    assert not advisor_score_open(date(2026, 8, 6), july)
    assert not advisor_score_open(date(2026, 7, 31), july)


def _in_window(monkeypatch, today=date(2026, 8, 3)):
    monkeypatch.setattr(db, "today_local", lambda: today)
    return advisor_score_month(today).strftime("%Y-%m")


def test_advisors_page_empty_then_appears(app_client):
    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    page = app_client.get("/advisors").get_data(as_text=True)
    assert "还没有顾问" in page
    _set_advisor()
    page = app_client.get("/advisors").get_data(as_text=True)
    assert "任阳" in page
    assert "adv-card" in page
    assert "保存打分" in page


def test_admin_saves_scores_and_shows_coeff(app_client):
    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    _set_advisor()
    month = db.today_local().strftime("%Y-%m")
    resp = app_client.post(
        "/advisors",
        data={
            "month": month,
            "advisor_0": "任阳",
            "wt_0": "正式",
            "bc_0": "1.2",
            "sm_0": "10",
            "sa_0": "9",
            "sc_0": "10",
        },
        follow_redirects=True,
    )
    html = resp.get_data(as_text=True)
    assert "顾问打分已保存" in html
    assert "0.97" in html
    assert "1.164" in html
    xlsx = app_client.get(f"/advisors.xlsx?month={month}")
    assert xlsx.status_code == 200
    assert xlsx.headers["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_filler_cannot_score(app_client):
    _set_advisor()
    app_client.post("/login", data={"username": "alpha", "pin": "123456"})
    page = app_client.get("/advisors").get_data(as_text=True)
    assert "保存打分" not in page
    month = db.today_local().strftime("%Y-%m")
    resp = app_client.post(
        "/advisors",
        data={"month": month, "advisor_0": "任阳", "sm_0": "10"},
        follow_redirects=True,
    )
    assert "没有打分权限" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.list_advisor_scores(conn, month) == []


def test_city_cannot_score_outside_window(app_client, monkeypatch):
    monkeypatch.setattr(db, "today_local", lambda: date(2026, 8, 29))
    _set_advisor("store-alpha", "任阳")
    with db.get_db() as conn:
        db.create_user(
            conn,
            username="cityboss",
            display_name="地市负责",
            pin="654321",
            role="city",
            store_ids=[],
            scope="示例市",
        )
        conn.execute("UPDATE users SET must_change_pin=0 WHERE username='cityboss'")
    app_client.post("/login", data={"username": "cityboss", "pin": "654321"})
    page = app_client.get("/advisors").get_data(as_text=True)
    assert "每月 1–5 日" in page or "次月 1–5 日" in page
    assert "保存打分" not in page
    resp = app_client.post(
        "/advisors",
        data={"month": "2026-07", "advisor_0": "任阳", "sc_0": "8"},
        follow_redirects=True,
    )
    assert "打分窗口已过" in resp.get_data(as_text=True)


def test_city_scores_only_city_column_and_own_city(app_client, monkeypatch):
    _set_advisor("store-alpha", "任阳")
    _set_advisor("store-epsilon", "邻市顾问")
    with db.get_db() as conn:
        db.create_user(
            conn,
            username="cityboss",
            display_name="地市负责",
            pin="654321",
            role="city",
            store_ids=[],
            scope="示例市",
        )
        conn.execute("UPDATE users SET must_change_pin=0 WHERE username='cityboss'")
    month = _in_window(monkeypatch)
    app_client.post("/login", data={"username": "cityboss", "pin": "654321"})
    page = app_client.get("/advisors").get_data(as_text=True)
    assert "任阳" in page
    assert "邻市顾问" not in page
    assert 'name="sc_0"' in page
    assert 'name="sm_0"' not in page
    assert 'name="sa_0"' not in page
    assert "保存打分" in page
    assert "还剩" in page
    resp = app_client.post(
        "/advisors",
        data={"month": month, "advisor_0": "任阳", "sc_0": "8", "sm_0": "10"},
        follow_redirects=True,
    )
    assert "顾问打分已保存" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        rec = db.list_advisor_scores(conn, month)[0]
        assert rec["score_city"] == 8
        assert rec["score_manager"] is None
    steal = app_client.post(
        "/advisors",
        data={"month": month, "advisor_0": "邻市顾问", "sc_0": "1"},
        follow_redirects=True,
    )
    assert "只能给自己可见范围内的顾问打分" in steal.get_data(as_text=True)


def test_advisor_sees_own_three_scores(app_client, monkeypatch):
    month = _in_window(monkeypatch)
    _set_advisor("store-alpha", "示例甲店")
    with db.get_db() as conn:
        db.upsert_advisor_score(
            conn,
            month,
            "示例甲店",
            {"score_manager": 10, "score_area": 9, "score_city": 8},
            1,
        )
    app_client.post("/login", data={"username": "alpha", "pin": "123456"})
    page = app_client.get("/advisors").get_data(as_text=True)
    assert "我的打分" in page
    assert "店长" in page and "区域经理" in page and "地市负责人" in page
    assert "保存打分" not in page
    assert "邻市顾问" not in page


def test_xlsx_is_admin_only(app_client):
    app_client.post("/login", data={"username": "alpha", "pin": "123456"})
    assert app_client.get("/advisors.xlsx").status_code in (302, 200)
    page = app_client.get("/advisors.xlsx", follow_redirects=True).get_data(as_text=True)
    assert "需要管理员权限" in page
