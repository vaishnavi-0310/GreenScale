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
    log_level: str = "INFO"

    # Google Cloud Vertex AI configuration
    use_vertex_ai: bool = False
    google_cloud_project: str = ""
    google_cloud_location: str = "global"


settings = Settings(log_level="INFO")