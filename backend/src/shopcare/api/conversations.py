import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.api.dependencies import (
    get_current_user_id,
    get_session,
    initialize_agent_checkpoint,
    open_agent_checkpointer,
)
from shopcare.api.errors import conversation_message_http_error
from shopcare.schemas.answers import PolicyAnswer
from shopcare.schemas.conversations import (
    ConversationCreateRequest,
    ConversationMessageRequest,
    ConversationResponse,
)
from shopcare.services.conversation_messages import ConversationMessageError, send_message
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
    request: Request,
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
            with open_agent_checkpointer(
                request.app.state.engine,
                request.app.state.settings.agent_checkpoint_db,
                create=True,
            ) as checkpointer:
                initialize_agent_checkpoint(
                    checkpointer,
                    conversation.thread_id,
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
    except (SQLAlchemyError, sqlite3.Error, OSError, ValueError):
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


@router.post(
    "/{conversation_id}/messages",
    response_model=PolicyAnswer,
)
def send_conversation_message(
    conversation_id: str,
    payload: ConversationMessageRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    try:
        return send_message(
            request.app.state.engine,
            request.app.state.settings,
            user_id,
            conversation_id,
            payload.query,
        )
    except ConversationMessageError as error:
        raise conversation_message_http_error(error.code) from None
