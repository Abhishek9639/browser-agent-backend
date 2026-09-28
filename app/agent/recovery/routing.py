"""Which rung to try first, given why the step failed (spec section 4.8).

The ladder's order is the order of likelihood across all failures. It is not
the best order for any particular one, and under a budget of three attempts
that difference is most of a task's wall clock.

The case that made this worth doing: a cookie banner over the search button.
The click fails with ``TIMEOUT`` because something is intercepting it, and the
first three rungs all try to click the same covered button with a fresh
locator. Each one waits out the ten second action timeout before failing, the
budget is gone, and the rung that would have worked in half a second is never
reached. Thirty seconds to not fix a popup.

So the error code chooses the order. Two rules carry almost all of it:

* ``ELEMENT_NOT_FOUND`` means the locator is wrong, so re-locate.
* ``TIMEOUT`` or ``NOT_INTERACTABLE`` on an element that is still on the page
  means the locator is right and something is in the way, so clear the way.

Routing reorders; it never truncates. A misclassified failure still gets every
rung if the budget lasts, because the error code is a hint from a message
Playwright wrote, not a diagnosis.
"""

from typing import Final

from app.agent.browser.errors import ActionErrorCode
from app.agent.contracts import RecoveryContext
from app.agent.recovery.strategies import RecoveryStrategy
from app.core.logging import get_logger

__all__ = ["ROUTES", "order_for", "route_for"]

logger = get_logger(__name__)

# Rung names in the order the spec lists them, for reference in the routes
# below: reperceive_by_index, role_name_locator, text_locator, stored_selector,
# scroll_and_wait, dismiss_overlay, visual_grounding.

RELOCATE: Final[tuple[str, ...]] = (
    "reperceive_by_index",
    "role_name_locator",
    "text_locator",
    "stored_selector",
)

# In the way of a click. Order depends on what is in the way, which is decided
# per failure rather than here.
CLEAR_THE_WAY: Final[tuple[str, ...]] = ("dismiss_overlay", "scroll_and_wait")

ROUTES: Final[dict[ActionErrorCode, tuple[str, ...]]] = {
    # Playwright could not resolve the selector at all, so a better locator is
    # the whole problem. This is also the ladder's default order, which is why
    # it was the only failure the old ladder handled well.
    ActionErrorCode.ELEMENT_NOT_FOUND: RELOCATE,
    # Resolved and then would not respond. Re-locating finds the same element
    # and waits out the same timeout.
    ActionErrorCode.TIMEOUT: CLEAR_THE_WAY,
    ActionErrorCode.NOT_INTERACTABLE: CLEAR_THE_WAY,
}


def route_for(ctx: RecoveryContext) -> tuple[str, ...]:
    """The rung names to try first for this failure.

    Empty means no opinion, and the ladder keeps its own order. That is the
    right answer for a step whose action succeeded and whose verification
    failed: nothing went wrong with the locator, so nothing here knows better
    than the spec's ordering.
    """
    code = ctx.result.error_code
    if code is None:
        return ()

    route = ROUTES.get(code, ())
    if route is not CLEAR_THE_WAY:
        return route

    # Both rungs go first either way; this only picks which of the two. The
    # cheap, likely case for an element that is on the page and off screen is
    # that it was never reachable, so scroll. Everything else, including an
    # element the collector withheld because something covers it completely,
    # reads as obstructed.
    if _present_but_off_screen(ctx):
        return tuple(reversed(CLEAR_THE_WAY))
    return CLEAR_THE_WAY


def order_for(
    strategies: tuple[RecoveryStrategy, ...],
    ctx: RecoveryContext,
) -> tuple[RecoveryStrategy, ...]:
    """The ladder, reordered for this failure.

    Every rung is still in the result. The named ones come first, in the order
    the route names them, and everything else follows in its original order,
    so a route that turns out to be wrong costs nothing but its own attempts.
    """
    route = route_for(ctx)
    if not route:
        return strategies

    by_name = {strategy.name: strategy for strategy in strategies}
    first = [by_name[name] for name in route if name in by_name]
    chosen = {strategy.name for strategy in first}
    rest = [strategy for strategy in strategies if strategy.name not in chosen]

    logger.debug(
        "recovery_route",
        error_code=ctx.result.error_code.value if ctx.result.error_code else None,
        first=[strategy.name for strategy in first],
    )
    return tuple(first + rest)


def _present_but_off_screen(ctx: RecoveryContext) -> bool:
    """Is the target still on the page, unobstructed, and outside the viewport?

    Read from the state captured after the failure rather than the one the
    decision was made against, because the question is what the page looks
    like now.

    An element that is missing from that state is not treated as off screen.
    The collector withholds a control that something covers completely, so
    "gone" is what an overlay over the target looks like from here, and the
    answer for that is to clear the overlay rather than to scroll to a control
    that is already where it should be.
    """
    if ctx.element is None:
        return False

    wanted = (ctx.element.role or "", ctx.element.name, ctx.element.text)
    current = next(
        (item for item in ctx.after.elements if (item.role or "", item.name, item.text) == wanted),
        None,
    )
    if current is None or current.covered:
        return False
    return not current.in_viewport
