"""step url and viewport

Revision ID: d5f1a8c3e927
Revises: c7d2e4f18a90
Create Date: 2026-11-28 12:00:00.000000

Adds ``task_steps.url``, ``viewport_width`` and ``viewport_height``: the page a
step left behind and the size of the page area its bbox and screenshot were
measured in. A step replayed from the database needs the viewport to draw its
outline box, which the live view places as a share of it. Nullable: steps
recorded before this have none, and replay them without the box as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5f1a8c3e927"
down_revision: str | Sequence[str] | None = "c7d2e4f18a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("task_steps", sa.Column("url", sa.Text(), nullable=True))
    op.add_column("task_steps", sa.Column("viewport_width", sa.Integer(), nullable=True))
    op.add_column("task_steps", sa.Column("viewport_height", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_steps", "viewport_height")
    op.drop_column("task_steps", "viewport_width")
    op.drop_column("task_steps", "url")
