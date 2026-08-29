"""门店数据层 — 门面：把 db_core / db_query / db_deals 汇成一个 db 命名空间。

外部一律 `from . import db` 然后 `db.xxx`，本模块只 re-export，
不包含业务逻辑。拆分（无环分层）：
- db_core  底层叶子：连接/常量/schema/迁移/种子/设置/用户/门店 CRUD/基础查询
- db_query 聚合查询：月累计/日值/保存/校准/区间事实/看板
- db_deals 成交播报：记录/去重/计数/列表/删除
为什么拆：原 db.py 1277 行揉在一起；这三块依赖方向单向（query/deals 只依赖 core），
拆分后改查询或成交逻辑不用动 schema/迁移核心。
"""

from __future__ import annotations

from contextlib import contextmanager

from flask import g, has_app_context

from .db_advances import (  # noqa: F401
    advance_month_totals,
    advance_today_inbox,
    cents_to_yuan,
    count_advances,
    delete_advance,
    get_advance,
    list_advances,
    list_all_advances,
    money_ok,
    parse_money,
    record_advance,
    set_advance_paid,
    yuan_to_cents,
)
from .db_bisuan_mobile import (  # noqa: F401
    bisuan_mobile_asof_map,
    bisuan_mobile_map,
    get_bisuan_mobile,
    list_bisuan_mobile_edits,
    save_bisuan_mobile,
)
from .db_core import *  # noqa: F401,F403 — 公开 API 由 core 汇集
from .db_core import _now  # noqa: F401 — 测试直接调用 db._now()
from .db_core import connect as _connect
from .db_core import get_db as _plain_get_db
from .db_deals import (  # noqa: F401
    _deal_payload,
    count_deal_posts,
    deal_counts,
    delete_deal_post,
    get_deal_post,
    list_all_deal_posts,
    list_deal_posts,
    record_deal_post,
)
from .db_invoice import (  # noqa: F401
    delete_invoice_month,
    get_invoice_month,
    invoice_diff,
    list_invoice_months,
    save_invoice_from_form,
    save_invoice_month,
)
from .db_policies import (  # noqa: F401
    ack_map,
    delete_policy,
    get_policy,
    list_policies,
    list_revisions,
    mark_policy_read,
    policy_read_status,
    policy_require_read,
    previous_revision_body,
    render_policy_diff,
    restore_policy_revision,
    sanitize_policy_html,
    save_policy,
    set_policy_active,
    set_policy_require_read,
    unread_policies,
)
from .db_query import (  # noqa: F401
    dashboard_today,
    day_values,
    facts_in_range,
    month_cum_through,
    prev_month_cum,
    range_metric_totals,
    save_daily,
    set_day_value,
    stores_reported_in_month,
    week_metric_total,
)
from .sesame import (  # noqa: F401
    classify_sesame_rows,
    import_sesame_rows,
    parse_sesame_xlsx,
)
from .wecom import (  # noqa: F401
    get_webhook,
    send_test,
    send_text,
)


@contextmanager
def get_db():
    """请求内复用一条连接（挂在 flask.g 上），替代「一开一关」的原始 get_db。

    一个请求里 load_user / 政策检查 / 品牌设置 / 路由各自开连接是纯浪费，
    现在整条请求共用一条。语义与原版一致：
    - 最外层 with 块退出才 commit（异常则 rollback）；嵌套块（如渲染模板时的
      context processor）只复用连接、不动事务，避免里层把外层未提交的写提前提交
    - 连接由 web.py 的 teardown_appcontext 统一关闭
    - 没有 app 上下文（脚本、init_db/migrate、测试直连）时行为与原来完全相同
    """
    if not has_app_context():
        with _plain_get_db() as conn:
            yield conn
        return
    conn = getattr(g, "_db_conn", None)
    if conn is None:
        conn = _connect()
        g._db_conn = conn
    depth = getattr(g, "_db_depth", 0)
    g._db_depth = depth + 1
    try:
        yield conn
    except Exception:
        if depth == 0:
            conn.rollback()
        raise
    else:
        if depth == 0:
            conn.commit()
    finally:
        g._db_depth = depth
