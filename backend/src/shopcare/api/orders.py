from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from shopcare.api.dependencies import get_current_user_id, get_session
from shopcare.schemas.orders import OrderItemResponse, OrderResponse
from shopcare.services.orders import (
    get_order_for_user,
    get_order_items_for_user,
    list_orders_for_user,
)

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("", response_model=list[OrderResponse])
def read_orders(
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return list_orders_for_user(session, user_id, limit=limit, offset=offset)


@router.get("/{order_id}", response_model=OrderResponse)
def read_order(
    order_id: str,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    order = get_order_for_user(session, user_id, order_id)

    if order is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "ORDER_NOT_FOUND", "message": "订单不存在或不可访问"},
        )

    return order


@router.get(
    "/{order_id}/items",
    response_model=list[OrderItemResponse],
)
def read_order_items(
    order_id: str,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    order = get_order_for_user(session, user_id, order_id)

    if order is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "ORDER_NOT_FOUND",
                "message": "订单不存在或不可访问",
            },
        )

    return get_order_items_for_user(session, user_id, order_id)
