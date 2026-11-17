from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.db.models import AfterSalesApplication, AfterSalesDraft, Order, PolicyChunkRecord
from shopcare.schemas.after_sales import AfterSalesConfirmRequest, AfterSalesDraftResponse
from shopcare.scripts.seed import seed_demo_data
from shopcare.services.after_sales import (
    DraftConflictError,
    SubmissionNotEligibleError,
    cancel_after_sales_draft,
    confirm_after_sales_draft,
    create_after_sales_draft,
)
from shopcare.services.policies.chunker import split_policy
from shopcare.services.policies.loader import load_policies
from shopcare.services.policies.store import replace_policy_chunks


@pytest.fixture
def submission_context(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'submit.db').as_posix()}")
    init_db(engine)
    with Session(engine) as session, session.begin():
        seed_demo_data(session)
        for policy in load_policies(Path(__file__).resolve().parents[3] / "data/policies"):
            replace_policy_chunks(session, policy, split_policy(policy))
        draft = create_after_sales_draft(
            session,
            "U001",
            "O004",
            "O004-I01",
            "exchange",
            "耳机单侧无声",
            date(2026, 11, 2),
        )
        draft_id = draft.id
    try:
        yield engine, draft_id
    finally:
        engine.dispose()


def confirmation(**overrides):
    return AfterSalesConfirmRequest.model_validate(
        {
            "expected_version": 1,
            "confirmed": True,
            "facts": {"issue_type": "malfunction"},
            **overrides,
        }
    )


def submit(engine, draft_id, payload=None):
    with Session(engine) as session, session.begin():
        row = confirm_after_sales_draft(session, "U001", draft_id, payload or confirmation())
        return row.id, row.snapshot


def test_submit_saves_evidence_and_retry_returns_original_even_after_state_changes(
    submission_context,
):
    engine, draft_id = submission_context
    application_id, snapshot = submit(engine, draft_id)
    assert snapshot["days_since_delivery"] == 3
    assert snapshot["policy_id"] == "digital-exchange"
    assert snapshot["policy_version"] == "v2"
    assert snapshot["policy_chunks"]
    assert "未经检测" in snapshot["notice"]
    with Session(engine) as session, session.begin():
        draft = session.get(AfterSalesDraft, draft_id)
        assert AfterSalesDraftResponse.model_validate(draft).status == "submitted"
        assert draft.version == 2
        assert session.get(Order, "O004").status == "delivered"
        draft.expires_at = datetime(2020, 1, 1)
        session.get(Order, "O004").status = "cancelled"
        session.execute(delete(PolicyChunkRecord))
    assert submit(engine, draft_id) == (application_id, snapshot)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AfterSalesApplication)) == 1
    for payload in (confirmation(expected_version=2), confirmation(facts={"issue_type": "damage"})):
        with pytest.raises(DraftConflictError):
            submit(engine, draft_id, payload)
    with Session(engine) as session, session.begin(), pytest.raises(DraftConflictError):
        cancel_after_sales_draft(session, "U001", draft_id, 2)


@pytest.mark.parametrize(
    "case",
    [
        "unconfirmed",
        "wrong_version",
        "cancelled",
        "expired",
        "not_delivered",
        "missing_delivery",
        "future_delivery",
        "outside_window",
        "missing_policy",
        "unsupported_version",
        "missing_issue",
    ],
)
def test_submit_rechecks_current_state_and_leaves_no_partial_application(submission_context, case):
    engine, draft_id = submission_context
    payload = confirmation()
    with Session(engine) as session, session.begin():
        draft, order = session.get(AfterSalesDraft, draft_id), session.get(Order, "O004")
        if case == "unconfirmed":
            payload = confirmation(confirmed=False)
        elif case == "wrong_version":
            payload = confirmation(expected_version=2)
        elif case == "cancelled":
            draft.status = "cancelled"
        elif case == "expired":
            draft.expires_at = datetime(2020, 1, 1)
        elif case == "not_delivered":
            order.status = "shipped"
        elif case == "missing_delivery":
            order.delivered_at = None
        elif case == "future_delivery":
            order.delivered_at = datetime(2027, 1, 1)
        elif case == "outside_window":
            draft.as_of = date(2026, 12, 1)
        elif case == "missing_policy":
            session.execute(delete(PolicyChunkRecord))
        elif case == "unsupported_version":
            for chunk in session.scalars(select(PolicyChunkRecord)):
                chunk.version = "unknown"
        elif case == "missing_issue":
            payload = confirmation(facts={})
    with pytest.raises((DraftConflictError, SubmissionNotEligibleError)):
        submit(engine, draft_id, payload)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AfterSalesApplication)) == 0
        assert session.get(AfterSalesDraft, draft_id).version == 1


@pytest.mark.parametrize("user_id", ["U002", "unknown"])
def test_confirm_and_receipt_do_not_cross_user_boundary(submission_context, user_id):
    engine, draft_id = submission_context
    with Session(engine) as session, session.begin(), pytest.raises(LookupError):
        confirm_after_sales_draft(session, user_id, draft_id, confirmation())
    submit(engine, draft_id)
    with Session(engine) as session, session.begin(), pytest.raises(LookupError):
        confirm_after_sales_draft(session, user_id, draft_id, confirmation())


@pytest.mark.parametrize(
    "facts,accepted",
    [
        ({}, False),
        ({"unopened": True, "unused": True}, False),
        ({"unopened": False, "unused": True, "accessories_complete": True}, False),
        ({"unopened": True, "unused": True, "accessories_complete": True}, True),
    ],
)
def test_return_requires_explicit_user_declarations(submission_context, facts, accepted):
    engine, draft_id = submission_context
    with Session(engine) as session, session.begin():
        session.get(AfterSalesDraft, draft_id).request_type = "return"
    if accepted:
        assert (
            submit(engine, draft_id, confirmation(facts=facts))[1]["policy_id"] == "digital-return"
        )
    else:
        with pytest.raises(SubmissionNotEligibleError):
            submit(engine, draft_id, confirmation(facts=facts))


def test_parallel_confirmations_produce_one_application(submission_context):
    engine, draft_id = submission_context
    barrier = Barrier(2)

    def confirm_at_once():
        barrier.wait(timeout=10)
        return submit(engine, draft_id)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm_at_once) for _ in range(2)]
        ids = [f.result(timeout=15) for f in futures]
    assert ids[0] == ids[1]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AfterSalesApplication)) == 1
        assert session.get(AfterSalesDraft, draft_id).version == 2


def test_confirm_cancel_race_has_exactly_one_winner(submission_context):
    engine, draft_id = submission_context
    barrier = Barrier(2)

    def race(cancel):
        barrier.wait(timeout=10)
        try:
            with Session(engine) as session, session.begin():
                if cancel:
                    cancel_after_sales_draft(session, "U001", draft_id, 1)
                else:
                    confirm_after_sales_draft(session, "U001", draft_id, confirmation())
            return "cancelled" if cancel else "submitted"
        except DraftConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(race, choice) for choice in (True, False)]
        results = [f.result(timeout=15) for f in futures]
    assert results.count("conflict") == 1
    with Session(engine) as session:
        draft = session.get(AfterSalesDraft, draft_id)
        assert draft.public_status in results
        assert draft.version == 2


def test_insert_failure_rolls_back_draft_and_application(submission_context):
    engine, draft_id = submission_context

    def fail_insert(mapper, connection, target):
        raise RuntimeError("simulated insert failure")

    event.listen(AfterSalesApplication, "before_insert", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="insert failure"):
            submit(engine, draft_id)
    finally:
        event.remove(AfterSalesApplication, "before_insert", fail_insert)
    with Session(engine) as session:
        assert session.get(AfterSalesDraft, draft_id).version == 1
        assert session.scalar(select(func.count()).select_from(AfterSalesApplication)) == 0


def test_simulated_clock_advances_across_calendar_day(submission_context):
    engine, draft_id = submission_context
    with Session(engine) as session, session.begin():
        draft = session.get(AfterSalesDraft, draft_id)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        draft.created_at = now - timedelta(days=1)
        draft.expires_at = now + timedelta(hours=1)
    assert submit(engine, draft_id)[1]["days_since_delivery"] == 4


@pytest.mark.parametrize(
    "category,request_type,as_of,delivered,days,facts,accepted",
    [
        (
            "digital",
            "exchange",
            date(2026, 11, 14),
            datetime(2026, 10, 30),
            15,
            {"issue_type": "malfunction"},
            True,
        ),
        (
            "digital",
            "exchange",
            date(2026, 11, 15),
            datetime(2026, 10, 30),
            16,
            {"issue_type": "malfunction"},
            False,
        ),
        (
            "digital",
            "exchange",
            date(2026, 9, 17),
            datetime(2026, 9, 10),
            7,
            {"issue_type": "malfunction"},
            True,
        ),
        (
            "digital",
            "exchange",
            date(2026, 9, 18),
            datetime(2026, 9, 10),
            8,
            {"issue_type": "malfunction"},
            False,
        ),
        (
            "accessory",
            "exchange",
            date(2026, 11, 2),
            datetime(2026, 10, 30),
            3,
            {"issue_type": "damage"},
            True,
        ),
        (
            "accessory",
            "exchange",
            date(2026, 11, 2),
            datetime(2026, 10, 30),
            3,
            {"issue_type": "malfunction"},
            False,
        ),
        (
            "accessory",
            "return",
            date(2026, 11, 2),
            datetime(2026, 10, 30),
            3,
            {"unused": True, "accessories_complete": True, "packaging_complete": True},
            True,
        ),
        (
            "digital",
            "return",
            date(2026, 9, 16),
            datetime(2026, 9, 10),
            6,
            {"unopened": True, "unused": True, "accessories_complete": True},
            False,
        ),
    ],
)
def test_versioned_admission_boundaries(
    submission_context,
    category,
    request_type,
    as_of,
    delivered,
    days,
    facts,
    accepted,
):
    engine, draft_id = submission_context
    with Session(engine) as session, session.begin():
        draft = session.get(AfterSalesDraft, draft_id)
        draft.request_type, draft.as_of = request_type, as_of
        if category == "accessory":
            draft.item_id = "O004-I02"
        session.get(Order, "O004").delivered_at = delivered
    if accepted:
        assert submit(engine, draft_id, confirmation(facts=facts))[1]["days_since_delivery"] == days
    else:
        with pytest.raises(SubmissionNotEligibleError):
            submit(engine, draft_id, confirmation(facts=facts))


def test_database_unique_constraint_prevents_second_application(submission_context):
    engine, draft_id = submission_context
    submit(engine, draft_id)
    with Session(engine) as session:
        original = session.scalar(select(AfterSalesApplication))
        session.add(
            AfterSalesApplication(
                id="duplicate",
                draft_id=draft_id,
                user_id="U001",
                confirmed_version=1,
                status="pending_review",
                snapshot=original.snapshot,
                created_at=original.created_at,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
