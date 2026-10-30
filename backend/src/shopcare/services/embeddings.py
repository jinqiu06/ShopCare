from math import sqrt

from langchain_openai import OpenAIEmbeddings

from shopcare.core.config import Settings


def cosine_similarity(
    left: list[float],
    right: list[float],
) -> float:
    if len(left) == 0 or len(right) == 0:
        raise ValueError("列表不能为空")
    if len(left) != len(right):
        raise ValueError("列表长度不同")
    len_left, len_right = 0.0, 0.0
    result = 0.0

    for i in left:
        len_left += i * i

    for j in right:
        len_right += j * j

    len_left = sqrt(len_left)
    len_right = sqrt(len_right)

    if len_left == 0 or len_right == 0:
        raise ValueError("向量长度不能为0")
    for i, j in zip(left, right):
        result += i * j

    similarity = float(result / (len_left * len_right))

    return similarity


def build_embedding_client(settings: Settings) -> OpenAIEmbeddings:
    api_key = settings.embedding_api_key or settings.llm_api_key
    base_url = settings.embedding_base_url or settings.llm_base_url

    if api_key is None or not api_key.get_secret_value().strip():
        raise ValueError("未配置 embedding API Key")

    if not base_url:
        raise ValueError("未配置 embedding 接口地址")

    return OpenAIEmbeddings(
        model=settings.embedding_model_id,
        api_key=api_key,
        base_url=base_url,
        dimensions=settings.embedding_dimensions,
        chunk_size=10,
        check_embedding_ctx_length=False,
        model_kwargs={"encoding_format": "float"},
        request_timeout=30,
        max_retries=0,
    )
