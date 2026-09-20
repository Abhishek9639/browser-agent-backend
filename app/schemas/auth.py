"""Auth request and response schemas."""

import re
import uuid
from datetime import datetime
from typing import Annotated, Final

from pydantic import Field, field_validator

from app.core.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from app.domain.enums import UserRole
from app.schemas.base import CamelModel

__all__ = [
    "LoginRequest",
    "LoginResponse",
    "RegisterRequest",
    "UserResponse",
]

# Deliberately permissive. Anything stricter rejects addresses that are legal
# under RFC 5321, and the only real proof an address works is sending to it.
EMAIL_PATTERN: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH: Final = 320  # RFC 5321 local part 64 + @ + domain 255

Password = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)]


def _normalise_email(value: str) -> str:
    """Lowercase and trim.

    Stored normalised so ``Alice@Example.com`` and ``alice@example.com`` cannot
    become two accounts, and so login does not depend on how it was typed.
    """
    email = value.strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not EMAIL_PATTERN.match(email):
        raise ValueError("Enter a valid email address.")
    return email


class RegisterRequest(CamelModel):
    name: Annotated[str, Field(min_length=1, max_length=255)]
    email: Annotated[str, Field(max_length=MAX_EMAIL_LENGTH)]
    password: Password

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name cannot be blank.")
        return stripped

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        return _normalise_email(value)

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        """Length is enforced by the annotation; this catches the weak shapes."""
        if value.isdigit():
            raise ValueError("Password cannot be all digits.")
        if not value.strip():
            raise ValueError("Password cannot be blank.")
        return value


class LoginRequest(CamelModel):
    email: Annotated[str, Field(max_length=MAX_EMAIL_LENGTH)]
    password: Annotated[str, Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)]

    @field_validator("email")
    @classmethod
    def _normalise(cls, value: str) -> str:
        """Lowercased to match storage, but never rejected here.

        A malformed address must fail as INVALID_CREDENTIALS like any other
        wrong login, not as a 400 that reveals the address was never valid.
        """
        return value.strip().lower()


class UserResponse(CamelModel):
    """A user as the API shows them. Never carries the password hash."""

    id: uuid.UUID
    name: str
    email: str
    role: UserRole
    created_at: datetime


class LoginResponse(CamelModel):
    access_token: str
    token_type: str
    expires_in: int
    user: UserResponse
