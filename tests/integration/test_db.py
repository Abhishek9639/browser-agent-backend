"""B03: the async database layer against a real Postgres."""

import uuid

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.db.session import Database
from tests.integration._models import SampleRow


async def test_session_executes_sql(db_session: AsyncSession) -> None:
    assert await db_session.scalar(text("SELECT 1")) == 1


async def test_connects_to_the_test_database_not_the_live_one(db_session: AsyncSession) -> None:
    """A suite that drops tables must never point at agent_core."""
    assert await db_session.scalar(text("SELECT current_database()")) == "agent_core_test"


async def test_server_is_postgres_16(db_session: AsyncSession) -> None:
    version = await db_session.scalar(text("SHOW server_version"))
    assert str(version).startswith("16.")


async def test_orm_round_trip_fills_defaults(db_session: AsyncSession) -> None:
    """UUID primary key and both timestamps are populated without being set."""
    row = SampleRow(label="round-trip")
    db_session.add(row)
    await db_session.commit()

    fetched = await db_session.scalar(select(SampleRow).where(SampleRow.label == "round-trip"))

    assert fetched is not None
    assert isinstance(fetched.id, uuid.UUID)
    assert fetched.created_at is not None
    assert fetched.updated_at is not None
    # Spec-wide rule: timestamps are timezone-aware.
    assert fetched.created_at.tzinfo is not None
    assert fetched.updated_at.tzinfo is not None


async def test_each_test_starts_clean(db_session: AsyncSession) -> None:
    """Proves the per-test rollback: the previous test's row is gone."""
    rows = (await db_session.scalars(select(SampleRow))).all()

    assert rows == []


async def test_unique_constraint_is_enforced(db_session: AsyncSession) -> None:
    db_session.add(SampleRow(label="duplicate"))
    await db_session.commit()

    db_session.add(SampleRow(label="duplicate"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_constraint_names_follow_the_naming_convention(db_engine: AsyncEngine) -> None:
    """Without this, alembic autogenerate produces churn on every run."""

    def _inspect(connection: object) -> tuple[str | None, list[str]]:
        inspector = inspect(connection)  # type: ignore[arg-type]
        pk = inspector.get_pk_constraint("_test_sample_rows")["name"]
        uniques = [c["name"] or "" for c in inspector.get_unique_constraints("_test_sample_rows")]
        return pk, uniques

    async with db_engine.connect() as connection:
        pk_name, unique_names = await connection.run_sync(_inspect)

    assert pk_name == "pk__test_sample_rows"
    assert "uq__test_sample_rows_label" in unique_names


async def test_healthcheck_passes_against_a_live_database(
    integration_settings: Settings,
) -> None:
    database = Database(integration_settings, url=integration_settings.test_database_url)
    try:
        assert await database.healthcheck() is True
    finally:
        await database.dispose()


async def test_healthcheck_fails_against_a_dead_database(integration_settings: Settings) -> None:
    """Wrong port, so the connection is refused rather than hanging."""
    dead = Database(
        integration_settings,
        url=integration_settings.dsn_for("agent_core").replace(
            f":{integration_settings.POSTGRES_PORT}/", ":1/"
        ),
    )
    try:
        assert await dead.healthcheck() is False
    finally:
        await dead.dispose()


async def test_live_database_is_at_migration_head(integration_settings: Settings) -> None:
    """`make migrate` has been run and nothing is pending.

    Compares against whatever alembic considers head rather than a hardcoded
    revision, so adding a migration does not break this test.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()

    database = Database(integration_settings)
    try:
        async with database.session() as session:
            version = await session.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await database.dispose()

    assert version == head, "run `make migrate`"
