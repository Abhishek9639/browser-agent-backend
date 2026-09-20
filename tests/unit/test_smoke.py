"""Smoke tests: the suite runs, the package imports, and async mode is live."""

import importlib


def test_app_package_imports() -> None:
    assert importlib.import_module("app") is not None


def test_agent_subpackages_import() -> None:
    for name in (
        "browser",
        "perception",
        "vision",
        "planner",
        "llm",
        "safety",
        "verification",
        "recovery",
        "cache",
    ):
        assert importlib.import_module(f"app.agent.{name}") is not None


async def test_asyncio_mode_is_auto() -> None:
    """No decorator here on purpose: this only runs if asyncio_mode=auto is active."""
    assert True
