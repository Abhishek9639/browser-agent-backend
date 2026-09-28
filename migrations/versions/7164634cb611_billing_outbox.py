"""billing outbox and the credits charged per task

Revision ID: 7164634cb611
Revises: a78013bebd3a
Create Date: 2026-09-28 22:09:58.120218

Hand-corrected after autogenerate, which wanted to rewrite the server default on
``tasks.task_number`` in both directions. That column is unchanged: Postgres
renders the default it already has as ``('T-'::text || lpad((nextval(...
::regclass))::text, 6, '0'::text))`` and the model spells the same expression
without the casts, so every autogenerate run sees a difference that is not one.
Applying it would be churn; the downgrade would have rewritten a default nobody
asked to change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7164634cb611"
down_revision: str | Sequence[str] | None = "a78013bebd3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the change."""
    op.create_table(
        "billing_outbox",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_outbox")),
        # The idempotency key is the identity of the call, not of the row. A
        # second attempt to queue the same grant or settle is refused by the
        # database rather than by whoever remembered to check first.
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_billing_outbox_idempotency_key")),
    )
    # The worker's only query: what is due and not yet delivered. Partial, so the
    # index is the size of the backlog rather than the size of the history.
    op.create_index(
        "ix_billing_outbox_pending",
        "billing_outbox",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("done_at IS NULL"),
    )
    # Nullable: every task that finished before this migration has no recorded
    # charge, and inventing one from its steps would be a guess at the prices
    # that were in force at the time.
    op.add_column("tasks", sa.Column("credits_used", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Undo the change."""
    op.drop_column("tasks", "credits_used")
    op.drop_index(
        "ix_billing_outbox_pending",
        table_name="billing_outbox",
        postgresql_where=sa.text("done_at IS NULL"),
    )
    op.drop_table("billing_outbox")
