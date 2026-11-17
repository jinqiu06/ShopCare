from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.api.dependencies import get_current_user_id, get_session
from shopcare.schemas.after_sales import (
    AfterSalesApplicationResponse,
    AfterSalesConfirmRequest,
    AfterSalesDraftCancelRequest,
    AfterSalesDraftCreateRequest,
    AfterSalesDraftResponse,
)
from shopcare.services.after_sales import (
    DraftConflictError,
    SubmissionNotEligibleError,
    cancel_after_sales_draft,
    confirm_after_sales_draft,
    create_after_sales_draft,
    get_after_sales_application_for_user,
    get_after_sales_draft_for_user,
)

router = APIRouter(
    prefix="/api/after-sales/drafts",
    tags=["after-sales"],
)


@router.post(
    "",
    response_model=AfterSalesDraftResponse,
    status_code=201,
)
def create_draft(
    payload: AfterSalesDraftCreateRequest,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        with session.begin():
            draft = create_after_sales_draft(
                session,
                user_id,
                payload.order_id,
                payload.item_id,
                payload.request_type,
                payload.reason,
                payload.as_of,
            )
            response = AfterSalesDraftResponse.model_validate(draft)
    except LookupError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "ORDER_CONTEXT_NOT_FOUND",
                "message": "订单或商品不存在或不可访问",
            },
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DRAFT_UNAVAILABLE",
                "message": "草稿服务暂不可用，请稍后重试",
            },
        ) from None
    return response


@router.post("/{draft_id}/confirm", response_model=AfterSalesApplicationResponse)
def confirm_draft(
    draft_id: str,
    payload: AfterSalesConfirmRequest,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        with session.begin():
            application = confirm_after_sales_draft(session, user_id, draft_id, payload)
            response = AfterSalesApplicationResponse.model_validate(application)
    except LookupError:
        raise HTTPException(
            404, detail={"code": "DRAFT_NOT_FOUND", "message": "草稿不存在或不可访问"}
        ) from None
    except DraftConflictError:
        raise HTTPException(
            409,
            detail={
                "code": "DRAFT_CONFLICT",
                "message": "请明确确认有效草稿及其版本，已提交时须使用原确认内容",
            },
        ) from None
    except SubmissionNotEligibleError as error:
        raise HTTPException(
            409, detail={"code": "SUBMISSION_NOT_ELIGIBLE", "message": str(error)}
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            503,
            detail={
                "code": "APPLICATION_UNAVAILABLE",
                "message": "申请服务暂不可用，请查询申请结果后重试",
            },
        ) from None
    return response


@router.get("/{draft_id}/application", response_model=AfterSalesApplicationResponse)
def read_application(
    draft_id: str,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        application = get_after_sales_application_for_user(session, user_id, draft_id)
        if application is None:
            raise HTTPException(
                404, detail={"code": "APPLICATION_NOT_FOUND", "message": "申请不存在或不可访问"}
            )
        return AfterSalesApplicationResponse.model_validate(application)
    except SQLAlchemyError:
        raise HTTPException(
            503,
            detail={"code": "APPLICATION_UNAVAILABLE", "message": "申请服务暂不可用，请稍后查询"},
        ) from None


@router.get(
    "/{draft_id}",
    response_model=AfterSalesDraftResponse,
)
def read_draft(
    draft_id: str,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        draft = get_after_sales_draft_for_user(session, user_id, draft_id)

    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DRAFT_UNAVAILABLE",
                "message": "草稿服务暂不可用，请稍后重试",
            },
        ) from None

    if draft is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "DRAFT_NOT_FOUND",
                "message": "草稿不存在或不可访问",
            },
        ) from None
    return draft


@router.post(
    "/{draft_id}/cancel",
    response_model=AfterSalesDraftResponse,
)
def cancel_draft(
    draft_id: str,
    payload: AfterSalesDraftCancelRequest,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        with session.begin():
            draft = cancel_after_sales_draft(
                session,
                user_id,
                draft_id,
                payload.expected_version,
            )
            response = AfterSalesDraftResponse.model_validate(draft)

    except LookupError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "DRAFT_NOT_FOUND",
                "message": "草稿不存在或不可访问",
            },
        ) from None
    except DraftConflictError:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DRAFT_CONFLICT",
                "message": "草稿状态或版本已变化，请重新查询",
            },
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DRAFT_UNAVAILABLE",
                "message": "草稿服务暂不可用，请稍后重试",
            },
        ) from None
    return response
