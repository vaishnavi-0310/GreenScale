from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://greenscale:greenscale@localhost:5432/greenscale"
    )
    gemini_api_key: str = "REMOVED_GEMINI_API_KEY"
    gemini_model: str = "gemini-3.8-flash"


settings = Settings()