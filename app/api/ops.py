"""Root-path endpoints.

JWKS sits at ``/.well-known/jwks.json`` because RFC 8615 puts it there and the
payments service will look for it at that well-known location, not under a
version prefix (spec section 6 calls this out explicitly).

Health is the other way round: it stays at ``/api/v1/health`` per decision A9,
because Docker and the frontend already use that path.
"""

from typing import Any

from fastapi import APIRouter, Response

from app.api.deps import KeyPairDep

router = APIRouter(tags=["ops"])


@router.get(
    "/.well-known/jwks.json",
    summary="Public keys for verifying access tokens",
)
async def jwks(keys: KeyPairDep, response: Response) -> dict[str, list[dict[str, Any]]]:
    """The public half of the signing key, as a JWK Set.

    Public by design: anyone may verify a token, only this service may mint one.
    Cached for five minutes so payments does not refetch on every request, but
    not so long that a key rotation takes hours to propagate.
    """
    response.headers["Cache-Control"] = "public, max-age=300"
    return keys.jwks()
