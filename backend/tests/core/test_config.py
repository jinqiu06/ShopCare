import pytest

from shopcare.core.config import Settings


@pytest.fixture(autouse=True)
def clear_settings_env(monkeypatch):
    names = [
        "APP_NAME",
        "APP_VERSION",
        "LLM_API_KEY",
        "LLM_MODEL_ID",
        "LLM_BASE_URL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_MODEL_ID",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_DIMENSIONS",
    ]

    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_settings_defaults():
    settings = Settings(_env_file=None)

    assert settings.app_name == "ShopCare API"
    assert settings.app_version == "0.1.0"
    assert settings.llm_api_key is None
    assert settings.llm_model_id is None
    assert settings.llm_base_url is None
    assert settings.embedding_api_key is None
    assert settings.embedding_model_id == "text-embedding-v4"
    assert settings.embedding_base_url is None
    assert settings.embedding_dimensions == 1024


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("APP_NAME", "ShopCare Test")
    monkeypatch.setenv("APP_VERSION", "0.2.0")

    settings = Settings(_env_file=None)

    assert settings.app_name == "ShopCare Test"
    assert settings.app_version == "0.2.0"


def test_read_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_NAME=ShopCare From File\nLLM_API_KEY=\n",
        encoding="utf-8",
    )
    setting = Settings(_env_file=env_file)

    assert setting.app_name == "ShopCare From File"
    assert setting.llm_api_key is None


def test_environment_overrides_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_NAME=Name From File\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_NAME", "Name From Environment")

    settings = Settings(_env_file=env_file)

    assert settings.app_name == "Name From Environment"


def test_embedding_settings_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EMBEDDING_API_KEY=test-only-key\n"
        "EMBEDDING_MODEL_ID=text-embedding-v4\n"
        "EMBEDDING_BASE_URL=https://example.com/v1\n"
        "EMBEDDING_DIMENSIONS=512\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)
    assert settings.embedding_api_key.get_secret_value() == "test-only-key"
    assert settings.embedding_model_id == "text-embedding-v4"
    assert settings.embedding_base_url == "https://example.com/v1"
    assert settings.embedding_dimensions == 512
    assert "test-only-key" not in repr(settings)
