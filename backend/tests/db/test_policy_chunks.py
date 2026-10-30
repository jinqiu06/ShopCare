from datetime import date

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.db.models import PolicyChunkRecord, PolicyEmbeddingRecord


@pytest.fixture
def engine(tmp_path):
    db_engine = build_engine(f"sqlite:///{(tmp_path / 'chunks.db').as_posix()}")
    init_db(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def make_record(**overrides):
    fields = dict(
        chunk_id="demo:v1:000",
        policy_id="demo",
        version="v1",
        title="模拟政策",
        category="数码",
        effective_from=date(2026, 10, 1),
        effective_until=None,
        text="中文政策",
        start_char=0,
        end_char=4,
    )
    fields.update(overrides)
    return PolicyChunkRecord(**fields)


@pytest.mark.parametrize("end", [None, date(2026, 11, 1)])
def test_policy_chunk_round_trip(engine, end):
    with Session(engine) as session, session.begin():
        session.add(make_record(effective_until=end))
    engine.dispose()
    with Session(engine) as session:
        record = session.get(PolicyChunkRecord, "demo:v1:000")
        assert record.text == "中文政策"
        assert record.effective_from == date(2026, 10, 1)
        assert record.effective_until == end
        assert (record.start_char, record.end_char) == (0, 4)


@pytest.mark.parametrize("start,end", [(-1, 4), (0, 0), (3, 2)])
def test_policy_chunk_invalid_offsets_rejected(engine, start, end):
    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            session.add(make_record(start_char=start, end_char=end))
            session.flush()


def test_policy_chunk_id_is_unique(engine):
    with Session(engine) as session, session.begin():
        session.add(make_record())
    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            session.add(make_record(text="另一正文"))
            session.flush()
    with Session(engine) as session:
        assert session.get(PolicyChunkRecord, "demo:v1:000").text == "中文政策"


def make_embedding(**overrides):
    fields = dict(
        chunk_id="demo:v1:000",
        model_id="test-embedding-model",
        dimensions=3,
        vector=[0.25, -0.5, 0.0],
    )
    fields.update(overrides)
    return PolicyEmbeddingRecord(**fields)


def test_embedding_round_trip_across_connections(engine):
    with Session(engine) as session, session.begin():
        session.add(make_record())
        session.flush()
        session.add(make_embedding())
    engine.dispose()
    with Session(engine) as session:
        embedding = session.get(PolicyEmbeddingRecord, "demo:v1:000")
        assert embedding.vector == [0.25, -0.5, 0.0]
        assert embedding.model_id == "test-embedding-model"
        assert embedding.dimensions == 3


@pytest.mark.parametrize("dimensions", [0, -1])
def test_embedding_dimensions_must_be_positive(engine, dimensions):
    with Session(engine) as session, session.begin():
        session.add(make_record())
    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            session.add(make_embedding(dimensions=dimensions))
            session.flush()


def test_embedding_rejects_missing_chunk(engine):
    engine.dispose()
    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            session.add(make_embedding())
            session.flush()


def test_bulk_chunk_deletion_cascades_to_embedding(engine):
    with Session(engine) as session, session.begin():
        session.add(make_record())
        session.flush()
        session.add(make_embedding())
    engine.dispose()
    with Session(engine) as session, session.begin():
        session.execute(
            delete(PolicyChunkRecord).where(PolicyChunkRecord.chunk_id == "demo:v1:000")
        )
    with Session(engine) as session:
        assert session.get(PolicyEmbeddingRecord, "demo:v1:000") is None
