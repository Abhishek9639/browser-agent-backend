"""Run the DOM collector and type what it returns (spec sections 4.6, 4.11).

The JavaScript lives in ``dom_extractor.js`` beside this module and is loaded
as a package resource, so it is edited as JavaScript with syntax highlighting
and linting rather than as a string literal.

Every element the collector returns has been stamped with ``data-agent-idx``,
which is what :func:`locator_for_index` turns back into a Playwright locator.
That indirection is the whole point of the perception layer: the planner picks
an index out of a compact summary, never a CSS selector it invented.
"""

import json
from functools import cache
from importlib import resources
from typing import Any, Final, Protocol

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from pydantic import BaseModel, ConfigDict

from app.core.logging import get_logger

__all__ = [
    "MASK",
    "BoundingBox",
    "ElementInfo",
    "PageNotReadyError",
    "extract",
    "locator_for_index",
    "locator_in_any_frame",
]

logger = get_logger(__name__)

MASK: Final = "••••"
STAMP_ATTRIBUTE: Final = "data-agent-idx"
DEFAULT_TIMEOUT_MS: Final = 5_000


class PageNotReadyError(RuntimeError):
    """The collector could not run.

    Usually a navigation in flight, so the document was replaced underneath the
    evaluate call. The orchestrator's answer is to wait and re-perceive rather
    than to fail the step, which is why this is distinct from a driver error.
    """


class BoundingBox(BaseModel):
    """Position in top-level viewport coordinates.

    Elements inside a same-origin iframe have the frame's offset already added,
    so a single coordinate space covers the whole page. Otherwise every
    consumer, the highlight in the live view, the set-of-marks annotator, the
    click-by-bbox recovery strategy, would need to redo that arithmetic.
    """

    x: int
    y: int
    width: int
    height: int


class ElementInfo(BaseModel):
    """One interactive element, as the planner will see it."""

    model_config = ConfigDict(frozen=True)

    index: int
    tag: str
    role: str | None = None
    name: str = ""
    text: str = ""
    placeholder: str | None = None
    input_type: str | None = None
    # Already masked by the collector when the field looks like a secret.
    value: str | None = None
    href: str | None = None
    bbox: BoundingBox
    in_viewport: bool
    covered: bool = False
    in_modal: bool = False
    frame_id: str = "main"
    css_path: str = ""
    xpath: str = ""
    disabled: bool = False
    checked: bool | None = None
    selected: bool | None = None
    expanded: bool | None = None
    cross_origin: bool = False

    @property
    def is_masked(self) -> bool:
        return self.value == MASK

    @property
    def label(self) -> str:
        """The best human-readable handle, for the summary line."""
        return self.name or self.text or self.placeholder or self.tag


class _Evaluator(Protocol):
    """The slice of a Playwright ``Page`` this module needs."""

    async def evaluate(self, expression: str, arg: Any = None) -> Any: ...


@cache
def modal_scope_source() -> str:
    """Shared, read-only modal detection for perception and action guards."""
    return (
        resources.files("app.agent.perception")
        .joinpath("modal_scope.js")
        .read_text(encoding="utf-8")
    )


@cache
def collector_source() -> str:
    """The collector script, read once per process."""
    return (
        resources.files("app.agent.perception")
        .joinpath("dom_extractor.js")
        .read_text(encoding="utf-8")
        .replace("__MODAL_SCOPE__", modal_scope_source())
    )


async def extract(
    page: _Evaluator,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> list[ElementInfo]:
    """Collect every visible interactive element on the page.

    Hidden elements are not returned at all. Spec section 4.11 requires that:
    ``display:none``, zero-size, off-screen and ``aria-hidden`` content is
    exactly where injected instructions hide, and an element the model cannot
    see is an element it cannot be told to act on.
    """
    script = f"async () => {{ const run = {collector_source()}; return run(); }}"

    try:
        raw = await _with_timeout(page, script, timeout_ms)
    except PlaywrightError as exc:
        message = str(exc).splitlines()[0]
        raise PageNotReadyError(f"Could not read the page: {message}") from exc

    if not isinstance(raw, list):  # pragma: no cover - the collector always returns a list
        raise PageNotReadyError("The collector returned something unexpected.")

    elements = [ElementInfo.model_validate(item) for item in raw]
    logger.debug(
        "dom_extracted",
        total=len(elements),
        in_viewport=sum(1 for element in elements if element.in_viewport),
        covered=sum(1 for element in elements if element.covered),
    )
    return elements


async def _with_timeout(page: _Evaluator, script: str, timeout_ms: int) -> Any:
    """Bound the collector's run.

    A page with tens of thousands of nodes, or one rebuilding itself in a loop,
    can make the walk take long enough to eat the step budget. Better to give
    up and let the orchestrator re-perceive.
    """
    import asyncio

    try:
        async with asyncio.timeout(timeout_ms / 1000):
            return await page.evaluate(script)
    except TimeoutError as exc:
        raise PageNotReadyError(f"The page did not respond within {timeout_ms} ms.") from exc


async def locator_in_any_frame(page: Page, selector: str) -> Locator | None:
    """The first frame in which a selector matches, or None.

    The main frame is tried first because it is first in ``page.frames``, so a
    page without iframes behaves exactly as it did when this was a plain
    ``page.locator``. Only when nothing matches there does this look inside
    child frames, which is where an element in a same-origin iframe lives:
    ``page.locator`` never sees it, and the agent perceives such elements
    perfectly well and then cannot touch them.
    """
    for frame in page.frames:
        try:
            locator = frame.locator(selector)
            if await locator.count():
                return locator.first
        except PlaywrightError:
            # A frame can detach between being listed and being queried.
            continue
    return None


async def locator_for_index(page: Page, index: int) -> Locator:
    """The Playwright locator for a stamped element.

    Searches every frame, because an element inside a same-origin iframe is
    stamped in that frame's document and ``page.locator`` only sees the main
    one.

    Resolving through the stamp rather than a stored CSS path is deliberate:
    the stamp survives a re-render that changes class names, which is one of
    the chaos cases in spec section 12.
    """
    selector = f"[{STAMP_ATTRIBUTE}={json.dumps(str(index))}]"

    found = await locator_in_any_frame(page, selector)
    if found is not None:
        return found

    raise PageNotReadyError(f"No element is stamped with index {index}. Re-perceive before acting.")
