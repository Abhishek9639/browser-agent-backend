"""Fixtures for the chaos suite.

The suite is marked `chaos` and is not part of `make check`: it drives a real
browser against a real site and takes minutes, not seconds.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm.base import LLMClient
from app.core.config import Settings
from tests.chaos.harness import build_client, skyfare_server


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run the chaos suite against the configured LLM provider instead of the mock. "
        "Costs real money.",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "chaos" in str(item.fspath):
            item.add_marker(pytest.mark.chaos)


@pytest.fixture(scope="session")
def skyfare() -> Iterator[str]:
    with skyfare_server() as base:
        yield base


@pytest.fixture(scope="session")
def live(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--live"))


@pytest.fixture
def chaos_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={
            "STORAGE_DIR": str(tmp_path),
            "AGENT_ALLOWED_DOMAINS": ["127.0.0.1", "localhost"],
            "AGENT_HEADLESS": True,
            # A chaos page can be slow on purpose; the default budget is for a
            # real task, not for one fighting a three second render delay.
            "AGENT_MAX_STEPS": 18,
            "AGENT_MAX_TASK_SECONDS": 240,
        }
    )


@pytest.fixture
def llm(chaos_settings: Settings, live: bool) -> LLMClient:
    return build_client(chaos_settings, live=live)


@pytest.fixture
async def chaos_session(db_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """The test transaction, rolled back like any other integration test."""
    yield db_session
