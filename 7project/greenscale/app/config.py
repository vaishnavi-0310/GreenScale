from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://greenscale:greenscale@localhost:5432/greenscale"
    )
    # Empty by default: set GEMINI_API_KEY in your local .env (never commit it).
    # When empty, LLM features fall back to deterministic defaults.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"


settings = Settings()