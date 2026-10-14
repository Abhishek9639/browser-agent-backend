"""One perception pass: look, summarise, hash (spec section 4.6)."""

from typing import Protocol

from app.agent.browser.scroller import METRICS_JS
from app.agent.perception.dom_extractor import extract
from app.agent.perception.state import PageState, ScrollState, compute_dom_hash
from app.agent.perception.summarizer import SUMMARY_TOKEN_BUDGET, build_header, build_summary
from app.core.logging import get_logger

__all__ = ["TabSource", "perceive"]

logger = get_logger(__name__)

# Page metrics in one round trip. Doing these as separate evaluate calls would
# be three crossings of the process boundary per step.
_METRICS = (
    "() => { const m = ("
    + METRICS_JS
    + ")(); return {"
    + " url: location.href,"
    + " title: document.title,"
    + " scrollY: m[0],"
    + ' text: ((document.body && document.body.innerText) || "").slice(0, 4000),'
    + " maxScrollY: m[1] }; }"
)


class TabSource(Protocol):
    """What perception needs to know about tabs.

    A protocol rather than the driver itself, so the loop can be exercised
    against a fake without a browser.
    """

    @property
    def tab_ids(self) -> list[str]: ...

    @property
    def active_tab_id(self) -> str: ...

    @property
    def tab_labels(self) -> dict[str, str]: ...


def _frame_urls(page: object) -> list[str]:
    """Every frame's URL, main frame first.

    Guarded because the loop is also driven against fakes that have no frames
    at all, and perception must not become browser-only to report this.
    """
    frames = getattr(page, "frames", None)
    if not frames:
        return []
    return [str(frame.url) for frame in frames if getattr(frame, "url", None)]


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
    labels = dict(session.tab_labels) if session else {}
    if active:
        # The manager only learns a tab's title when that tab is perceived,
        # so its label for the active tab is one pass behind. This pass has
        # the current title in hand, so use it.
        labels[active] = metrics["title"] or metrics["url"]

    frame_urls = _frame_urls(page)
    header = build_header(
        url=metrics["url"],
        frame_urls=frame_urls,
        title=metrics["title"],
        tabs=tabs,
        tab_labels=labels,
        active_tab_id=active,
        scroll_y=metrics["scrollY"],
        scroll_max_y=metrics["maxScrollY"],
    )
    summary = build_summary(elements, header, token_budget=token_budget)

    state = PageState(
        url=metrics["url"],
        frame_urls=frame_urls,
        title=metrics["title"],
        tabs=tabs,
        tab_labels=labels,
        active_tab_id=active,
        scroll=ScrollState(y=metrics["scrollY"], max_y=metrics["maxScrollY"]),
        elements=elements,
        summary_text=summary,
        # Not sent to the model, which gets the compact summary. This is for
        # the section 4.7 text_present check, which has to see ordinary page
        # text and not only the interactive elements.
        text=metrics.get("text", ""),
        dom_hash=compute_dom_hash(metrics["url"], elements),
    )

    logger.debug(
        "perceived",
        elements=len(elements),
        summary_chars=len(summary),
        dom_hash=state.dom_hash,
    )
    return state
