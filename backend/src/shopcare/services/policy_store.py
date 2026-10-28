from sqlalchemy import delete
from sqlalchemy.orm import Session

from shopcare.db.models import PolicyChunkRecord
from shopcare.schemas.policy_models import PolicyChunk, PolicyDocument


def replace_policy_chunks(
    session: Session,
    policy: PolicyDocument,
    chunks: list[PolicyChunk],
) -> int:
    if not chunks:
        raise ValueError("chunks不能为空")

    seen = set()
    for chunk in chunks:
        if chunk.chunk_id not in seen:
            seen.add(chunk.chunk_id)
        else:
            raise ValueError("chunk_id不能重复")

        if (
            chunk.policy_id != policy.policy_id
            or chunk.version != policy.version
            or chunk.title != policy.title
            or chunk.category != policy.category
            or chunk.effective_from != policy.effective_from
            or chunk.effective_until != policy.effective_until
        ):
            raise ValueError("chunk信息与policy不一致")

        if not (0 <= chunk.start_char < chunk.end_char <= len(policy.content)):
            raise ValueError("chunk 字符范围不合法")

        if chunk.text != policy.content[chunk.start_char : chunk.end_char]:
            raise ValueError("chunk内容与正文不符")

    statement = delete(PolicyChunkRecord).where(
        PolicyChunkRecord.policy_id == policy.policy_id, PolicyChunkRecord.version == policy.version
    )

    session.execute(statement)

    for chunk in chunks:
        record = PolicyChunkRecord(**chunk.model_dump())
        session.add(record)

    session.flush()
    return len(chunks)
