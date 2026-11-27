"""Application configuration loaded from environment variables / .env."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_CORS_ALLOW_ORIGINS: tuple[str, ...] = ("http://localhost:3000",)


def parse_cors_origins(value: Any) -> list[str]:
    """Parse ``CORS_ALLOW_ORIGINS`` from comma-separated string, JSON array, or sequence.

    Wildcard ``*`` is explicitly rejected.
    """
    if value is None:
        return list(DEFAULT_CORS_ALLOW_ORIGINS)
    items: list[str]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            parsed = json.loads(stripped)
            if not isinstance(parsed, list):
                raise ValueError("CORS_ALLOW_ORIGINS JSON value must be a list of origins.")
            items = [str(item) for item in parsed]
        else:
            items = stripped.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        raise ValueError("CORS_ALLOW_ORIGINS must be a comma-separated string or list of origins.")

    origins: list[str] = []
    for item in items:
        origin = item.strip()
        if not origin:
            continue
        if origin == "*":
            raise ValueError("Wildcard '*' is not allowed in CORS_ALLOW_ORIGINS.")
        origins.append(origin)
    return origins


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
    CRAWL_CACHE_TTL_HOURS: int = 48

    APIFY_RUN_TIMEOUT_SECONDS: float = 600.0
    APIFY_SHOPEE_PRODUCT_ACTOR: str = "cliqtomedia~shopee-scraper"
    APIFY_SHOPEE_REVIEWS_ACTOR: str = "themineworks~shopee-reviews-scraper"
    SHOPEE_REVIEW_WINDOW_MONTHS: int = 12
    SHOPEE_LOW_STAR_MAX: int = 150
    SHOPEE_HIGH_STAR_MAX: int = 30

    EMBEDDING_BACKEND: str = "local"
    EMBEDDING_MODEL_NAME: str = "BAAI/bge-m3"
    EMBEDDING_BATCH_SIZE: int = 16
    SENTIMENT_BATCH_SIZE: int = 20
    CHUNK_MAX_CHARS: int = 1200
    CHUNK_OVERLAP_CHARS: int = 150
    HF_HOME: str | None = None

    JOB_HEARTBEAT_INTERVAL_SECONDS: float = 15.0
    JOB_STALE_TIMEOUT_SECONDS: float = 120.0
    JOB_QUEUED_TIMEOUT_SECONDS: float = 600.0
    JOB_STREAM_MAX_SECONDS: float = 3600.0

    SEARCH_MIN_COSINE_SIMILARITY: float = 0.6

    AGENT_MAX_STEPS: int = 8
    AGENT_TOOL_RESULT_MAX_CHARS: int = 6000
    AGENT_REVIEW_MAX_CHARS: int = 500
    AGENT_HISTORY_MESSAGES: int = 10
    AGENT_CRAWL_MAX_PER_TURN: int = 1
    AGENT_CRAWL_MAX_PER_SESSION: int = 3

    LOG_LEVEL: str = "INFO"
    CORS_ALLOW_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_CORS_ALLOW_ORIGINS),
    )
    ADMIN_PASSWORD: SecretStr | None = None
    COOKIE_SECURE: bool = False
    TRUST_PROXY_HEADERS: bool = False

    @field_validator("CORS_ALLOW_ORIGINS", mode="before")
    @classmethod
    def _validate_cors_origins(cls, value: Any) -> list[str]:
        return parse_cors_origins(value)

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
