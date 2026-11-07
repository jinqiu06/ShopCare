from datetime import date

from langchain_core.tools import tool
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.schemas.orders import OrderLookupInput, OrderResponse
from shopcare.services.answer_generation import get_days_since_delivery
from shopcare.services.orders import get_order_for_user


def build_order_tools(session: Session, user_id: str, as_of: date):
    @tool(args_schema=OrderLookupInput)
    def lookup_order(order_id: str) -> dict:
        """查询当前用户订单及服务端计算的签收天数；未知签收时间时天数为null。"""
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
        data = OrderResponse.model_validate(order)
        try:
            days_since_delivery = get_days_since_delivery(data, as_of)
        except ValueError:
            return {
                "ok": False,
                "error": {
                    "code": "INVALID_REFERENCE_DATE",
                    "message": "参照日期早于签收日期，无法计算签收天数",
                },
            }
        return {
            "ok": True,
            "data": {
                **data.model_dump(mode="json"),
                "as_of": as_of.isoformat(),
                "days_since_delivery": days_since_delivery,
            },
        }

    return [lookup_order]
