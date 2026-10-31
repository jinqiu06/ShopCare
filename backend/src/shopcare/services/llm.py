from langchain_openai import ChatOpenAI

from shopcare.core.config import Settings


def build_chat_client(settings: Settings) -> ChatOpenAI:
    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value().strip():
        raise ValueError("未配置对话模型 API Key")
    if not settings.llm_model_id or not settings.llm_model_id.strip():
        raise ValueError("未配置对话模型名称")

    if not settings.llm_base_url or not settings.llm_base_url.strip():
        raise ValueError("未配置对话模型接口地址")
    return ChatOpenAI(
        model=settings.llm_model_id,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=0,
        timeout=30,
        max_retries=0,
    )
