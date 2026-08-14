"""一次性回填：如皋宁海路体验店 8/13 当天 + 本月截至 8/12 的累计。

8/1 记的是「月初到昨天」的合计数，不是真的 8/1 单日。
只在该店还没有 8 月数据时写入，避免覆盖已填日报。
"""

from datetime import date

from app import db


AUG1 = date(2026, 8, 1)
AUG13 = date(2026, 8, 13)

# 8/13 当天
TODAY = {
    "phone_sales": 1,
    "id_check": 1,
    "lead": 0,
    "reserve": 0,
    "bisuan": 0,
    "bisuan_high": 3,
    "ai_contract": 0,
    "anxin_sub": 0,
    "other_card": 0,
    "broadband": 3,
    "tv": 3,
    "fttr": 0,
    "gigabit": 0,
    "security": 0,
    "coin_cut_old": 0,
    "phone_discount": 2,
    "gift_2g": 0,
    "welcome_back": 0,
    "renwoxuan": 0,
    "min_spend": 0,
    "fangzha": 0,
    "he_msg": 0,
    "cloud_disk": 0,
    "direct_pack": 1,
    "crbt": 0,
    "migu": 0,
    "safe_mgr": 0,
    "watch_pack": 0,
    "addon": 0,
    "fund": 0,
    "health": 0,
    "pet": 0,
    "new_call": 0,
}

# 播报里的累 − 当天 = 8/1 回填
PRIOR = {
    "phone_sales": 12,
    "id_check": 3,
    "lead": 9,
    "reserve": 2,
    "bisuan": 5,
    "bisuan_high": 0,
    "other_card": 5,
    "broadband": 7,
    "tv": 7,
    "fttr": 1,
    "gigabit": 2,
    "coin_cut_old": 1,
    "phone_discount": 6,
    "gift_2g": 1,
    "fangzha": 3,
    "he_msg": 3,
    "crbt": 2,
}


def main() -> None:
    db.init_db()
    with db.get_db() as conn:
        store = conn.execute("SELECT * FROM stores WHERE code='rg-ninghai'").fetchone()
        if store is None:
            raise SystemExit("没有宁海路店")
        user = conn.execute("SELECT * FROM users WHERE username='admin'").fetchone()
        exists = conn.execute(
            "SELECT 1 FROM daily_reports WHERE store_id=? AND biz_date LIKE '2026-08-%'",
            (store["id"],),
        ).fetchone()
        if exists:
            print("8 月已有数据，跳过回填，避免覆盖。")
            return
        db.save_daily(
            conn,
            store_id=store["id"],
            biz_date=AUG1,
            values=PRIOR,
            user_id=user["id"],
            note="月初至 8/12 累计回填，不是 8/1 单日",
        )
        db.save_daily(
            conn,
            store_id=store["id"],
            biz_date=AUG13,
            values=TODAY,
            user_id=user["id"],
            note="微信群 8/13 播报回填",
        )
        print("已写入 8/1 回填 + 8/13 当天。打开今日填报选 2026-08-13 即可复制原文。")


if __name__ == "__main__":
    main()
