"""B04: the migration really round-trips.

Runs against a throwaway database so a failure cannot leave ``agent_core`` or
``agent_core_test`` half-migrated. Alembic is driven as a subprocess, which both
avoids nesting event loops (``migrations/env.py`` calls ``asyncio.run``) and
exercises the same code path ``make migrate`` uses.
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator

import asyncpg
import pytest

from app.core.config import Settings

SCRATCH_DB = "agent_core_migration_check"


async def _admin_connection(settings: Settings) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD.get_secret_value(),
        database="postgres",
    )


@pytest.fixture
async def scratch_database(integration_settings: Settings) -> AsyncIterator[str]:
    """A database of its own, dropped however the test ends."""
    admin = await _admin_connection(integration_settings)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        await admin.close()

    try:
        yield SCRATCH_DB
    finally:
        admin = await _admin_connection(integration_settings)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        finally:
            await admin.close()


def run_alembic(*args: str, database: str) -> None:
    """Invoke alembic against `database`, as a subprocess with its own settings."""
    env = {**os.environ, "POSTGRES_DB": database}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")


async def inspect_schema(settings: Settings, database: str) -> dict[str, list[str]]:
    connection = await asyncpg.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD.get_secret_value(),
        database=database,
    )
    try:
        tables = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
        )
        types = await connection.fetch("SELECT typname FROM pg_type WHERE typtype = 'e' ORDER BY 1")
        sequences = await connection.fetch(
            "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' ORDER BY 1"
        )
    finally:
        await connection.close()
    return {
        "tables": [r[0] for r in tables],
        "types": [r[0] for r in types],
        "sequences": [r[0] for r in sequences],
    }


async def test_upgrade_downgrade_upgrade(
    integration_settings: Settings, scratch_database: str
) -> None:
    """The cycle autogenerate gets wrong: enum types survive a drop_table."""
    run_alembic("upgrade", "head", database=scratch_database)

    after_upgrade = await inspect_schema(integration_settings, scratch_database)
    assert "tasks" in after_upgrade["tables"]
    assert "task_steps" in after_upgrade["tables"]
    assert "refresh_tokens" in after_upgrade["tables"]
    assert "task_status" in after_upgrade["types"]
    assert "task_number_seq" in after_upgrade["sequences"]

    run_alembic("downgrade", "base", database=scratch_database)

    after_downgrade = await inspect_schema(integration_settings, scratch_database)
    # alembic_version is created by alembic itself and legitimately remains.
    assert [t for t in after_downgrade["tables"] if t != "alembic_version"] == []
    assert after_downgrade["types"] == [], "enum types outlived downgrade"
    assert after_downgrade["sequences"] == [], "sequence outlived downgrade"

    # The real point: this only works because downgrade dropped the types.
    run_alembic("upgrade", "head", database=scratch_database)

    after_reupgrade = await inspect_schema(integration_settings, scratch_database)
    assert after_reupgrade["tables"] == after_upgrade["tables"]
    assert after_reupgrade["types"] == after_upgrade["types"]


async def test_every_spec_table_is_created_by_the_migration(
    integration_settings: Settings, scratch_database: str
) -> None:
    """Not just by metadata.create_all, which is what the other tests use."""
    run_alembic("upgrade", "head", database=scratch_database)

    schema = await inspect_schema(integration_settings, scratch_database)

    assert set(schema["tables"]) == {
        "alembic_version",
        "approvals",
        "audit_events",
        "cached_flows",
        "llm_decisions",
        "recovery_attempts",
        "refresh_tokens",
        "task_steps",
        "tasks",
        "users",
        "verifications",
    }
    assert set(schema["types"]) == {
        "action_type",
        "approval_status",
        "recovery_outcome",
        "risk_level",
        "step_status",
        "task_status",
        "user_role",
        "verification_method",
    }
