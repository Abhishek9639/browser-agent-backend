"""Everything under tests/integration needs Postgres.

Marking here rather than decorating each test keeps `make test-int`'s
`-m integration` selector working without per-test boilerplate.
"""

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.integration)
