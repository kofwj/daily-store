from app.incentive import DEFAULTS, judge, judge_with_advisor, judge_without_advisor, money_text


def test_with_advisor_table():
    assert judge_with_advisor(3, 7)["store_reward"] == 500
    assert judge_with_advisor(2, 8)["store_reward"] == 200
    row = judge_with_advisor(0, 10)
    assert row["store_reward"] == 0
    assert row["advisor_penalty"] == 100
    row = judge_with_advisor(5, 2)
    assert row["store_penalty"] == 200
    assert row["advisor_penalty"] == 0
    row = judge_with_advisor(2, 3)
    assert row["store_penalty"] == 100
    assert row["advisor_penalty"] == 50
    row = judge_with_advisor(0, 4)
    assert row["store_penalty"] == 200
    assert row["advisor_penalty"] == 100


def test_without_advisor_table():
    assert judge_without_advisor(1, 1)["store_reward"] == 200
    assert judge_without_advisor(2, 0)["store_penalty"] == 50
    assert judge_without_advisor(0, 3)["store_penalty"] == 50
    assert judge_without_advisor(0, 0)["store_penalty"] == 100


def test_custom_rules_change_threshold():
    rules = dict(DEFAULTS)
    rules["total_threshold"] = 12
    rules["reward_best"] = 800
    assert judge(True, 3, 7, rules)["passed"] is False
    assert judge(True, 5, 7, rules)["store_reward"] == 800


def test_money_text():
    assert money_text(judge(True, 3, 7)) == "奖门店 500"
    assert money_text(judge(False, 0, 0)) == "罚门店 100"
    assert "罚门店 100" in money_text(judge(True, 2, 3))
    assert "罚顾问 50" in money_text(judge(True, 2, 3))
