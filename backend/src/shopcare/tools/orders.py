from langchain_core.tools import tool
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.schemas.orders import OrderLookupInput, OrderResponse
from shopcare.services.orders import get_order_for_user


def build_order_tools(session: Session, user_id: str):
    @tool(args_schema=OrderLookupInput)
    def lookup_order(order_id: str) -> dict:
        """按订单号查询当前用户的订单状态、金额和签收时间。"""
        if not order_id.strip():
            return {
                "ok": False,
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "订单号不能为空",
                },
            }
        try:
            order = get_order_for_user(session, user_id, order_id.strip())
        except SQLAlchemyError:
            return {
                "ok": False,
                "error": {
                    "code": "TEMPORARILY_UNAVAILABLE",
                    "message": "订单查询暂时不可用，请稍后重试",
                },
            }
        if not order:
            return {
                "ok": False,
                "error": {
                    "code": "ORDER_NOT_FOUND",
                    "message": "订单不存在或不可访问",
                },
            }
        return {
            "ok": True,
            "data": OrderResponse.model_validate(order).model_dump(mode="json"),
        }

    return [lookup_order]
