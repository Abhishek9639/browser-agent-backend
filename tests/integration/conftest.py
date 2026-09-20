"""Everything under tests/integration needs Postgres.

Marking here rather than decorating each test keeps `make test-int`'s
`-m integration` selector working without per-test boilerplate.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
async def clean_committed_audit_events(db_engine: AsyncEngine) -> None:
    """Remove audit rows that were committed outside the test transaction.

    Almost everything a test writes is rolled back by the ``db_session``
    fixture. The failed-login audit is the deliberate exception: it opens its
    own transaction so the record survives the 401, which is the behaviour we
    want in production and leaked state in a test suite.

    This runs before each test rather than after, so a failing test leaves its
    rows on the table to inspect.
    """
    async with db_engine.begin() as connection:
        await connection.execute(text("DELETE FROM audit_events"))
