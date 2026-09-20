"""Aggregates every v1 router. Mounted under ``settings.API_PREFIX``."""

from fastapi import APIRouter

from app.api.v1 import health

api_router = APIRouter()
api_router.include_router(health.router)

__all__ = ["api_router"]
