"""Finding the thing on a page that actually scrolls.

Plenty of modern sites keep the window still and scroll an inner panel (an
app shell with a fixed header, a results pane, a modal). ``window.scrollBy`` on
such a page does nothing, and ``window.scrollY`` reads zero before and after,
so the agent concludes it cannot scroll and the verifier agrees.

One snippet decides which element scrolls, and is used both to scroll and to
measure, so the two can never disagree about what "the page" is.
"""

from typing import Final

__all__ = ["METRICS_JS", "SCROLLER_JS", "scroll_by_js"]

# Defines `scroller()`: the document if it can scroll, else the largest visible
# panel that can. Cheap on an ordinary page: the document check returns first,
# and only a page whose window cannot scroll pays for the search.
SCROLLER_JS: Final = r"""
const scroller = () => {
  const root = document.scrollingElement || document.documentElement;
  if (root.scrollHeight - root.clientHeight > 4) return root;
  let best = null;
  let bestArea = 0;
  const floor = innerWidth * innerHeight * 0.3;
  const nodes = document.body ? document.body.querySelectorAll('*') : [];
  const limit = Math.min(nodes.length, 4000);
  for (let i = 0; i < limit; i++) {
    const el = nodes[i];
    if (el.scrollHeight - el.clientHeight < 50) continue;
    const overflow = getComputedStyle(el).overflowY;
    if (overflow !== 'auto' && overflow !== 'scroll') continue;
    const r = el.getBoundingClientRect();
    const visibleHeight = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0);
    const area = Math.max(0, r.width) * Math.max(0, visibleHeight);
    if (area > bestArea && area > floor) {
      best = el;
      bestArea = area;
    }
  }
  return best || root;
};
const isRoot = (el) => el === document.scrollingElement || el === document.documentElement;
"""

# Scroll position and room to scroll, of whatever scrolls.
METRICS_JS: Final = (
    "() => {"
    + SCROLLER_JS
    + r"""
  const el = scroller();
  const y = isRoot(el) ? window.scrollY : el.scrollTop;
  const room = isRoot(el)
    ? Math.max(
        document.documentElement.scrollHeight,
        document.body ? document.body.scrollHeight : 0
      ) - window.innerHeight
    : el.scrollHeight - el.clientHeight;
  return [Math.round(y), Math.max(0, Math.round(room))];
}"""
)


def scroll_by_js(behavior: str) -> str:
    """A function expression taking ``dy``; scrolls whatever scrolls."""
    return (
        "(dy) => {"
        + SCROLLER_JS
        + f"""
  const el = scroller();
  if (isRoot(el)) window.scrollBy({{ top: dy, left: 0, behavior: '{behavior}' }});
  else el.scrollBy({{ top: dy, left: 0, behavior: '{behavior}' }});
}}"""
    )
