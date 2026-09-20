"""A throwaway model used only by the integration suite.

It exercises the pieces B03 actually delivers: the UUID primary key default, the
timestamp mixin's server defaults, and the metadata naming convention. Importing
this module registers the table on ``Base.metadata``, which is what the engine
fixture creates and drops.

The name is prefixed so it can never collide with a real table from spec
section 5.
"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SampleRow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "_test_sample_rows"

    label: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
