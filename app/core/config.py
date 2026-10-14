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
    # Bytes. A task instruction is two thousand characters and the largest
    # honest body is an approval reason, so a megabyte is generous. The point
    # is that an unbounded body is a memory limit somebody else decides.
    MAX_REQUEST_BYTES: int = 1_048_576
    # Sent only in production, and only over HTTPS. Two years with preload is
    # the usual recommendation; it is a setting because turning it on is hard
    # to undo for anyone who has already seen it.
    HSTS_MAX_AGE_SECONDS: int = 63_072_000
    LOG_LEVEL: LogLevel = LogLevel.INFO

    # ---------------------------------------------------------------- rate limits
    # Spec section 10 requires throttling on auth and task creation.
    # Values are decision A17.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 5
    RATE_LIMIT_REGISTER_PER_MINUTE: int = 3
    RATE_LIMIT_TASK_CREATE_PER_MINUTE: int = 10
    # Spec section 10 asks for rate limits on auth and task creation. These
    # two are here because both are cheap to call and expensive to serve: an
    # approval decision writes an audit row and wakes a parked run, and a
    # stream ticket is a credential.
    RATE_LIMIT_APPROVAL_PER_MINUTE: int = 20
    RATE_LIMIT_STREAM_TICKET_PER_MINUTE: int = 30
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
    # A just-rotated refresh token presented again within this many seconds is
    # a client race (two tabs, a double mount), not a theft: it gets an access
    # token but no new refresh cookie, and the session chain survives. 0 turns
    # the tolerance off and every reuse destroys the chain.
    REFRESH_REUSE_GRACE_SECONDS: int = Field(default=10, ge=0)

    # ---------------------------------------------------------------- llm (B23, B24)
    LLM_PROVIDER: str = "mock"
    # Used when the primary provider is unreachable. Blank disables it.
    LLM_FALLBACK_PROVIDER: str | None = None
    # Requests per minute the provider's plan allows; 0 means do not pace. Free
    # tiers are tight (Gemini's is 5), and pacing is cheaper than a 429.
    GEMINI_REQUESTS_PER_MINUTE: int = Field(default=0, ge=0)
    GROQ_REQUESTS_PER_MINUTE: int = Field(default=0, ge=0)
    # Tried in order when LLM_MODEL is used up or unavailable. Comma separated.
    GEMINI_FALLBACK_MODELS: str = (
        "gemini-3-flash-preview,gemini-3.1-flash-lite,gemini-3.5-flash-lite"
    )
    # Optional. Left unset, each provider picks its own sensible default, so
    # switching LLM_PROVIDER does not also require knowing that vendor's model
    # names. Set it to pin a specific model.
    LLM_MODEL: str | None = None
    LLM_VISION_MODEL: str | None = None

    # ------------------------------------------------------ visual grounding
    # Spec section 4.9. Grounding is the expensive path, so every one of these
    # is a threshold for when it is worth paying for.
    VISION_PROMPT_VERSION: str = "v1"
    VISION_MAX_WIDTH: int = 1024
    # Below this the planner is guessing, and a guess about which button to
    # press is worth a picture.
    VISION_CONFIDENCE_THRESHOLD: float = 0.6
    # A page with fewer interactive elements than this, that is plainly not
    # blank, is probably drawing its interface rather than marking it up.
    VISION_MIN_ELEMENTS: int = 5
    # A marked frame larger than this is refused rather than sent: providers
    # reject oversized images, and finding out mid-task wastes the step.
    VISION_MAX_IMAGE_BYTES: int = 1_500_000

    # ------------------------------------------------------------- pricing
    # Rupees per million tokens, for the spec section 9 cost-per-task metric.
    # Parameters rather than a table baked into the benchmark: prices change,
    # and a number hardcoded in a script is a number nobody updates.
    LLM_PRICE_INPUT_PER_MTOK: float = 25.0
    LLM_PRICE_OUTPUT_PER_MTOK: float = 100.0
    LLM_PRICE_CURRENCY: str = "INR"
    # One key per provider, so switching providers does not mean re-keying.
    ANTHROPIC_API_KEY: SecretStr | None = None
    OPENAI_API_KEY: SecretStr | None = None
    GOOGLE_API_KEY: SecretStr | None = None
    GROQ_API_KEY: SecretStr | None = None
    # Groq's own model names, separate from LLM_MODEL so a Groq fallback is
    # never sent the primary provider's model name.
    GROQ_MODEL: str | None = None
    GROQ_VISION_MODEL: str | None = None
    # Tried in order when GROQ_MODEL is rate limited. Comma separated.
    GROQ_FALLBACK_MODELS: str = "openai/gpt-oss-20b,qwen/qwen3.8-27b"
    LLM_TIMEOUT_SECONDS: int = 60
    LLM_MAX_OUTPUT_TOKENS: int = 2048
    PLANNER_PROMPT_VERSION: str = "v2"

    # ---------------------------------------------------------------- agent (B19 onwards)
    AGENT_MAX_STEPS: int = 25
    AGENT_MAX_TASK_SECONDS: int = 300
    AGENT_MAX_RETRIES_PER_STEP: int = 3
    AGENT_MAX_TABS: int = 5
    # Spec section 4.12. A flow below this rate is not replayed, and after
    # at least three runs it is deleted.
    CACHE_ENABLED: bool = True
    CACHE_MIN_SUCCESS_RATE: float = 0.5
    # Spec section 6: /metrics is admin or internal only. A Prometheus
    # scraper has no user account, so it presents this instead. Unset means
    # the header is not accepted at all and only an ADMIN token works.
    METRICS_INTERNAL_TOKEN: SecretStr | None = None
    AGENT_APPROVAL_TIMEOUT_SECONDS: int = 600
    # Spec section 4.10. Empty means the built-in list in app/agent/safety.
    RISK_HIGH_PHRASES: CsvList = Field(default_factory=list)
    # Whether the person who submitted a task may approve its own high-risk
    # steps. True by default because a single-user deployment with nobody else
    # to ask would otherwise be unable to finish a booking at all; set it false
    # where approvals are meant to be a second pair of eyes.
    APPROVAL_OWNER_CAN_APPROVE: bool = True
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
    # How long a task waits for a person to sign in or sign up when a site asks
    # for it. Longer than an ordinary approval, because signing up means email
    # codes and forms. The wait does not count against AGENT_MAX_TASK_SECONDS.
    AGENT_LOGIN_WAIT_SECONDS: int = 900
    # Other browsers to try, in order, when a site answers the agent's browser
    # with 401, 403 or 429. A separate browser, never the person's own. Names are
    # `msedge`, `chrome` or `firefox`; empty turns it off.
    AGENT_BROWSER_FALLBACKS: CsvList = Field(default_factory=lambda: ["msedge", "firefox"])
    # Open the visible browser maximised and let the page fill the window,
    # instead of a fixed 1280x800 box. Ignored when headless or attached to the
    # user's own browser.
    AGENT_FULLSCREEN: bool = False
    # One extra model call before the first action that outlines the expected
    # steps, so the person can see how long the task is. A forecast only: the
    # agent still chooses each real step from the page. Off here so a bare
    # checkout and the scripted tests spend no extra request.
    AGENT_PLAN_PREVIEW: bool = False
    # Which browser Playwright launches. Empty = its bundled Chromium. "chrome"
    # or "msedge" = the copy installed on this machine, which looks like a
    # normal browser to sites and is the one the user sees.
    AGENT_BROWSER_CHANNEL: str = ""
    # Pause between browser operations, so a visible window is watchable.
    # Opt-in: attach to the user's own Chrome (started with
    # --remote-debugging-port) instead of launching an isolated browser.
    AGENT_CDP_URL: str = ""
    AGENT_SLOW_MO_MS: int = Field(default=0, ge=0)
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

    # ---------------------------------------------------------------- billing
    # Off by default, and off means off: nothing below runs and the agent
    # behaves exactly as it does without a payments service (spec section 7).
    BILLING_ENABLED: bool = False
    PAYMENTS_BASE_URL: str = "http://localhost:8100"
    INTERNAL_API_TOKEN: SecretStr | None = None
    # Three seconds. A reservation is on the path of a user waiting for a 201,
    # so the budget is a person's patience rather than a machine's.
    PAYMENTS_TIMEOUT_SECONDS: float = 3.0
    PAYMENTS_MAX_ATTEMPTS: int = 3

    # Spec section 7 pricing. Configurable because the spec calls the numbers a
    # placeholder, and because the payments service holds the same figures: a
    # disagreement between the two shows up as a settle that does not match
    # what was reserved.
    CREDITS_PER_STEP: int = 1
    CREDITS_PER_VISION_STEP: int = 2
    # "replayed steps 0.5, round up per task". Rounding per task rather than per
    # step is the difference between two replayed steps costing one credit and
    # costing two.
    CREDITS_REPLAY_STEP_HALF: bool = True
    # Reserve MAX_STEPS times this. Two, so a task that needs vision on every
    # step is still covered by its own reservation.
    RESERVE_MULTIPLIER: int = 2

    # How often the outbox worker looks for undelivered calls, and how long it
    # waits after a failure before the first retry.
    BILLING_OUTBOX_POLL_SECONDS: float = 5.0
    BILLING_OUTBOX_BACKOFF_SECONDS: float = 10.0
    BILLING_OUTBOX_MAX_ATTEMPTS: int = 12

    # ---------------------------------------------------------------- storage (B15)
    STORAGE_BACKEND: str = "local"
    STORAGE_DIR: str = "storage"
    STORAGE_S3_BUCKET: str | None = None
    STORAGE_S3_ENDPOINT_URL: str | None = None
    STORAGE_S3_ACCESS_KEY: SecretStr | None = None
    STORAGE_S3_SECRET_KEY: SecretStr | None = None

    # ---------------------------------------------------------------- validators
    @field_validator(
        "CORS_ORIGINS",
        "AGENT_ALLOWED_DOMAINS",
        "AGENT_BROWSER_FALLBACKS",
        "RISK_HIGH_PHRASES",
        mode="before",
    )
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
