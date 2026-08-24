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
from .db_core import *  # noqa: F401,F403 — 公开 API 由 core 汇集
from .db_core import _now  # noqa: F401 — 测试直接调用 db._now()
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
    DETAIL_GROUPS,
    DETAIL_ITEMS,
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
    policy_require_read,
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
