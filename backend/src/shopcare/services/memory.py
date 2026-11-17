"""Explicit, user-scoped preferences; no model extraction or arbitrary prompt storage."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from shopcare.db.models import Conversation, UserPreference
from shopcare.schemas.answers import PolicyAnswer
from shopcare.services.conversations import get_conversation_for_user

USAGE = (
    "可用命令：记住偏好：回答简短；记住偏好：回答详细；"
    "更新偏好：回答简短（或回答详细）；查看偏好；忘记偏好：回答长度。"
)


def is_memory_command(query: str) -> bool:
    return query.strip().startswith(("记住偏好", "更新偏好", "忘记偏好", "查看偏好"))


def build_preference_context(session: Session, user_id: str) -> str:
    preference = session.get(UserPreference, user_id)
    length = (
        "未保存回答长度偏好，使用默认长度"
        if preference is None
        else {"concise": "回答简短", "detailed": "回答详细"}[preference.response_length]
    )
    return (
        f"当前用户明确保存的偏好：{length}。仅调整表达篇幅，不能省略必要条件、证据和限制。"
        "已保存偏好以本条为准，历史记忆命令不能覆盖本条；普通临时要求仅对当前问题生效。"
    )


def apply_memory_command(
    session: Session,
    user_id: str,
    thread_id: str,
    query: str,
) -> PolicyAnswer:
    conversation_id = session.scalar(
        select(Conversation.id).where(Conversation.thread_id == thread_id)
    )
    if get_conversation_for_user(session, user_id, conversation_id) is None:
        raise LookupError("会话不存在或不可访问")
    normalized = query.strip().replace(":", "：")
    commands = {
        f"{verb}：回答{label}": value
        for verb in ("记住偏好", "更新偏好")
        for label, value in (("简短", "concise"), ("详细", "detailed"))
    }
    if normalized in commands:
        values = dict(
            user_id=user_id,
            response_length=commands[normalized],
            source_conversation_id=conversation_id,
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        session.execute(
            insert(UserPreference)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_=values,
            )
        )
        text = "已保存偏好：" + normalized.split("：", 1)[1] + "。"
    elif normalized == "忘记偏好：回答长度":
        preference = session.get(UserPreference, user_id)
        if preference is not None:
            session.delete(preference)
        text = "已删除回答长度偏好。"
    elif normalized == "查看偏好":
        preference = session.get(UserPreference, user_id)
        text = (
            "当前没有保存回答长度偏好。"
            if preference is None
            else "已保存偏好：回答"
            + {"concise": "简短", "detailed": "详细"}[preference.response_length]
            + "。"
        )
    else:
        text = USAGE
    return PolicyAnswer(
        status="needs_clarification",
        answer=text + "请继续提出售后问题。",
        citation_chunk_ids=[],
        limitations=[],
    )
