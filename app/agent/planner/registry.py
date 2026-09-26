"""Versioned prompt loading.

Spec section 9 requires the prompt version on every step, and
``llm_decisions.prompt_version`` stores it. That is not bookkeeping: prompt
changes move an agent's behaviour as much as model changes do, and without the
version recorded there is no way to tell whether last week's better numbers
came from a better prompt or a different page.

Prompts are files on disk rather than string literals so a change to one shows
up as a readable diff.
"""

from functools import lru_cache
from importlib import resources
from typing import Final

from app.core.logging import get_logger

__all__ = ["PROMPT_PACKAGE", "PromptNotFoundError", "PromptRegistry"]

logger = get_logger(__name__)

PROMPT_PACKAGE: Final = "app.agent.planner.prompts"
_FILENAME: Final = "planner_{version}.md"


class PromptNotFoundError(RuntimeError):
    """The configured prompt version does not exist.

    Raised at load time rather than on the first task: a misconfigured version
    means the agent would run with no system prompt at all, which is precisely
    the condition the injection defences depend on.
    """


@lru_cache(maxsize=8)
def _read(version: str) -> str:
    name = _FILENAME.format(version=version)
    try:
        return resources.files(PROMPT_PACKAGE).joinpath(name).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise PromptNotFoundError(
            f"No planner prompt for version {version!r} (expected {name})."
        ) from exc


class PromptRegistry:
    """Loads the planner system prompt for a given version."""

    def __init__(self, version: str = "v1") -> None:
        self._version = version.strip() or "v1"
        # Read eagerly so a bad version fails at construction, not mid-task.
        self._text = _read(self._version)
        logger.debug("prompt_loaded", version=self._version, chars=len(self._text))

    @property
    def version(self) -> str:
        """Recorded with every decision."""
        return self._version

    @property
    def system_prompt(self) -> str:
        return self._text

    def for_version(self, version: str) -> "PromptRegistry":
        """Another version, for an A/B comparison or an eval sweep."""
        return PromptRegistry(version)
