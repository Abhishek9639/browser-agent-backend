"""Masking sensitive values.

Spec section 10 forbids page content, screenshots, credentials and typed
passwords from reaching logs, and the same rule applies to anything persisted
for inspection, such as ``audit_events.metadata``.

The rules live here, rather than inside the logging pipeline, so every consumer
masks identically. Two use it today: the structlog processor in
:mod:`app.core.logging` and :mod:`app.services.audit`.
"""

from typing import Any, Final

__all__ = ["REDACTED", "SAFE_KEYS", "SENSITIVE_KEY_PARTS", "is_sensitive_key", "redact"]

REDACTED: Final = "[REDACTED]"

# Substring match against the lowercased key. Deliberately broad: over-redacting
# is cosmetic, under-redacting is a security incident.
SENSITIVE_KEY_PARTS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "card",
        "cookie",
        "credential",
        "cvc",
        "cvv",
        "jwt",
        "passwd",
        "password",
        "private_key",
        "screenshot",
        "secret",
        "token",
    }
)

# Exact key names that contain a sensitive-looking word but are never secret.
# Kept tiny and exact, never a substring rule: every entry is a measurement,
# and these are the names our own schema uses (`llm_decisions.input_tokens`),
# so renaming them to dodge the filter would be worse than listing them.
#
# Add to this only for a value that is a count or a size. Never for anything
# that could carry a credential.
SAFE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "max_tokens",
        "token_budget",
        "tokens",
    }
)

MAX_DEPTH: Final = 6


def is_sensitive_key(key: object) -> bool:
    lowered = str(key).lower()
    if lowered in SAFE_KEYS:
        return False
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def redact(value: Any, depth: int = 0) -> Any:
    """Recursively replace values whose key looks sensitive.

    Returns a new structure; the input is never mutated. Recursion stops at
    ``MAX_DEPTH`` so a pathological payload cannot stall a request.
    """
    if depth >= MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_key(key) else redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, depth + 1) for item in value)
    return value
