"""编辑权限规则：把「谁能改什么日期的日报」收成纯函数，视图层统一调用。

原先 views_daily 的 POST 分支手写一套判断、GET 又单独算一遍 can_edit，
两套口径容易漂移（GET 没算当天锁定，未来日期两边结论也不一致）。
这里收敛成一个入口：POST 挡写入、GET 只决定表单能否编辑，走同一套判定。

规则（与 README「口径」一致）：
- admin：无条件可改（未来、跨月、锁定都能动）
- 非 admin：不能填未来日期；只能改本月
  - city（地市负责人）：本月任意日期，不受「本月可改」开关和当天锁定限制
  - 填报员：往日补录要开「本月可改」（filler_month）；当天受锁定时间限制
- readonly：不能填

本模块不查库、不依赖请求上下文，只做判定；数据（开关、锁定状态）由调用方传入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .db_core import LOCK_HOUR, LOCK_MINUTE


@dataclass
class EditGate:
    allowed: bool
    reason: str = ""


def daily_edit_gate(
    role: str,
    biz_date: date,
    today: date,
    *,
    filler_month: bool,
    locked: bool = False,
) -> EditGate:
    """判断 role 能否填/改 biz_date 这天的日报。

    locked 由调用方算好传入（正常只对「今天」为真）；非今天日期本函数不看 locked，
    与 db.is_locked 的口径一致。
    """
    if role == "readonly":
        return EditGate(False, "只读账号不能填日报")
    if role == "admin":
        return EditGate(True)
    if biz_date > today:
        return EditGate(False, "非管理员不能填写未来日期。")
    same_month = biz_date.year == today.year and biz_date.month == today.month
    if not same_month:
        return EditGate(False, "只能改本月的日报，历史跨月需找管理员修改。")
    if role == "city":
        return EditGate(True)
    if biz_date != today:
        if not filler_month:
            return EditGate(False, "只能改当天（管理员开启『本月可改』后可补录本月）。")
        return EditGate(True)
    if locked:
        return EditGate(
            False,
            f"当天数据已锁定（{LOCK_HOUR:02d}:{LOCK_MINUTE:02d} 后不可改），找管理员解锁修改。",
        )
    return EditGate(True)
