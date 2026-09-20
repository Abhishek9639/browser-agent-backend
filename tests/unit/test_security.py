"""B06: password hashing and access tokens."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from jwt.algorithms import RSAAlgorithm

from app.core.config import Settings
from app.core.errors import Unauthorized
from app.core.jwt_keys import KeyPair, load_key_pair
from app.core.security import TokenService, hash_password, verify_password
from app.domain.enums import UserRole


@pytest.fixture
def keys(settings: Settings) -> KeyPair:
    return load_key_pair(settings)


@pytest.fixture
def tokens(settings: Settings, keys: KeyPair) -> TokenService:
    return TokenService(settings, keys)


@pytest.fixture
def user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), role=UserRole.TASK_SUBMITTER)


# ------------------------------------------------------------------ hashing


def test_hash_is_not_the_password() -> None:
    digest = hash_password("correct horse battery")

    assert "correct horse battery" not in digest
    assert digest.startswith("$argon2id$")


def test_verify_accepts_the_right_password() -> None:
    assert verify_password("s3cret-passphrase", hash_password("s3cret-passphrase"))


def test_verify_rejects_the_wrong_password() -> None:
    assert not verify_password("wrong-passphrase", hash_password("s3cret-passphrase"))


def test_hashes_are_salted() -> None:
    """Two identical passwords must not produce the same digest."""
    assert hash_password("same-password") != hash_password("same-password")


def test_verify_returns_false_for_a_malformed_hash() -> None:
    """A corrupt row must not raise out of the login path."""
    assert not verify_password("anything", "not-a-hash")


# ------------------------------------------------------------------ tokens


def test_token_carries_every_required_claim(tokens: TokenService, user: SimpleNamespace) -> None:
    issued = tokens.create_access_token(user)
    payload = jwt.decode(
        issued.token, options={"verify_signature": False}, audience="browser-agent"
    )

    assert set(payload) >= {"sub", "role", "iss", "aud", "iat", "exp", "jti"}
    assert payload["sub"] == str(user.id)
    assert payload["role"] == "TASK_SUBMITTER"


def test_token_header_names_the_key(tokens: TokenService, settings: Settings) -> None:
    """The kid is what lets a verifier pick the right key from the JWKS."""
    issued = tokens.create_access_token(SimpleNamespace(id=uuid.uuid4(), role=UserRole.ADMIN))
    header = jwt.get_unverified_header(issued.token)

    assert header["alg"] == "RS256"
    assert header["kid"] == settings.JWT_KID


def test_round_trip(tokens: TokenService, user: SimpleNamespace) -> None:
    issued = tokens.create_access_token(user)

    claims = tokens.decode_access_token(issued.token)

    assert claims.subject == user.id
    assert claims.role is UserRole.TASK_SUBMITTER
    assert claims.jti


def test_expires_in_is_fifteen_minutes(tokens: TokenService, user: SimpleNamespace) -> None:
    """Spec section 2."""
    assert tokens.create_access_token(user).expires_in == 900


def test_each_token_has_a_unique_jti(tokens: TokenService, user: SimpleNamespace) -> None:
    first = tokens.create_access_token(user)
    second = tokens.create_access_token(user)

    assert first.claims.jti != second.claims.jti


# ------------------------------------------------------------------ rejection


def test_expired_token_is_rejected(tokens: TokenService, user: SimpleNamespace) -> None:
    issued = tokens.create_access_token(user, now=datetime.now(UTC) - timedelta(hours=2))

    with pytest.raises(Unauthorized) as caught:
        tokens.decode_access_token(issued.token)

    assert "expired" in caught.value.message.lower()


def test_wrong_audience_is_rejected(settings: Settings, keys: KeyPair) -> None:
    """A token minted for another service must not be accepted here."""
    other = Settings(
        JWT_PRIVATE_KEY_PATH=settings.JWT_PRIVATE_KEY_PATH,
        JWT_PUBLIC_KEY_PATH=settings.JWT_PUBLIC_KEY_PATH,
        JWT_AUDIENCE="some-other-service",
    )
    foreign = TokenService(other, keys).create_access_token(
        SimpleNamespace(id=uuid.uuid4(), role=UserRole.ADMIN)
    )

    with pytest.raises(Unauthorized):
        TokenService(settings, keys).decode_access_token(foreign.token)


def test_wrong_issuer_is_rejected(settings: Settings, keys: KeyPair) -> None:
    other = Settings(
        JWT_PRIVATE_KEY_PATH=settings.JWT_PRIVATE_KEY_PATH,
        JWT_PUBLIC_KEY_PATH=settings.JWT_PUBLIC_KEY_PATH,
        JWT_ISSUER="someone-else",
    )
    foreign = TokenService(other, keys).create_access_token(
        SimpleNamespace(id=uuid.uuid4(), role=UserRole.ADMIN)
    )

    with pytest.raises(Unauthorized):
        TokenService(settings, keys).decode_access_token(foreign.token)


def test_tampered_signature_is_rejected(tokens: TokenService, user: SimpleNamespace) -> None:
    issued = tokens.create_access_token(user)
    header, payload, signature = issued.token.split(".")
    flipped = signature[:-4] + ("abcd" if not signature.endswith("abcd") else "efgh")

    with pytest.raises(Unauthorized):
        tokens.decode_access_token(f"{header}.{payload}.{flipped}")


def test_tampered_payload_is_rejected(tokens: TokenService, user: SimpleNamespace) -> None:
    """Escalating the role in the payload must invalidate the signature."""
    import base64
    import json

    issued = tokens.create_access_token(user)
    header, payload, signature = issued.token.split(".")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=="))
    decoded["role"] = "ADMIN"
    forged = base64.urlsafe_b64encode(json.dumps(decoded).encode()).rstrip(b"=").decode()

    with pytest.raises(Unauthorized):
        tokens.decode_access_token(f"{header}.{forged}.{signature}")


def test_alg_none_is_rejected(tokens: TokenService, user: SimpleNamespace) -> None:
    """The classic JWT attack: an unsigned token claiming it needs no signature."""
    unsigned = jwt.encode({"sub": str(user.id), "role": "ADMIN"}, key="", algorithm="none")

    with pytest.raises(Unauthorized):
        tokens.decode_access_token(unsigned)


def test_hs256_signed_with_the_public_key_is_rejected(
    tokens: TokenService, keys: KeyPair, user: SimpleNamespace
) -> None:
    """Algorithm confusion, the classic RS256 to HS256 downgrade.

    The public key is, by definition, known to everyone. If the verifier
    accepted HS256 it would treat that public key as a shared secret, and
    anyone who fetched the JWKS could mint an admin token.

    Built by hand: PyJWT refuses to *encode* HS256 with a PEM, but an attacker
    is not using PyJWT.
    """
    import base64
    import hashlib
    import hmac
    import json

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(
        json.dumps(
            {
                "sub": str(user.id),
                "role": "ADMIN",
                "iss": "browser-agent-backend",
                "aud": "browser-agent",
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
                "jti": "forged",
            }
        ).encode()
    )
    signing_input = header + b"." + payload
    signature = b64(hmac.new(keys.public_pem.encode(), signing_input, hashlib.sha256).digest())
    forged = (signing_input + b"." + signature).decode()

    with pytest.raises(Unauthorized):
        tokens.decode_access_token(forged)


def test_garbage_is_rejected(tokens: TokenService) -> None:
    with pytest.raises(Unauthorized):
        tokens.decode_access_token("not-a-token")


def test_token_missing_a_required_claim_is_rejected(tokens: TokenService, keys: KeyPair) -> None:
    incomplete = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iss": "browser-agent-backend",
            "aud": "browser-agent",
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        keys.private_pem,
        algorithm="RS256",
    )

    with pytest.raises(Unauthorized):
        tokens.decode_access_token(incomplete)


# ------------------------------------------------------------------ jwks


def test_jwks_shape(keys: KeyPair, settings: Settings) -> None:
    document = keys.jwks()

    assert list(document) == ["keys"]
    jwk = document["keys"][0]
    assert jwk["kty"] == "RSA"
    assert jwk["use"] == "sig"
    assert jwk["alg"] == "RS256"
    assert jwk["kid"] == settings.JWT_KID
    assert jwk["n"] and jwk["e"]


def test_jwk_values_are_unpadded_base64url(keys: KeyPair) -> None:
    jwk = keys.public_jwk()

    for field in ("n", "e"):
        assert "=" not in jwk[field], field
        assert "+" not in jwk[field] and "/" not in jwk[field], field


def test_jwks_contains_no_private_material(keys: KeyPair) -> None:
    """Only the public half is ever published."""
    jwk = keys.public_jwk()

    assert set(jwk) == {"kty", "kid", "use", "alg", "n", "e"}
    for private_field in ("d", "p", "q", "dp", "dq", "qi"):
        assert private_field not in jwk


def test_a_token_verifies_using_only_the_jwks(
    tokens: TokenService, keys: KeyPair, user: SimpleNamespace
) -> None:
    """This is exactly what the payments service will do.

    It never sees a PEM or a shared secret: it fetches the JWKS, picks the key
    named by the token's kid, and verifies with that alone.
    """
    import json

    issued = tokens.create_access_token(user)
    published = keys.jwks()

    kid = jwt.get_unverified_header(issued.token)["kid"]
    matching = next(k for k in published["keys"] if k["kid"] == kid)
    public_key = RSAAlgorithm.from_jwk(json.dumps(matching))

    payload = jwt.decode(
        issued.token,
        public_key,  # type: ignore[arg-type]
        algorithms=["RS256"],
        issuer="browser-agent-backend",
        audience="browser-agent",
    )

    assert payload["sub"] == str(user.id)
