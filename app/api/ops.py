"""Root-path endpoints.

JWKS sits at ``/.well-known/jwks.json`` because RFC 8615 puts it there and the
payments service will look for it at that well-known location, not under a
version prefix (spec section 6 calls this out explicitly).

``/metrics`` is at the root too, per decision A9, and is admin or internal
only: the exposition says how many tasks exist, how often they fail and what
the model costs, which is not public information.

Health is the other way round: it stays at ``/api/v1/health`` per decision A9,
because Docker and the frontend both already use that path.
"""

import secrets
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Header, Response

from app.api.deps import KeyPairDep, SettingsDep, TokenServiceDep
from app.core import metrics
from app.core.errors import Forbidden, Unauthorized
from app.domain.enums import UserRole
from app.repositories.unit_of_work import UnitOfWorkDep

router = APIRouter(tags=["ops"])

# The header name, not a secret: the value lives in settings.
INTERNAL_TOKEN_HEADER: Final = "X-Internal-Token"  # noqa: S105


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


async def require_metrics_reader(
    settings: SettingsDep,
    uow: UnitOfWorkDep,
    tokens: TokenServiceDep,
    authorization: Annotated[str | None, Header()] = None,
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    """Either an ADMIN bearer token or the internal scrape token.

    Two credentials because there are two callers. An operator reads this in a
    browser with the account they already have; Prometheus has no account and
    cannot obtain one, so it presents a shared secret instead.

    The token comparison is constant time. A scrape token is long-lived and
    checked on every scrape, which is exactly the shape of thing a timing
    attack is built for.

    Headers are read directly rather than through ``CurrentUser``, because that
    dependency raises 401 on a missing bearer and would reject a perfectly
    valid internal scrape before this function ever ran.
    """
    configured = settings.METRICS_INTERNAL_TOKEN
    if configured is not None and x_internal_token:
        if secrets.compare_digest(x_internal_token, configured.get_secret_value()):
            return
        # A wrong shared secret is a 401 rather than a 403: the caller has not
        # established who it is, so there is no identity to refuse.
        raise Unauthorized("That internal token is not valid.")

    if not authorization:
        raise Unauthorized("Metrics require an ADMIN token or the internal token.")

    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        raise Unauthorized("Authorization must use the Bearer scheme.")

    # Raises Unauthorized itself on anything malformed, expired or unsigned.
    claims = tokens.decode_access_token(credential.strip())

    user = await uow.users.get(claims.subject)
    # The role comes from the row, not the token, so a demotion takes effect at
    # once rather than when the token expires.
    if user is None:
        raise Unauthorized("That access token is not valid.")
    if user.role is not UserRole.ADMIN:
        raise Forbidden("Metrics are restricted to administrators.")


@router.get(
    "/metrics",
    summary="Prometheus exposition",
    description=(
        "Admin or internal only. Present an ADMIN bearer token, or the "
        "configured token in `X-Internal-Token` if the deployment sets one."
    ),
    dependencies=[Depends(require_metrics_reader)],
    response_class=Response,
    responses={
        200: {"content": {metrics.CONTENT_TYPE: {}}, "description": "Prometheus text format"},
        401: {"description": "No usable credential"},
        403: {"description": "Authenticated, but not an administrator"},
    },
)
async def prometheus_metrics() -> Response:
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)
