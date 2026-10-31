from datetime import date

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from sqlalchemy.orm import Session

from shopcare.schemas.answers import PolicyAnswer
from shopcare.schemas.orders import OrderItemResponse, OrderResponse
from shopcare.services.answer_generation import generate_policy_answer
from shopcare.services.orders import (
    get_order_for_user,
    get_order_items_for_user,
)
from shopcare.services.semantic_retriever import search_semantic_policy_chunks


def get_order_answer_context(
    session: Session,
    user_id: str,
    order_id: str,
    item_id: str,
) -> tuple[OrderResponse, OrderItemResponse]:
    order = get_order_for_user(session, user_id, order_id)

    if not order:
        raise LookupError("订单不存在或不可访问")

    order_items = get_order_items_for_user(session, user_id, order_id)

    for item in order_items:
        if item.id == item_id:
            return (
                OrderResponse.model_validate(order),
                OrderItemResponse.model_validate(item),
            )
    raise LookupError("指定商品不在该订单中")


def answer_order_question(
    session: Session,
    embedding_client: OpenAIEmbeddings,
    chat_client: ChatOpenAI,
    user_id: str,
    order_id: str,
    item_id: str,
    query: str,
    as_of: date,
) -> tuple[PolicyAnswer, AIMessage]:
    if not query.strip():
        raise ValueError("问题不能为空")

    order, item = get_order_answer_context(session, user_id, order_id, item_id)

    evidence = search_semantic_policy_chunks(
        session,
        embedding_client,
        query,
        item.category,
        as_of,
        top_k=3,
    )

    return generate_policy_answer(
        chat_client,
        query,
        order,
        [item],
        evidence,
        as_of,
    )
