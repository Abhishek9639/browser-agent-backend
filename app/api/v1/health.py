"""Liveness and readiness.

Kept at ``/api/v1/health`` per decision A9: Docker and the frontend both use it.

The database probe makes this a readiness check as much as a liveness one: a
process that is running but cannot reach Postgres cannot serve a task, so it
reports 503 and lets an orchestrator take it out of rotation.
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.api.deps import SettingsDep
from app.core.config import AppEnv
from app.db.session import DatabaseDep

router = APIRouter(tags=["ops"])

DbStatus = Literal["ok", "down"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    env: AppEnv
    db: DbStatus
    time: datetime


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness and readiness probe",
    responses={503: {"model": HealthResponse, "description": "A dependency is down"}},
)
async def health(
    settings: SettingsDep,
    database: DatabaseDep,
    response: Response,
) -> HealthResponse:
    db_ok = await database.healthcheck()
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        version=settings.APP_VERSION,
        env=settings.APP_ENV,
        db="ok" if db_ok else "down",
        time=datetime.now(UTC),
    )
