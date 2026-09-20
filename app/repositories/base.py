"""Generic async repository.

Repositories own queries; they do not own transactions. Every method here works
on a session someone else opened, and none of them commit. See
``docs/phase-notes/B05.md`` for who does.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil
from typing import Any, ClassVar, Final

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import NotFound
from app.db.base import Base

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "BaseRepository",
    "Page",
    "escape_like",
]

# Decision A17. The API layer validates its own query parameters; these are the
# repository's own guard rails so no caller can ask for a million rows.
DEFAULT_PAGE_SIZE: Final = 20
MAX_PAGE_SIZE: Final = 100


@dataclass(frozen=True, slots=True)
class Page[ModelT: Base]:
    """One page of results, plus what a client needs to ask for the next."""

    items: Sequence[ModelT]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        return ceil(self.total / self.page_size) if self.page_size else 0

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def has_previous(self) -> bool:
        return self.page > 1


def escape_like(term: str) -> str:
    """Neutralise LIKE wildcards in user input.

    Without this, a search for ``100%`` matches everything, and ``_`` matches
    any single character. Pair with ``escape="\\\\"`` on the comparison.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class BaseRepository[ModelT: Base]:
    """Query helpers shared by every repository.

    Subclasses set :attr:`model` and add the queries their aggregate needs.
    """

    model: ClassVar[type[Any]]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        """By primary key, or ``None``."""
        result: ModelT | None = await self.session.get(self.model, entity_id)
        return result

    async def get_or_404(self, entity_id: uuid.UUID) -> ModelT:
        """By primary key, raising :class:`~app.core.errors.NotFound`."""
        entity = await self.get(entity_id)
        if entity is None:
            raise NotFound(f"{self.model.__name__} {entity_id} does not exist.")
        return entity

    async def add(self, entity: ModelT) -> ModelT:
        """Stage an insert and flush it.

        Flushing means the caller gets server-side defaults (the primary key,
        ``task_number``) straight away. It does not commit: the transaction is
        still the session owner's to finish.
        """
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def add_all(self, entities: Sequence[ModelT]) -> Sequence[ModelT]:
        self.session.add_all(list(entities))
        await self.session.flush()
        return entities

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)
        await self.session.flush()

    async def count(self, *where: ColumnElement[bool]) -> int:
        stmt = select(func.count()).select_from(self.model)
        if where:
            stmt = stmt.where(*where)
        return int(await self.session.scalar(stmt) or 0)

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        where: Sequence[ColumnElement[bool]] = (),
        order_by: Sequence[Any] = (),
    ) -> Page[ModelT]:
        """A page of rows, newest first unless told otherwise."""
        stmt: Select[tuple[Any]] = select(self.model)
        if where:
            stmt = stmt.where(*where)
        if order_by:
            stmt = stmt.order_by(*order_by)
        return await self.paginate(stmt, page=page, page_size=page_size)

    async def paginate(
        self,
        stmt: Select[Any],
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[ModelT]:
        """Run a built statement as a page.

        The count is taken from the same statement with its ordering stripped,
        so filters can never drift between the rows and the total.
        """
        page = max(page, 1)
        page_size = min(max(page_size, 1), MAX_PAGE_SIZE)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(await self.session.scalar(count_stmt) or 0)

        rows = await self.session.scalars(stmt.limit(page_size).offset((page - 1) * page_size))
        return Page(items=list(rows.all()), total=total, page=page, page_size=page_size)
