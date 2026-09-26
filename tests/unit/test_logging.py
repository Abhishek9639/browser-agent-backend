"""B02: the redaction processor (spec section 10)."""

from typing import Any

import pytest

from app.core.logging import REDACTED, redact_sensitive


def scrub(event: dict[str, Any]) -> dict[str, Any]:
    return dict(redact_sensitive(None, "info", event))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "passwd",
        "token",
        "access_token",
        "authorization",
        "api_key",
        "apikey",
        "secret",
        "client_secret",
        "card",
        "card_number",
        "cvv",
        "cvc",
        "screenshot",
        "cookie",
        "jwt",
        "private_key",
    ],
)
def test_sensitive_keys_are_masked(key: str) -> None:
    assert scrub({key: "sensitive-value"})[key] == REDACTED


@pytest.mark.parametrize("key", ["author", "username", "status", "task_id", "step_number"])
def test_ordinary_keys_survive(key: str) -> None:
    assert scrub({key: "value"})[key] == "value"


def test_masking_is_case_insensitive() -> None:
    assert scrub({"Authorization": "Bearer x"})["Authorization"] == REDACTED


def test_nested_structures_are_scrubbed() -> None:
    event = {
        "outer": {"api_key": "sk-1", "safe": 1},
        "items": [{"password": "p"}, {"ok": "yes"}],
        "pair": ({"cvv": "123"},),
    }

    result = scrub(event)

    assert result["outer"] == {"api_key": REDACTED, "safe": 1}
    assert result["items"] == [{"password": REDACTED}, {"ok": "yes"}]
    assert result["pair"][0]["cvv"] == REDACTED


def test_the_log_message_itself_is_untouched() -> None:
    assert scrub({"event": "request_completed", "token": "t"})["event"] == "request_completed"


# ------------------------------------------------------------------ counts


@pytest.mark.parametrize(
    "key",
    ["input_tokens", "output_tokens", "total_tokens", "max_tokens", "token_budget"],
)
def test_token_counts_are_not_redacted(key: str) -> None:
    """These contain "token" but are measurements, and they are the names our
    own schema uses. Redacting them would make the cost metrics unreadable."""
    assert scrub({key: 1234})[key] == 1234


@pytest.mark.parametrize(
    "key",
    ["access_token", "refresh_token", "id_token", "token", "bearer_token", "token_hash"],
)
def test_actual_tokens_are_still_redacted(key: str) -> None:
    assert scrub({key: "secret-value"})[key] == REDACTED
