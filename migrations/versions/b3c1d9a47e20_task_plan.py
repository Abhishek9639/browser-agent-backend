"""task plan outline

Revision ID: b3c1d9a47e20
Revises: 7164634cb611
Create Date: 2026-10-13 12:00:00.000000

Adds ``tasks.plan``: the outline of expected steps shown before the first
action, so a reconnecting client still knows how long the task is.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b3c1d9a47e20"
down_revision: str | Sequence[str] | None = "7164634cb611"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks", sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tasks", "plan")
