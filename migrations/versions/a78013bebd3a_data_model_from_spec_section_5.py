"""data model from spec section 5

Revision ID: a78013bebd3a
Revises: 0001_baseline
Create Date: 2026-09-20 19:47:25.622532

Hand-corrected after autogenerate, which got four things wrong:

1. It emitted no ``CREATE SEQUENCE`` for ``task_number_seq``, although the
   ``tasks.task_number`` server default calls ``nextval`` on it. Alembic does
   not autogenerate sequences.
2. ``risk_level`` is used by both ``approvals.risk_flag`` and
   ``llm_decisions.risk_level``. Autogenerate inlined ``CREATE TYPE`` in each
   ``create_table``, so the second one aborts with "type already exists". The
   types are now created once, up front, with ``checkfirst``.
3. ``drop_table`` does not drop a Postgres enum type, so downgrade left all
   eight behind and the next upgrade failed. Downgrade now drops them.
4. ``credits_reservation_id`` came out as generic ``sa.Uuid`` rather than the
   Postgres ``UUID`` every other id uses; fixed in the model.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a78013bebd3a"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every native enum, created once before the tables that reference them.
# Values, not member names, matching app/domain/enums.py.
ENUM_VALUES: dict[str, tuple[str, ...]] = {
    "user_role": ("TASK_SUBMITTER", "APPROVER", "ADMIN"),
    "task_status": (
        "SUBMITTED",
        "PLANNING",
        "ACTING",
        "VERIFYING",
        "RECOVERING",
        "AWAITING_APPROVAL",
        "COMPLETED",
        "EXECUTION_FAILED",
        "REJECTED",
        "CANCELLED",
        "TIMED_OUT",
    ),
    "step_status": (
        "PENDING",
        "AWAITING_APPROVAL",
        "EXECUTING",
        "VERIFYING",
        "RECOVERING",
        "VERIFIED",
        "FAILED",
        "SKIPPED",
    ),
    "action_type": (
        "navigate",
        "click",
        "type",
        "scroll",
        "select",
        "extract",
        "wait",
        "go_back",
        "new_tab",
        "switch_tab",
        "close_tab",
        "done",
    ),
    "risk_level": ("LOW", "MEDIUM", "HIGH"),
    "approval_status": ("PENDING", "APPROVED", "REJECTED", "EXPIRED"),
    "verification_method": (
        "url_contains",
        "element_visible",
        "text_present",
        "value_equals",
        "dom_changed",
        "new_tab_opened",
    ),
    "recovery_outcome": ("SUCCEEDED", "FAILED", "SKIPPED"),
}

TASK_NUMBER_SEQUENCE = "task_number_seq"


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created type; never emit CREATE TYPE inline."""
    return postgresql.ENUM(*ENUM_VALUES[name], name=name, create_type=False)


def upgrade() -> None:
    """Create the spec section 5 schema, plus refresh_tokens."""
    bind = op.get_bind()

    for name, values in ENUM_VALUES.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    # Must exist before `tasks`, whose default calls nextval on it.
    op.execute(sa.text(f"CREATE SEQUENCE IF NOT EXISTS {TASK_NUMBER_SEQUENCE} START 1 INCREMENT 1"))

    op.create_table(
        "cached_flows",
        sa.Column("task_signature", sa.String(length=64), nullable=False),
        sa.Column("action_sequence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("success_rate", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("run_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cached_flows")),
        sa.UniqueConstraint("task_signature", name=op.f("uq_cached_flows_task_signature")),
    )
    op.create_table(
        "users",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", _enum("user_role"), server_default="TASK_SUBMITTER", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "refresh_tokens",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.UUID(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["refresh_tokens.id"],
            name=op.f("fk_refresh_tokens_replaced_by_id_refresh_tokens"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)
    op.create_table(
        "tasks",
        sa.Column(
            "task_number",
            sa.String(length=16),
            server_default=sa.text(
                f"'T-' || lpad(nextval('{TASK_NUMBER_SEQUENCE}')::text, 6, '0')"
            ),
            nullable=False,
        ),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", _enum("task_status"), server_default="SUBMITTED", nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("start_url", sa.String(length=2048), nullable=True),
        sa.Column("final_answer", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("credits_reservation_id", sa.UUID(), nullable=True),
        sa.Column("cache_flow_id", sa.UUID(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cache_flow_id"],
            ["cached_flows.id"],
            name=op.f("fk_tasks_cache_flow_id_cached_flows"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_tasks_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.UniqueConstraint("task_number", name=op.f("uq_tasks_task_number")),
    )
    op.create_table(
        "audit_events",
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_audit_events_actor_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_audit_events_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        "ix_audit_events_task_id_timestamp",
        "audit_events",
        ["task_id", "timestamp"],
        unique=False,
    )
    op.create_table(
        "task_steps",
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("action", _enum("action_type"), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("target_index", sa.Integer(), nullable=True),
        sa.Column("value_redacted", sa.Text(), nullable=True),
        sa.Column("tab_id", sa.String(length=32), nullable=True),
        sa.Column("selector_used", sa.Text(), nullable=True),
        sa.Column("bbox", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("screenshot_ref", sa.String(length=512), nullable=True),
        sa.Column("status", _enum("step_status"), server_default="PENDING", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_task_steps_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_steps")),
        sa.UniqueConstraint("task_id", "step_number", name="uq_task_steps_task_id_step_number"),
    )
    op.create_index("ix_task_steps_task_id", "task_steps", ["task_id"], unique=False)
    op.create_table(
        "approvals",
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("step_id", sa.UUID(), nullable=True),
        sa.Column("risk_flag", _enum("risk_level"), nullable=False),
        sa.Column("proposed_action", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "approval_status",
            _enum("approval_status"),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("approved_by", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name=op.f("fk_approvals_approved_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["task_steps.id"],
            name=op.f("fk_approvals_step_id_task_steps"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_approvals_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    op.create_index(
        "ix_approvals_task_id_created_at",
        "approvals",
        ["task_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "llm_decisions",
        sa.Column("step_id", sa.UUID(), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("reasoning_summary", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("risk_level", _enum("risk_level"), nullable=False),
        sa.Column("used_vision", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("raw_output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["task_steps.id"],
            name=op.f("fk_llm_decisions_step_id_task_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_decisions")),
    )
    op.create_index("ix_llm_decisions_step_id", "llm_decisions", ["step_id"], unique=False)
    op.create_table(
        "recovery_attempts",
        sa.Column("step_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("strategy_used", sa.String(length=64), nullable=False),
        sa.Column("outcome", _enum("recovery_outcome"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["task_steps.id"],
            name=op.f("fk_recovery_attempts_step_id_task_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recovery_attempts")),
    )
    op.create_index("ix_recovery_attempts_step_id", "recovery_attempts", ["step_id"], unique=False)
    op.create_table(
        "verifications",
        sa.Column("step_id", sa.UUID(), nullable=False),
        sa.Column("expected_outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actual_outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("verification_method", _enum("verification_method"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["task_steps.id"],
            name=op.f("fk_verifications_step_id_task_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verifications")),
    )
    op.create_index("ix_verifications_step_id", "verifications", ["step_id"], unique=False)


def downgrade() -> None:
    """Drop everything upgrade created, including the types and the sequence."""
    bind = op.get_bind()

    op.drop_index("ix_verifications_step_id", table_name="verifications")
    op.drop_table("verifications")
    op.drop_index("ix_recovery_attempts_step_id", table_name="recovery_attempts")
    op.drop_table("recovery_attempts")
    op.drop_index("ix_llm_decisions_step_id", table_name="llm_decisions")
    op.drop_table("llm_decisions")
    op.drop_index("ix_approvals_task_id_created_at", table_name="approvals")
    op.drop_table("approvals")
    op.drop_index("ix_task_steps_task_id", table_name="task_steps")
    op.drop_table("task_steps")
    op.drop_index("ix_audit_events_task_id_timestamp", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("tasks")
    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
    op.drop_table("cached_flows")

    op.execute(sa.text(f"DROP SEQUENCE IF EXISTS {TASK_NUMBER_SEQUENCE}"))

    # drop_table leaves enum types behind; without this a re-upgrade fails.
    for name, values in ENUM_VALUES.items():
        postgresql.ENUM(*values, name=name).drop(bind, checkfirst=True)
