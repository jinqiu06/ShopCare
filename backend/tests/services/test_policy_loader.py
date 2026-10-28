import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from shopcare.schemas.policy_models import PolicyDocument
from shopcare.services.policy_loader import load_policies, load_policy


def policy_data(**overrides):
    values = dict(
        policy_id="demo",
        version="v1",
        title="模拟规则",
        category="数码",
        effective_from="2026-10-01",
        effective_until=None,
        content="模拟政策正文。",
    )
    values.update(overrides)
    return values


def write_policy(path, **overrides):
    path.write_text(json.dumps(policy_data(**overrides), ensure_ascii=False), encoding="utf-8")
    return path


def test_load_repository_samples():
    directory = Path(__file__).resolve().parents[3] / "data" / "policies"
    policies = load_policies(directory)
    assert len(policies) == 8
    assert len({(p.policy_id, p.version) for p in policies}) == 8
    assert {p.version for p in policies if p.policy_id == "digital-return"} == {"v1", "v2"}


def test_single_policy_dates_chinese_and_original_content(tmp_path):
    content = "\n  中文规则原文。\n"
    policy = load_policy(write_policy(tmp_path / "one.json", content=content))
    assert policy.title == "模拟规则"
    assert policy.effective_from == date(2026, 10, 1)
    assert policy.effective_until is None
    assert policy.content == content


def test_empty_directory(tmp_path):
    assert load_policies(tmp_path) == []


def test_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_policies(tmp_path / "missing")


def test_file_is_not_directory(tmp_path):
    path = write_policy(tmp_path / "one.json")
    with pytest.raises(NotADirectoryError):
        load_policies(path)


def test_only_top_level_json_files(tmp_path):
    write_policy(tmp_path / "one.json")
    (tmp_path / "notes.txt").write_text("not a policy", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    write_policy(nested / "two.json", policy_id="nested")
    assert [p.policy_id for p in load_policies(tmp_path)] == ["demo"]


def test_duplicate_version_reports_file(tmp_path):
    write_policy(tmp_path / "a.json")
    write_policy(tmp_path / "b.json")
    with pytest.raises(ValueError, match="b.json"):
        load_policies(tmp_path)


def test_same_policy_different_versions_allowed(tmp_path):
    write_policy(tmp_path / "v1.json")
    write_policy(tmp_path / "v2.json", version="v2")
    assert [p.version for p in load_policies(tmp_path)] == ["v1", "v2"]


def test_filename_order_is_stable(tmp_path):
    write_policy(tmp_path / "z.json", policy_id="last")
    write_policy(tmp_path / "a.json", policy_id="first")
    assert [p.policy_id for p in load_policies(tmp_path)] == ["first", "last"]


def test_invalid_json_reports_file_and_preserves_cause(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="broken.json") as error:
        load_policies(tmp_path)
    assert isinstance(error.value.__cause__, ValidationError)


def test_missing_required_field_rejected(tmp_path):
    values = policy_data()
    del values["category"]
    path = tmp_path / "missing-field.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(ValueError, match="missing-field.json"):
        load_policy(path)


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        PolicyDocument.model_validate(policy_data(unexpected="not allowed"))


def test_invalid_date_rejected():
    with pytest.raises(ValidationError):
        PolicyDocument.model_validate(policy_data(effective_from="not-a-date"))


@pytest.mark.parametrize("field", ["policy_id", "version", "title", "category", "content"])
def test_blank_field_rejected(field):
    with pytest.raises(ValidationError):
        PolicyDocument.model_validate(policy_data(**{field: " \t\n "}))


@pytest.mark.parametrize("end", ["2026-09-30", "2026-10-01"])
def test_invalid_date_range_rejected(end):
    with pytest.raises(ValidationError):
        PolicyDocument.model_validate(policy_data(effective_until=end))
