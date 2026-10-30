from datetime import date
from unittest.mock import Mock

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.db.models import PolicyChunkRecord, PolicyEmbeddingRecord
from shopcare.services.semantic_retriever import search_semantic_policy_chunks

AS_OF = date(2026, 10, 30)


@pytest.fixture
def engine(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'semantic.db').as_posix()}")
    init_db(engine)
    try:
        with Session(engine) as session, session.begin():
            for name, category, start, end in [
                ("B", "数码", date(2026, 10, 1), None),
                ("A", "数码", date(2026, 10, 1), None),
                ("C", "数码", date(2026, 10, 1), None),
                ("other", "配件", date(2026, 10, 1), None),
                ("expired", "数码", date(2026, 1, 1), AS_OF),
                ("future", "数码", date(2026, 11, 1), None),
            ]:
                session.add(
                    PolicyChunkRecord(
                        chunk_id=name,
                        policy_id=name,
                        version="v1",
                        title=name,
                        category=category,
                        effective_from=start,
                        effective_until=end,
                        text="政策正文",
                        start_char=0,
                        end_char=4,
                    )
                )
            session.flush()
            # Filtered-out chunks deliberately have no vectors.
            for name, vector in [("A", [1.0, 0.0]), ("B", [1.0, 0.0]), ("C", [-1.0, 0.0])]:
                session.add(
                    PolicyEmbeddingRecord(
                        chunk_id=name,
                        model_id="test-model",
                        dimensions=2,
                        vector=vector,
                    )
                )
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def client():
    client = Mock()
    client.model = "test-model"
    client.dimensions = 2
    client.embed_query.return_value = [1.0, 0.0]
    return client


def test_ranking_tie_break_filtering_and_source(engine, client):
    with Session(engine) as session:
        hits = search_semantic_policy_chunks(session, client, "想换货", "数码", AS_OF)
    assert [hit.chunk.chunk_id for hit in hits] == ["A", "B", "C"]
    assert [hit.score for hit in hits] == pytest.approx([1.0, 1.0, -1.0])
    assert hits[0].chunk.text == "政策正文"
    assert hits[0].model_dump(mode="json")["chunk"]["effective_from"] == "2026-10-01"
    client.embed_query.assert_called_once_with("想换货")


def test_top_k_limits_ranked_results(engine, client):
    with Session(engine) as session:
        hits = search_semantic_policy_chunks(session, client, "换货", "数码", AS_OF, 1)
    assert [hit.chunk.chunk_id for hit in hits] == ["A"]


@pytest.mark.parametrize("top_k", [0, 11, True, 1.5, "3"])
def test_invalid_top_k_does_not_call_model(engine, client, top_k):
    with Session(engine) as session, pytest.raises(ValueError):
        search_semantic_policy_chunks(session, client, "换货", "数码", AS_OF, top_k)
    client.embed_query.assert_not_called()


@pytest.mark.parametrize("query,category", [("  ", "数码"), ("换货", "不存在")])
def test_empty_query_or_candidates_do_not_call_model(engine, client, query, category):
    with Session(engine) as session:
        assert search_semantic_policy_chunks(session, client, query, category, AS_OF) == []
    client.embed_query.assert_not_called()


@pytest.mark.parametrize("mode", ["missing", "model", "dimensions", "unconfigured"])
def test_index_errors_fail_before_model_call(engine, client, mode):
    if mode == "model":
        client.model = "other-model"
    elif mode == "dimensions":
        client.dimensions = 3
    elif mode == "unconfigured":
        client.dimensions = None
    else:
        with Session(engine) as session, session.begin():
            session.execute(
                delete(PolicyEmbeddingRecord).where(PolicyEmbeddingRecord.chunk_id == "B")
            )
    with Session(engine) as session, pytest.raises(ValueError):
        search_semantic_policy_chunks(session, client, "换货", "数码", AS_OF)
    client.embed_query.assert_not_called()


@pytest.mark.parametrize(
    "vector",
    [
        [],
        [1.0],
        [0.0, 0.0],
        [float("nan"), 1.0],
        [float("inf"), 1.0],
        [True, 1.0],
        ["1", 1.0],
    ],
)
def test_invalid_query_vector_is_rejected(engine, client, vector):
    client.embed_query.return_value = vector
    with Session(engine) as session, pytest.raises(ValueError):
        search_semantic_policy_chunks(session, client, "换货", "数码", AS_OF)


def test_provider_timeout_propagates(engine, client):
    client.embed_query.side_effect = TimeoutError("simulated timeout")
    with Session(engine) as session, pytest.raises(TimeoutError):
        search_semantic_policy_chunks(session, client, "换货", "数码", AS_OF)
