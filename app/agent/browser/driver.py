"""Playwright driver (spec section 4.1, sandboxed per section 4.13).

Everything that touches a real browser lives here. Two rules hold the layer
together:

* **No Playwright exception escapes.** Every failure comes back either as an
  unsuccessful :class:`ActionResult` carrying one of our error codes, or as a
  :class:`~app.agent.browser.errors.BrowserError` for a request that was not
  allowed in the first place.
* **No action runs unless it is on the allow-list**, checked before the browser
  is involved at all.
"""

import asyncio
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self, runtime_checkable
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from pydantic import BaseModel

from app.agent.browser.actions import (
    ClickAction,
    CloseTabAction,
    DoneAction,
    ExtractAction,
    GoBackAction,
    NavigateAction,
    NewTabAction,
    ScrollAction,
    SelectAction,
    SwitchTabAction,
    TypeAction,
    WaitAction,
)
from app.agent.browser.errors import (
    ActionErrorCode,
    ActionNotAllowedError,
    DomainNotAllowedError,
)
from app.agent.browser.sessions import DEFAULT_MAX_TABS, TabLimitReached, TabSessionManager
from app.agent.perception.dom_extractor import locator_in_any_frame
from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.enums import ActionType

__all__ = [
    "ActionResult",
    "BrowserConfig",
    "BrowserDriver",
    "BrowserSnapshot",
    "PlaywrightDriver",
]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    """How a task's browser context is built.

    The locale and timezone are pinned rather than inherited from the host, so
    a run on a developer laptop sees the same dates and currency formatting as
    a run in CI. For an Indian flight-booking demo that is the difference
    between ``20/10`` and ``10/20``.
    """

    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 800
    locale: str = "en-IN"
    timezone: str = "Asia/Kolkata"
    action_timeout_ms: int = 10_000
    navigation_timeout_ms: int = 30_000
    allowed_domains: tuple[str, ...] = ()
    user_agent: str | None = None
    max_tabs: int = DEFAULT_MAX_TABS

    @classmethod
    def from_settings(cls, settings: Settings) -> "BrowserConfig":
        return cls(
            headless=settings.AGENT_HEADLESS,
            allowed_domains=tuple(settings.AGENT_ALLOWED_DOMAINS),
            action_timeout_ms=settings.BROWSER_ACTION_TIMEOUT_MS,
            navigation_timeout_ms=settings.BROWSER_NAVIGATION_TIMEOUT_MS,
            max_tabs=settings.AGENT_MAX_TABS,
        )


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What happened when one action ran.

    ``url_before`` and ``url_after`` are what the verifier compares for the
    ``url_contains`` outcome, so they are captured even when the action fails.
    """

    ok: bool
    duration_ms: int
    url_before: str
    url_after: str
    error_code: ActionErrorCode | None = None
    error_message: str | None = None
    extracted: str | None = None

    @property
    def url_changed(self) -> bool:
        return self.url_before != self.url_after


class BrowserSnapshot(BaseModel):
    """A cheap snapshot, taken before and after every action.

    Distinct from :class:`app.agent.perception.state.PageState`, which is the
    expensive one: that carries the element list and the summary. This is what
    the verifier compares either side of an action.

    The compact DOM summary from spec section 4.6 is a separate, more expensive
    thing. This is the part the verifier and the live view need on every step.
    """

    url: str
    title: str
    tab_ids: list[str]
    active_tab_id: str
    scroll_y: int
    viewport_width: int
    viewport_height: int


@runtime_checkable
class BrowserDriver(Protocol):
    """What the orchestrator depends on.

    A protocol rather than a base class so the loop can be tested against a
    fake with no browser at all, which is what makes the agent evaluation suite
    in spec section 11 possible without burning real page loads.
    """

    async def launch(self, config: BrowserConfig) -> None: ...

    async def execute(self, action: BaseModel) -> ActionResult: ...

    async def capture_state(self) -> BrowserSnapshot: ...

    async def close(self) -> None: ...


def host_is_allowed(url: str, allowed: tuple[str, ...]) -> bool:
    """Is this URL inside the sandbox (spec section 4.13)?

    An entry without a port matches any port on that host, which is what makes
    ``localhost`` cover every local demo service. An entry *with* a port
    matches only that port. Comparison is on the parsed hostname, never on a
    substring of the URL: ``evil-localhost.test`` must not pass because the
    string contains ``localhost``.
    """
    if not allowed:
        return False

    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    if not hostname:
        return False

    port = parts.port
    candidates = {hostname}
    if port is not None:
        candidates.add(f"{hostname}:{port}")

    return any(entry.strip().lower() in candidates for entry in allowed)


class PlaywrightDriver:
    """One isolated browser context per task.

    A fresh context every time means no cookies, storage or cache carry between
    tasks, so one user's session can never leak into another's run. Downloads
    are refused outright: an agent has no business writing to the host disk.

    The ``Playwright`` instance and ``Browser`` are expensive, so they can be
    supplied by the caller and shared across tasks. Only the context is
    per-task.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        browser: Browser | None = None,
    ) -> None:
        self._settings = settings
        self._config = BrowserConfig.from_settings(settings)
        self._shared_browser = browser
        self._playwright: Playwright | None = None
        self._owns_browser = browser is None
        self._browser: Browser | None = browser
        self._context: BrowserContext | None = None
        self._sessions = TabSessionManager(max_tabs=self._config.max_tabs)

    # ------------------------------------------------------------ lifecycle

    async def launch(self, config: BrowserConfig | None = None) -> None:
        if config is not None:
            self._config = config

        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self._config.headless)

        self._context = await self._browser.new_context(
            viewport={
                "width": self._config.viewport_width,
                "height": self._config.viewport_height,
            },
            locale=self._config.locale,
            timezone_id=self._config.timezone,
            # Spec section 4.13: the agent never writes to the host disk.
            accept_downloads=False,
            user_agent=self._config.user_agent,
        )
        self._context.set_default_timeout(self._config.action_timeout_ms)
        self._context.set_default_navigation_timeout(self._config.navigation_timeout_ms)

        # A popup opened by the page becomes a tracked tab, so the agent can
        # switch to it instead of losing it. A fresh manager per launch, so a
        # relaunched driver does not carry the last context's tab ids.
        self._sessions = TabSessionManager(max_tabs=self._config.max_tabs)
        self._sessions.attach(self._context)
        await self._sessions.open(self._context)
        logger.info("browser_context_launched", headless=self._config.headless)

    async def close(self) -> None:
        """Tear down. Safe to call twice, and never raises."""
        closers = (
            # Tabs first, while the context is still alive. A popup the page
            # opened and nobody closed outlives the task otherwise.
            self._sessions.close_all,
            self._close_context,
            self._close_browser,
            self._stop_playwright,
        )
        for closer in closers:
            try:
                await closer()
            except Exception as exc:  # pragma: no cover - teardown must not mask errors
                logger.warning("browser_teardown_failed", error_type=type(exc).__name__)

    async def _close_context(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None

    async def _close_browser(self) -> None:
        if self._browser is not None and self._owns_browser:
            await self._browser.close()
            self._browser = None

    async def _stop_playwright(self) -> None:
        if self._playwright is not None and self._owns_browser:
            await self._playwright.stop()
            self._playwright = None

    async def __aenter__(self) -> Self:
        await self.launch(self._config)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    # ------------------------------------------------------------ tabs

    @property
    def sessions(self) -> TabSessionManager:
        """The tab registry for this task.

        Exposed because perception needs the tab list and the orchestrator
        needs the active tab id, and both should read them from the one place
        that owns them rather than from a second copy kept here.
        """
        return self._sessions

    @property
    def tab_ids(self) -> list[str]:
        return self._sessions.tab_ids

    @property
    def active_tab_id(self) -> str:
        return self._sessions.active_tab_id

    @property
    def tab_labels(self) -> dict[str, str]:
        return self._sessions.tab_labels

    @property
    def page(self) -> Page:
        try:
            return self._sessions.page
        except PlaywrightError as exc:
            raise RuntimeError("The driver has not been launched.") from exc

    # ------------------------------------------------------------ state

    async def capture_state(self) -> BrowserSnapshot:
        page = self.page
        try:
            title = await page.title()
            scroll_y = int(await page.evaluate("() => Math.round(window.scrollY)"))
        except PlaywrightError:
            # A page mid-navigation can refuse both. A snapshot is never worth
            # failing a step over.
            title, scroll_y = "", 0

        viewport = page.viewport_size or {
            "width": self._config.viewport_width,
            "height": self._config.viewport_height,
        }
        return BrowserSnapshot(
            url=page.url,
            title=title,
            tab_ids=self._sessions.tab_ids,
            active_tab_id=self._sessions.active_tab_id,
            scroll_y=scroll_y,
            viewport_width=viewport["width"],
            viewport_height=viewport["height"],
        )

    # ------------------------------------------------------------ execute

    async def execute(self, action: BaseModel) -> ActionResult:
        """Run one action.

        The allow-list is checked first, before the browser is touched at all.
        Everything after that is wrapped: a Playwright failure becomes an
        unsuccessful result with one of our codes, never an exception the
        orchestrator has to know how to catch.
        """
        name = getattr(action, "action", None)
        if not isinstance(name, ActionType):
            raise ActionNotAllowedError(f"{name!r} is not in the action allow-list.")

        url_before = self._current_url()
        started = time.perf_counter()

        try:
            extracted = await self._dispatch(action)
        except DomainNotAllowedError:
            # A sandbox breach is not a page problem, so it is raised rather
            # than reported: the orchestrator must not retry it.
            raise
        except TabLimitReached as exc:
            # Recoverable by closing a tab, so this comes back as a failed
            # action the ladder can work with rather than as an exception.
            code = ActionErrorCode.TAB_LIMIT_REACHED
            return self._failure(action, started, url_before, code, exc)
        except PlaywrightTimeoutError as exc:
            return self._failure(action, started, url_before, ActionErrorCode.TIMEOUT, exc)
        except PlaywrightError as exc:
            return self._failure(action, started, url_before, self._classify(exc), exc)

        return ActionResult(
            ok=True,
            duration_ms=self._elapsed_ms(started),
            url_before=url_before,
            url_after=self._current_url(),
            extracted=extracted,
        )

    async def _dispatch(self, action: BaseModel) -> str | None:
        match action:
            case NavigateAction():
                await self._navigate(action)
            case ClickAction():
                await (await self._locate(action.selector)).click()
            case TypeAction():
                await self._type(action)
            case ScrollAction():
                await self._scroll(action)
            case SelectAction():
                await (await self._locate(action.selector)).select_option(action.value)
            case ExtractAction():
                return await self._extract(action)
            case WaitAction():
                await asyncio.sleep(action.seconds)
            case GoBackAction():
                await self.page.go_back()
            case NewTabAction():
                await self._new_tab(action)
            case SwitchTabAction():
                self._switch_tab(action.tab_id)
            case CloseTabAction():
                await self._close_tab(action)
            case DoneAction():
                # Terminal marker. The orchestrator stops on it; there is
                # nothing for the browser to do.
                return action.final_answer
        return None

    async def _navigate(self, action: NavigateAction) -> None:
        self._require_allowed(action.url)
        response = await self.page.goto(action.url, wait_until="domcontentloaded")
        # A redirect can land somewhere outside the sandbox, so the destination
        # is re-checked rather than trusted from the request.
        self._require_allowed(self.page.url)
        if response is not None and response.status >= 400:
            raise PlaywrightError(f"Navigation returned HTTP {response.status}")

    async def _type(self, action: TypeAction) -> None:
        locator = await self._locate(action.selector)
        if action.clear_first:
            await locator.fill("")
        await locator.fill(action.value)
        if action.press_enter:
            await locator.press("Enter")

    async def _scroll(self, action: ScrollAction) -> None:
        """Scroll the window.

        ``mouse.wheel`` needs the pointer over the scrollable area and silently
        does nothing otherwise, which made this a no-op headless.
        ``window.scrollBy`` is deterministic and still fires scroll events, so
        infinite-scroll pages behave the same.
        """
        delta = action.pixels if action.direction == "down" else -action.pixels
        await self.page.evaluate(
            "(dy) => window.scrollBy({ top: dy, left: 0, behavior: 'instant' })", delta
        )

    async def _extract(self, action: ExtractAction) -> str:
        if action.selector:
            return await (await self._locate(action.selector)).inner_text()
        return await self._all_text()

    async def _locate(self, selector: str) -> Locator:
        """Resolve a selector, looking in every frame.

        Falls back to the main frame when nothing matches anywhere, so a
        selector that is simply wrong still fails through Playwright with its
        own message and timeout rather than through a different error here.
        """
        found = await locator_in_any_frame(self.page, selector)
        return found if found is not None else self.page.locator(selector).first

    async def _all_text(self) -> str:
        """Visible text from the page and every frame in it.

        A page that renders its content inside an iframe, which is one of the
        spec section 12 chaos cases, has almost nothing in the outer body. An
        extract that read only that would come back with the chrome around the
        results and none of the results.
        """
        parts: list[str] = []
        for frame in self.page.frames:
            try:
                text = await frame.inner_text("body")
            except PlaywrightError:
                continue
            if text and text.strip():
                parts.append(text)
        return "\n".join(parts)

    async def _new_tab(self, action: NewTabAction) -> None:
        if action.url is not None:
            self._require_allowed(action.url)
        if self._context is None:
            raise RuntimeError("The driver has not been launched.")

        await self._sessions.open(self._context, action.url)

        if action.url is not None:
            # A redirect can land outside the sandbox, so the page we ended up
            # on is checked rather than the URL we asked for.
            self._require_allowed(self.page.url)

    def _switch_tab(self, tab_id: str) -> None:
        self._sessions.switch(tab_id)

    async def _close_tab(self, action: CloseTabAction) -> None:
        await self._sessions.close(action.tab_id)

    # ------------------------------------------------------------ helpers

    def _require_allowed(self, url: str) -> None:
        if not host_is_allowed(url, self._config.allowed_domains):
            raise DomainNotAllowedError(f"{url} is outside the allowed domains.")

    def _current_url(self) -> str:
        try:
            return self.page.url
        except (RuntimeError, PlaywrightError):
            return ""

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    @staticmethod
    def _classify(exc: PlaywrightError) -> ActionErrorCode:
        """Map a Playwright failure onto our vocabulary.

        Playwright reports everything as one exception type with a message, so
        this reads the message. Crude, but the alternative is leaking the
        library's error model into every layer above.
        """
        message = str(exc).lower()
        if "timeout" in message:
            return ActionErrorCode.TIMEOUT
        if "no such tab" in message:
            return ActionErrorCode.TAB_NOT_FOUND
        if any(
            phrase in message
            for phrase in ("no element", "not found", "resolve to", "strict mode violation")
        ):
            return ActionErrorCode.ELEMENT_NOT_FOUND
        if any(
            phrase in message
            for phrase in (
                "not visible",
                "not enabled",
                "not editable",
                "intercepts pointer events",
                "element is not attached",
                "outside of the viewport",
            )
        ):
            return ActionErrorCode.NOT_INTERACTABLE
        if any(
            phrase in message
            for phrase in ("net::", "navigation", "err_", "http 4", "http 5", "frame was detached")
        ):
            return ActionErrorCode.NAVIGATION_FAILED
        return ActionErrorCode.UNKNOWN

    def _failure(
        self,
        action: BaseModel,
        started: float,
        url_before: str,
        code: ActionErrorCode,
        exc: Exception,
    ) -> ActionResult:
        logger.warning(
            "action_failed",
            action=str(getattr(action, "action", "unknown")),
            error_code=code.value,
            error_type=type(exc).__name__,
        )
        return ActionResult(
            ok=False,
            duration_ms=self._elapsed_ms(started),
            url_before=url_before,
            url_after=self._current_url(),
            error_code=code,
            # Playwright messages are long and include page snippets. One line
            # is enough for the activity log, and page content must not be
            # stored (spec section 10).
            error_message=str(exc).splitlines()[0][:200],
        )
