"""指标字典与播报版式。只改这里，表单和播报会一起变。"""

from __future__ import annotations

from typing import Dict, List, Tuple

# 播报分组：与现有微信群格式对齐
# header 为 None 表示基础项，直接跟在店名后面
SECTIONS: List[Dict] = [
    {
        "code": "basic",
        "header": None,
        "blank_before": False,
        "metrics": [
            ("phone_sales", "当天手机销量", ""),
            ("id_check", "查询身份证数", ""),
            ("lead", "商机录入", ""),
            ("reserve", "储备", ""),
        ],
    },
    {
        "code": "focus",
        "header": "重点业务",
        "blank_before": True,
        "metrics": [
            ("bisuan", "比算新增", "纯新或低消迎回，有效插卡，ARPU>0"),
            ("bisuan_high", "比算新增[高]", "上面这类里，折后主套105以上"),
            ("ai_contract", "Ai手机合约", "店内考核项；结算文件没有单独科目"),
        ],
    },
    {
        "code": "new_card",
        "header": "新增类",
        "blank_before": True,
        "metrics": [
            ("anxin_sub", "安心/副卡", "非主用卡：安心小号 / 副卡 / 畅享卡"),
            ("other_card", "其他卡类", ""),
        ],
    },
    {
        "code": "family",
        "header": "家庭类",
        "blank_before": True,
        "metrics": [
            ("broadband", "宽带", "新装竣工一笔；续费走「宽带续费」；自倒、换号可能结算为0"),
            ("broadband_renew", "宽带续费", "到期续费 / 转融合；新装走「宽带」"),
            ("tv", "电视", "竣工且有登录"),
            ("tv_member", "电视会员", "竣工且有登录"),
            ("wifi", "普通组网", "不含FTTR；组网竣工且有登录"),
            ("fttr", "FTTR", "全光组网；单主光猫可能不结算"),
            ("gigabit", "千兆", ""),
            ("security", "安防", ""),
        ],
    },
    {
        "code": "contract",
        "header": "终端合约",
        "blank_before": True,
        "metrics": [
            ("coin_cut_old", "老用户直降", "不算进考核"),
            ("coin_cut_new_recharge", "新用户直降·充值", "考核看四项合计"),
            ("coin_cut_new_sesame", "新用户直降·芝麻免充", "考核看四项合计"),
            ("coin_cut_new_savings", "新用户直降·储蓄卡冻结", "考核看四项合计"),
            ("coin_cut_new_full", "新用户直降·全品类", "考核看四项合计"),
            ("coin_cut_xtc", "小天才直降", "不算进考核"),
            ("phone_discount", "购机让利", ""),
            ("gift_2g", "送2G流量", ""),
            ("welcome_back", "底部迎回", ""),
        ],
    },
    {
        "code": "digital",
        "header": "数字化",
        "blank_before": True,
        "metrics": [
            ("fangzha", "防诈宝", ""),
            ("he_msg", "和留言", ""),
            ("cloud_disk", "云盘", ""),
            ("direct_pack", "定向包", ""),
            ("crbt", "彩铃", ""),
            ("migu", "咪咕视频", ""),
            ("safe_mgr", "安全管家", ""),
            ("watch_pack", "观赛包", ""),
            ("addon", "叠加包", ""),
            ("fund", "基金通", ""),
            ("health", "健康无忧", ""),
            ("pet", "萌宠无忧", ""),
            ("new_call", "新通话", ""),
            ("renwoxuan", "任我选会员", ""),
            ("min_spend", "个人/全家保底", ""),
        ],
    },
]


def all_metrics() -> List[Tuple[str, str, str, int]]:
    """(code, name, section, sort)."""
    rows: List[Tuple[str, str, str, int]] = []
    sort = 10
    for section in SECTIONS:
        for code, name, _hint in section["metrics"]:
            rows.append((code, name, section["code"], sort))
            sort += 10
    return rows


def metric_name_map() -> Dict[str, str]:
    return {code: name for code, name, _section, _sort in all_metrics()}


def metric_codes() -> List[str]:
    return [code for code, _name, _section, _sort in all_metrics()]


def section_by_code(code: str) -> Dict:
    for section in SECTIONS:
        if section["code"] == code:
            return section
    raise KeyError(code)


COIN_NEW_PARTS = (
    "coin_cut_new_recharge",
    "coin_cut_new_sesame",
    "coin_cut_new_savings",
    "coin_cut_new_full",
)
COIN_ALL_PARTS = ("coin_cut_old",) + COIN_NEW_PARTS + ("coin_cut_xtc",)

# 群播报把老用户/新用户/全品类/小天才合成「金币直降」一行；月指标只计新用户四项
ROLLUPS = {
    "coin_cut": {
        "name": "新用户直降",
        "parts": COIN_NEW_PARTS,
        "legacy": ("coin_cut_new",),
    },
    "coin_cut_all": {
        "name": "金币直降",
        "parts": COIN_ALL_PARTS,
        "legacy": ("coin_cut", "coin_cut_new"),
    },
    "bisuan_total": {
        "name": "比算新增",
        "parts": ("bisuan", "bisuan_high"),
        "legacy": (),
    },
}

# 月指标只盯这三项；目标存在 kpi_targets
KPI_TARGETS = (
    ("bisuan_total", "比算新增", "日常分「比算新增」和「比算新增[高]」填，考核看合计"),
    ("ai_contract", "Ai手机合约", ""),
    ("coin_cut", "金币直降", "只计新用户：充值 + 芝麻免充 + 储蓄卡冻结 + 全品类"),
)


def rollup_pair(values: Dict, key: str) -> Tuple[int, int]:
    spec = ROLLUPS[key]
    codes = spec["parts"] + spec["legacy"]
    day = sum(int((values.get(code) or (0, 0))[0] or 0) for code in codes)
    cum = sum(int((values.get(code) or (0, 0))[1] or 0) for code in codes)
    return day, cum


def rollup_amount(values: Dict[str, int], key: str) -> int:
    spec = ROLLUPS[key]
    codes = spec["parts"] + spec["legacy"]
    return sum(int(values.get(code, 0) or 0) for code in codes)
