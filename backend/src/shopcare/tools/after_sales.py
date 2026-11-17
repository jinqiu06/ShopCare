from datetime import date, datetime, timezone

from langchain_core.tools import tool
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.schemas.after_sales import (
    AfterSalesConfirmRequest,
    AfterSalesDraftResponse,
    AfterSalesFacts,
    AfterSalesPrepareInput,
)
from shopcare.services.after_sales import (
    SubmissionNotEligibleError,
    create_after_sales_draft,
    validate_submission_eligibility,
)


def build_after_sales_tools(engine: Engine, user_id: str, as_of: date, *, allow_prepare: bool):
    @tool(args_schema=AfterSalesPrepareInput)
    def prepare_after_sales_draft(
        order_id: str,
        item_id: str,
        request_type: str,
        reason: str,
        facts: AfterSalesFacts,
    ) -> dict:
        """用户明确申请时准备待确认草稿。facts仅填写用户已描述的事实；绝不提交申请。"""
        if not allow_prepare:
            return {
                "ok": False,
                "error": {
                    "code": "EXPLICIT_INTENT_REQUIRED",
                    "message": (
                        "准备草稿需用户发送以“申请售后：”开头的明确请求。普通咨询和“好”不授权写入。"
                    ),
                },
            }
        try:
            with Session(engine) as session, session.begin():
                draft = create_after_sales_draft(
                    session,
                    user_id,
                    order_id,
                    item_id,
                    request_type,
                    reason,
                    as_of,
                )
                validate_submission_eligibility(
                    session,
                    user_id,
                    draft,
                    AfterSalesConfirmRequest(expected_version=1, confirmed=False, facts=facts),
                    datetime.now(timezone.utc).replace(tzinfo=None),
                )
                data = {
                    "draft": AfterSalesDraftResponse.model_validate(draft).model_dump(mode="json"),
                    "facts": facts.model_dump(exclude_none=True),
                }
            return {"ok": True, "data": data}
        except LookupError:
            return {
                "ok": False,
                "error": {
                    "code": "ORDER_CONTEXT_NOT_FOUND",
                    "message": "订单或商品不存在或不可访问",
                },
            }
        except SubmissionNotEligibleError as error:
            return {
                "ok": False,
                "error": {
                    "code": "SUBMISSION_NOT_ELIGIBLE",
                    "message": str(error),
                },
            }
        except SQLAlchemyError:
            return {
                "ok": False,
                "error": {
                    "code": "TEMPORARILY_UNAVAILABLE",
                    "message": "草稿服务暂不可用，请查询后再操作",
                },
            }

    return [prepare_after_sales_draft]
