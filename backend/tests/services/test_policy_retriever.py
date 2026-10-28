from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.db.models import PolicyChunkRecord
from shopcare.services.policy_chunker import split_policy
from shopcare.services.policy_loader import load_policies
from shopcare.services.policy_retriever import (
    list_effective_chunks,
    score_chunk,
    search_policy_chunks,
    tokenize_query,
)
from shopcare.services.policy_store import replace_policy_chunks


@pytest.fixture
def engine(tmp_path):
    db_engine = build_engine(f"sqlite:///{(tmp_path / 'retriever.db').as_posix()}")
    init_db(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def record(policy_id="demo", version="v1", start=0, **overrides):
    fields = dict(
        chunk_id=f"{policy_id}:{version}:{start:03d}",
        policy_id=policy_id,
        version=version,
        title="模拟规则",
        category="数码",
        effective_from=date(2026, 10, 1),
        effective_until=date(2026, 10, 10),
        text="A",
        start_char=start,
        end_char=start + 1,
    )
    fields.update(overrides)
    return PolicyChunkRecord(**fields)


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
def test_sql_candidate_date_boundaries(engine, as_of, expected):
    with Session(engine) as session, session.begin():
        session.add(record())
    with Session(engine) as session:
        rows = list_effective_chunks(session, "数码", as_of)
        assert bool(rows) is expected


@pytest.mark.parametrize(
    "as_of,expected",
    [
        (date(2026, 9, 30), False),
        (date(2026, 10, 1), True),
        (date(2030, 1, 1), True),
    ],
)
def test_sql_open_ended_policy_still_checks_start(engine, as_of, expected):
    with Session(engine) as session, session.begin():
        session.add(record(effective_until=None))
    with Session(engine) as session:
        assert bool(list_effective_chunks(session, "数码", as_of)) is expected


@pytest.mark.parametrize("category,expected", [("数码", 3), ("配件", 3), ("unknown", 0)])
def test_sql_candidates_from_all_sample_versions(engine, category, expected):
    policies = load_policies(Path(__file__).resolve().parents[3] / "data/policies")
    with Session(engine) as session, session.begin():
        for policy in policies:
            replace_policy_chunks(session, policy, split_policy(policy))
    with Session(engine) as session:
        rows = list_effective_chunks(session, category, date(2026, 11, 2))
        assert len(rows) == expected
        assert all(row.category == category for row in rows)
        assert all(
            row.version == "v2"
            for row in rows
            if row.policy_id in {"digital-return", "digital-exchange"}
        )


def test_sql_candidate_sorting(engine):
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                record("z"),
                record("a", "v2", 10),
                record("a", "v1"),
                record("a", "v2"),
            ]
        )
    with Session(engine) as session:
        rows = list_effective_chunks(session, "数码", date(2026, 10, 5))
        assert [(row.policy_id, row.version, row.start_char) for row in rows] == [
            ("a", "v1", 0),
            ("a", "v2", 0),
            ("a", "v2", 10),
            ("z", "v1", 0),
        ]


def test_sql_candidates_empty_database(engine):
    with Session(engine) as session:
        assert list_effective_chunks(session, "数码", date(2026, 11, 2)) == []


@pytest.mark.parametrize(
    "query,expected",
    [
        ("耳机换货", {"耳机", "机换", "换货"}),
        ("API 123 api", {"api", "123"}),
        ("", set()),
        ("  ，！？ ", set()),
        ("退", {"退"}),
        ("退款API", {"退款", "api"}),
    ],
)
def test_query_tokenization(query, expected):
    assert tokenize_query(query) == expected


@pytest.mark.parametrize(
    "title,text,tokens,expected",
    [
        ("API", "Refund API", {"api"}, 1),
        ("耳机", "耳机换货耳机", {"耳机", "换货"}, 2),
        ("换", "货", {"换货"}, 0),
        ("规则", "测试正文", set(), 0),
        ("规则", "测试正文", {"不存在"}, 0),
    ],
)
def test_chunk_scoring(title, text, tokens, expected):
    chunk = record(title=title, text=text)
    assert score_chunk(chunk, tokens) == expected


@pytest.mark.parametrize("top_k", [0, -1, 11, 1.5, "3", None, True, False])
def test_search_rejects_invalid_top_k(engine, top_k):
    with Session(engine) as session:
        with pytest.raises(ValueError):
            search_policy_chunks(session, "耳机", "数码", date(2026, 10, 5), top_k=top_k)


@pytest.mark.parametrize("query", ["", " \n ", "!!!", "😀"])
def test_search_without_tokens_returns_empty(engine, query):
    with Session(engine) as session:
        assert search_policy_chunks(session, query, "数码", date(2026, 10, 5)) == []


def test_search_ranking_top_k_and_zero_score_exclusion(engine):
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                record("low", title="耳机", text="耳机"),
                record("zero", title="unknown", text="unrelated"),
                record("high", title="耳机换货", text="耳机换货"),
            ]
        )
    with Session(engine) as session:
        hits = search_policy_chunks(session, "耳机换货", "数码", date(2026, 10, 5))
        assert [(hit.chunk.policy_id, hit.score) for hit in hits] == [("high", 3), ("low", 1)]
        limited = search_policy_chunks(session, "耳机换货", "数码", date(2026, 10, 5), top_k=1)
        assert [hit.chunk.policy_id for hit in limited] == ["high"]


def test_search_ties_sort_by_chunk_id(engine):
    with Session(engine) as session, session.begin():
        session.add_all([record("z", title="耳机"), record("a", title="耳机")])
    with Session(engine) as session:
        hits = search_policy_chunks(session, "耳机", "数码", date(2026, 10, 5))
        assert [hit.chunk.policy_id for hit in hits] == ["a", "z"]


def test_search_no_matches_returns_empty(engine):
    with Session(engine) as session, session.begin():
        session.add(record(title="耳机", text="换货"))
    with Session(engine) as session:
        assert search_policy_chunks(session, "zzzzunique", "数码", date(2026, 10, 5)) == []


def test_search_applies_date_and_category_before_scoring(engine):
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                record("current", title="耳机"),
                record("expired", title="耳机", effective_until=date(2026, 10, 5)),
                record("future", title="耳机", effective_from=date(2026, 10, 6)),
                record("other", title="耳机", category="配件"),
            ]
        )
    with Session(engine) as session:
        hits = search_policy_chunks(session, "耳机", "数码", date(2026, 10, 5))
        assert [hit.chunk.policy_id for hit in hits] == ["current"]


def test_search_results_survive_session_close(engine):
    with Session(engine) as session, session.begin():
        session.add(record(title="耳机"))
    with Session(engine) as session:
        hits = search_policy_chunks(session, "耳机", "数码", date(2026, 10, 5))
    engine.dispose()
    payload = hits[0].model_dump(mode="json")
    assert payload["chunk"]["effective_from"] == "2026-10-01"
    assert payload["score"] == 1


def test_search_real_sample_exchange_evidence(engine):
    policies = load_policies(Path(__file__).resolve().parents[3] / "data/policies")
    with Session(engine) as session, session.begin():
        for policy in policies:
            replace_policy_chunks(session, policy, split_policy(policy))
    with Session(engine) as session:
        hits = search_policy_chunks(session, "耳机单侧无声换货", "数码", date(2026, 11, 2), top_k=1)
        assert len(hits) == 1
        assert hits[0].chunk.policy_id == "digital-exchange"
        assert hits[0].chunk.version == "v2"
