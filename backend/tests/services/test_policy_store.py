from datetime import date

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.db.models import PolicyChunkRecord
from shopcare.schemas.policy_models import PolicyDocument
from shopcare.services.policy_chunker import split_policy
from shopcare.services.policy_store import replace_policy_chunks


@pytest.fixture
def engine(tmp_path):
    db_engine = build_engine(f"sqlite:///{(tmp_path / 'policy-store.db').as_posix()}")
    init_db(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def policy(**overrides):
    data = dict(
        policy_id="demo",
        version="v1",
        title="测试政策",
        category="数码",
        effective_from=date(2026, 10, 1),
        effective_until=None,
        content="ABCDEFGHI",
    )
    data.update(overrides)
    return PolicyDocument(**data)


def snapshot(session):
    records = session.scalars(select(PolicyChunkRecord).order_by(PolicyChunkRecord.chunk_id)).all()
    return [
        {
            column.name: getattr(record, column.name)
            for column in PolicyChunkRecord.__table__.columns
        }
        for record in records
    ]


def save(engine, source, size=4, overlap=1):
    chunks = split_policy(source, size, overlap)
    with Session(engine) as session, session.begin():
        return replace_policy_chunks(session, source, chunks)


def assert_rejected_without_deletion(engine, source, chunks):
    save(engine, source)
    with Session(engine) as session:
        before = snapshot(session)
        with pytest.raises(ValueError):
            replace_policy_chunks(session, source, chunks)
        assert snapshot(session) == before


def test_store_normal_round_trip(engine):
    source = policy()
    chunks = split_policy(source, 4, 1)
    assert save(engine, source) == 3
    engine.dispose()
    with Session(engine) as session:
        assert snapshot(session) == [chunk.model_dump() for chunk in chunks]


def test_repeated_store_has_identical_state(engine):
    source = policy()
    save(engine, source)
    with Session(engine) as session:
        before = snapshot(session)
    assert save(engine, source) == 3
    with Session(engine) as session:
        assert snapshot(session) == before


def test_rechunking_removes_old_tail(engine):
    source = policy()
    assert save(engine, source) == 3
    assert save(engine, source, size=20, overlap=0) == 1
    with Session(engine) as session:
        rows = snapshot(session)
        assert len(rows) == 1
        assert rows[0]["text"] == source.content
        assert rows[0]["chunk_id"] == "demo:v1:000"


def test_replacement_preserves_other_policies_and_versions(engine):
    source = policy()
    other = policy(policy_id="other")
    old = policy(version="old")
    for item in (source, other, old):
        save(engine, item)
    with Session(engine) as session:
        before = [r for r in snapshot(session) if (r["policy_id"], r["version"]) != ("demo", "v1")]
    save(engine, source, size=20, overlap=0)
    with Session(engine) as session:
        after = [r for r in snapshot(session) if (r["policy_id"], r["version"]) != ("demo", "v1")]
        assert after == before


@pytest.mark.parametrize("chunks", [[], None])
def test_empty_input_cannot_clear_existing_data(engine, chunks):
    assert_rejected_without_deletion(engine, policy(), chunks)


def test_duplicate_ids_rejected_before_delete(engine):
    source = policy()
    chunks = split_policy(source, 4, 1)
    chunks[1] = chunks[1].model_copy(update={"chunk_id": chunks[0].chunk_id})
    assert_rejected_without_deletion(engine, source, chunks)


@pytest.mark.parametrize(
    "field,value",
    [
        ("policy_id", "wrong"),
        ("version", "wrong"),
        ("title", "wrong"),
        ("category", "配件"),
        ("effective_from", date(2026, 9, 1)),
        ("effective_until", date(2027, 1, 1)),
    ],
)
def test_metadata_mismatch_rejected_before_delete(engine, field, value):
    source = policy()
    chunks = split_policy(source, 4, 1)
    chunks[-1] = chunks[-1].model_copy(update={field: value})
    assert_rejected_without_deletion(engine, source, chunks)


@pytest.mark.parametrize("start,end", [(-1, 3), (0, 0), (5, 2), (0, 1000)])
def test_invalid_offsets_rejected_before_delete(engine, start, end):
    source = policy()
    chunks = split_policy(source, 4, 1)
    chunks[-1] = chunks[-1].model_copy(update={"start_char": start, "end_char": end})
    assert_rejected_without_deletion(engine, source, chunks)


def test_modified_text_rejected_before_delete(engine):
    source = policy()
    chunks = split_policy(source, 4, 1)
    chunks[-1] = chunks[-1].model_copy(update={"text": "forged"})
    assert_rejected_without_deletion(engine, source, chunks)


def test_caller_can_rollback_valid_replacement(engine):
    source = policy()
    save(engine, source)
    with Session(engine) as session:
        before = snapshot(session)
        replace_policy_chunks(session, source, split_policy(source, 20, 0))
        assert len(snapshot(session)) == 1
        session.rollback()
    with Session(engine) as session:
        assert snapshot(session) == before


def test_database_failure_restores_deleted_old_chunks(engine):
    source = policy()
    other = policy(policy_id="other")
    save(engine, source)
    save(engine, other)
    with Session(engine) as session:
        before = snapshot(session)
    conflicting = split_policy(source, 20, 0)
    conflicting[0] = conflicting[0].model_copy(update={"chunk_id": "other:v1:000"})
    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            replace_policy_chunks(session, source, conflicting)
    with Session(engine) as session:
        assert snapshot(session) == before


def test_policy_category_index_exists(engine):
    indexes = inspect(engine).get_indexes("policy_chunks")
    assert any(index["column_names"] == ["category"] for index in indexes)
