"""Finding the thing on a page that actually scrolls.

Plenty of modern sites keep the window still and scroll an inner panel (an
app shell with a fixed header, a results pane, a modal). ``window.scrollBy`` on
such a page does nothing, and ``window.scrollY`` reads zero before and after,
so the agent concludes it cannot scroll and the verifier agrees.

One snippet decides which element scrolls, and is used both to scroll and to
measure, so the two can never disagree about what "the page" is.
"""

from typing import Final

from app.agent.perception.dom_extractor import modal_scope_source

__all__ = ["METRICS_JS", "SCROLLER_JS", "scroll_by_js"]

# Prefer the active widget or dialog over its background document. Keep the
# same selection rule for actions and perception so verification measures the
# panel the driver actually moved.
SCROLLER_JS: Final = r"""
const scroller = () => {
  const root = document.scrollingElement || document.documentElement;
  const area = el => {
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0')
      return 0;
    const r = el.getBoundingClientRect();
    const left = Math.max(0, r.left), right = Math.min(innerWidth, r.right);
    const top = Math.max(0, r.top), bottom = Math.min(innerHeight, r.bottom);
    if (right <= left || bottom <= top) return 0;
    const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
    return hit && (el === hit || el.contains(hit)) ? (right - left) * (bottom - top) : 0;
  };
  const scrollable = el => el.scrollHeight - el.clientHeight > 4 &&
    ['auto', 'scroll'].includes(getComputedStyle(el).overflowY) && area(el) > 0;
  const largest = scope => {
    let best = scrollable(scope) ? scope : null;
    let bestArea = best ? area(best) : 0;
    const nodes = scope.querySelectorAll('*');
    for (let i = 0; i < Math.min(nodes.length, 4000); i++) {
      const el = nodes[i];
      if (!scrollable(el)) continue;
      const size = area(el);
      if (size > bestArea) { best = el; bestArea = size; }
    }
    return best;
  };
  const modal = (__MODAL_SCOPE__)(document);
  let active = document.activeElement;
  while (active && active.shadowRoot && active.shadowRoot.activeElement)
    active = active.shadowRoot.activeElement;
  // Autocomplete options can live outside the field's own container.
  if (active && active.getAttribute('aria-expanded') === 'true') {
    const ids = (active.getAttribute('aria-controls') || '').split(/\s+/);
    for (const id of ids) {
      const popup = document.getElementById(id);
      if (popup && (!modal || modal.contains(popup))) {
        const target = largest(popup);
        if (target) return target;
      }
    }
  }
  for (let el = active; el && el !== root && el !== document.body;
       el = el.parentElement || el.getRootNode().host) {
    if ((!modal || modal.contains(el)) && scrollable(el)) return el;
  }
  if (modal) return largest(modal) || modal;
  const rootUnlocked = !['hidden', 'clip'].includes(getComputedStyle(root).overflowY) &&
    (!document.body || !['hidden', 'clip'].includes(getComputedStyle(document.body).overflowY));
  if (rootUnlocked && root.scrollHeight - root.clientHeight > 4) return root;
  return document.body ? (largest(document.body) || root) : root;
};
const isRoot = (el) => el === document.scrollingElement || el === document.documentElement;
""".replace("__MODAL_SCOPE__", modal_scope_source())

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
