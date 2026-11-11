"""功能开关：芝麻周报、顾问打分默认收起，管理员在 设置 → 功能开关 勾回来即恢复。

芝麻周报只是暂时不用（不是下线），顾问打分正式下线；两者都靠 app_meta 的
module_* 键控制入口显示与页面准入，代码与历史数据都留在库里。
"""

from app import db

ADVISOR_HREF = 'href="/advisors"'
SESAME_HREF = 'href="/advance/sesame/week"'


def _home(client):
    return client.get("/", follow_redirects=True).get_data(as_text=True)


def _set_flags(**flags):
    """直接写开关，跳过界面（界面本身另有用例覆盖）。"""
    with db.get_db() as conn:
        for key, value in flags.items():
            db.set_setting(conn, f"module_{key}", value)


def test_default_off_hides_nav_and_404s_pages(admin_client):
    """默认收起：标题栏没有入口，直接输网址也是 404。"""
    page = _home(admin_client)
    assert ADVISOR_HREF not in page
    assert SESAME_HREF not in page
    assert admin_client.get("/advisors").status_code == 404
    assert admin_client.get("/advisors.xlsx").status_code == 404
    assert admin_client.get("/advance/sesame/week").status_code == 404
    assert admin_client.get("/advance/sesame/week.xlsx").status_code == 404
    assert admin_client.get("/advance/sesame").status_code == 404


def test_settings_tab_lists_both_switches(admin_client):
    """设置里有「功能开关」页，两个模块各一个勾选框，默认都不勾。"""
    page = admin_client.get("/settings?tab=modules").get_data(as_text=True)
    assert "功能开关" in page
    assert 'name="module_sesame_week"' in page
    assert 'name="module_advisor_score"' in page
    assert 'name="module_sesame_week" value="1" checked' not in page
    assert 'name="module_advisor_score" value="1" checked' not in page
    # 设置导航里有入口，管理员点得到
    assert "settings?tab=modules" in page


def test_admin_turns_modules_back_on(admin_client):
    """勾上两个开关：入口回到标题栏，页面恢复 200（代码和数据一直在）。"""
    resp = admin_client.post(
        "/settings",
        data={
            "action": "save_modules",
            "tab": "modules",
            "module_sesame_week": "1",
            "module_advisor_score": "1",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        assert db.get_setting(conn, "module_sesame_week", "0") == "1"
        assert db.get_setting(conn, "module_advisor_score", "0") == "1"

    page = _home(admin_client)
    assert ADVISOR_HREF in page
    assert SESAME_HREF in page
    assert admin_client.get("/advisors").status_code == 200
    assert admin_client.get("/advance/sesame/week").status_code == 200


def test_unchecking_switches_hides_again(admin_client):
    """再取消勾选就重新收起，随时可切，不需要改代码或重新部署。"""
    _set_flags(sesame_week="1", advisor_score="1")
    resp = admin_client.post(
        "/settings",
        data={"action": "save_modules", "tab": "modules"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        assert db.get_setting(conn, "module_sesame_week", "1") == "0"
        assert db.get_setting(conn, "module_advisor_score", "1") == "0"
    page = _home(admin_client)
    assert ADVISOR_HREF not in page
    assert SESAME_HREF not in page
    assert admin_client.get("/advisors").status_code == 404


def test_filler_cannot_toggle_modules(filler_client):
    """普通店员看不到开关页，也改不动开关。"""
    page = filler_client.get("/settings?tab=modules").get_data(as_text=True)
    assert 'name="module_advisor_score"' not in page
    filler_client.post(
        "/settings",
        data={"action": "save_modules", "tab": "modules", "module_advisor_score": "1"},
        follow_redirects=True,
    )
    with db.get_db() as conn:
        assert db.get_setting(conn, "module_advisor_score", "0") == "0"
    assert filler_client.get("/advisors").status_code == 404
