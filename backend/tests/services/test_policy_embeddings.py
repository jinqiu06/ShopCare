from datetime import date
from unittest.mock import Mock

import pytest
from langchain_openai import OpenAIEmbeddings
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.db.models import PolicyChunkRecord, PolicyEmbeddingRecord
from shopcare.services.policy_embeddings import embed_policy_chunks, save_policy_embeddings


def make_chunks():
    return [
        PolicyChunkRecord(chunk_id="second:v1:000", title="换货", text="质量问题可换货"),
        PolicyChunkRecord(chunk_id="first:v1:000", title="退货", text="退货要求"),
    ]


def make_client(dimensions=2, vectors=None):
    client = Mock(spec=OpenAIEmbeddings)
    client.dimensions = dimensions
    client.embed_documents.return_value = [[1.0, 0.0], [0.0, 1.0]] if vectors is None else vectors
    return client


def test_text_and_vector_order_are_preserved():
    client = make_client()
    vectors = embed_policy_chunks(client, make_chunks())
    client.embed_documents.assert_called_once_with(["换货\n质量问题可换货", "退货\n退货要求"])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]


def test_empty_chunks_do_not_call_provider():
    client = make_client(dimensions=None)
    assert embed_policy_chunks(client, []) == []
    client.embed_documents.assert_not_called()


def test_missing_dimensions_fails_before_provider_call():
    client = make_client(dimensions=None)
    with pytest.raises(ValueError, match="未配置向量维度"):
        embed_policy_chunks(client, make_chunks())
    client.embed_documents.assert_not_called()


def test_wrong_vector_count_is_rejected():
    client = make_client(vectors=[[1.0, 0.0]])
    with pytest.raises(ValueError, match="向量数量"):
        embed_policy_chunks(client, make_chunks())


def test_wrong_dimension_in_later_vector_is_rejected():
    client = make_client(vectors=[[1.0, 0.0], [1.0]])
    with pytest.raises(ValueError, match="向量维度不正确"):
        embed_policy_chunks(client, make_chunks())


def test_provider_failure_is_not_disguised_as_empty_results():
    client = make_client()
    client.embed_documents.side_effect = TimeoutError("simulated timeout")
    with pytest.raises(TimeoutError, match="simulated timeout"):
        embed_policy_chunks(client, make_chunks())


@pytest.fixture
def engine(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'embeddings.db').as_posix()}")
    init_db(engine)
    try:
        with Session(engine) as session, session.begin():
            for chunk in make_chunks():
                chunk.policy_id = chunk.chunk_id.split(":")[0]
                chunk.version = "v1"
                chunk.category = "数码"
                chunk.effective_from = date(2026, 10, 1)
                chunk.start_char = 0
                chunk.end_char = len(chunk.text)
                session.add(chunk)
            session.flush()
            for chunk_id in ["first:v1:000", "second:v1:000"]:
                session.add(
                    PolicyEmbeddingRecord(
                        chunk_id=chunk_id,
                        model_id="old-model",
                        dimensions=2,
                        vector=[1.0, 0.0],
                    )
                )
        yield engine
    finally:
        engine.dispose()


def stored_embeddings(session):
    rows = session.scalars(
        select(PolicyEmbeddingRecord).order_by(PolicyEmbeddingRecord.chunk_id)
    ).all()
    return [(row.chunk_id, row.model_id, row.dimensions, row.vector) for row in rows]


def test_save_batch_repeatedly_preserves_pairing_and_count(engine):
    for _ in range(2):
        with Session(engine) as session, session.begin():
            chunks = session.scalars(
                select(PolicyChunkRecord).order_by(PolicyChunkRecord.chunk_id)
            ).all()
            assert (
                save_policy_embeddings(
                    session, list(chunks), [[0.0, 1.0], [-1.0, 0.0]], "new-model", 2
                )
                == 2
            )
    with Session(engine) as session:
        assert stored_embeddings(session) == [
            ("first:v1:000", "new-model", 2, [0.0, 1.0]),
            ("second:v1:000", "new-model", 2, [-1.0, 0.0]),
        ]


def test_partial_replacement_keeps_other_chunks(engine):
    with Session(engine) as session, session.begin():
        chunk = session.get(PolicyChunkRecord, "first:v1:000")
        save_policy_embeddings(session, [chunk], [[0.0, 1.0]], "new-model", 2)
    with Session(engine) as session:
        assert stored_embeddings(session) == [
            ("first:v1:000", "new-model", 2, [0.0, 1.0]),
            ("second:v1:000", "old-model", 2, [1.0, 0.0]),
        ]


@pytest.mark.parametrize(
    "case",
    [
        "blank_model",
        "zero_dimensions",
        "bool_dimensions",
        "float_dimensions",
        "count",
        "duplicate",
        "dimension",
        "nan",
        "infinity",
        "zero_vector",
        "bool_value",
        "string_value",
    ],
)
def test_invalid_batch_does_not_delete_existing_embeddings(engine, case):
    with Session(engine) as session, session.begin():
        before = stored_embeddings(session)
        chunks = [session.get(PolicyChunkRecord, "first:v1:000")]
        vectors, model_id, dimensions = [[0.0, 1.0]], "new-model", 2
        if case == "blank_model":
            model_id = "  "
        elif case == "zero_dimensions":
            dimensions = 0
        elif case == "bool_dimensions":
            dimensions = True
        elif case == "float_dimensions":
            dimensions = 2.0
        elif case == "count":
            vectors = []
        elif case == "duplicate":
            chunks *= 2
            vectors *= 2
        elif case == "dimension":
            vectors = [[1.0]]
        elif case == "nan":
            vectors = [[float("nan"), 1.0]]
        elif case == "infinity":
            vectors = [[float("inf"), 1.0]]
        elif case == "zero_vector":
            vectors = [[0.0, 0.0]]
        elif case == "bool_value":
            vectors = [[True, 1.0]]
        elif case == "string_value":
            vectors = [["1.0", 1.0]]
        with pytest.raises(ValueError):
            save_policy_embeddings(session, chunks, vectors, model_id, dimensions)
        # No rollback here: validation must happen before destructive statements.
        assert stored_embeddings(session) == before
    with Session(engine) as session:
        assert stored_embeddings(session) == before


def test_empty_batch_leaves_existing_embeddings_unchanged(engine):
    with Session(engine) as session, session.begin():
        before = stored_embeddings(session)
        assert save_policy_embeddings(session, [], [], "new-model", 2) == 0
        assert stored_embeddings(session) == before


def test_caller_can_rollback_replacement(engine):
    with Session(engine) as session:
        before = stored_embeddings(session)
        chunk = session.get(PolicyChunkRecord, "first:v1:000")
        save_policy_embeddings(session, [chunk], [[0.0, 1.0]], "new-model", 2)
        session.rollback()
    with Session(engine) as session:
        assert stored_embeddings(session) == before


def test_database_failure_rolls_back_deletion(engine):
    with Session(engine) as session:
        before = stored_embeddings(session)
    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            chunks = [
                session.get(PolicyChunkRecord, "first:v1:000"),
                PolicyChunkRecord(chunk_id="missing:v1:000"),
            ]
            save_policy_embeddings(session, chunks, [[0.0, 1.0], [1.0, 0.0]], "new-model", 2)
    with Session(engine) as session:
        assert stored_embeddings(session) == before


def test_load_returns_only_requested_chunk(engine):
    from shopcare.services.policy_embeddings import load_policy_embeddings

    with Session(engine) as session:
        chunk = session.get(PolicyChunkRecord, "first:v1:000")
        result = load_policy_embeddings(session, [chunk], "old-model", 2)
        assert list(result) == ["first:v1:000"]
        assert result[chunk.chunk_id].vector == [1.0, 0.0]


@pytest.mark.parametrize("mode", ["missing", "model", "dimensions", "partial"])
def test_load_rejects_incomplete_or_incompatible_index(engine, mode):
    from shopcare.services.policy_embeddings import load_policy_embeddings

    with Session(engine) as session:
        first = session.get(PolicyChunkRecord, "first:v1:000")
        missing = PolicyChunkRecord(chunk_id="missing:v1:000")
        chunks, model, dimensions = [first], "old-model", 2
        if mode == "missing":
            chunks = [missing]
        elif mode == "model":
            model = "other-model"
        elif mode == "dimensions":
            dimensions = 1024
        else:
            chunks = [first, missing]
        with pytest.raises(ValueError, match="重新构建索引"):
            load_policy_embeddings(session, chunks, model, dimensions)


def test_load_empty_chunks_does_not_query():
    from shopcare.services.policy_embeddings import load_policy_embeddings

    session = Mock(spec=Session)
    assert load_policy_embeddings(session, [], "old-model", 2) == {}
    session.scalars.assert_not_called()


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf")])
def test_semantic_hit_rejects_nonfinite_scores(engine, score):
    from pydantic import ValidationError

    from shopcare.schemas.policy_models import PolicyChunk, SemanticPolicySearchHit

    with Session(engine) as session:
        chunk = PolicyChunk.model_validate(
            session.get(PolicyChunkRecord, "first:v1:000"), from_attributes=True
        )
        with pytest.raises(ValidationError):
            SemanticPolicySearchHit(chunk=chunk, score=score)
