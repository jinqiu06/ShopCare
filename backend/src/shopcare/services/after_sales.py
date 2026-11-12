from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from shopcare.db.models import AfterSalesDraft
from shopcare.services.orders import (
    get_order_for_user,
    get_order_items_for_user,
)


class DraftConflictError(RuntimeError):
    """草稿状态或版本已变化。"""


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
