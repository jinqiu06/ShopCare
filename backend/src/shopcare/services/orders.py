from sqlalchemy import select
from sqlalchemy.orm import Session

from shopcare.db.models import Order, OrderItem


def get_order_for_user(
    session: Session,
    user_id: str,
    order_id: str,
) -> Order | None:
    statement = select(Order).where(Order.id == order_id, Order.user_id == user_id)
    order = session.scalar(statement)

    return order


def list_orders_for_user(
    session: Session, user_id: str, limit: int = 20, offset: int = 0
) -> list[Order]:
    if not 1 <= limit <= 100:
        raise ValueError("limit 必须在 1 到 100 之间")

    if offset < 0:
        raise ValueError("offset 不能为负数")

    statement = (
        select(Order).where(Order.user_id == user_id).order_by(Order.id).offset(offset).limit(limit)
    )

    return list(session.scalars(statement).all())


def get_order_items_for_user(session: Session, user_id: str, order_id: str) -> list[OrderItem]:
    order = get_order_for_user(session, user_id, order_id)
    if order is None:
        return []
    statement = select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id)

    return list(session.scalars(statement).all())
