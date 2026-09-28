"""Render elements as the compact lines a planner reads (spec section 4.6).

    [14] button "Search flights"
    [15] input[type=text] "From" value="Delhi"
    [16] a "Cheapest" href=/results?sort=price
    ... 42 more elements below the fold (scroll to see)

The budget is the reason this module exists. A results page can carry hundreds
of controls, and sending all of them would cost more in tokens than the whole
rest of the prompt while burying the three that matter. Selection is by
usefulness, not by document order.
"""

from collections.abc import Mapping, Sequence
from typing import Final

from app.agent.perception.dom_extractor import ElementInfo

__all__ = [
    "CHARS_PER_TOKEN",
    "SUMMARY_TOKEN_BUDGET",
    "build_header",
    "build_summary",
    "describe_tabs",
    "format_element",
]

# Spec section 4.6: "hard token budget (default ~3k tokens)".
SUMMARY_TOKEN_BUDGET: Final = 3000
# Good enough for budgeting English plus markup, and it needs no tokenizer
# dependency or per-model tokenizer choice.
CHARS_PER_TOKEN: Final = 4

_FORM_TAGS: Final = frozenset({"input", "select", "textarea"})
_BUTTON_TAGS: Final = frozenset({"button", "summary"})
_BUTTON_ROLES: Final = frozenset({"button", "tab", "menuitem", "switch", "checkbox", "radio"})
_LINK_ROLES: Final = frozenset({"link"})

_MORE_BELOW_FOLD: Final = "... {count} more elements below the fold (scroll to see)"
# When the budget cuts elements that *are* in view, "scroll to see" would
# send the planner scrolling for something already on screen.
_MORE_TRUNCATED: Final = "... {count} more elements not shown (summary budget reached)"


def _quote(value: str) -> str:
    """Quote for the summary, keeping a quote in the text from breaking it."""
    return '"' + value.replace('"', "'") + '"'


def format_element(element: ElementInfo) -> str:
    """One summary line.

    Spec section 4.6 shows ``placeholder="From"`` where the brief shows
    ``"From"``. Both are produced by the same rule: the accessible name is
    quoted when there is one, and ``placeholder=`` is added only when the
    placeholder says something the name does not. A field labelled "From" with
    placeholder "Departure city" shows both; a field whose only name *is* its
    placeholder shows it once.
    """
    descriptor = element.tag
    if element.input_type:
        descriptor = f"{element.tag}[type={element.input_type}]"

    parts = [f"[{element.index}]", descriptor]

    name = element.name
    placeholder = element.placeholder
    if name:
        parts.append(_quote(name))
    if placeholder and placeholder != name:
        parts.append(f"placeholder={_quote(placeholder)}")

    if element.value:
        parts.append(f"value={_quote(element.value)}")
    if element.href:
        parts.append(f"href={element.href}")

    # Two states the planner cannot infer and will otherwise waste a step on.
    if element.disabled:
        parts.append("(disabled)")
    if element.covered:
        parts.append("(covered)")

    return " ".join(parts)


def _tier(element: ElementInfo) -> int:
    """Lower is kept first when the budget bites."""
    if not element.in_viewport:
        return 5
    if element.covered:
        # A covered element means an overlay is in the way. Saying so first is
        # what lets the planner reach for "dismiss the modal" rather than
        # clicking something it has been told is blocked.
        return 0
    if element.tag in _FORM_TAGS:
        return 1
    if element.tag in _BUTTON_TAGS or (element.role or "") in _BUTTON_ROLES:
        return 2
    if element.tag == "a" or (element.role or "") in _LINK_ROLES:
        return 3
    return 4


def describe_tabs(
    tabs: Sequence[str],
    active_tab_id: str,
    labels: Mapping[str, str] | None = None,
) -> str:
    """Render the tab line.

    ``t1 SkyFare results (active), t2 Flight AI-203 details`` rather than
    ``t1, t2``: a planner deciding whether to switch tabs needs to know what
    is in the other one, and a bare id tells it nothing. A tab that has not
    been perceived yet has no label, and prints as its id alone.
    """
    known = labels or {}
    parts = []
    for tab in tabs:
        label = known.get(tab, "").strip()
        marker = " (active)" if tab == active_tab_id else ""
        parts.append(f"{tab} {label}{marker}" if label else f"{tab}{marker}")
    return ", ".join(parts)


def build_header(
    *,
    url: str,
    title: str,
    tabs: Sequence[str],
    active_tab_id: str,
    scroll_y: int,
    scroll_max_y: int,
    frame_urls: Sequence[str] = (),
    tab_labels: Mapping[str, str] | None = None,
) -> str:
    """The page header from spec section 4.6.

    Scroll position is here so the planner knows a ``scroll`` action is
    available and roughly how much page is left, which is the other half of the
    "N more below the fold" line.

    Framed content gets its own line. A page that renders its results inside
    an iframe leaves the top-level URL unchanged when something in the frame
    navigates, so a planner told only ``URL:`` cannot tell that its click
    worked and will do it again.
    """
    tab_list = describe_tabs(tabs, active_tab_id, tab_labels)
    framed = [frame for frame in frame_urls if frame and frame != url]
    lines = [
        f"URL: {url}",
        f"Title: {title}",
        f"Tabs: {tab_list or active_tab_id}",
        f"Scroll: {scroll_y} of {scroll_max_y}",
    ]
    if framed:
        lines.insert(1, f"Frames: {', '.join(framed)}")
    return "\n".join(tuple(lines))


def build_summary(
    elements: Sequence[ElementInfo],
    header: str = "",
    *,
    token_budget: int = SUMMARY_TOKEN_BUDGET,
) -> str:
    """Render as many elements as the budget allows, most useful first.

    Chosen by priority, then emitted in index order, because a planner reading
    ascending indices can scan the list the way it reads the page.
    """
    budget_chars = max(0, token_budget * CHARS_PER_TOKEN)
    prefix = f"{header}\n\n" if header else ""
    remaining = budget_chars - len(prefix)

    ordered = sorted(enumerate(elements), key=lambda pair: (_tier(pair[1]), pair[0]))

    kept: list[ElementInfo] = []
    used = 0
    for _, element in ordered:
        line = format_element(element)
        # Leave room for the trailing count, which is only worth writing if
        # something was actually dropped.
        reserve = len(_MORE_TRUNCATED.format(count=len(elements))) + 1
        if used + len(line) + 1 + reserve > remaining:
            break
        kept.append(element)
        used += len(line) + 1

    lines = [format_element(element) for element in sorted(kept, key=lambda item: item.index)]

    dropped = [element for element in elements if element not in kept]
    if dropped:
        template = (
            _MORE_BELOW_FOLD
            if all(not element.in_viewport for element in dropped)
            else _MORE_TRUNCATED
        )
        lines.append(template.format(count=len(dropped)))

    return prefix + "\n".join(lines)
