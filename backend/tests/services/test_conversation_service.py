from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.db.models import Conversation, User
from shopcare.services import conversations
from shopcare.services.conversations import (
    create_conversation,
    finish_conversation_turn,
    get_conversation_for_user,
    start_conversation_turn,
)


@pytest.fixture
def conversation_engine(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'conversations.db').as_posix()}")
    try:
        init_db(engine)
        with Session(engine) as session, session.begin():
            session.add_all(
                [User(id="U001", display_name="first"), User(id="U002", display_name="second")]
            )
        yield engine
    finally:
        engine.dispose()


def test_create_conversation_generates_distinct_ids_and_persists(conversation_engine):
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(conversation_engine) as session, session.begin():
        first = create_conversation(session, "U001", date(2026, 11, 2))
        second = create_conversation(session, "U002", date(2026, 11, 5))
        ids = [first.id, first.thread_id, second.id, second.thread_id]
        assert len(set(ids)) == 4
        assert all(UUID(value).version == 4 for value in ids)
        first_id, second_id = first.id, second.id
        assert session.get(Conversation, first_id) is first
        assert first.status == second.status == "ready"
        after = datetime.now(timezone.utc).replace(tzinfo=None)
        for record in (first, second):
            assert before <= record.created_at == record.updated_at <= after
            assert record.created_at.tzinfo is None

    conversation_engine.dispose()
    with Session(conversation_engine) as session:
        first = session.get(Conversation, first_id)
        second = session.get(Conversation, second_id)
        assert (first.user_id, first.as_of, first.thread_id) == ("U001", date(2026, 11, 2), ids[1])
        assert (second.user_id, second.as_of, second.thread_id) == (
            "U002",
            date(2026, 11, 5),
            ids[3],
        )


def test_create_conversation_does_not_commit_callers_transaction(conversation_engine):
    with Session(conversation_engine) as session:
        # 已有的待提交写入也必须由调用方控制，不能被服务顺带提交。
        session.add(User(id="U003", display_name="pending user"))
        record = create_conversation(session, "U003", date(2026, 11, 2))
        record_id = record.id
        assert get_conversation_for_user(session, "U003", record_id) is record
        session.rollback()
        assert session.is_active
    with Session(conversation_engine) as session:
        assert session.get(Conversation, record_id) is None
        assert session.get(User, "U003") is None


def test_create_conversation_missing_user_leaves_session_usable(conversation_engine):
    with Session(conversation_engine) as session, session.begin():
        with pytest.raises(LookupError, match="用户不存在"):
            create_conversation(session, "missing", date(2026, 11, 2))
        assert session.scalar(select(func.count()).select_from(Conversation)) == 0
        record = create_conversation(session, "U001", date(2026, 11, 2))
        record_id = record.id
    with Session(conversation_engine) as session:
        assert session.get(Conversation, record_id) is not None


@pytest.mark.parametrize("status", ["ready", "running", "failed"])
def test_get_conversation_for_user_returns_record_without_changing_status(
    conversation_engine, status
):
    with Session(conversation_engine) as session, session.begin():
        record = create_conversation(session, "U001", date(2026, 11, 2))
        record.status = status
        record_id = record.id
        original = (record.thread_id, record.as_of, record.created_at, record.updated_at)
    with Session(conversation_engine) as session:
        found = get_conversation_for_user(session, "U001", record_id)
        assert isinstance(found, Conversation)
        assert found.id == record_id
        assert found.user_id == "U001"
        assert found.status == status
        assert (found.thread_id, found.as_of, found.created_at, found.updated_at) == original


@pytest.mark.parametrize(
    "user_id, use_existing_id",
    [("U002", True), ("missing-user", True), ("U001", False)],
)
def test_get_conversation_for_user_hides_foreign_and_missing_records(
    conversation_engine, user_id, use_existing_id
):
    with Session(conversation_engine) as session, session.begin():
        record = create_conversation(session, "U001", date(2026, 11, 2))
        record_id = record.id
    with Session(conversation_engine) as session:
        requested_id = record_id if use_existing_id else "missing-conversation"
        assert get_conversation_for_user(session, user_id, requested_id) is None


def test_start_conversation_turn_returns_refreshed_running_record(conversation_engine):
    with Session(conversation_engine) as session, session.begin():
        record = create_conversation(session, "U001", date(2026, 11, 2))
        record_id = record.id
        original = (record.thread_id, record.as_of, record.created_at)
        previous_update = record.updated_at
    with Session(conversation_engine) as session, session.begin():
        result = start_conversation_turn(session, "U001", record_id)
        assert isinstance(result, Conversation)
        assert result.status == "running"
        assert result.updated_at >= previous_update
        assert (result.thread_id, result.as_of, result.created_at) == original
    with Session(conversation_engine) as session:
        assert session.get(Conversation, record_id).status == "running"


@pytest.mark.parametrize("succeeded", [True, False])
def test_finish_conversation_turn_persists_result_and_controls_next_turn(
    conversation_engine, succeeded
):
    with Session(conversation_engine) as session, session.begin():
        record = create_conversation(session, "U001", date(2026, 11, 2))
        record_id = record.id
        original = (record.thread_id, record.as_of, record.created_at)
        start_conversation_turn(session, "U001", record_id)
    with Session(conversation_engine) as session, session.begin():
        result = finish_conversation_turn(session, "U001", record_id, succeeded=succeeded)
        assert isinstance(result, Conversation)
        assert result.status == ("ready" if succeeded else "failed")
        assert (result.thread_id, result.as_of, result.created_at) == original
    with Session(conversation_engine) as session:
        assert session.get(Conversation, record_id).status == ("ready" if succeeded else "failed")
        if succeeded:
            assert start_conversation_turn(session, "U001", record_id).status == "running"
        else:
            with pytest.raises(RuntimeError, match="当前不可执行"):
                start_conversation_turn(session, "U001", record_id)


@pytest.mark.parametrize("status", ["ready", "failed"])
def test_finish_conversation_turn_rejects_non_running_record(conversation_engine, status):
    with Session(conversation_engine) as session, session.begin():
        record = create_conversation(session, "U001", date(2026, 11, 2))
        record.status = status
        record_id = record.id
        previous_update = record.updated_at
    with Session(conversation_engine) as session, session.begin():
        with pytest.raises(RuntimeError, match="没有正在执行的轮次"):
            finish_conversation_turn(session, "U001", record_id, succeeded=True)
    with Session(conversation_engine) as session:
        record = session.get(Conversation, record_id)
        assert record.status == status
        assert record.updated_at == previous_update


@pytest.mark.parametrize(
    "user_id, use_existing_id",
    [("U002", True), ("missing-user", True), ("U001", False)],
)
def test_finish_conversation_turn_rejects_missing_or_foreign_record(
    conversation_engine, user_id, use_existing_id
):
    with Session(conversation_engine) as session, session.begin():
        record_id = create_conversation(session, "U001", date(2026, 11, 2)).id
        start_conversation_turn(session, "U001", record_id)
    with Session(conversation_engine) as session, session.begin():
        with pytest.raises(LookupError, match="会话不存在或不可访问"):
            finish_conversation_turn(
                session,
                user_id,
                record_id if use_existing_id else "missing-conversation",
                succeeded=True,
            )
    with Session(conversation_engine) as session:
        assert session.get(Conversation, record_id).status == "running"


@pytest.mark.parametrize("succeeded", [1, 0, "true", None])
def test_finish_conversation_turn_rejects_non_boolean_outcome(conversation_engine, succeeded):
    with Session(conversation_engine) as session, session.begin():
        record_id = create_conversation(session, "U001", date(2026, 11, 2)).id
        start_conversation_turn(session, "U001", record_id)
    with Session(conversation_engine) as session, session.begin():
        with pytest.raises(ValueError, match="参数类型不合法"):
            finish_conversation_turn(session, "U001", record_id, succeeded=succeeded)
    with Session(conversation_engine) as session:
        assert session.get(Conversation, record_id).status == "running"


def test_finish_conversation_turn_does_not_commit_callers_transaction(conversation_engine):
    with Session(conversation_engine) as session, session.begin():
        record_id = create_conversation(session, "U001", date(2026, 11, 2)).id
        record = start_conversation_turn(session, "U001", record_id)
        previous_update = record.updated_at
    with Session(conversation_engine) as session:
        session.add(User(id="U003", display_name="pending user"))
        finish_conversation_turn(session, "U001", record_id, succeeded=True)
        session.rollback()
    with Session(conversation_engine) as session:
        record = session.get(Conversation, record_id)
        assert record.status == "running"
        assert record.updated_at == previous_update
        assert session.get(User, "U003") is None


@pytest.mark.parametrize("status", ["running", "failed"])
def test_start_conversation_turn_rejects_non_ready_record(conversation_engine, status):
    with Session(conversation_engine) as session, session.begin():
        record = create_conversation(session, "U001", date(2026, 11, 2))
        record.status = status
        record_id = record.id
        previous_update = record.updated_at
    with Session(conversation_engine) as session, session.begin():
        with pytest.raises(RuntimeError, match="当前不可执行"):
            start_conversation_turn(session, "U001", record_id)
    with Session(conversation_engine) as session:
        found = session.get(Conversation, record_id)
        assert found.status == status
        assert found.updated_at == previous_update


@pytest.mark.parametrize(
    "user_id, use_existing_id",
    [("U002", True), ("missing-user", True), ("U001", False)],
)
def test_start_conversation_turn_rejects_missing_or_foreign_record(
    conversation_engine, user_id, use_existing_id
):
    with Session(conversation_engine) as session, session.begin():
        record = create_conversation(session, "U001", date(2026, 11, 2))
        record_id = record.id
    with Session(conversation_engine) as session, session.begin():
        with pytest.raises(LookupError, match="会话不存在或不可访问"):
            start_conversation_turn(
                session, user_id, record_id if use_existing_id else "missing-conversation"
            )
    with Session(conversation_engine) as session:
        assert session.get(Conversation, record_id).status == "ready"


def test_start_conversation_turn_does_not_commit_callers_transaction(conversation_engine):
    with Session(conversation_engine) as session, session.begin():
        record = create_conversation(session, "U001", date(2026, 11, 2))
        record_id = record.id
        previous_update = record.updated_at
    with Session(conversation_engine) as session:
        session.add(User(id="U003", display_name="pending user"))
        start_conversation_turn(session, "U001", record_id)
        session.rollback()
    with Session(conversation_engine) as session:
        record = session.get(Conversation, record_id)
        assert record.status == "ready"
        assert record.updated_at == previous_update
        assert session.get(User, "U003") is None


def test_start_conversation_turn_allows_only_one_concurrent_caller(
    conversation_engine, monkeypatch
):
    with Session(conversation_engine) as session, session.begin():
        record_id = create_conversation(session, "U001", date(2026, 11, 2)).id
    both_read = Barrier(2)
    original_lookup = conversations.get_conversation_for_user

    def lookup_before_race(session, user_id, conversation_id):
        record = original_lookup(session, user_id, conversation_id)
        assert record.status == "ready"
        both_read.wait(timeout=10)
        return record

    def attempt_start():
        try:
            with Session(conversation_engine) as session, session.begin():
                record = start_conversation_turn(session, "U001", record_id)
                assert record.status == "running"
            return "started"
        except RuntimeError as error:
            assert str(error) == "会话当前不可执行"
            return "rejected"

    monkeypatch.setattr(conversations, "get_conversation_for_user", lookup_before_race)
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(attempt_start) for _ in range(2)]
        outcomes = [attempt.result(timeout=15) for attempt in attempts]
    assert sorted(outcomes) == ["rejected", "started"]
    with Session(conversation_engine) as session:
        assert session.get(Conversation, record_id).status == "running"
