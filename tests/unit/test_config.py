"""B02: settings behaviour that later parts depend on."""

import pytest

from app.core.config import AppEnv, Settings, get_settings


def test_defaults_match_the_spec() -> None:
    settings = Settings()

    assert settings.API_PREFIX == "/api/v1"
    assert settings.ACCESS_TOKEN_TTL_MIN == 15
    assert settings.REFRESH_TOKEN_TTL_DAYS == 7
    assert settings.access_token_ttl.total_seconds() == 900
    assert settings.refresh_token_ttl.total_seconds() == 604_800
    assert settings.AGENT_MAX_STEPS == 25
    assert settings.AGENT_MAX_TASK_SECONDS == 300
    assert settings.AGENT_MAX_RETRIES_PER_STEP == 3
    assert settings.AGENT_APPROVAL_TIMEOUT_SECONDS == 600
    assert settings.AGENT_MAX_CONCURRENT_TASKS == 3
    assert settings.AGENT_MAX_CONCURRENT_TASKS_PER_USER == 2
    assert settings.BILLING_ENABLED is False


@pytest.mark.parametrize(
    "field", ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "POSTGRES_PASSWORD"]
)
def test_secrets_do_not_appear_in_repr(field: str) -> None:
    settings = Settings(**{field: "sk-should-never-be-printed"})

    assert "sk-should-never-be-printed" not in repr(settings)
    assert getattr(settings, field).get_secret_value() == "sk-should-never-be-printed"


def test_list_settings_accept_comma_separated_values() -> None:
    settings = Settings(CORS_ORIGINS="http://a.test, http://b.test")

    assert settings.CORS_ORIGINS == ["http://a.test", "http://b.test"]


def test_api_prefix_is_normalised() -> None:
    assert Settings(API_PREFIX="api/v1/").API_PREFIX == "/api/v1"


def test_environment_helpers() -> None:
    assert Settings(APP_ENV=AppEnv.PROD).is_prod
    assert Settings(APP_ENV=AppEnv.DEV).is_dev
    assert not Settings(APP_ENV=AppEnv.TEST).is_prod


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_env_example_documents_every_setting() -> None:
    """CLAUDE.md: .env.example stays in sync with every new setting.

    This fails the moment someone adds a field without documenting it, which is
    the only way that rule survives contact with a deadline.
    """
    import re
    from pathlib import Path

    declared = set(Settings.model_fields)
    documented = set(
        re.findall(r"^([A-Z][A-Z0-9_]*)=", Path(".env.example").read_text(), re.MULTILINE)
    )

    assert declared - documented == set(), "undocumented settings"
    assert documented - declared == set(), "stale entries in .env.example"


@pytest.mark.parametrize(
    "field",
    [
        "AGENT_MAX_CONCURRENT_TASKS",
        "AGENT_MAX_CONCURRENT_TASKS_PER_USER",
        "AGENT_MAX_STEPS",
        "AGENT_MAX_TASK_SECONDS",
        "AGENT_MAX_TABS",
    ],
)
def test_zero_agent_limits_are_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        Settings(**{field: 0})


def test_test_database_cannot_point_at_application_database() -> None:
    with pytest.raises(ValueError, match="must differ"):
        _ = Settings(POSTGRES_TEST_DB="agent_core").test_database_url
    with pytest.raises(ValueError, match="must differ"):
        _ = Settings(
            DATABASE_URL="postgresql+asyncpg://user:pass@localhost/agent_core_test"
        ).test_database_url


def test_database_credentials_round_trip_with_spaces_and_percent() -> None:
    from sqlalchemy.engine import make_url

    configured = Settings(POSTGRES_USER="local user", POSTGRES_PASSWORD="a b%/c@d")
    parsed = make_url(configured.database_url)
    assert parsed.username == "local user"
    assert parsed.password == "a b%/c@d"
