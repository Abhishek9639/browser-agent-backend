"""Waiting for a page to stop moving (spec section 4.7).

Verification compares before and after. If "after" is read while the page is
still assembling itself, a click that worked looks like a click that did
nothing, and the agent starts recovering from a success. That is the most
expensive kind of false negative: it burns steps, it burns tokens, and it ends
with a task failed for no reason.

Two signals, whichever settles first. Network idle catches a page waiting on a
fetch; a quiet DOM catches one rendering from data it already has. Both are
capped, because a page with a polling timer or an animation never goes quiet
and waiting forever is worse than reading slightly early.

A quiet page is still not always a finished one. A site that builds itself,
goes quiet, and THEN puts up a city picker or a consent prompt on a timer
settles before the popup exists, so the planner chooses a click on the page
underneath. ``await_late_overlays`` closes that gap for freshly loaded pages.

Nor is a popup that exists always one that can be read. BookMyShow's format
picker puts its dimmed veil up at once, holds the panel below the fold for most
of a second and then slides it in with a CSS animation, which changes nothing in
the DOM. Read in that window, the popup is open but every one of its choices is
off-screen, the page behind it is covered, and the planner is left with the
background text ("I'm interested", "Releasing on 17 Dec") to answer from.
``await_popup_in_place`` waits for the panel to come to rest first.
"""

import asyncio
from typing import Final, Literal, Protocol, cast

from app.core.logging import get_logger

__all__ = [
    "DEFAULT_LOAD_POPUP_GRACE_MS",
    "DEFAULT_POPUP_IN_PLACE_MS",
    "DEFAULT_QUIET_MS",
    "DEFAULT_TIMEOUT_MS",
    "PopupPlacement",
    "await_late_overlays",
    "await_popup_in_place",
    "settle",
]

logger = get_logger(__name__)

# Long enough to cover a render triggered by a response that has just landed,
# short enough to pay on every step without anyone noticing.
DEFAULT_QUIET_MS: Final = 300
DEFAULT_TIMEOUT_MS: Final = 3000

# How old a document has to be before "no popup" is believed. Location pickers
# and consent prompts go up on a timer a moment after load, once the page has
# already gone quiet.
DEFAULT_LOAD_POPUP_GRACE_MS: Final = 2000

# How long an open popup is given to come to rest before it is read anyway.
# BookMyShow's panel takes about 1.3 seconds from the click; this leaves room
# for a slower machine without letting one stuck panel hold up a step for long.
DEFAULT_POPUP_IN_PLACE_MS: Final = 2500

PopupPlacement = Literal["none", "ready", "waited", "timeout"]

# A popup is in place once at least one of its controls is on screen and none
# of them moved since the last look. Off-screen catches a panel still waiting
# to slide in; "moved" catches one sliding, growing or springing into place.
# Neither needs the DOM to change, which is what lets a CSS animation slip past
# the quiet-DOM wait. Positions rather than running animations are compared, so
# a spinner or a pulsing button inside the popup does not hold it open.
# The control selector is the one modal_scope.js uses.
_POPUP_IN_PLACE = """
(args) => new Promise((resolve) => {
  const findModal = (__MODAL_SCOPE__);
  const controls = 'button,a[href],input:not([type="hidden"]),select,textarea,' +
    '[role="button"],[role="option"],[role="radio"],[onclick]';
  const started = performance.now();
  let previous = null;
  let looks = 0;
  const onScreen = (r) => r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
    r.top < window.innerHeight && r.left < window.innerWidth;
  const tick = () => {
    let modal = null;
    try { modal = findModal(document); } catch (e) { modal = null; }
    if (!modal) { resolve('none'); return; }
    looks += 1;
    const rects = [...modal.querySelectorAll(controls)].slice(0, 8)
      .map((el) => el.getBoundingClientRect());
    const layout = rects
      .map((r) => [r.left, r.top, r.width, r.height].map(Math.round).join(','))
      .join(';');
    const placed = rects.length === 0 || rects.some(onScreen);
    if (placed && layout === previous) { resolve(looks > 2 ? 'waited' : 'ready'); return; }
    if (performance.now() - started >= args.timeoutMs) { resolve('timeout'); return; }
    previous = layout;
    setTimeout(tick, 40);
  };
  tick();
})
"""

# Polls the page's own clock, not ours: performance.now() is milliseconds since
# THIS document started loading, so the wait is measured from the load the
# popup is timed against, and a document that has been up a while costs one
# round trip. Once the shared modal detector finds something it waits one quiet
# period for the popup to finish drawing, then reports it.
_LATE_OVERLAY = """
(args) => new Promise((resolve) => {
  const findModal = (__MODAL_SCOPE__);
  const tick = () => {
    if (performance.now() >= args.graceMs) { resolve(false); return; }
    let found = null;
    try { found = findModal(document); } catch (e) { found = null; }
    if (found) { setTimeout(() => resolve(true), args.quietMs); return; }
    setTimeout(tick, 100);
  };
  tick();
})
"""

# Runs in the page. Resolves once nothing has mutated for `quiet` ms, and the
# observer is disconnected on the way out so a long run does not leave one
# attached per step.
_QUIET_DOM = """
(quiet) => new Promise((resolve) => {
  let timer = null;
  const observer = new MutationObserver(() => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(finish, quiet);
  });
  function finish() {
    observer.disconnect();
    resolve(true);
  }
  observer.observe(document.documentElement, {
    subtree: true,
    childList: true,
    attributes: true,
    characterData: true,
  });
  timer = setTimeout(finish, quiet);
})
"""


class _Settleable(Protocol):
    """The slice of a Playwright page this needs.

    ``wait_for_load_state`` takes a ``timeout`` because Playwright's does; this
    only mirrors its signature. The wait is really bounded by the
    ``asyncio.timeout`` around the caller.
    """

    async def evaluate(self, expression: str, arg: object = None) -> object: ...

    # Playwright's own signature, which this only mirrors. The outer
    # asyncio.timeout is what actually bounds the wait.
    async def wait_for_load_state(self, state: str, *, timeout: float) -> None: ...


async def settle(
    page: _Settleable,
    *,
    quiet_ms: int = DEFAULT_QUIET_MS,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> bool:
    """Wait until the page looks finished. Returns whether it settled in time.

    Never raises. A page that will not go quiet is a page to read anyway: the
    alternative is failing a step because the site has a carousel on it.
    """
    deadline = timeout_ms / 1000

    try:
        async with asyncio.timeout(deadline):
            await _network_idle(page, deadline)
            await page.evaluate(_QUIET_DOM, quiet_ms)
    except TimeoutError:
        logger.debug("page_did_not_settle", timeout_ms=timeout_ms)
        return False
    except Exception as exc:
        # A navigation mid-wait destroys the execution context. That is not a
        # failure to settle, it is the page having moved on.
        logger.debug("page_settle_interrupted", error_type=type(exc).__name__)
        return False

    return True


async def _network_idle(page: _Settleable, deadline: float) -> None:
    """Best effort. A page with a websocket open is never idle."""
    try:
        await page.wait_for_load_state("networkidle", timeout=deadline * 1000)
    except Exception:
        return


async def await_late_overlays(
    page: _Settleable,
    *,
    modal_scope: str,
    grace_ms: int = DEFAULT_LOAD_POPUP_GRACE_MS,
    quiet_ms: int = DEFAULT_QUIET_MS,
) -> bool:
    """Give a freshly loaded page the chance to put up a late popup.

    Returns whether one appeared. On a document older than ``grace_ms``, which
    is every read after the first couple of seconds on a page, it costs a
    single round trip. On a new one it costs at most ``grace_ms``, and returns
    early the moment a popup is up. ``grace_ms`` of 0 turns it off.

    ``modal_scope`` is the shared detector's source, the same one perception
    uses for ``modal_open``. It is passed in so this module keeps no dependency
    on perception, and so "a popup appeared" here means exactly what it means
    to the planner.

    Never raises, for the same reason ``settle`` does not.
    """
    if grace_ms <= 0:
        return False

    script = _LATE_OVERLAY.replace("__MODAL_SCOPE__", modal_scope)
    # The page bounds itself at grace_ms plus one quiet period. This is only a
    # backstop for a page that stops running its timers.
    deadline = (grace_ms + quiet_ms) / 1000 + 1

    try:
        async with asyncio.timeout(deadline):
            appeared = await page.evaluate(script, {"graceMs": grace_ms, "quietMs": quiet_ms})
    except TimeoutError:
        logger.debug("late_overlay_wait_timed_out", grace_ms=grace_ms)
        return False
    except Exception as exc:
        # As in settle: a navigation mid-wait is the page moving on, not an error.
        logger.debug("late_overlay_wait_interrupted", error_type=type(exc).__name__)
        return False

    if appeared:
        logger.info("late_popup_waited_for", grace_ms=grace_ms)
    return bool(appeared)


async def await_popup_in_place(
    page: _Settleable,
    *,
    modal_scope: str,
    timeout_ms: int = DEFAULT_POPUP_IN_PLACE_MS,
) -> PopupPlacement:
    """Hold the read until an open popup's controls are on screen and still.

    Returns ``"none"`` when no popup is open, which costs one round trip;
    ``"ready"`` when the popup was already at rest; ``"waited"`` when it was
    still arriving and came to rest; ``"timeout"`` when it had not by
    ``timeout_ms`` and is read as it is.

    ``modal_scope`` is the shared detector's source, as for
    ``await_late_overlays``, so "a popup is open" means what it means to the
    planner. Never raises.
    """
    script = _POPUP_IN_PLACE.replace("__MODAL_SCOPE__", modal_scope)
    # The page bounds itself at timeout_ms; this is the backstop for a page that
    # stops running its timers.
    deadline = timeout_ms / 1000 + 1

    try:
        async with asyncio.timeout(deadline):
            outcome = cast(str, await page.evaluate(script, {"timeoutMs": timeout_ms}))
    except TimeoutError:
        logger.debug("popup_placement_wait_timed_out", timeout_ms=timeout_ms)
        return "timeout"
    except Exception as exc:
        # A navigation mid-wait replaces the document: the popup is gone.
        logger.debug("popup_placement_wait_interrupted", error_type=type(exc).__name__)
        return "none"

    if outcome == "waited":
        logger.info("popup_waited_into_place")
    elif outcome == "timeout":
        logger.info("popup_not_in_place", timeout_ms=timeout_ms)
    if outcome in ("none", "ready", "waited", "timeout"):
        return cast(PopupPlacement, outcome)
    return "none"
