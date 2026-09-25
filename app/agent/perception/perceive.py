"""One perception pass: look, summarise, hash (spec section 4.6)."""

from typing import Protocol

from app.agent.perception.dom_extractor import extract
from app.agent.perception.state import PageState, ScrollState, compute_dom_hash
from app.agent.perception.summarizer import SUMMARY_TOKEN_BUDGET, build_header, build_summary
from app.core.logging import get_logger

__all__ = ["TabSource", "perceive"]

logger = get_logger(__name__)

# Page metrics in one round trip. Doing these as separate evaluate calls would
# be three crossings of the process boundary per step.
_METRICS = """() => ({
  url: location.href,
  title: document.title,
  scrollY: Math.round(window.scrollY),
  maxScrollY: Math.max(
    0,
    Math.round(
      Math.max(
        document.documentElement.scrollHeight,
        document.body ? document.body.scrollHeight : 0
      ) - window.innerHeight
    )
  )
})"""


class TabSource(Protocol):
    """What perception needs to know about tabs.

    A protocol rather than the driver itself, so the loop can be exercised
    against a fake without a browser.
    """

    @property
    def tab_ids(self) -> list[str]: ...

    @property
    def active_tab_id(self) -> str: ...


async def perceive(
    page: object,
    session: TabSource | None = None,
    *,
    token_budget: int = SUMMARY_TOKEN_BUDGET,
) -> PageState:
    """Read the page and build the state the planner will see.

    The ordering matters: elements are collected first, because collecting
    stamps the DOM, and everything afterwards refers to those indices.
    """
    elements = await extract(page)  # type: ignore[arg-type]
    metrics = await page.evaluate(_METRICS)  # type: ignore[attr-defined]

    tabs = list(session.tab_ids) if session else []
    active = session.active_tab_id if session else "main"

    header = build_header(
        url=metrics["url"],
        title=metrics["title"],
        tabs=tabs,
        active_tab_id=active,
        scroll_y=metrics["scrollY"],
        scroll_max_y=metrics["maxScrollY"],
    )
    summary = build_summary(elements, header, token_budget=token_budget)

    state = PageState(
        url=metrics["url"],
        title=metrics["title"],
        tabs=tabs,
        active_tab_id=active,
        scroll=ScrollState(y=metrics["scrollY"], max_y=metrics["maxScrollY"]),
        elements=elements,
        summary_text=summary,
        dom_hash=compute_dom_hash(metrics["url"], elements),
    )

    logger.debug(
        "perceived",
        elements=len(elements),
        summary_chars=len(summary),
        dom_hash=state.dom_hash,
    )
    return state
