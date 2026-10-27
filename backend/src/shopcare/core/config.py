from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "ShopCare API"
    app_version: str = "0.1.0"
    llm_api_key: SecretStr | None = None
    llm_model_id: str | None = None
    llm_base_url: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
    )

    database_url: str = "sqlite:///./data/shopcare.db"
