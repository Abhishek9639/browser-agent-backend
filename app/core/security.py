"""Password hashing and access tokens.

Spec section 2: RS256 access tokens, 15 minutes. Spec section 10: a password is
never logged and never stored, only its argon2 digest.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings
from app.core.errors import Unauthorized
from app.core.jwt_keys import KeyPair
from app.domain.enums import UserRole

__all__ = [
    "MIN_PASSWORD_LENGTH",
    "AccessToken",
    "TokenClaims",
    "TokenService",
    "hash_password",
    "verify_password",
]

MIN_PASSWORD_LENGTH: Final = 10
# argon2 has no 72 byte truncation like bcrypt, but an unbounded password is a
# way to make the server do unbounded work.
MAX_PASSWORD_LENGTH: Final = 256

# argon2id with the library defaults, which track the current OWASP guidance.
_hasher = PasswordHasher()

# Verified against when the email is unknown, so a login attempt costs the same
# whether or not the account exists. See AuthService.authenticate.
DUMMY_HASH: Final = _hasher.hash("timing-equalisation-placeholder")


def hash_password(password: str) -> str:
    """argon2id digest. The plaintext is never stored or logged."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time comparison. False on any mismatch, never an exception."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The validated contents of an access token."""

    subject: uuid.UUID
    role: UserRole
    jti: str
    issuer: str
    audience: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A freshly minted token and what the client needs to use it."""

    token: str
    expires_in: int
    claims: TokenClaims
    token_type: str = "Bearer"  # noqa: S105 - a scheme name, not a credential


class TokenService:
    """Mints and validates access tokens.

    The keypair and settings are bound once here rather than passed to every
    call, so a route handler just says ``tokens.create_access_token(user)``.
    """

    def __init__(self, settings: Settings, keys: KeyPair) -> None:
        self._settings = settings
        self._keys = keys

    def create_access_token(self, user: Any, *, now: datetime | None = None) -> AccessToken:
        """Sign an RS256 token for ``user``.

        ``jti`` is a unique id per token. Nothing consumes it yet; it is what a
        future revocation list or a replay check would key on, and it costs
        nothing to include from the start.
        """
        issued_at = now or datetime.now(UTC)
        ttl: timedelta = self._settings.access_token_ttl
        expires_at = issued_at + ttl

        claims: dict[str, Any] = {
            "sub": str(user.id),
            "role": UserRole(user.role).value,
            "iss": self._settings.JWT_ISSUER,
            "aud": self._settings.JWT_AUDIENCE,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": uuid.uuid4().hex,
        }

        token = jwt.encode(
            claims,
            self._keys.private_pem,
            algorithm="RS256",
            # The kid lets a verifier pick the right key out of the JWKS, which
            # is what makes rotation possible without downtime.
            headers={"kid": self._keys.kid},
        )

        return AccessToken(
            token=token,
            expires_in=int(ttl.total_seconds()),
            claims=self._to_claims(claims),
        )

    def decode_access_token(self, token: str) -> TokenClaims:
        """Verify and decode, or raise :class:`Unauthorized`.

        ``algorithms`` is an allow-list of exactly one entry. Without it a token
        could arrive with ``alg: none``, or with ``HS256`` signed using the
        public key as the shared secret, and both would verify.
        """
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._keys.public_pem,
                algorithms=["RS256"],
                issuer=self._settings.JWT_ISSUER,
                audience=self._settings.JWT_AUDIENCE,
                options={
                    "require": ["sub", "role", "iss", "aud", "iat", "exp", "jti"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise Unauthorized("The access token has expired.") from exc
        except jwt.InvalidTokenError as exc:
            # Covers a bad signature, wrong issuer or audience, a missing
            # required claim, and anything malformed. The detail stays in the
            # log; the client gets one message either way.
            raise Unauthorized("The access token is invalid.") from exc

        try:
            return self._to_claims(payload)
        except (ValueError, KeyError) as exc:
            raise Unauthorized("The access token is invalid.") from exc

    @staticmethod
    def _to_claims(payload: dict[str, Any]) -> TokenClaims:
        return TokenClaims(
            subject=uuid.UUID(str(payload["sub"])),
            role=UserRole(payload["role"]),
            jti=str(payload["jti"]),
            issuer=str(payload["iss"]),
            audience=str(payload["aud"]),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        )
