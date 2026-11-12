from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.db.models import AfterSalesDraft, Order
from shopcare.scripts.seed import seed_demo_data
from shopcare.services.after_sales import (
    DraftConflictError,
    cancel_after_sales_draft,
    create_after_sales_draft,
    get_after_sales_draft_for_user,
)


@pytest.fixture
def draft_engine(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'drafts.db').as_posix()}")
    try:
        init_db(engine)
        with Session(engine) as session, session.begin():
            seed_demo_data(session)
        yield engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("request_type", ["return", "exchange"])
def test_create_draft_persists_intent_without_changing_order(draft_engine, request_type):
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(draft_engine) as session, session.begin():
        original_status = session.get(Order, "O004").status
        first = create_after_sales_draft(
            session, "U001", "O004", "O004-I01", request_type, "  无法充电  ", date(2026, 11, 2)
        )
        second = create_after_sales_draft(
            session, "U001", "O004", "O004-I02", request_type, "商品故障", date(2026, 11, 2)
        )
        first_id, second_id = first.id, second.id
        assert first_id != second_id
        assert UUID(first_id).version == UUID(second_id).version == 4
    after = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(draft_engine) as session:
        draft = session.get(AfterSalesDraft, first_id)
        assert (draft.user_id, draft.order_id, draft.item_id) == ("U001", "O004", "O004-I01")
        assert (draft.request_type, draft.reason) == (request_type, "无法充电")
        assert (draft.status, draft.version) == ("pending", 1)
        assert draft.as_of == date(2026, 11, 2)
        assert before <= draft.created_at == draft.updated_at <= after
        assert draft.expires_at - draft.created_at == timedelta(hours=24)
        assert session.get(Order, "O004").status == original_status


@pytest.mark.parametrize(
    "user_id, order_id, item_id",
    [
        ("U002", "O004", "O004-I01"),
        ("missing-user", "O004", "O004-I01"),
        ("U001", "missing-order", "O004-I01"),
        ("U001", "O004", "O001-I01"),
        ("U001", "O004", "missing-item"),
    ],
)
def test_create_draft_rejects_inaccessible_context(draft_engine, user_id, order_id, item_id):
    with Session(draft_engine) as session, session.begin():
        with pytest.raises(LookupError, match="订单或商品不存在或不可访问"):
            create_after_sales_draft(
                session, user_id, order_id, item_id, "exchange", "故障", date(2026, 11, 2)
            )
    with Session(draft_engine) as session:
        assert session.scalar(select(func.count()).select_from(AfterSalesDraft)) == 0


@pytest.mark.parametrize("request_type, reason", [("refund", "故障"), ("exchange", " \t\n ")])
def test_create_draft_rejects_invalid_input(draft_engine, request_type, reason):
    with Session(draft_engine) as session, session.begin():
        with pytest.raises(ValueError, match="输入不合法"):
            create_after_sales_draft(
                session, "U001", "O004", "O004-I01", request_type, reason, date(2026, 11, 2)
            )
    with Session(draft_engine) as session:
        assert session.scalar(select(func.count()).select_from(AfterSalesDraft)) == 0


def test_create_draft_leaves_commit_and_rollback_to_caller(draft_engine):
    with Session(draft_engine) as session:
        draft = create_after_sales_draft(
            session, "U001", "O004", "O004-I01", "exchange", "故障", date(2026, 11, 2)
        )
        draft_id = draft.id
        assert session.get(AfterSalesDraft, draft_id) is draft
        with Session(draft_engine) as observer:
            assert observer.get(AfterSalesDraft, draft_id) is None
        session.rollback()
    with Session(draft_engine) as session:
        assert session.get(AfterSalesDraft, draft_id) is None


@pytest.mark.parametrize(
    "owner, order_id, item_id",
    [
        ("U001", "O004", "O004-I01"),
        ("U002", "O011", "O011-I01"),
    ],
)
@pytest.mark.parametrize("status", ["pending", "cancelled"])
def test_read_draft_returns_owned_record_without_changing_it(
    draft_engine, owner, order_id, item_id, status
):
    with Session(draft_engine) as session, session.begin():
        draft = create_after_sales_draft(
            session, owner, order_id, item_id, "exchange", "无法充电", date(2026, 11, 2)
        )
        draft.status = status
        draft.expires_at = draft.created_at - timedelta(hours=1)
        draft_id = draft.id
        expected = (
            draft.status,
            draft.version,
            draft.created_at,
            draft.updated_at,
            draft.expires_at,
        )
    with Session(draft_engine) as session:
        found = get_after_sales_draft_for_user(session, owner, draft_id)
        assert found is not None
        assert (found.user_id, found.order_id, found.item_id) == (owner, order_id, item_id)
        assert found.reason == "无法充电"
        assert (
            found.status,
            found.version,
            found.created_at,
            found.updated_at,
            found.expires_at,
        ) == expected
        assert not session.dirty
    with Session(draft_engine) as session:
        saved = session.get(AfterSalesDraft, draft_id)
        assert (
            saved.status,
            saved.version,
            saved.created_at,
            saved.updated_at,
            saved.expires_at,
        ) == expected


@pytest.mark.parametrize("requester", ["U002", "missing-user"])
def test_read_draft_returns_none_for_both_missing_and_unowned(draft_engine, requester):
    with Session(draft_engine) as session, session.begin():
        draft = create_after_sales_draft(
            session, "U001", "O004", "O004-I01", "exchange", "故障", date(2026, 11, 2)
        )
        draft_id = draft.id
    with Session(draft_engine) as session:
        assert get_after_sales_draft_for_user(session, requester, draft_id) is None
        assert get_after_sales_draft_for_user(session, requester, "missing-draft") is None
        assert session.get(AfterSalesDraft, draft_id).status == "pending"
        assert not session.dirty


@pytest.mark.parametrize("expired", [False, True])
def test_cancel_draft_updates_state_and_version_without_changing_intent(draft_engine, expired):
    with Session(draft_engine) as session, session.begin():
        draft = create_after_sales_draft(
            session, "U001", "O004", "O004-I01", "exchange", "故障", date(2026, 11, 2)
        )
        if expired:
            draft.created_at -= timedelta(days=2)
            draft.expires_at = draft.created_at + timedelta(hours=24)
        draft_id = draft.id
        original_created_at, original_expires_at = draft.created_at, draft.expires_at
        original_order_status = session.get(Order, "O004").status
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(draft_engine) as session, session.begin():
        loaded = get_after_sales_draft_for_user(session, "U001", draft_id)
        cancelled = cancel_after_sales_draft(session, "U001", draft_id, 1)
        assert cancelled is loaded
        assert (cancelled.status, cancelled.version) == ("cancelled", 2)
        assert cancelled.updated_at >= before
    with Session(draft_engine) as session:
        saved = session.get(AfterSalesDraft, draft_id)
        assert (saved.status, saved.version) == ("cancelled", 2)
        assert (saved.user_id, saved.order_id, saved.item_id) == ("U001", "O004", "O004-I01")
        assert (saved.request_type, saved.reason, saved.as_of) == (
            "exchange",
            "故障",
            date(2026, 11, 2),
        )
        assert (saved.created_at, saved.expires_at) == (original_created_at, original_expires_at)
        assert session.get(Order, "O004").status == original_order_status


@pytest.mark.parametrize(
    "status, version, expected_version",
    [
        ("pending", 2, 1),
        ("cancelled", 2, 1),
        ("cancelled", 2, 2),
    ],
)
def test_cancel_draft_rejects_version_and_state_conflicts(
    draft_engine, status, version, expected_version
):
    with Session(draft_engine) as session, session.begin():
        draft = create_after_sales_draft(
            session, "U001", "O004", "O004-I01", "exchange", "故障", date(2026, 11, 2)
        )
        draft.status, draft.version = status, version
        draft_id, original_updated_at = draft.id, draft.updated_at
    with Session(draft_engine) as session, session.begin():
        with pytest.raises(DraftConflictError, match="草稿状态或版本已变化"):
            cancel_after_sales_draft(session, "U001", draft_id, expected_version)
    with Session(draft_engine) as session:
        saved = session.get(AfterSalesDraft, draft_id)
        assert (saved.status, saved.version, saved.updated_at) == (
            status,
            version,
            original_updated_at,
        )


@pytest.mark.parametrize(
    "requester, missing", [("U002", False), ("missing-user", False), ("U001", True)]
)
def test_cancel_draft_rejects_missing_and_unowned_records(draft_engine, requester, missing):
    with Session(draft_engine) as session, session.begin():
        draft = create_after_sales_draft(
            session, "U001", "O004", "O004-I01", "exchange", "故障", date(2026, 11, 2)
        )
        draft_id, original_updated_at = draft.id, draft.updated_at
    with Session(draft_engine) as session, session.begin():
        with pytest.raises(LookupError, match="草稿不存在或不可访问"):
            cancel_after_sales_draft(
                session, requester, "missing-draft" if missing else draft_id, 1
            )
    with Session(draft_engine) as session:
        saved = session.get(AfterSalesDraft, draft_id)
        assert (saved.status, saved.version, saved.updated_at) == (
            "pending",
            1,
            original_updated_at,
        )


def test_cancel_draft_does_not_commit_and_caller_can_roll_back(draft_engine):
    with Session(draft_engine) as session, session.begin():
        draft = create_after_sales_draft(
            session, "U001", "O004", "O004-I01", "exchange", "故障", date(2026, 11, 2)
        )
        draft_id, original_updated_at = draft.id, draft.updated_at
    with Session(draft_engine) as session:
        cancel_after_sales_draft(session, "U001", draft_id, 1)
        with Session(draft_engine) as observer:
            saved = observer.get(AfterSalesDraft, draft_id)
            assert (saved.status, saved.version, saved.updated_at) == (
                "pending",
                1,
                original_updated_at,
            )
        session.rollback()
    with Session(draft_engine) as session:
        saved = session.get(AfterSalesDraft, draft_id)
        assert (saved.status, saved.version, saved.updated_at) == (
            "pending",
            1,
            original_updated_at,
        )
