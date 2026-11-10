from unittest.mock import Mock

import pytest
from httpx import Client

from shopcare.core.config import Settings
from shopcare.services import embeddings, llm


@pytest.mark.parametrize("kind", ["chat", "embedding"])
@pytest.mark.parametrize("supplied", [False, True])
def test_model_factory_accepts_caller_owned_http_client_and_keeps_old_calls(
    monkeypatch, kind, supplied
):
    settings = Settings(
        _env_file=None,
        llm_api_key="test-only-key",
        llm_model_id="test-chat",
        llm_base_url="https://chat.invalid/v1",
        embedding_api_key="test-only-embedding-key",
        embedding_base_url="https://embedding.invalid/v1",
    )
    module, factory, sdk_name = (
        (llm, llm.build_chat_client, "ChatOpenAI")
        if kind == "chat"
        else (embeddings, embeddings.build_embedding_client, "OpenAIEmbeddings")
    )
    constructor = Mock()
    monkeypatch.setattr(module, sdk_name, constructor)
    with Client() as http_client:
        result = factory(settings, http_client=http_client) if supplied else factory(settings)
        assert result is constructor.return_value
        assert constructor.call_args.kwargs["http_client"] is (http_client if supplied else None)
        assert constructor.call_args.kwargs["max_retries"] == 0
        assert (
            constructor.call_args.kwargs["timeout" if kind == "chat" else "request_timeout"] == 30
        )
        assert not http_client.is_closed  # 工厂不关闭调用方拥有的客户端。
    assert http_client.is_closed


@pytest.mark.parametrize("kind", ["chat", "embedding"])
def test_model_factory_rejects_missing_credentials_before_sdk_construction(monkeypatch, kind):
    settings = Settings(_env_file=None, llm_api_key=None, embedding_api_key=None)
    module, factory, sdk_name = (
        (llm, llm.build_chat_client, "ChatOpenAI")
        if kind == "chat"
        else (embeddings, embeddings.build_embedding_client, "OpenAIEmbeddings")
    )
    constructor = Mock()
    monkeypatch.setattr(module, sdk_name, constructor)
    with pytest.raises(ValueError, match="API Key"):
        factory(settings)
    constructor.assert_not_called()
