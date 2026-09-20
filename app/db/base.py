"""Declarative base and the mixins every table reuses.

The naming convention matters more than it looks: Alembic autogenerate compares
the model metadata against what is actually in Postgres, and unnamed constraints
come back with server-generated names that never match. Fixing the names here
means a later ``alembic revision --autogenerate`` produces a clean diff instead
of dropping and recreating constraints on every run.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["NAMING_CONVENTION", "Base", "TimestampMixin", "UUIDPrimaryKeyMixin"]

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base for every ORM model. Spec section 5 keeps table names snake_case."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        pk: Any = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


class UUIDPrimaryKeyMixin:
    """A ``uuid`` primary key, defaulted on both sides.

    ``default`` covers ORM inserts, ``server_default`` covers raw SQL and
    migrations. Postgres 16 ships ``gen_random_uuid()`` in core, so there is no
    pgcrypto extension to install.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )


class TimestampMixin:
    """Timezone-aware audit timestamps.

    ``server_default`` means a row inserted outside the ORM still gets a
    timestamp. ``onupdate`` is applied by SQLAlchemy on ORM updates; a raw
    ``UPDATE`` will not touch ``updated_at``, which is a deliberate trade against
    adding a trigger this early.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
