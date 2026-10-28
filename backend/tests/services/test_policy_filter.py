from datetime import date
from pathlib import Path

import pytest

from shopcare.schemas.policy_models import PolicyDocument
from shopcare.services.policy_filter import filter_applicable_policies, is_policy_effective
from shopcare.services.policy_loader import load_policies


def make_policy(**overrides):
    values = dict(
        policy_id="demo",
        version="v1",
        title="模拟政策",
        category="数码",
        effective_from=date(2026, 10, 1),
        effective_until=date(2026, 10, 10),
        content="仅用于测试的政策正文。",
    )
    values.update(overrides)
    return PolicyDocument(**values)


@pytest.mark.parametrize(
    "as_of,expected",
    [
        (date(2026, 9, 30), False),
        (date(2026, 10, 1), True),
        (date(2026, 10, 9), True),
        (date(2026, 10, 10), False),
        (date(2026, 10, 11), False),
    ],
)
def test_bounded_policy_date_boundaries(as_of, expected):
    assert is_policy_effective(make_policy(), as_of) is expected


@pytest.mark.parametrize(
    "as_of,expected",
    [
        (date(2026, 9, 30), False),
        (date(2026, 10, 1), True),
        (date(2030, 1, 1), True),
    ],
)
def test_open_ended_policy_still_requires_start(as_of, expected):
    assert is_policy_effective(make_policy(effective_until=None), as_of) is expected


@pytest.mark.parametrize("category,count", [("数码", 3), ("配件", 3), ("unknown", 0)])
def test_sample_category_and_date_filter(category, count):
    policies = load_policies(Path(__file__).resolve().parents[3] / "data/policies")
    result = filter_applicable_policies(policies, category, date(2026, 11, 2))
    assert len(result) == count
    assert all(p.category == category for p in result)
    assert all(is_policy_effective(p, date(2026, 11, 2)) for p in result)


@pytest.mark.parametrize(
    "as_of,version",
    [
        (date(2026, 9, 30), "v1"),
        (date(2026, 10, 1), "v2"),
    ],
)
def test_sample_version_cutover_has_no_overlap(as_of, version):
    policies = load_policies(Path(__file__).resolve().parents[3] / "data/policies")
    result = filter_applicable_policies(policies, "数码", as_of)
    returns = [p for p in result if p.policy_id == "digital-return"]
    assert len(returns) == 1
    assert returns[0].version == version


def test_filter_preserves_input_order_and_original_documents():
    policies = [
        make_policy(policy_id="z"),
        make_policy(policy_id="expired", effective_until=date(2026, 10, 4)),
        make_policy(policy_id="other", category="配件"),
        make_policy(policy_id="a"),
    ]
    before = [p.model_dump() for p in policies]
    result = filter_applicable_policies(policies, "数码", date(2026, 10, 5))
    assert [p.policy_id for p in result] == ["z", "a"]
    assert [p.model_dump() for p in policies] == before
    assert result is not policies


def test_filter_empty_input():
    assert filter_applicable_policies([], "数码", date(2026, 11, 2)) == []
