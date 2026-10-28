import re
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from shopcare.db.models import PolicyChunkRecord
from shopcare.schemas.policy_models import PolicyChunk, PolicySearchHit


def list_effective_chunks(
    session: Session,
    category: str,
    as_of: date,
) -> list[PolicyChunkRecord]:
    statement = (
        select(PolicyChunkRecord)
        .where(
            PolicyChunkRecord.category == category,
            PolicyChunkRecord.effective_from <= as_of,
            or_(
                PolicyChunkRecord.effective_until.is_(None),
                PolicyChunkRecord.effective_until > as_of,
            ),
        )
        .order_by(
            PolicyChunkRecord.policy_id,
            PolicyChunkRecord.version,
            PolicyChunkRecord.start_char,
        )
    )

    return list(session.scalars(statement).all())


def tokenize_query(query: str) -> set[str]:
    parts = re.findall(
        r"[\u4e00-\u9fff]+|[a-z0-9]+",
        query.lower(),
    )

    tokens: set[str] = set()

    for part in parts:
        if part.isascii() or len(part) == 1:
            tokens.add(part)
        else:
            for i in range(len(part) - 1):
                tokens.add(part[i : i + 2])

    return tokens


def score_chunk(
    chunk: PolicyChunkRecord,
    tokens: set[str],
) -> int:
    score = 0

    context = f"{chunk.title} {chunk.text}".lower()

    for token in tokens:
        if token in context:
            score += 1

    return score


def search_policy_chunks(
    session: Session,
    query: str,
    category: str,
    as_of: date,
    top_k: int = 3,
) -> list[PolicySearchHit]:
    if type(top_k) is not int or not (1 <= top_k <= 10):
        raise ValueError("top_k必须是1到10的整数")

    tokens = tokenize_query(query)

    if not tokens:
        return []

    chunks = list_effective_chunks(session, category, as_of)

    hits = []

    for chunk in chunks:
        score = score_chunk(chunk, tokens)

        if score > 0:
            hit = PolicySearchHit(
                chunk=PolicyChunk.model_validate(chunk, from_attributes=True), score=score
            )
            hits.append(hit)

    hits.sort(key=lambda hit: (-hit.score, hit.chunk.chunk_id))

    return hits[:top_k]
