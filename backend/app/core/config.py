"""Application configuration loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide application settings.

    Note: ``Settings`` inherits from Pydantic's ``BaseSettings`` which defines
    its own metaclass (``ModelMetaclass``) and therefore cannot directly use
    ``SingletonMeta``. Singleton behavior is provided via ``get_settings()``
    decorated with ``functools.lru_cache``.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = "postgresql+asyncpg://shopqa:shopqa@localhost:5433/shopqa"
    REDIS_URL: str = "redis://localhost:6380/0"
    SECRET_KEY: SecretStr

    ALLOW_PRIVATE_LLM_HOSTS: bool = True
    ALLOW_PRIVATE_CRAWL_HOSTS: bool = False

    LLM_TIMEOUT_SECONDS: float = 60.0
    LLM_PROBE_TIMEOUT_SECONDS: float = 20.0
    LLM_MAX_RETRIES: int = 2
    LLM_REGISTRY_MAX_CLIENTS: int = 64

    CRAWL_RATE_PER_SECOND: float = 1.0
    CRAWL_MAX_RETRIES: int = 3
    CRAWL_TIMEOUT_SECONDS: float = 20.0
    CRAWL_MAX_PRODUCTS: int = 100
    CRAWL_MAX_REVIEWS_PER_PRODUCT: int = 200
    CRAWL_CONCURRENCY: int = 2
    CRAWL_CACHE_TTL_HOURS: int = 24

    EMBEDDING_BACKEND: str = "local"
    EMBEDDING_MODEL_NAME: str = "BAAI/bge-m3"
    EMBEDDING_BATCH_SIZE: int = 16
    SENTIMENT_BATCH_SIZE: int = 20
    CHUNK_MAX_CHARS: int = 1200
    CHUNK_OVERLAP_CHARS: int = 150
    HF_HOME: str | None = None

    LOG_LEVEL: str = "INFO"

    @field_validator("SECRET_KEY")
    @classmethod
    def _validate_fernet_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if not raw:
            raise ValueError(
                "SECRET_KEY is required and must be a valid Fernet key "
                "(32 url-safe base64-encoded bytes)."
            )
        try:
            Fernet(raw.encode("utf-8"))
        except Exception as exc:
            raise ValueError(
                "SECRET_KEY must be a valid Fernet key (32 url-safe base64-encoded bytes). "
                'Generate one with: python -c "from cryptography.fernet import Fernet;'
                'print(Fernet.generate_key().decode())"'
            ) from exc
        return SecretStr(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton ``Settings`` instance.

    Uses ``functools.lru_cache`` instead of ``SingletonMeta`` because Pydantic's
    ``BaseSettings`` already uses ``ModelMetaclass``. Call
    ``get_settings.cache_clear()`` in tests when changing environment variables.
    """
    return Settings()
