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
import contextlib
import json
import re
import time
from dataclasses import dataclass, replace
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
    BrowserClosedError,
    DomainNotAllowedError,
)
from app.agent.browser.scroller import METRICS_JS, scroll_by_js
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
    slow_mo_ms: int = 0
    fullscreen: bool = False
    channel: str = ""
    engine: str = "chromium"
    fallbacks: tuple[str, ...] = ()
    cdp_url: str = ""
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
            slow_mo_ms=settings.AGENT_SLOW_MO_MS,
            fullscreen=settings.AGENT_FULLSCREEN,
            channel=settings.AGENT_BROWSER_CHANNEL,
            fallbacks=tuple(settings.AGENT_BROWSER_FALLBACKS),
            cdp_url=settings.AGENT_CDP_URL,
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


INDICATOR_ID = "__baa_indicator__"
TYPE_DELAY_MS = 30
# A site that says no to the browser rather than to the page. Another browser may
# be let in.
BLOCKED_STATUSES = frozenset({401, 403, 429})
MIN_RENDERED_TEXT = 200
RENDER_WAIT_S = 6.0
# Titles of the pages that stand in for the real one when a site is checking
# whether it is talking to a person.
_CHALLENGE = re.compile(
    r"just a moment|attention required|access denied|forbidden|blocked|captcha|are you a robot",
    re.IGNORECASE,
)
NEW_TAB_WAIT_S = 3.0
NEW_TAB_PEEK_S = 0.4
NEW_TAB_LOAD_MS = 8_000
OBSTRUCTION_SETTLE_S = 0.4
# Things a person can type into. Checkboxes, radios and buttons are inputs too,
# but typing into them is never what anybody meant.
EDITABLE_SELECTOR = (
    "input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=button])"
    ":not([type=submit]):not([type=reset]):not([type=file]):not([type=image]), textarea, "
    "[contenteditable=''], [contenteditable='true'], [role=textbox], [role=searchbox], "
    "[role=combobox]"
)
GLIDE_STEPS = 22
SMOOTH_SCROLL_POLL_S = 0.1
SMOOTH_SCROLL_MAX_S = 3.0

_INDICATOR_JS = """
(function (mode) {
  if (window.top !== window) return;
  function apply() {
    var root = document.documentElement;
    if (!root) return;
    var waiting = mode === 'waiting';
    var color = waiting ? '#f59e0b' : '#1a73e8';
    var el = document.getElementById('__baa_indicator__');
    if (!el) {
      el = document.createElement('div');
      el.id = '__baa_indicator__';
      el.setAttribute('aria-hidden', 'true');
      el.appendChild(document.createElement('div'));
      root.appendChild(el);
    }
    el.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;' +
      'box-sizing:border-box;border:3px solid ' + color + ';' +
      'box-shadow:inset 0 0 22px 2px ' + color + '66;';
    var label = el.firstChild;
    label.textContent = waiting ? 'Waiting for you to sign in' : 'Agent is working';
    label.style.cssText = 'position:absolute;top:0;left:50%;transform:translateX(-50%);' +
      'padding:3px 14px;border-radius:0 0 10px 10px;font:600 12px/1.4 system-ui,sans-serif;' +
      'color:#fff;background:' + color + ';';
  }
  function cursor() {
    var root = document.documentElement;
    if (!root || document.getElementById('__baa_cursor__')) return;
    var c = document.createElement('div');
    c.id = '__baa_cursor__';
    c.setAttribute('aria-hidden', 'true');
    c.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;' +
      'z-index:2147483647;display:none;';
    c.innerHTML =
      '<svg width="26" height="26" viewBox="0 0 24 24" style="position:absolute;' +
      'left:-2px;top:-2px;filter:drop-shadow(0 1px 2px rgba(0,0,0,.45))">' +
      '<path d="M3 2l7.5 19 2.6-7.9L21 10.5z" fill="#1a73e8" stroke="#fff" ' +
      'stroke-width="1.6" stroke-linejoin="round"/></svg>' +
      '<span style="position:absolute;left:-14px;top:-14px;width:28px;height:28px;' +
      'border-radius:50%;border:3px solid #1a73e8;opacity:0;"></span>';
    root.appendChild(c);
    var ring = c.lastChild;
    function move(e) {
      c.style.display = 'block';
      c.style.transform = 'translate(' + e.clientX + 'px,' + e.clientY + 'px)';
    }
    addEventListener('mousemove', move, true);
    addEventListener('mousedown', function (e) {
      move(e);
      ring.style.transition = 'none';
      ring.style.opacity = '0.9';
      ring.style.transform = 'scale(0.4)';
      requestAnimationFrame(function () {
        ring.style.transition = 'opacity .5s, transform .5s';
        ring.style.opacity = '0';
        ring.style.transform = 'scale(1.8)';
      });
    }, true);
  }
  apply();
  cursor();
  document.addEventListener('DOMContentLoaded', function () { apply(); cursor(); });
})(__MODE__);
"""


def indicator_script(mode: str) -> str:
    """The script that draws the coloured frame around a tab the agent is using.

    ``working`` is blue and ``waiting`` is amber. It is pointer-events: none, so
    it never intercepts a click, and it has no role or label the perception
    layer would list as something to act on.
    """
    return _INDICATOR_JS.replace("__MODE__", json.dumps(mode))


def launch_args(fullscreen: bool, headless: bool) -> list[str]:
    """Chromium flags for the launch: open maximised when a full screen was asked for."""
    return ["--start-maximized"] if fullscreen and not headless else []


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
        self._owns_context = True
        self._indicator_mode = "working"
        self._indicated: set[int] = set()
        self._sessions = TabSessionManager(max_tabs=self._config.max_tabs)
        self._fallbacks_left: list[str] | None = None

    # ------------------------------------------------------------ lifecycle

    async def launch(self, config: BrowserConfig | None = None) -> None:
        if config is not None:
            self._config = config

        if self._browser is None:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._connect_or_launch(self._playwright)

        if self.attached:
            # The user's own browser: reuse its default context so their
            # sign-ins are visible, and never close it. Viewport, locale and
            # download settings belong to the user's browser, not to us.
            contexts = self._browser.contexts
            self._owns_context = not contexts
            self._context = contexts[0] if contexts else await self._browser.new_context()
        else:
            self._owns_context = True
            self._context = await self._browser.new_context(
                # A maximised window has no fixed size: let the page fill it.
                no_viewport=self.fills_window,
                viewport=None
                if self.fills_window
                else {
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
        await self._focus()
        self._indicated.clear()
        await self._sync_indicator()
        logger.info(
            "browser_context_launched", headless=self._config.headless, attached=self.attached
        )

    @property
    def fills_window(self) -> bool:
        """Does the page fill a maximised window rather than a fixed-size box?"""
        return self._config.fullscreen and not self._config.headless and not self.attached

    async def viewport_size(self) -> dict[str, int]:
        """The size of the page area right now, measured rather than assumed.

        A maximised window has no configured size and Playwright reports none,
        so it is read from the page. Everything that maps a picture back onto
        the page (screenshots, a click at a coordinate) needs the real one.
        """
        page = self.page
        fixed = page.viewport_size
        if fixed:
            return {"width": fixed["width"], "height": fixed["height"]}
        try:
            size = await page.evaluate("() => [window.innerWidth, window.innerHeight]")
            return {"width": int(size[0]), "height": int(size[1])}
        except PlaywrightError:
            return {"width": self._config.viewport_width, "height": self._config.viewport_height}

    @property
    def attached(self) -> bool:
        """Is this driving the user's own browser over CDP (opt-in, see ADR)?"""
        return bool(self._config.cdp_url)

    async def _connect_or_launch(self, playwright: Playwright) -> Browser:
        if self.attached:
            return await playwright.chromium.connect_over_cdp(
                self._config.cdp_url, slow_mo=self._config.slow_mo_ms
            )
        if self._config.engine == "firefox":
            return await playwright.firefox.launch(
                headless=self._config.headless, slow_mo=self._config.slow_mo_ms
            )
        return await playwright.chromium.launch(
            headless=self._config.headless,
            slow_mo=self._config.slow_mo_ms,
            channel=self._config.channel or None,
            args=launch_args(self._config.fullscreen, self._config.headless),
        )

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
            # Never close the user's own context: that would shut their windows.
            if self._owns_context:
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
            if self._context is None:
                raise RuntimeError("The driver has not been launched.") from exc
            raise BrowserClosedError("The browser window or tab was closed.") from exc

    # ------------------------------------------------------------ state

    async def capture_state(self) -> BrowserSnapshot:
        page = self.page
        try:
            title = await page.title()
            scroll_y = int((await page.evaluate(METRICS_JS))[0])
        except PlaywrightError:
            # A page mid-navigation can refuse both. A snapshot is never worth
            # failing a step over.
            title, scroll_y = "", 0

        viewport = await self.viewport_size()
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
        """Run one action, then make sure every tab it touched shows the indicator."""
        result = await self._run_action(action)
        await self._sync_indicator()
        return result

    async def _run_action(self, action: BaseModel) -> ActionResult:
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
                locator = await self._locate(action.selector)
                await self._clear_obstruction(locator)
                await self._glide_to(locator)
                tabs_before = set(self._sessions.tab_ids)
                opens_tab = await self._opens_new_tab(locator)
                await locator.click()
                await self._follow_new_tab(tabs_before, opens_tab)
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
                await self._switch_tab(action.tab_id)
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
        # A site that turns this browser away may let another one in. Each is a
        # separate browser of its own; the person's Chrome is never touched.
        rendered: bool | None = None
        while (
            response is not None
            and response.status in BLOCKED_STATUSES
            and not self.attached
            and not (rendered := await self._rendered_a_page())
            and (name := self._next_fallback())
        ):
            rendered = None
            if not await self._switch_browser(name):
                continue
            response = await self.page.goto(action.url, wait_until="domcontentloaded")
        # A redirect can land somewhere outside the sandbox, so the destination
        # is re-checked rather than trusted from the request.
        self._require_allowed(self.page.url)
        if response is not None and response.status >= 400:
            if response.status in BLOCKED_STATUSES and (
                rendered if rendered is not None else await self._rendered_a_page()
            ):
                # Plenty of sites answer a signed-out visitor with 401 or 403 and
                # still draw the page (a sign-in prompt, a preview). The person
                # is better served by the agent looking at it, and handing over
                # to them if it asks for a sign-in, than by a task that dies on
                # a status code.
                logger.info("navigation_status_ignored", status=response.status)
                return
            raise PlaywrightError(f"Navigation returned HTTP {response.status}")

    async def _rendered_a_page(self) -> bool:
        """Is this a real page, not a bare error or a bot-check screen?

        An app that draws itself after the load event is given a few seconds to
        do it before it is called empty.
        """
        for _ in range(int(RENDER_WAIT_S / 0.5)):
            try:
                title = (await self.page.title()).strip()
                length = await self.page.evaluate(
                    "() => (document.body?.innerText || '').trim().length"
                )
            except PlaywrightError:
                return False
            if _CHALLENGE.search(title):
                return False
            if title and int(length) >= MIN_RENDERED_TEXT:
                return True
            await asyncio.sleep(0.5)
        return False

    def _next_fallback(self) -> str | None:
        """The next browser to try, or None when they have all had their turn."""
        if self._fallbacks_left is None:
            tried = {self._config.channel or "chromium"}
            self._fallbacks_left = [n for n in self._config.fallbacks if n not in tried]
        return self._fallbacks_left.pop(0) if self._fallbacks_left else None

    async def _switch_browser(self, name: str) -> bool:
        """Carry on in a different browser. False when it is not installed.

        The old window is closed first, so two do not sit on the screen. If the
        new one will not start, the old configuration is brought back rather
        than leaving the task with no browser at all.
        """
        original = self._config
        config = replace(
            original,
            channel="" if name == "firefox" else name,
            engine="firefox" if name == "firefox" else "chromium",
        )
        await self._sessions.close_all()
        await self._close_context()
        await self._close_browser()
        if not self._owns_browser:
            # The shared browser belongs to the runner. Leave it, and run on one
            # of this driver's own from here.
            self._browser = None
            self._owns_browser = True
        try:
            await self.launch(config)
        except PlaywrightError as exc:
            logger.warning("fallback_browser_unavailable", browser=name, error=type(exc).__name__)
            self._browser = None
            await self.launch(original)
            return False
        logger.info("switched_browser", browser=name)
        return True

    async def _type(self, action: TypeAction) -> None:
        """Put text into whatever the selector found, however the page built it.

        A plain field takes ``fill``. A custom widget (a date picker, an
        autocomplete, a masked input) may ignore it, or the selector may land
        on the wrapper around the real field, so each of those is handled
        rather than failing the step.
        """
        locator = await self._locate(action.selector)
        await self._clear_obstruction(locator)
        await self._glide_to(locator)
        field = await self._editable_in(locator)

        if "\n" in action.value:
            await self._insert_block(locator, action)
            return
        if field is None:
            # Nothing editable to fill: focus it and type at the keyboard, which
            # is what a custom widget listens for.
            await locator.click()
            if action.clear_first:
                await self.page.keyboard.press("Control+A")
                await self.page.keyboard.press("Backspace")
            await self.page.keyboard.type(action.value, delay=TYPE_DELAY_MS)
            if action.press_enter:
                await self.page.keyboard.press("Enter")
            return

        if action.clear_first:
            await field.fill("")
        await field.fill(action.value)
        if not await self._holds(field, action.value):
            # The widget threw the value away, which is what a mask or an
            # autocomplete that watches key events does to a programmatic fill.
            # Type it the way a person would.
            await field.click()
            await field.fill("")
            await field.press_sequentially(action.value, delay=TYPE_DELAY_MS)
        if action.press_enter:
            await field.press("Enter")

    async def _editable_in(self, locator: Locator) -> Locator | None:
        """The locator itself if it is a field, else a field inside it, else None."""
    async def _insert_block(self, locator: Locator, action: TypeAction) -> None:
        """Put a multi-line text (a program) into an editor in one piece.

        Typing code key by key goes wrong in every code editor: Enter
        auto-indents, an opening bracket brings its closing one with it, and the
        result is a program that no longer parses. Inserting the text at once
        sends it to the editor as a paste would, with none of that. The editor's
        contents are replaced, because a half-edited program is never the goal.
        """
        await locator.click()
        if action.clear_first:
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Delete")
        await self.page.keyboard.insert_text(action.value)
        if action.press_enter:
            await self.page.keyboard.press("Enter")

        if await locator.evaluate("(el, sel) => el.matches(sel)", EDITABLE_SELECTOR):
            return locator
        inner = locator.locator(EDITABLE_SELECTOR).first
        return inner if await inner.count() else None

    @staticmethod
    async def _holds(field: Locator, wanted: str) -> bool:
        """Does the field still hold what was put in it, give or take formatting?"""
        try:
            held = await field.input_value()
        except PlaywrightError:
            return True  # not a form control (contenteditable): nothing to compare
        squash = re.compile(r"[^a-z0-9]+")
        have, want = squash.sub("", held.casefold()), squash.sub("", wanted.casefold())
        return bool(want) and bool(have) and (want in have or have in want)

    @staticmethod
    async def _opens_new_tab(locator: Locator) -> bool:
        """Is the target a link that asks for a new tab?"""
        try:
            return bool(
                await locator.evaluate(
                    "(el) => { const a = el.closest('a'); return !!(a && a.target === '_blank'); }"
                )
            )
        except PlaywrightError:
            return False

    async def _follow_new_tab(self, before: set[str], expected: bool) -> None:
        """Move to a tab the click opened, as a person looking at the screen would.

        Listings, "open in new tab" links and sign-in popups all land in a tab
        of their own. The click succeeded, but the agent kept looking at the
        page it started on, saw nothing change, and called a working step
        failed. A link that asked for a new tab is waited for; anything else
        only gets a short look, for a script that opens one itself.
        """
        limit = NEW_TAB_WAIT_S if expected else NEW_TAB_PEEK_S
        waited = 0.0
        opened: list[str] = []
        while waited <= limit:
            opened = [tab for tab in self._sessions.tab_ids if tab not in before]
            if opened:
                break
            await asyncio.sleep(0.1)
            waited += 0.1
        if not opened:
            return

        newest = max(opened, key=lambda tab: int(tab.removeprefix("t")))
        previous = self._sessions.active_tab_id
        self._sessions.switch(newest)
        page = self.page
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("domcontentloaded", timeout=NEW_TAB_LOAD_MS)
        if page.url not in {"", "about:blank"}:
            try:
                self._require_allowed(page.url)
            except DomainNotAllowedError:
                # A page outside the sandbox does not get to be the one the agent
                # works on. Close it, go back, and report the breach.
                await self._sessions.close(newest)
                if previous in self._sessions.tab_ids:
                    self._sessions.switch(previous)
                raise
        await self._focus()
        logger.info("followed_new_tab", tab_id=newest)

    async def _clear_obstruction(self, locator: Locator) -> None:
        """Close whatever transient widget is sitting over the target, if anything.

        A date picker stays open after the date is typed, an autocomplete list
        stays open after a station is chosen, a dropdown stays open until
        something else is clicked, and each of them covers the next control the
        agent wants. Playwright then waits ten seconds for a click that can
        never land. Escape closes a well-built widget and does nothing to a page
        without one, so it is tried once, and only when the target really is
        covered.
        """
        try:
            await locator.scroll_into_view_if_needed(timeout=2_000)
            if not await self._is_covered(locator):
                return
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(OBSTRUCTION_SETTLE_S)
            if await self._is_covered(locator):
                logger.info("target_still_covered")
        except PlaywrightError as exc:
            logger.debug("obstruction_check_failed", error_type=type(exc).__name__)

    @staticmethod
    async def _is_covered(locator: Locator) -> bool:
        """Is something other than the target (or part of it) at the target's centre?"""
        return bool(
            await locator.evaluate(
                """(el) => {
                  const r = el.getBoundingClientRect();
                  if (r.width === 0 || r.height === 0) return false;
                  const x = r.left + r.width / 2;
                  const y = r.top + r.height / 2;
                  if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return false;
                  const top = document.elementFromPoint(x, y);
                  return !(top && (el === top || el.contains(top) || top.contains(el)));
                }"""
            )
        )

    async def _glide_to(self, locator: Locator) -> None:
        """Bring the target into view and move the pointer to it in a visible arc.

        Only for a visible browser: nobody watches a headless one, and the extra
        mouse events would only cost time. Best effort. The action that follows
        does its own actionability checks, so a failure here changes nothing.
        """
        if not self._shows_indicator:
            return
        try:
            await locator.scroll_into_view_if_needed(timeout=2_000)
            box = await locator.bounding_box()
            if box is None:
                return
            await self.page.mouse.move(
                box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=GLIDE_STEPS
            )
        except PlaywrightError as exc:
            logger.debug("glide_failed", error_type=type(exc).__name__)

    async def _scroll(self, action: ScrollAction) -> None:
        """Scroll the window.

        ``mouse.wheel`` needs the pointer over the scrollable area and silently
        does nothing otherwise, which made this a no-op headless.
        ``window.scrollBy`` is deterministic and still fires scroll events, so
        infinite-scroll pages behave the same.
        """
        delta = action.pixels if action.direction == "down" else -action.pixels
        smooth = self._shows_indicator
        start, room = (0, 0)
        if smooth:
            start, room = (int(v) for v in await self.page.evaluate(METRICS_JS))
        # Nowhere to go (already at the end in that direction): nothing to wait for.
        can_move = (start < room - 1) if delta > 0 else (start > 0)
        # Whatever actually scrolls: the window, or the panel an app shell
        # scrolls instead of it.
        await self.page.evaluate(scroll_by_js("smooth" if smooth else "instant"), delta)
        if smooth and can_move:
            await self._wait_for_scroll(start)

    async def _wait_for_scroll(self, start: int) -> None:
        """Wait until a smooth scroll has stopped moving.

        It can start late (a headless browser throttles the animation) and the
        check that follows reads the position straight away, so a fixed sleep is
        either too short or wasteful. Gives up after SMOOTH_SCROLL_MAX_S, which
        is what happens when there was nowhere to scroll to.
        """
        last = start
        still = 0
        for _ in range(int(SMOOTH_SCROLL_MAX_S / SMOOTH_SCROLL_POLL_S)):
            await asyncio.sleep(SMOOTH_SCROLL_POLL_S)
            try:
                now = int((await self.page.evaluate(METRICS_JS))[0])
            except PlaywrightError:
                return
            if now != last:
                last, still = now, 0
            elif last != start:
                still += 1
                if still >= 2:
                    return

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
        await self._focus()

        if action.url is not None:
            # A redirect can land outside the sandbox, so the page we ended up
            # on is checked rather than the URL we asked for.
            self._require_allowed(self.page.url)

    async def _switch_tab(self, tab_id: str) -> None:
        self._sessions.switch(tab_id)
        await self._focus()

    @property
    def _shows_indicator(self) -> bool:
        return not self._config.headless or self.attached

    async def set_indicator(self, mode: str) -> None:
        """Switch the frame on every tab between ``working`` (blue) and ``waiting``."""
        self._indicator_mode = mode
        self._indicated.clear()
        await self._sync_indicator()

    async def _sync_indicator(self) -> None:
        """Draw the frame on any tab that does not have it yet.

        Registered as an init script so it survives navigation, and run once
        now so it appears on the page that is already open. Best effort: a tab
        that closed mid-way, or a page that forbids it, is not an action failure.
        """
        if not self._shows_indicator:
            return
        script = indicator_script(self._indicator_mode)
        for tab in self._sessions.open_tabs:
            key = id(tab.page)
            if key in self._indicated:
                continue
            self._indicated.add(key)
            try:
                await tab.page.add_init_script(script)
                await tab.page.evaluate(script)
            except PlaywrightError as exc:
                logger.debug("indicator_failed", error_type=type(exc).__name__)

    async def hide_indicator(self, hidden: bool) -> None:
        """Hide the frame while a screenshot is taken, so it never reaches the model."""
        if not self._shows_indicator or self._sessions.active is None:
            return
        display = "none" if hidden else "block"
        try:
            await self.page.evaluate(
                "d => { const e = document.getElementById('__baa_indicator__');"
                " if (e) e.style.display = d; }",
                display,
            )
        except PlaywrightError:
            return

    async def _focus(self) -> None:
        """Raise the active tab in front of the user.

        Only meaningful with a visible browser, so it is skipped headless. A
        failure is logged and swallowed: losing focus must never fail an action.
        """
        if (self._config.headless and not self.attached) or self._sessions.active is None:
            return
        try:
            await self.page.bring_to_front()
        except PlaywrightError as exc:
            logger.debug("bring_to_front_failed", error_type=type(exc).__name__)

    async def _close_tab(self, action: CloseTabAction) -> None:
        await self._sessions.close(action.tab_id)
        await self._focus()

    # ------------------------------------------------------------ helpers

    def _require_allowed(self, url: str) -> None:
        if not host_is_allowed(url, self._config.allowed_domains):
            raise DomainNotAllowedError(f"{url} is outside the allowed domains.")

    def _current_url(self) -> str:
        try:
            return self.page.url
        except (RuntimeError, PlaywrightError, BrowserClosedError):
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
                "not an <input>",
                "not a form control",
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
