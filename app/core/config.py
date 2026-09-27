"""Application settings.

Every value comes from the environment (or a local ``.env``). Nothing sensitive
is ever defaulted here: secrets are typed ``SecretStr`` and default to ``None``
so a missing one fails loudly at the point of use rather than silently working
with a placeholder.

Settings for subsystems that arrive in later parts are declared now, grouped by
prefix, so ``.env.example`` and this module stay a single description of the
service's configuration surface.
"""

from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Annotated
from urllib.parse import quote_plus

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = ["AppEnv", "LogLevel", "Settings", "get_settings"]


class AppEnv(StrEnum):
    """Deployment environment. Drives log rendering and error verbosity."""

    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# Lists are accepted as comma-separated strings, which reads far better in a
# .env file than JSON. NoDecode turns off pydantic-settings' JSON pre-parse so
# the validator below sees the raw string.
CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    """The full configuration surface of the backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ---------------------------------------------------------------- app
    APP_NAME: str = "browser-agent-backend"
    APP_ENV: AppEnv = AppEnv.DEV
    APP_VERSION: str = "0.1.0"
    API_PREFIX: str = "/api/v1"
    CORS_ORIGINS: CsvList = Field(default_factory=lambda: ["http://localhost:3000"])
    LOG_LEVEL: LogLevel = LogLevel.INFO

    # ---------------------------------------------------------------- rate limits
    # Spec section 10 requires throttling on auth and task creation.
    # Values are decision A17.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 5
    RATE_LIMIT_REGISTER_PER_MINUTE: int = 3
    RATE_LIMIT_TASK_CREATE_PER_MINUTE: int = 10
    # Trust X-Forwarded-For only behind a proxy you control: a client can set it
    # freely, and believing it hands every caller a fresh rate-limit bucket.
    RATE_LIMIT_TRUST_FORWARDED_FOR: bool = False

    # ---------------------------------------------------------------- database
    # docker compose and the application read the same variables, so the
    # container credentials and the app's DSN cannot drift apart.
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: SecretStr = SecretStr("postgres")
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "agent_core"
    POSTGRES_TEST_DB: str = "agent_core_test"
    # Set this to bypass the assembled DSN entirely (a managed instance, say).
    DATABASE_URL: str | None = None
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_ECHO: bool = False
    DB_HEALTHCHECK_TIMEOUT_SECONDS: float = 2.0

    # ---------------------------------------------------------------- jwt
    JWT_ALGORITHM: str = "RS256"
    JWT_ISSUER: str = "browser-agent-backend"
    # Who the token is for. Payments validates this, so a token minted for one
    # audience cannot be replayed against a different service later.
    JWT_AUDIENCE: str = "browser-agent"
    # Names the key in the JWKS. Changing it is how a key is rotated: publish
    # both, sign with the new one, retire the old after the access TTL.
    JWT_KID: str = "browser-agent-key-1"
    JWT_PRIVATE_KEY_PATH: str = "keys/jwt_private.pem"
    JWT_PUBLIC_KEY_PATH: str = "keys/jwt_public.pem"
    ACCESS_TOKEN_TTL_MIN: int = 15  # spec section 2
    REFRESH_TOKEN_TTL_DAYS: int = 7  # spec section 2
    JWT_REFRESH_COOKIE_NAME: str = "refresh_token"

    # ---------------------------------------------------------------- llm (B23, B24)
    LLM_PROVIDER: str = "mock"
    # Used when the primary provider is unreachable. Blank disables it.
    LLM_FALLBACK_PROVIDER: str | None = None
    # Optional. Left unset, each provider picks its own sensible default, so
    # switching LLM_PROVIDER does not also require knowing that vendor's model
    # names. Set it to pin a specific model.
    LLM_MODEL: str | None = None
    LLM_VISION_MODEL: str | None = None
    # One key per provider, so switching providers does not mean re-keying.
    ANTHROPIC_API_KEY: SecretStr | None = None
    OPENAI_API_KEY: SecretStr | None = None
    GOOGLE_API_KEY: SecretStr | None = None
    LLM_TIMEOUT_SECONDS: int = 60
    LLM_MAX_OUTPUT_TOKENS: int = 2048
    PLANNER_PROMPT_VERSION: str = "v1"

    # ---------------------------------------------------------------- agent (B19 onwards)
    AGENT_MAX_STEPS: int = 25
    AGENT_MAX_TASK_SECONDS: int = 300
    AGENT_MAX_RETRIES_PER_STEP: int = 3
    AGENT_APPROVAL_TIMEOUT_SECONDS: int = 600
    AGENT_MAX_CONCURRENT_TASKS: int = 3
    AGENT_MAX_CONCURRENT_TASKS_PER_USER: int = 2
    AGENT_DOM_TOKEN_BUDGET: int = 3000
    # Spec section 4.13. A bare host matches any port on that host; add a port
    # to pin one. SkyFare on :8200 is listed explicitly even though the bare
    # entries already cover it, because it is the intended target and a reader
    # should not have to work that out.
    AGENT_ALLOWED_DOMAINS: CsvList = Field(
        default_factory=lambda: [
            "localhost",
            "127.0.0.1",
            "localhost:8200",  # SkyFare, the demo airline site
            "127.0.0.1:8200",
        ]
    )
    AGENT_HEADLESS: bool = True
    # Spec section 4.7. How long the page must stop changing before it is read
    # for verification, and how long to wait for that before reading anyway.
    AGENT_SETTLE_QUIET_MS: int = 300
    AGENT_SETTLE_TIMEOUT_MS: int = 3000

    # ----------------------------------------------------------- live view
    # Spec section 6.1. The ticket is short-lived because it travels in a URL,
    # which is the one place an access token must never go.
    WS_TICKET_TTL_SECONDS: int = 60
    WS_HEARTBEAT_SECONDS: int = 15

    # ---------------------------------------------------------------- browser
    BROWSER_ACTION_TIMEOUT_MS: int = 10_000
    BROWSER_NAVIGATION_TIMEOUT_MS: int = 30_000

    # ---------------------------------------------------------------- screenshots
    # Spec section 6.1: base64 JPEG, no wider than 1280, quality about 60.
    # Those numbers are a bandwidth budget for the live view, not a guess.
    SCREENSHOT_MAX_WIDTH: int = 1280
    SCREENSHOT_JPEG_QUALITY: int = 60

    # ---------------------------------------------------------------- billing (B12, B30)
    BILLING_ENABLED: bool = False
    BILLING_BASE_URL: str = "http://localhost:8100"
    BILLING_INTERNAL_TOKEN: SecretStr | None = None
    BILLING_TIMEOUT_SECONDS: int = 10

    # ---------------------------------------------------------------- storage (B15)
    STORAGE_BACKEND: str = "local"
    STORAGE_DIR: str = "storage"
    STORAGE_S3_BUCKET: str | None = None
    STORAGE_S3_ENDPOINT_URL: str | None = None
    STORAGE_S3_ACCESS_KEY: SecretStr | None = None
    STORAGE_S3_SECRET_KEY: SecretStr | None = None

    # ---------------------------------------------------------------- validators
    @field_validator("CORS_ORIGINS", "AGENT_ALLOWED_DOMAINS", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept ``a,b,c`` as well as a real list."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("API_PREFIX")
    @classmethod
    def _normalise_prefix(cls, value: str) -> str:
        """A router prefix must start with / and must not end with one."""
        return "/" + value.strip().strip("/")

    # ---------------------------------------------------------------- helpers
    @property
    def access_token_ttl(self) -> timedelta:
        return timedelta(minutes=self.ACCESS_TOKEN_TTL_MIN)

    @property
    def refresh_token_ttl(self) -> timedelta:
        return timedelta(days=self.REFRESH_TOKEN_TTL_DAYS)

    def dsn_for(self, database: str) -> str:
        """Assemble an asyncpg DSN for one database on the configured server."""
        user = quote_plus(self.POSTGRES_USER)
        password = quote_plus(self.POSTGRES_PASSWORD.get_secret_value())
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{database}"
        )

    @property
    def database_url(self) -> str:
        """The DSN the application runs against."""
        return self.DATABASE_URL or self.dsn_for(self.POSTGRES_DB)

    @property
    def test_database_url(self) -> str:
        """The DSN the integration suite runs against. Never the live database."""
        return self.dsn_for(self.POSTGRES_TEST_DB)

    @property
    def is_prod(self) -> bool:
        return self.APP_ENV is AppEnv.PROD

    @property
    def is_dev(self) -> bool:
        return self.APP_ENV is AppEnv.DEV


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Use this everywhere rather than constructing Settings."""
    return Settings()
