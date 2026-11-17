from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from shopcare.db.models import AfterSalesApplication, AfterSalesDraft
from shopcare.schemas.after_sales import AfterSalesConfirmRequest
from shopcare.schemas.orders import OrderResponse
from shopcare.services.answer_generation import get_days_since_delivery
from shopcare.services.orders import (
    get_order_for_user,
    get_order_items_for_user,
)
from shopcare.services.policies.lexical_retriever import list_effective_chunks


class DraftConflictError(RuntimeError):
    """草稿状态或版本已变化。"""


class SubmissionNotEligibleError(RuntimeError):
    """The supported simulated admission rules are not satisfied or facts are missing."""


def create_after_sales_draft(
    session: Session,
    user_id: str,
    order_id: str,
    item_id: str,
    request_type: str,
    reason: str,
    as_of: date,
) -> AfterSalesDraft:
    if request_type not in ("return", "exchange") or not reason.strip():
        raise ValueError("输入不合法")
    order = get_order_for_user(session, user_id, order_id)
    if order is None:
        raise LookupError("订单或商品不存在或不可访问")

    items = get_order_items_for_user(session, user_id, order_id)
    if not any(item.id == item_id for item in items):
        raise LookupError("订单或商品不存在或不可访问")

    draft_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    draft = AfterSalesDraft(
        id=draft_id,
        user_id=user_id,
        order_id=order.id,
        item_id=item_id,
        request_type=request_type,
        reason=reason.strip(),
        as_of=as_of,
        status="pending",
        version=1,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )

    session.add(draft)
    session.flush()

    return draft


def get_after_sales_draft_for_user(
    session: Session,
    user_id: str,
    draft_id: str,
) -> AfterSalesDraft | None:

    statement = select(AfterSalesDraft).where(
        AfterSalesDraft.id == draft_id,
        AfterSalesDraft.user_id == user_id,
    )
    return session.scalar(statement)


def cancel_after_sales_draft(
    session: Session,
    user_id: str,
    draft_id: str,
    expected_version: int,
) -> AfterSalesDraft:
    draft = get_after_sales_draft_for_user(session, user_id, draft_id)

    if draft is None:
        raise LookupError("草稿不存在或不可访问")

    statement = (
        update(AfterSalesDraft)
        .where(
            AfterSalesDraft.id == draft_id,
            AfterSalesDraft.user_id == user_id,
            AfterSalesDraft.status == "pending",
            AfterSalesDraft.version == expected_version,
            ~select(AfterSalesApplication.id)
            .where(
                AfterSalesApplication.draft_id == draft_id,
            )
            .exists(),
        )
        .values(
            status="cancelled",
            version=AfterSalesDraft.version + 1,
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        .execution_options(synchronize_session=False)
    )

    result = session.execute(statement)
    if result.rowcount != 1:
        raise DraftConflictError("草稿状态或版本已变化")

    session.refresh(draft)

    return draft


def validate_submission_eligibility(
    session: Session,
    user_id: str,
    draft: AfterSalesDraft,
    payload: AfterSalesConfirmRequest,
    now: datetime,
) -> dict:
    """Deterministic admission only; keep policy evidence and user declarations for review."""
    order = get_order_for_user(session, user_id, draft.order_id)
    items = get_order_items_for_user(session, user_id, draft.order_id)
    item = next((entry for entry in items if entry.id == draft.item_id), None)
    if order is None or item is None:
        raise LookupError("订单或商品不存在或不可访问")
    if order.status != "delivered" or order.delivered_at is None:
        raise SubmissionNotEligibleError("订单尚未签收或签收信息不完整")
    # Advance the demo's fixed reference date by elapsed real calendar days.
    as_of = draft.as_of + timedelta(days=(now.date() - draft.created_at.date()).days)
    if order.delivered_at.date() > as_of:
        raise SubmissionNotEligibleError("参照日期早于签收日期")
    days = get_days_since_delivery(OrderResponse.model_validate(order), as_of)
    category_prefix = {"数码": "digital", "配件": "accessory"}.get(item.category)
    policy_id = f"{category_prefix}-{draft.request_type}"
    chunks = [
        entry
        for entry in list_effective_chunks(session, item.category, as_of)
        if entry.policy_id == policy_id
    ]
    versions = {entry.version for entry in chunks}
    # Explicit executable counterparts of the project's versioned mock policies.
    windows = {
        ("digital-return", "v1"): 5,
        ("digital-return", "v2"): 10,
        ("digital-exchange", "v1"): 7,
        ("digital-exchange", "v2"): 15,
        ("accessory-return", "v1"): 10,
        ("accessory-exchange", "v1"): 15,
    }
    if len(versions) != 1 or (policy_id, next(iter(versions), None)) not in windows:
        raise SubmissionNotEligibleError("缺少可执行的有效模拟政策，请先核实政策")
    version = next(iter(versions))
    if days > windows[(policy_id, version)]:
        raise SubmissionNotEligibleError("已超过当前模拟政策受理时限")
    facts = payload.facts
    if draft.request_type == "exchange":
        allowed = (
            ("damage", "wrong_spec")
            if item.category == "配件"
            else (
                "malfunction",
                "damage",
                "wrong_spec",
            )
        )
        if facts.issue_type not in allowed:
            raise SubmissionNotEligibleError("请补充当前模拟政策支持的具体问题类型")
    else:
        required = [facts.unused, facts.accessories_complete]
        required.append(facts.unopened if item.category == "数码" else facts.packaging_complete)
        if any(value is not True for value in required):
            raise SubmissionNotEligibleError("退货所需商品状态尚未明确或不符合模拟条件")
    return {
        "order_id": order.id,
        "item_id": item.id,
        "request_type": draft.request_type,
        "reason": draft.reason,
        "order_status": order.status,
        "delivered_at": order.delivered_at.isoformat(),
        "category": item.category,
        "as_of": as_of.isoformat(),
        "days_since_delivery": days,
        "declared_facts": facts.model_dump(exclude_none=True),
        "policy_id": policy_id,
        "policy_version": version,
        "policy_chunks": [{"chunk_id": c.chunk_id, "text": c.text} for c in chunks],
        "notice": "仅已登记模拟申请，用户自述未经检测，不代表资格审核通过或已退款/换货。",
    }


def confirm_after_sales_draft(
    session: Session,
    user_id: str,
    draft_id: str,
    payload: AfterSalesConfirmRequest,
) -> AfterSalesApplication:
    """Caller owns the transaction; SQLite write lock serializes confirm/cancel races."""
    if not payload.confirmed:
        raise DraftConflictError("需要用户明确确认指定草稿版本")
    # Acquire the write lock before reading mutable state; no read-then-upgrade race.
    locked = session.execute(
        update(AfterSalesDraft)
        .where(
            AfterSalesDraft.id == draft_id,
            AfterSalesDraft.user_id == user_id,
        )
        .values(version=AfterSalesDraft.version)
        .execution_options(synchronize_session=False)
    )
    if locked.rowcount != 1:
        raise LookupError("草稿不存在或不可访问")
    application = session.scalar(
        select(AfterSalesApplication).where(
            AfterSalesApplication.draft_id == draft_id,
            AfterSalesApplication.user_id == user_id,
        )
    )
    if application is not None:
        if application.confirmed_version != payload.expected_version or application.snapshot[
            "declared_facts"
        ] != payload.facts.model_dump(exclude_none=True):
            raise DraftConflictError("确认内容与已提交申请不一致")
        return application
    draft = get_after_sales_draft_for_user(session, user_id, draft_id)
    session.refresh(draft)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if draft.status != "pending" or draft.version != payload.expected_version:
        raise DraftConflictError("草稿状态或版本已变化")
    if draft.expires_at <= now:
        raise DraftConflictError("草稿已过期，请重新准备并确认")
    snapshot = validate_submission_eligibility(session, user_id, draft, payload, now)
    application = AfterSalesApplication(
        id=str(uuid4()),
        draft_id=draft.id,
        user_id=user_id,
        confirmed_version=draft.version,
        status="pending_review",
        snapshot=snapshot,
        created_at=now,
    )
    draft.version += 1
    draft.updated_at = now
    session.add(application)
    session.flush()
    session.expire(draft, ["application"])
    return application


def get_after_sales_application_for_user(
    session: Session,
    user_id: str,
    draft_id: str,
) -> AfterSalesApplication | None:
    return session.scalar(
        select(AfterSalesApplication).where(
            AfterSalesApplication.draft_id == draft_id,
            AfterSalesApplication.user_id == user_id,
        )
    )
