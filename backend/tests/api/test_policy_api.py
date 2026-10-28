from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.main import app, settings
from shopcare.services.policy_chunker import split_policy
from shopcare.services.policy_loader import load_policies
from shopcare.services.policy_store import replace_policy_chunks


@pytest.fixture
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'policies-api.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    policies = load_policies(Path(__file__).resolve().parents[3] / "data/policies")
    engine = build_engine(url)
    init_db(engine)
    try:
        with Session(engine) as session, session.begin():
            for policy in policies:
                replace_policy_chunks(session, policy, split_policy(policy))
    finally:
        engine.dispose()
    with TestClient(app) as test_client:
        yield test_client


def params(**overrides):
    values = {"q": "耳机单侧无声换货", "category": "数码", "as_of": "2026-11-02", "top_k": 1}
    values.update(overrides)
    return values


def test_policy_search_api_returns_citable_evidence(client):
    response = client.get("/api/policies/search", params=params())
    assert response.status_code == 200
    hits = response.json()
    assert len(hits) == 1
    hit = hits[0]
    assert set(hit) == {"chunk", "score"}
    assert hit["score"] >= 1
    chunk = hit["chunk"]
    assert chunk["policy_id"] == "digital-exchange"
    assert chunk["version"] == "v2"
    assert chunk["category"] == "数码"
    source = next(
        p
        for p in load_policies(Path(__file__).resolve().parents[3] / "data/policies")
        if (p.policy_id, p.version) == (chunk["policy_id"], chunk["version"])
    )
    assert chunk["text"] == source.content[chunk["start_char"] : chunk["end_char"]]
    assert date.fromisoformat(chunk["effective_from"]) <= date(2026, 11, 2)


@pytest.mark.parametrize("as_of,version", [("2026-09-30", "v1"), ("2026-10-01", "v2")])
def test_policy_api_historical_cutover(client, as_of, version):
    response = client.get("/api/policies/search", params=params(as_of=as_of))
    assert response.status_code == 200
    assert response.json()[0]["chunk"]["policy_id"] == "digital-exchange"
    assert response.json()[0]["chunk"]["version"] == version


@pytest.mark.parametrize(
    "overrides",
    [
        {"q": "zzzzuniquenomatch"},
        {"q": "!!!"},
        {"category": "unknown"},
    ],
)
def test_policy_api_no_evidence_returns_empty_list(client, overrides):
    response = client.get("/api/policies/search", params=params(**overrides))
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    "case",
    [
        "missing_q",
        "empty_q",
        "long_q",
        "missing_category",
        "empty_category",
        "missing_date",
        "bad_date",
        "zero_k",
        "large_k",
        "bad_k",
    ],
)
def test_policy_api_invalid_parameters(client, case):
    values = params()
    if case == "missing_q":
        values.pop("q")
    elif case == "empty_q":
        values["q"] = ""
    elif case == "long_q":
        values["q"] = "x" * 501
    elif case == "missing_category":
        values.pop("category")
    elif case == "empty_category":
        values["category"] = ""
    elif case == "missing_date":
        values.pop("as_of")
    elif case == "bad_date":
        values["as_of"] = "invalid-date"
    elif case == "zero_k":
        values["top_k"] = 0
    elif case == "large_k":
        values["top_k"] = 11
    elif case == "bad_k":
        values["top_k"] = "not-an-integer"
    response = client.get("/api/policies/search", params=values)
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "请求参数格式不正确",
        }
    }


def test_policy_api_default_limit_and_score_order(client):
    values = params(q="数码商品可以换货")
    values.pop("top_k")
    response = client.get("/api/policies/search", params=values)
    assert response.status_code == 200
    hits = response.json()
    assert 0 < len(hits) <= 3
    assert [(-h["score"], h["chunk"]["chunk_id"]) for h in hits] == sorted(
        (-h["score"], h["chunk"]["chunk_id"]) for h in hits
    )
