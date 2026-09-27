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

__all__ = ["PLANNER_FILENAME", "PROMPT_PACKAGE", "PromptNotFoundError", "PromptRegistry"]

logger = get_logger(__name__)

PROMPT_PACKAGE: Final = "app.agent.planner.prompts"
PLANNER_FILENAME: Final = "planner_{version}.md"


class PromptNotFoundError(RuntimeError):
    """The configured prompt version does not exist.

    Raised at load time rather than on the first task: a misconfigured version
    means the agent would run with no system prompt at all, which is precisely
    the condition the injection defences depend on.
    """


@lru_cache(maxsize=16)
def _read(package: str, pattern: str, version: str) -> str:
    name = pattern.format(version=version)
    try:
        return resources.files(package).joinpath(name).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise PromptNotFoundError(
            f"No prompt for version {version!r} (expected {name} in {package})."
        ) from exc


class PromptRegistry:
    """Loads a versioned system prompt.

    Parameterised by package and filename so the vision prompt uses the same
    loading, the same eager failure and the same recorded version as the
    planner's, rather than a second copy of all three.
    """

    def __init__(
        self,
        version: str = "v1",
        *,
        package: str = PROMPT_PACKAGE,
        filename: str = PLANNER_FILENAME,
    ) -> None:
        self._version = version.strip() or "v1"
        # Read eagerly so a bad version fails at construction, not mid-task.
        self._text = _read(package, filename, self._version)
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
