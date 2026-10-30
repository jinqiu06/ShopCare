from datetime import date
from math import isfinite

from langchain_openai import OpenAIEmbeddings
from sqlalchemy.orm import Session

from shopcare.schemas.policy_models import (
    PolicyChunk,
    SemanticPolicySearchHit,
)
from shopcare.services.embeddings import cosine_similarity
from shopcare.services.policy_embeddings import load_policy_embeddings
from shopcare.services.policy_retriever import list_effective_chunks


def search_semantic_policy_chunks(
    session: Session,
    client: OpenAIEmbeddings,
    query: str,
    category: str,
    as_of: date,
    top_k: int = 3,
) -> list[SemanticPolicySearchHit]:
    if type(top_k) is not int or not (1 <= top_k <= 10):
        raise ValueError("top_k数值不合法")

    if not query.strip():
        return []

    chunks = list_effective_chunks(session, category, as_of)
    if not chunks:
        return []

    if not client.dimensions:
        raise ValueError("dimensions未配置")
    embeddings = load_policy_embeddings(
        session,
        chunks,
        client.model,
        client.dimensions,
    )
    query_vector = client.embed_query(query)

    if len(query_vector) != client.dimensions:
        raise ValueError("问题向量维度不正确")

    for value in query_vector:
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError("问题向量必须由有限数值组成")

    if all(value == 0 for value in query_vector):
        raise ValueError("问题向量不能全为零")

    result: list[SemanticPolicySearchHit] = []

    for chunk in chunks:
        embedding = embeddings[chunk.chunk_id]
        score = cosine_similarity(query_vector, embedding.vector)

        hit = SemanticPolicySearchHit(
            chunk=PolicyChunk.model_validate(chunk, from_attributes=True),
            score=score,
        )
        result.append(hit)
    result.sort(key=lambda hit: (-hit.score, hit.chunk.chunk_id))

    return result[:top_k]
