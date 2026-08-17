"""示例门店目录。真实门店数据放 stores_seed_local.py（不进 git）。

启动时优先加载 local，没有就用这里的示例数据。
示例数据用假名假地址，能跑全部测试。
"""

from __future__ import annotations

from typing import Dict, List, Optional

# 顺序即看板 / 通报 / 下拉顺序。
STORES: List[Dict] = [
    {
        "code": "store-alpha",
        "login": "alpha",
        "short_name": "示例甲店",
        "name": "示例市甲街vivo体验店",
        "region_group": "示例",
        "city": "示例市",
        "mobile_code": "10000001",
        "area_manager": "张管理",
        "store_manager": "王店长",
        "follow_ai": True,
        "follow_bisuan": True,
        "store_grade": "A",
        "ai_target": 10,
    },
    {
        "code": "store-beta",
        "login": "beta",
        "short_name": "示例乙店",
        "name": "示例市乙街vivo专卖店",
        "region_group": "示例",
        "city": "示例市",
        "mobile_code": "10000002",
        "area_manager": "张管理",
        "store_manager": "李店长",
        "follow_ai": True,
        "follow_bisuan": False,
        "store_grade": "A",
        "ai_target": 10,
    },
    {
        "code": "store-gamma",
        "login": "gamma",
        "short_name": "示例丙店",
        "name": "示例市丙路vivo体验店",
        "region_group": "示例",
        "city": "示例市",
        "mobile_code": "",
        "area_manager": "张管理",
        "store_manager": "",
        "follow_ai": False,
        "follow_bisuan": False,
        "store_grade": "B",
        "ai_target": 2,
    },
    {
        "code": "store-delta",
        "login": "delta",
        "short_name": "示例丁店",
        "name": "示例市丁路vivo体验店",
        "region_group": "示例",
        "city": "邻市",
        "mobile_code": "10000003",
        "area_manager": "刘经理",
        "store_manager": "陈店长",
        "follow_ai": True,
        "follow_bisuan": False,
        "store_grade": "B",
        "ai_target": 10,
    },
    {
        "code": "store-epsilon",
        "login": "epsilon",
        "short_name": "示例戊店",
        "name": "邻市戊路vivo体验店",
        "region_group": "示例",
        "city": "邻市",
        "mobile_code": "10000004",
        "area_manager": "刘经理",
        "store_manager": "赵店长",
        "follow_ai": False,
        "follow_bisuan": True,
        "store_grade": "A",
        "ai_target": 10,
    },
    {
        "code": "store-zeta",
        "login": "zeta",
        "short_name": "示例巳店",
        "name": "邻市巳街vivo专卖店",
        "region_group": "示例",
        "city": "邻市",
        "mobile_code": "",
        "area_manager": "刘经理",
        "store_manager": "",
        "follow_ai": False,
        "follow_bisuan": False,
        "store_grade": "B",
        "ai_target": 2,
    },
]

NINGHAI_CODE = "store-epsilon"
PROFILE_FIELDS = (
    "region_group",
    "city",
    "mobile_code",
    "area_manager",
    "store_manager",
    "follow_ai",
    "follow_bisuan",
    "store_grade",
    "ai_target",
)


def store_by_code(code: str) -> Dict:
    for item in STORES:
        if item["code"] == code:
            return item
    raise KeyError(code)


def store_name(code: str) -> str:
    return store_by_code(code)["name"]


def store_codes() -> List[str]:
    return [item["code"] for item in STORES]


def store_names() -> List[str]:
    return [item["name"] for item in STORES]


def filler_accounts() -> List[Dict]:
    return [
        {
            "login": item["login"],
            "short_name": item["short_name"],
            "code": item["code"],
            "name": item["name"],
        }
        for item in STORES
    ]


def find_store(code: Optional[str] = None, name: Optional[str] = None) -> Optional[Dict]:
    for item in STORES:
        if code and item["code"] == code:
            return item
        if name and item["name"] == name:
            return item
    return None
