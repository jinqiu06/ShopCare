from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from shopcare.db.models import Conversation, User


class ConversationTurnConflictError(RuntimeError):
    """会话当前不可领取新的执行轮次。"""


class ConversationNotFoundError(LookupError):
    """The requested conversation is absent or belongs to another user."""


def create_conversation(
    session: Session,
    user_id: str,
    as_of: date,
) -> Conversation:
    user = session.get(User, user_id)
    if user is None:
        raise LookupError("用户不存在")

    id = str(uuid4())
    thread_id = str(uuid4())

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    conversation = Conversation(
        id=id,
        user_id=user_id,
        thread_id=thread_id,
        as_of=as_of,
        status="ready",
        created_at=now,
        updated_at=now,
    )
    session.add(conversation)
    session.flush()

    return conversation


def get_conversation_for_user(
    session: Session,
    user_id: str,
    conversation_id: str,
) -> Conversation | None:
    statement = select(Conversation).where(
        Conversation.user_id == user_id,
        Conversation.id == conversation_id,
    )
    conversation = session.scalar(statement)
    return conversation


def start_conversation_turn(
    session: Session,
    user_id: str,
    conversation_id: str,
) -> Conversation:
    conversation = get_conversation_for_user(
        session,
        user_id,
        conversation_id,
    )
    if conversation is None:
        raise ConversationNotFoundError("会话不存在或不可访问")

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    statement = (
        update(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.status == "ready",
        )
        .values(
            status="running",
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )

    result = session.execute(statement)
    if result.rowcount != 1:
        raise ConversationTurnConflictError("会话当前不可执行")

    session.refresh(conversation)

    return conversation


def finish_conversation_turn(
    session: Session,
    user_id: str,
    conversation_id: str,
    *,
    succeeded: bool,
) -> Conversation:
    if not isinstance(succeeded, bool):
        raise ValueError("参数类型不合法")
    conversation = get_conversation_for_user(
        session,
        user_id,
        conversation_id,
    )
    if conversation is None:
        raise ConversationNotFoundError("会话不存在或不可访问")

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    statement = (
        update(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.status == "running",
        )
        .values(
            status="ready" if succeeded else "failed",
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )

    result = session.execute(statement)
    if result.rowcount != 1:
        raise RuntimeError("会话当前没有正在执行的轮次")

    session.refresh(conversation)

    return conversation
