"""Find a popup that is blocking the page, and the control that closes it.

A sign-up promo or a cookie banner covers everything underneath, so every
other element on the page is marked covered and nothing can be clicked or typed
into. Some of those popups close with an icon that has no name at all (an
``<i>`` or a bare ``<button>``), which a label match never finds.

Pure functions over :class:`PageState`, so they are cheap to test and the
orchestrator can use them without a browser.
"""

from typing import Final

from app.agent.perception.dom_extractor import ElementInfo
from app.agent.perception.state import PageState

__all__ = ["BLOCKED_COVERED_COUNT", "CLOSE_LABELS", "icon_closers", "is_blocked", "safe_closers"]

# This many covered elements means something is sitting on top of the page. A
# sticky header or a floating chat button covers a few; a modal covers dozens.
BLOCKED_COVERED_COUNT: Final = 8

# Labels that close a popup without agreeing to anything on the user's behalf.
# "Accept" is left out on purpose: that is consent, not dismissal.
CLOSE_LABELS: Final[tuple[str, ...]] = (
    "reject non-essential",
    "reject all",
    "reject",
    "decline",
    "no thanks",
    "not now",
    "maybe later",
    "close",
    "dismiss",
    "×",  # noqa: RUF001  (real close buttons use the multiplication sign)
    "✕",
    "x",
    "got it",
    # A language gate in front of the whole site: English is the safe reading.
    "english",
)

_ICON_TAGS: Final = frozenset({"i", "button", "span", "a", "svg", "img"})
_MAX_ICON_SIZE: Final = 56


def is_blocked(state: PageState) -> bool:
    """Is a popup covering the page?"""
    return sum(1 for element in state.elements if element.covered) >= BLOCKED_COVERED_COUNT


def _label(element: ElementInfo) -> str:
    return (element.name or element.text).strip().casefold()


def _usable(element: ElementInfo) -> bool:
    return not element.covered and not element.disabled and element.in_viewport


def icon_closers(state: PageState) -> list[ElementInfo]:
    """Unnamed icon controls that sit in the top-right corner of a popup.

    A popup's close mark has no label, but it has a position: above, and to the
    right of, the named controls inside the same popup. Requiring at least two
    named controls beneath it, close by, keeps a stray icon in a page header
    from qualifying.
    """
    named = [e for e in state.elements if _usable(e) and _label(e)]
    found: list[ElementInfo] = []
    for icon in state.elements:
        if not _usable(icon) or _label(icon) or icon.tag not in _ICON_TAGS:
            continue
        if icon.bbox.width > _MAX_ICON_SIZE or icon.bbox.height > _MAX_ICON_SIZE:
            continue

        peers = [
            n
            for n in named
            if 0 < n.bbox.y - icon.bbox.y < 700 and abs(n.bbox.x - icon.bbox.x) < 450
        ]
        if len(peers) < 2:
            continue
        centres = [p.bbox.x + p.bbox.width / 2 for p in peers]
        if icon.bbox.x + icon.bbox.width / 2 >= sum(centres) / len(centres):
            found.append(icon)
    return found


def safe_closers(state: PageState) -> list[ElementInfo]:
    """What to click to dismiss a blocking popup, best first. Empty if nothing blocks.

    Labelled closers outrank unlabelled icons because a label is evidence and a
    position is a guess.
    """
    if not is_blocked(state):
        return []

    labelled: list[tuple[int, ElementInfo]] = []
    for element in state.elements:
        if element.tag not in {"button", "a"} or not _usable(element):
            continue
        # Some pages give a control a long or garbled accessible name and keep
        # the words a person reads in its text, so either may carry the label.
        for label in (element.name.strip().casefold(), element.text.strip().casefold()):
            if label in CLOSE_LABELS:
                labelled.append((CLOSE_LABELS.index(label), element))
                break
    labelled.sort(key=lambda pair: pair[0])

    return [element for _, element in labelled] + icon_closers(state)
