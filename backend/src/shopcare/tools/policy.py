from datetime import date

from langchain_core.tools import tool
from langchain_openai import OpenAIEmbeddings
from openai import OpenAIError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.schemas.policy_models import OrderPolicySearchInput
from shopcare.services.order_answers import get_order_answer_context
from shopcare.services.semantic_retriever import search_semantic_policy_chunks


def build_policy_tools(
    session: Session,
    embedding_client: OpenAIEmbeddings,
    user_id: str,
    as_of: date,
):
    @tool(args_schema=OrderPolicySearchInput)
    def search_order_policies(order_id: str, item_id: str, query: str) -> dict:
        """查询当前用户指定订单商品适用的政策证据，不判断最终售后资格。"""
        if not all(value.strip() for value in (order_id, item_id, query)):
            return {
                "ok": False,
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "订单号、商品项编号和问题不能为空",
                },
            }
        try:
            _, item = get_order_answer_context(session, user_id, order_id.strip(), item_id.strip())
            hits = search_semantic_policy_chunks(
                session,
                embedding_client,
                query.strip(),
                item.category,
                as_of,
                top_k=3,
            )
        except LookupError:
            return {
                "ok": False,
                "error": {
                    "code": "ORDER_CONTEXT_NOT_FOUND",
                    "message": "订单或商品不存在或不可访问",
                },
            }
        except (SQLAlchemyError, OpenAIError, TimeoutError, ValueError):
            return {
                "ok": False,
                "error": {
                    "code": "RETRIEVAL_UNAVAILABLE",
                    "message": "政策检索暂时不可用",
                },
            }
        return {
            "ok": True,
            "data": [hit.model_dump(mode="json") for hit in hits],
        }

    return [search_order_policies]
