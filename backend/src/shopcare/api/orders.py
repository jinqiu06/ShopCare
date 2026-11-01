from fastapi import APIRouter, Depends, HTTPException, Query
from openai import APITimeoutError, OpenAIError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.api.dependencies import get_current_user_id, get_session
from shopcare.core.config import Settings
from shopcare.schemas.answers import OrderAnswerRequest, PolicyAnswer
from shopcare.schemas.orders import OrderItemResponse, OrderResponse
from shopcare.services.embeddings import build_embedding_client
from shopcare.services.llm import build_chat_client
from shopcare.services.order_answers import answer_order_question
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


@router.post("/{order_id}/answer", response_model=PolicyAnswer)
def answer_order(
    order_id: str,
    body: OrderAnswerRequest,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        settings = Settings()
        embedding_client = build_embedding_client(settings)
        chat_client = build_chat_client(settings)
    except ValueError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ANSWER_NOT_CONFIGURED",
                "message": "回答服务配置暂不可用",
            },
        ) from None

    try:
        answer, _ = answer_order_question(
            session,
            embedding_client,
            chat_client,
            user_id,
            order_id,
            body.item_id,
            body.query,
            body.as_of,
        )
    except LookupError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "ORDER_CONTEXT_NOT_FOUND",
                "message": "订单或商品不存在或不可访问",
            },
        ) from None
    except (APITimeoutError, TimeoutError):
        raise HTTPException(
            status_code=504,
            detail={
                "code": "ANSWER_TIMEOUT",
                "message": "回答服务超时，请稍后重试",
            },
        ) from None
    except OpenAIError:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "ANSWER_UPSTREAM_ERROR",
                "message": "回答服务暂时不可用",
            },
        ) from None
    except (ValueError, SQLAlchemyError):
        raise HTTPException(
            status_code=500,
            detail={
                "code": "ANSWER_FAILED",
                "message": "暂时无法生成可靠回答",
            },
        ) from None
    return answer
