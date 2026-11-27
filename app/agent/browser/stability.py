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
"""

import asyncio
from typing import Final, Protocol

from app.core.logging import get_logger

__all__ = [
    "DEFAULT_LOAD_POPUP_GRACE_MS",
    "DEFAULT_QUIET_MS",
    "DEFAULT_TIMEOUT_MS",
    "await_late_overlays",
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
