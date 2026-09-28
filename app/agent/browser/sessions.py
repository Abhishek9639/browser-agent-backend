"""Multi-tab tracking for one task (spec sections 4.1 and 4.4).

A tab needs a name the planner can say back to us. Playwright identifies
pages by object identity, which is no use in a prompt, so every page a task
touches gets a stable id: ``t1``, ``t2``, and so on, handed out once and never
reused.

Two kinds of tab arrive here. The ones the agent asks for with ``new_tab``,
and the ones a page opens by itself: a ``target="_blank"`` link, a popup, a
payment window. Both are registered the same way, because to the planner they
are both somewhere else it can now look, and a tab nobody tracked is a tab the
agent cannot switch back from.

One manager per task, like the browser context it wraps. Nothing is shared
between tasks, which is what makes ``t1`` mean something inside one run.
"""

import asyncio
import contextlib
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, Final

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from app.agent.perception.state import PageState
from app.core.logging import get_logger

__all__ = [
    "DEFAULT_MAX_TABS",
    "Tab",
    "TabLimitReached",
    "TabSessionManager",
]

logger = get_logger(__name__)

# A task that wants a sixth tab has lost track of what it is doing, and a page
# that opens them in a loop would otherwise exhaust the context.
DEFAULT_MAX_TABS: Final = 5


class TabLimitReached(RuntimeError):
    """The agent asked for a tab and the task already has its limit open."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"This task already has {limit} tabs open.")
        self.limit = limit


@dataclass(slots=True)
class Tab:
    """One page, and what is known about it."""

    tab_id: str
    page: Page
    # The last state perceived here. Without it a tab the agent is not looking
    # at reads as its bare id in the prompt, and "switch to t2" becomes a
    # guess. With it, the header can say what t2 actually contains.
    last_state: PageState | None = None

    @property
    def label(self) -> str:
        """What to call this tab in a prompt."""
        if self.last_state is None:
            return ""
        return self.last_state.title.strip() or self.last_state.url

    @property
    def closed(self) -> bool:
        return self.page.is_closed()


# No ``slots``: Playwright caches its wrapper for an event handler as an
# attribute on the object the handler is bound to, and a slotted class has
# nowhere to put it. ``context.on("page", self._on_page)`` fails with an
# AttributeError from inside the library if this is slotted.
@dataclass
class TabSessionManager:
    """The tabs one task has open.

    Satisfies both the :class:`~app.agent.contracts.SessionManager` protocol
    the orchestrator depends on and the ``TabSource`` protocol perception uses,
    so the loop gets its tab ids from the one place that actually owns them.
    """

    max_tabs: int = DEFAULT_MAX_TABS
    _tabs: dict[str, Tab] = field(default_factory=dict)
    _active: str | None = None
    _counter: int = 0
    # Closes fired from the synchronous ``page`` handler. Held so the event
    # loop does not collect a task mid-flight.
    _chores: set[asyncio.Task[None]] = field(default_factory=set)

    # ------------------------------------------------------------- tracking

    def attach(self, context: BrowserContext) -> None:
        """Register every page this context opens from now on.

        Popups included, which is the whole point: a ``target="_blank"`` link
        that opens a tab nobody tracked leaves the agent looking at a page it
        cannot name and cannot leave.
        """
        context.on("page", self._on_page)

    def _on_page(self, page: Page) -> None:
        if self.is_tracked(page):
            return
        if len(self.open_tabs) >= self.max_tabs:
            # A page opening tabs faster than the agent closes them is a page
            # fighting back. Dropping the surplus keeps the limit honest; the
            # alternative is a context that grows without bound.
            logger.warning("tab_limit_refused_popup", limit=self.max_tabs)
            self._spawn(page.close())
            return
        self.track(page)

    def track(self, page: Page) -> str:
        """Give a page an id, or return the one it already has.

        Idempotent because ``context.new_page()`` fires the ``page`` event as
        well, so this is reached twice for every tab the agent opens itself.
        An earlier version counted both and reported tabs that did not exist.
        """
        existing = self._id_of(page)
        if existing is not None:
            return existing

        self._counter += 1
        tab_id = f"t{self._counter}"
        self._tabs[tab_id] = Tab(tab_id=tab_id, page=page)
        if self._active is None:
            self._active = tab_id

        logger.debug("tab_tracked", tab_id=tab_id, tabs=len(self._tabs))
        return tab_id

    def is_tracked(self, page: Page) -> bool:
        return self._id_of(page) is not None

    def _id_of(self, page: Page) -> str | None:
        return next((tab_id for tab_id, tab in self._tabs.items() if tab.page is page), None)

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        """Run a coroutine from a synchronous Playwright event handler."""
        try:
            task = asyncio.ensure_future(work)
        except RuntimeError:  # pragma: no cover - no running loop
            work.close()
            return
        self._chores.add(task)
        task.add_done_callback(self._chores.discard)

    # ---------------------------------------------------------------- state

    def remember(self, state: PageState) -> None:
        """Record what the active tab is showing.

        Called after a perception pass, which is the only moment the manager
        can learn a page's title without doing browser work of its own.
        """
        tab = self.active
        if tab is not None:
            tab.last_state = state

    def _prune(self) -> None:
        """Forget tabs the browser closed behind our back.

        A page can go away without the agent asking: a script calls
        ``window.close()``, or the site replaces a popup. Reporting it as open
        would give the planner a tab it cannot switch to.
        """
        for tab_id, tab in list(self._tabs.items()):
            if tab.closed:
                logger.debug("tab_closed_elsewhere", tab_id=tab_id)
                del self._tabs[tab_id]
                if self._active == tab_id:
                    self._active = self._most_recent()

    @property
    def open_tabs(self) -> list[Tab]:
        self._prune()
        return list(self._tabs.values())

    @property
    def tab_ids(self) -> list[str]:
        return [tab.tab_id for tab in self.open_tabs]

    @property
    def tab_labels(self) -> dict[str, str]:
        return {tab.tab_id: tab.label for tab in self.open_tabs if tab.label}

    @property
    def active_tab_id(self) -> str:
        self._prune()
        return self._active or ""

    @property
    def active(self) -> Tab | None:
        tab_id = self.active_tab_id
        return self._tabs.get(tab_id) if tab_id else None

    @property
    def page(self) -> Page:
        tab = self.active
        if tab is None:
            raise PlaywrightError("This task has no open tab.")
        return tab.page

    def tab_id(self, state: PageState) -> str:
        """The :class:`~app.agent.contracts.SessionManager` protocol method.

        The manager's own answer wins over the one baked into the state: ids
        here survive a reload, and the state is only ever a snapshot of what
        perception last saw.
        """
        return self.active_tab_id or state.active_tab_id

    # -------------------------------------------------------------- actions

    async def open(self, context: BrowserContext, url: str | None = None) -> str:
        """Open a tab and make it active."""
        if len(self.open_tabs) >= self.max_tabs:
            raise TabLimitReached(self.max_tabs)

        page = await context.new_page()
        tab_id = self.track(page)
        self._active = tab_id

        if url is not None:
            await page.goto(url, wait_until="domcontentloaded")

        logger.info("tab_opened", tab_id=tab_id, tabs=len(self._tabs))
        return tab_id

    def switch(self, tab_id: str) -> str:
        if tab_id not in {tab.tab_id for tab in self.open_tabs}:
            raise PlaywrightError(f"No such tab: {tab_id}")
        self._active = tab_id
        logger.info("tab_switched", tab_id=tab_id)
        return tab_id

    async def close(self, tab_id: str | None = None) -> str | None:
        """Close a tab and return whichever is active afterwards.

        Falls back to the most recently opened tab, not the oldest. An agent
        that opened a detail view from a results page wants the results page
        back, and that is the tab before this one rather than the one it
        started on three tabs ago.
        """
        target = tab_id or self.active_tab_id
        tab = self._tabs.get(target)
        if tab is None:
            raise PlaywrightError(f"No such tab: {target}")

        with contextlib.suppress(PlaywrightError):
            await tab.page.close()
        self._tabs.pop(target, None)

        if self._active == target:
            self._active = self._most_recent()

        logger.info("tab_closed", tab_id=target, active=self._active, tabs=len(self._tabs))
        return self._active

    async def close_all(self) -> int:
        """Shut every tab this task opened.

        Called when the task ends. A popup nobody closed keeps its page alive
        inside the context, and on a browser shared between runs that is a
        leak per run rather than per task.
        """
        # Let a refused popup finish closing rather than cancelling it. A
        # cancelled close leaves the page open, and that page is not in the
        # registry, so nothing below would catch it.
        if self._chores:
            await asyncio.gather(*list(self._chores), return_exceptions=True)

        closed = 0
        for tab in list(self._tabs.values()):
            with contextlib.suppress(PlaywrightError):
                if not tab.closed:
                    await tab.page.close()
                closed += 1

        self._tabs.clear()
        self._active = None
        self._chores.clear()
        return closed

    def _most_recent(self) -> str | None:
        """The highest numbered tab still open. Ids only ever go up."""
        if not self._tabs:
            return None
        return max(self._tabs, key=lambda tab_id: int(tab_id.removeprefix("t")))
