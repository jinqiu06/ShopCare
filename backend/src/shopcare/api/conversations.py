from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.api.dependencies import get_current_user_id, get_session
from shopcare.schemas.conversations import (
    ConversationCreateRequest,
    ConversationResponse,
)
from shopcare.services.conversations import (
    create_conversation,
    get_conversation_for_user,
)

router = APIRouter(
    prefix="/api/conversations",
    tags=["conversations"],
)


@router.post(
    "",
    response_model=ConversationResponse,
    status_code=201,
)
def create_conversation_api(
    payload: ConversationCreateRequest,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        with session.begin():
            conversation = create_conversation(
                session,
                user_id,
                payload.as_of,
            )
            response = ConversationResponse.model_validate(conversation)
    except LookupError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "USER_NOT_FOUND",
                "message": "当前用户不存在",
            },
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CONVERSATION_UNAVAILABLE",
                "message": "会话服务暂不可用，请稍后重试",
            },
        ) from None

    return response


@router.get(
    "/{conversation_id}",
    response_model=ConversationResponse,
)
def read_conversation(
    conversation_id: str,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        conversation = get_conversation_for_user(
            session,
            user_id,
            conversation_id,
        )
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CONVERSATION_UNAVAILABLE",
                "message": "会话服务暂不可用，请稍后重试",
            },
        ) from None

    if conversation is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "CONVERSATION_NOT_FOUND",
                "message": "会话不存在或不可访问",
            },
        )

    return conversation
