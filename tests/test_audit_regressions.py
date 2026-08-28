"""上轮外部审计中严重项回归测试。。"""

from app import db
from app.incentive import DEFAULTS, judge_without_advisor


def test_without_advisor_uses_gt_zero_not_ai_pass(tmp_db):
    rules = dict(DEFAULTS)
    rules["ai_pass"] = 10_000  # 管理员调高的 ai_pass 不应影响“破 0”口径
    row = judge_without_advisor(
        50,
        50,
        rules,
    )
    assert row["store_reward"] == DEFAULTS["reward_no_advisor"]


def test_pick_store_invalid_id_prompts(tmp_db, client):
    client.post(
        "/login",
        data={"username": "admin", "pin": "123456"},
    )
    page = client.get("/today?store_id=zzz")
    assert "店号无效" in page.get_data(as_text=True)


def test_csrf_referrer_only_same_site(tmp_db, client):
    client.post(
        "/login",
        data={"username": "admin", "pin": "123456"},
    )
    resp = client.post(
        "/settings",
        data={"action": "bogus"},
        headers={"Referer": "https://evil.example/x"},
    )
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert loc.startswith("/") and not loc.startswith("//")


def test_login_lock_keyed_by_user_and_ip(tmp_db, client):
    for _ in range(5):
        client.post(
            "/login",
            data={"username": "admin", "pin": "0000"},
        )
    with db.get_db() as conn:
        keys = {
            r[0]
            for r in conn.execute("SELECT key FROM login_attempts")
        }
        assert keys  # 有记录
        assert not any(k.startswith("user:") for k in keys)
