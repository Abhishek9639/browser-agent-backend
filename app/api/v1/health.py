"""Liveness endpoint.

Kept at ``/api/v1/health`` per decision A9: Docker and the frontend both use it.
The database probe arrives with the database.
"""

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import SettingsDep
from app.core.config import AppEnv

router = APIRouter(tags=["ops"])


class HealthResponse(BaseModel):
    status: str
    version: str
    env: AppEnv
    time: datetime


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=settings.APP_VERSION,
        env=settings.APP_ENV,
        time=datetime.now(UTC),
    )
