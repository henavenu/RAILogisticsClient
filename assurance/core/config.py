from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    arize_api_key: str = ""
    arize_space_id: str = ""
    assurance_provider: str = "mock"   # arize | local | mock
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    store_backend: str = "sqlite"      # sqlite | postgres
    database_url: str = ""             # postgresql+asyncpg://user:pass@host/dbname

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
