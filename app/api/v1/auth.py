"""Authentication endpoints (spec section 6).

Register and login land here. Refresh, logout and ``/auth/me`` arrive with the
refresh cookie in the next part.
"""

from fastapi import APIRouter, status

from app.api.deps import AuthServiceDep
from app.schemas.auth import LoginRequest, LoginResponse, RegisterRequest, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    responses={409: {"description": "Email already registered"}},
)
async def register(payload: RegisterRequest, auth: AuthServiceDep) -> UserResponse:
    """Register a TASK_SUBMITTER.

    The response never contains the password hash: it is built from an explicit
    schema rather than dumping the model.
    """
    user = await auth.register(
        name=payload.name,
        email=payload.email,
        password=payload.password,
    )
    return UserResponse.model_validate(user)


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Exchange credentials for an access token",
    responses={401: {"description": "Email or password is incorrect"}},
)
async def login(payload: LoginRequest, auth: AuthServiceDep) -> LoginResponse:
    """Authenticate and mint a 15 minute access token.

    The refresh cookie is issued in the next part; this returns the access
    token only.
    """
    user, token = await auth.authenticate(email=payload.email, password=payload.password)
    return LoginResponse(
        access_token=token.token,
        token_type=token.token_type,
        expires_in=token.expires_in,
        user=UserResponse.model_validate(user),
    )
