"""RSA keypair loading and the JWKS document.

Spec section 2 makes this service the only identity provider: it signs RS256
access tokens with a private key that never leaves the process, and publishes
the matching public key at ``/.well-known/jwks.json`` so the payments service
can verify tokens without ever holding a shared secret.

That asymmetry is the point. With HS256 both services would need the same key,
and a compromise of payments would let an attacker mint tokens for the backend.
"""

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import Settings

__all__ = ["KeyPair", "MissingKeyError", "load_key_pair"]


class MissingKeyError(RuntimeError):
    """Raised at startup when the signing keys are not on disk."""


def _b64url_uint(value: int) -> str:
    """Encode an RSA parameter the way RFC 7518 requires: big-endian, unpadded."""
    length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class KeyPair:
    """The signing key, the verifying key, and the id that names them."""

    private_pem: str
    public_pem: str
    kid: str

    @property
    def public_key(self) -> rsa.RSAPublicKey:
        key = serialization.load_pem_public_key(self.public_pem.encode())
        if not isinstance(key, rsa.RSAPublicKey):  # pragma: no cover - configuration error
            raise MissingKeyError("The configured public key is not RSA.")
        return key

    def public_jwk(self) -> dict[str, Any]:
        """The public key as a JWK (RFC 7517).

        ``use`` and ``alg`` are advisory but let a strict client reject a key
        offered for the wrong purpose.
        """
        numbers = self.public_key.public_numbers()
        return {
            "kty": "RSA",
            "kid": self.kid,
            "use": "sig",
            "alg": "RS256",
            "n": _b64url_uint(numbers.n),
            "e": _b64url_uint(numbers.e),
        }

    def jwks(self) -> dict[str, list[dict[str, Any]]]:
        """The document served at ``/.well-known/jwks.json``.

        A list, even with one key, so a rotation can publish the old and new
        keys together without breaking consumers.
        """
        return {"keys": [self.public_jwk()]}


def load_key_pair(settings: Settings) -> KeyPair:
    """Read the keypair from the configured paths.

    Fails loudly: a service that starts without its signing key would accept
    registrations and then fail every login.
    """
    private_path = Path(settings.JWT_PRIVATE_KEY_PATH)
    public_path = Path(settings.JWT_PUBLIC_KEY_PATH)

    missing = [str(p) for p in (private_path, public_path) if not p.is_file()]
    if missing:
        raise MissingKeyError(
            f"JWT keys not found: {', '.join(missing)}. Run `python scripts/gen_keys.py`."
        )

    return KeyPair(
        private_pem=private_path.read_text(encoding="utf-8"),
        public_pem=public_path.read_text(encoding="utf-8"),
        kid=settings.JWT_KID,
    )
