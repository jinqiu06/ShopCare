from math import isfinite

from langchain_openai import OpenAIEmbeddings
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from shopcare.db.models import PolicyChunkRecord, PolicyEmbeddingRecord


def embed_policy_chunks(
    client: OpenAIEmbeddings,
    chunks: list[PolicyChunkRecord],
) -> list[list[float]]:
    if not chunks:
        return []

    dimensions = client.dimensions
    if dimensions is None:
        raise ValueError("未配置向量维度")

    texts: list[str] = []
    for chunk in chunks:
        text = f"{chunk.title}\n{chunk.text}"
        texts.append(text)

    vectors = client.embed_documents(texts)

    if len(vectors) != len(chunks):
        raise ValueError("向量数量与片段数量不一致")

    for vector in vectors:
        if len(vector) != dimensions:
            raise ValueError("向量维度不正确")

    return vectors


def save_policy_embeddings(
    session: Session,
    chunks: list[PolicyChunkRecord],
    vectors: list[list[float]],
    model_id: str,
    dimensions: int,
) -> int:
    if not model_id.strip():
        raise ValueError("model_id 不合法")
    if type(dimensions) is not int or dimensions <= 0:
        raise ValueError("向量维数必须是正整数")

    if len(chunks) != len(vectors):
        raise ValueError("向量数量与片段数量不一致")
    if len(chunks) == len(vectors) == 0:
        return 0
    seen: set = set()
    for chunk in chunks:
        if chunk.chunk_id not in seen:
            seen.add(chunk.chunk_id)
    if len(seen) != len(chunks):
        raise ValueError("片段ID重复")

    for vector in vectors:
        if len(vector) != dimensions:
            raise ValueError("向量维度不正确")
        for value in vector:
            if type(value) not in (int, float) or not isfinite(value):
                raise ValueError("向量必须由有限数值组成")
        if all(value == 0 for value in vector):
            raise ValueError("向量不能全为零")

    chunk_ids = [chunk.chunk_id for chunk in chunks]

    statement = delete(PolicyEmbeddingRecord).where(PolicyEmbeddingRecord.chunk_id.in_(chunk_ids))

    session.execute(statement)

    for chunk, vector in zip(chunks, vectors):
        record = PolicyEmbeddingRecord(
            chunk_id=chunk.chunk_id,
            model_id=model_id,
            dimensions=dimensions,
            vector=vector,
        )
        session.add(record)

    session.flush()

    return len(chunks)


def load_policy_embeddings(
    session: Session,
    chunks: list[PolicyChunkRecord],
    model_id: str,
    dimensions: int,
) -> dict[str, PolicyEmbeddingRecord]:
    if not chunks:
        return {}

    chunk_ids = [chunk.chunk_id for chunk in chunks]

    statement = select(PolicyEmbeddingRecord).where(
        PolicyEmbeddingRecord.chunk_id.in_(chunk_ids),
        PolicyEmbeddingRecord.model_id == model_id,
        PolicyEmbeddingRecord.dimensions == dimensions,
    )

    result: dict[str, PolicyEmbeddingRecord] = {}

    for record in session.scalars(statement):
        result[record.chunk_id] = record

    for chunk_id in chunk_ids:
        if chunk_id not in result:
            raise ValueError("政策向量缺失或配置不匹配，请重新构建索引")

    return result
