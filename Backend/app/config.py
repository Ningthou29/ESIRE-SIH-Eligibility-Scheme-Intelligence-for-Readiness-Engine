from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "PAANGBA"
    environment: str = "development"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7

    database_url: str = "sqlite:///./PAANGBA.db"

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "PAANGBA"

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "PAANGBA-dev-password"

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2"
    ollama_timeout_seconds: float = 45.0

    # Ollama is never used to decide eligibility (that's deterministic —
    # see z3_engine.py) and is never called once per scheme. Only the top
    # N schemes actually shown to the user get an AI-rewritten explanation;
    # everything else uses the deterministic, rule-based explanation text.
    ai_explanations_enabled: bool = True
    ai_explanation_top_n: int = 5

    otp_ttl_seconds: int = 300
    otp_echo: bool = True
    otp_static_code: str = "123456"

    eligibility_weight: float = 0.60
    document_weight: float = 0.40
    display_threshold: float = 50.0

    admin_api_key: str = "PAANGBA-admin-dev"
    upload_dir: str = "./uploads"
    allow_inmemory_fallback: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
