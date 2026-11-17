"""step plan item

Revision ID: c7d2e4f18a90
Revises: b3c1d9a47e20
Create Date: 2026-11-10 12:00:00.000000

Adds ``task_steps.plan_step``: which item of the task's plan a step worked on,
so a reconnecting client can tick off the same plan items the live one did.
Nullable: a task without a plan, and every step recorded before this, has none.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d2e4f18a90"
down_revision: str | Sequence[str] | None = "b3c1d9a47e20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("task_steps", sa.Column("plan_step", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_steps", "plan_step")
