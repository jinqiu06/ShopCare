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
