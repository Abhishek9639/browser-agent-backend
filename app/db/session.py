"""Async engine, session factory, and the FastAPI dependencies over them.

The engine lives on ``app.state`` rather than at module scope, for the same
reason settings do: an app built by ``create_app(settings)`` must really use
those settings, and a test must be able to swap the whole thing out.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.errors import DependencyUnavailable
from app.core.logging import get_logger

__all__ = [
    "Database",
    "DatabaseDep",
    "SessionDep",
    "get_database",
    "get_session",
]

logger = get_logger(__name__)


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, settings: Settings, url: str | None = None) -> None:
        self._settings = settings
        self._url = url or settings.database_url
        self._engine: AsyncEngine = create_async_engine(
            self._url,
            echo=settings.DB_ECHO,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_pre_ping=True,
        )
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        return self._sessionmaker

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session that commits on success and rolls back on failure."""
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def healthcheck(self) -> bool:
        """``SELECT 1`` under a timeout. Never raises."""
        timeout = self._settings.DB_HEALTHCHECK_TIMEOUT_SECONDS
        try:
            # asyncio.timeout raises TimeoutError, which Exception already covers.
            async with asyncio.timeout(timeout), self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as exc:
            logger.warning("database_healthcheck_failed", error_type=type(exc).__name__)
            return False
        return True

    async def dispose(self) -> None:
        await self._engine.dispose()


def get_database(request: Request) -> Database:
    """The app's Database, or a 503 if the lifespan never attached one."""
    database = getattr(request.app.state, "db", None)
    if not isinstance(database, Database):
        raise DependencyUnavailable("The database is not configured.")
    return database


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """Request-scoped session. One transaction per request."""
    async with database.session() as session:
        yield session


DatabaseDep = Annotated[Database, Depends(get_database)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
