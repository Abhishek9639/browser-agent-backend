"""B04: the schema matches spec section 5 without touching a database."""

import pytest
from sqlalchemy import UniqueConstraint

import app.models  # registers the tables
from app.db.base import Base

SPEC_TABLES = {
    "users",
    "tasks",
    "task_steps",
    "llm_decisions",
    "verifications",
    "recovery_attempts",
    "approvals",
    "audit_events",
    "cached_flows",
}


def test_every_spec_table_exists() -> None:
    assert set(Base.metadata.tables) >= SPEC_TABLES


# Tables that are not in spec section 5, each with the reason it exists. The
# test below fails on anything else, which is the point: a new table has to be
# argued for here before it can be added.
JUSTIFIED_ADDITIONS = {
    # Refresh rotation needs somewhere to keep a chain. The spec notes this in
    # section 5 itself.
    "refresh_tokens",
    # Section 7 says a settle must survive the payments service being briefly
    # unavailable. A queue written in the same transaction as the business
    # change is the only way to promise that, and a queue needs a table.
    "billing_outbox",
}


def test_only_the_justified_additions_exist() -> None:
    """Anything else appearing here is scope creep and should be justified."""
    ours = {name for name in Base.metadata.tables if not name.startswith("_test_")}

    assert ours - SPEC_TABLES == JUSTIFIED_ADDITIONS


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("tasks", ("task_number",)),
        ("task_steps", ("task_id",)),
        ("llm_decisions", ("step_id",)),
        ("verifications", ("step_id",)),
        ("recovery_attempts", ("step_id",)),
        ("approvals", ("task_id", "created_at")),
        ("audit_events", ("task_id", "timestamp")),
        ("cached_flows", ("task_signature",)),
    ],
)
def test_spec_section_5_indexes_exist(table: str, columns: tuple[str, ...]) -> None:
    """Every index the spec names, by the columns it names."""
    target = Base.metadata.tables[table]
    indexed = {tuple(c.name for c in ix.columns) for ix in target.indexes}
    unique = {
        tuple(c.name for c in con.columns)
        for con in target.constraints
        if isinstance(con, UniqueConstraint)
    }
    assert columns in indexed | unique


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("users", ("email",)),
        ("tasks", ("task_number",)),
        ("task_steps", ("task_id", "step_number")),
        ("cached_flows", ("task_signature",)),
        ("refresh_tokens", ("token_hash",)),
    ],
)
def test_unique_constraints(table: str, columns: tuple[str, ...]) -> None:
    target = Base.metadata.tables[table]
    unique = {
        tuple(c.name for c in con.columns)
        for con in target.constraints
        if isinstance(con, UniqueConstraint)
    }
    assert columns in unique


@pytest.mark.parametrize(
    ("table", "column", "expected"),
    [
        ("task_steps", "task_id", "CASCADE"),
        ("llm_decisions", "step_id", "CASCADE"),
        ("verifications", "step_id", "CASCADE"),
        ("recovery_attempts", "step_id", "CASCADE"),
        ("approvals", "task_id", "CASCADE"),
        ("tasks", "created_by", "RESTRICT"),
        ("approvals", "approved_by", "RESTRICT"),
        ("audit_events", "actor_id", "RESTRICT"),
        ("tasks", "cache_flow_id", "SET NULL"),
        ("refresh_tokens", "user_id", "CASCADE"),
        ("refresh_tokens", "replaced_by_id", "SET NULL"),
    ],
)
def test_on_delete_behaviour(table: str, column: str, expected: str) -> None:
    """The task tree cascades; users are protected."""
    fks = [fk for fk in Base.metadata.tables[table].foreign_keys if fk.parent.name == column]
    assert len(fks) == 1
    assert fks[0].ondelete == expected


def test_credits_reservation_id_is_not_a_foreign_key() -> None:
    """It lives in the payments service's database (spec section 7)."""
    tasks = Base.metadata.tables["tasks"]
    assert tasks.c.credits_reservation_id.foreign_keys == set()


def test_audit_metadata_column_keeps_the_spec_name() -> None:
    """Decision A11: column is `metadata`, attribute is `event_metadata`."""
    assert "metadata" in Base.metadata.tables["audit_events"].c
    assert app.models.AuditEvent.event_metadata.key == "event_metadata"


def test_json_columns_are_jsonb() -> None:
    from sqlalchemy.dialects.postgresql import JSONB

    expected = {
        "tasks": ["final_answer"],
        "task_steps": ["bbox"],
        "llm_decisions": ["raw_output"],
        "verifications": ["expected_outcome", "actual_outcome"],
        "approvals": ["proposed_action"],
        "audit_events": ["metadata"],
        "cached_flows": ["action_sequence"],
    }
    for table, columns in expected.items():
        for column in columns:
            actual = Base.metadata.tables[table].c[column].type
            assert isinstance(actual, JSONB), f"{table}.{column} is {actual}"


def test_relationships_never_lazy_load() -> None:
    """lazy="raise" everywhere: no accidental N+1, no implicit IO in async."""
    from sqlalchemy import inspect

    for model in (
        app.models.User,
        app.models.Task,
        app.models.TaskStep,
        app.models.LLMDecision,
        app.models.Verification,
        app.models.RecoveryAttempt,
        app.models.Approval,
        app.models.RefreshToken,
    ):
        for rel in inspect(model).relationships:
            assert rel.lazy == "raise", f"{model.__name__}.{rel.key} is {rel.lazy}"
