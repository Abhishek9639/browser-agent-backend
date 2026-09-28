"""Aggregates every v1 router. Mounted under ``settings.API_PREFIX``."""

from fastapi import APIRouter

from app.api.v1 import auth, health, stats, stream, tasks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(tasks.router)
api_router.include_router(stats.router)
api_router.include_router(stream.router)

__all__ = ["api_router"]
