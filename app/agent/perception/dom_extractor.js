/**
 * Interactive element collector (spec sections 4.6 and 4.11).
 *
 * Runs inside the page via page.evaluate and returns a plain array. Two rules
 * shape it:
 *
 *   - What is invisible to a person is invisible to the agent. Spec section
 *     4.11 excludes display:none, zero-size, off-screen and aria-hidden
 *     elements precisely because they are where injected instructions hide.
 *   - Values that look like a secret never leave the page. Masking happens
 *     here, at the point of collection, so a password cannot reach a log, a
 *     prompt or a database row even by accident.
 */
() => {
  const modalScope = __MODAL_SCOPE__;
  const MAX_TEXT = 80;

  // "Zero size" cannot be taken literally. A button styled width:0;height:0
  // still renders about 16x6 from the browser's default border and padding,
  // which is an easy place to park an injected control. Nothing thinner than
  // this in either axis is a real target for a person either; the usual
  // accessibility guidance asks for far more.
  const MIN_INTERACTIVE_PX = 8;
  const MASK = "••••";
  const FIELD_TAGS = new Set(["INPUT", "SELECT", "TEXTAREA"]);

  const SEMANTIC_SELECTOR = [
    "a[href]",
    "button",
    "input:not([type=hidden])",
    "select",
    "textarea",
    "summary",
    '[role="button"]',
    '[role="link"]',
    '[role="tab"]',
    '[role="option"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="menuitem"]',
    '[role="switch"]',
    '[role="combobox"]',
    // A day in a date picker. Pickers give one day tabindex 0 and the rest -1,
    // so without this the agent saw a month with a single date in it.
    '[role="gridcell"]',
    "[onclick]",
    '[contenteditable="true"]',
  ].join(",");

  // name, id or autocomplete hinting at something that must never be echoed.
  // Substring matching on purpose: field names run words together, so
  // "cardNumber" and "newCreditCard" both have to match.
  const SECRET_HINT =
    /(pass|pwd|card|cc[-_]?num|credit|cvv|cvc|csc|otp|pin|passcode|security[-_]?code|secret|token)/i;

  // Ordinary words that happen to contain one of those substrings. Stripped
  // from the hint before it is tested, rather than vetoing the whole field, so
  // "passenger_card_number" is still masked on the "card" in the middle of it.
  // Without this a passenger count reads as "••••" because it starts with
  // "pass", and a shipping address is a secret because "shipping" contains
  // "pin".
  const BENIGN = /(passenger|shipping|campaign|spinner|tokeni[sz]|discard|placard)/gi;

  const collapse = (value) => (value || "").replace(/\s+/g, " ").trim();
  const clip = (value) => {
    const text = collapse(value);
    return text.length > MAX_TEXT ? text.slice(0, MAX_TEXT) : text;
  };

  function accessibleName(el) {
    // Order follows spec section 4.6.
    const label = el.getAttribute("aria-label");
    if (collapse(label)) return clip(label);

    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const parts = labelledBy
        .split(/\s+/)
        .map((id) => el.ownerDocument.getElementById(id))
        .filter(Boolean)
        .map((node) => node.innerText || node.textContent || "");
      if (collapse(parts.join(" "))) return clip(parts.join(" "));
    }

    if (el.id) {
      const forLabel = el.ownerDocument.querySelector(
        `label[for="${CSS.escape(el.id)}"]`,
      );
      if (forLabel && collapse(forLabel.innerText)) return clip(forLabel.innerText);
    }
    const wrapping = el.closest("label");
    if (wrapping && collapse(wrapping.innerText)) return clip(wrapping.innerText);

    for (const attr of ["placeholder", "alt", "title"]) {
      const value = el.getAttribute(attr);
      if (collapse(value)) return clip(value);
    }

    return clip(el.innerText || el.textContent || "");
  }

  function isMasked(el) {
    if ((el.getAttribute("type") || "").toLowerCase() === "password") return true;
    const hints = [
      el.getAttribute("name"),
      el.getAttribute("id"),
      el.getAttribute("autocomplete"),
    ]
      .filter(Boolean)
      .join(" ");
    return SECRET_HINT.test(hints.replace(BENIGN, " "));
  }

  function valueOf(el) {
    if (!("value" in el)) return null;
    const raw = el.value;
    if (raw === undefined || raw === null || raw === "") return null;
    return isMasked(el) ? MASK : clip(String(raw));
  }

  function hrefOf(el) {
    const raw = el.getAttribute("href");
    if (!raw) return null;
    try {
      // Path and query only: the origin is already known from the page URL,
      // and full URLs bloat the prompt for no benefit.
      const url = new URL(raw, el.ownerDocument.location.href);
      return url.pathname + url.search;
    } catch (err) {
      return clip(raw);
    }
  }

  function cssPath(el) {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 12) {
      if (node.id) {
        parts.unshift(`#${CSS.escape(node.id)}`);
        break;
      }
      const tag = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (!parent) {
        parts.unshift(tag);
        break;
      }
      const siblings = Array.from(parent.children).filter(
        (child) => child.tagName === node.tagName,
      );
      parts.unshift(
        siblings.length > 1 ? `${tag}:nth-of-type(${siblings.indexOf(node) + 1})` : tag,
      );
      node = parent;
    }
    return parts.join(" > ");
  }

  function xpathOf(el) {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 16) {
      const tag = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (!parent) {
        parts.unshift(`/${tag}`);
        break;
      }
      const siblings = Array.from(parent.children).filter(
        (child) => child.tagName === node.tagName,
      );
      parts.unshift(`/${tag}[${siblings.indexOf(node) + 1}]`);
      node = parent;
    }
    return parts.join("");
  }

  function isModal(el) {
    if (!el) return false;
    if (el.closest('[role="dialog"], [role="alertdialog"], [aria-modal="true"]')) return true;
    let node = el;
    while (node && node.nodeType === 1) {
      const style = node.ownerDocument.defaultView.getComputedStyle(node);
      const layer = parseInt(style.zIndex, 10);
      if (
        (style.position === "fixed" || style.position === "sticky") &&
        Number.isFinite(layer) &&
        layer >= 100
      ) {
        return true;
      }
      node = node.parentElement;
    }
    return false;
  }

  function isAutosizedInput(el, rect, win) {
    // Base Web selects (including Uber's address fields) keep an empty native
    // text input only 2px wide inside a full-size visible select box. It is
    // editable, not an injected sliver. Require the real widget around it;
    // ordinary tiny controls and zero-size inputs still stay excluded.
    if (el.tagName !== "INPUT" || !["text", "search"].includes(el.type)) return false;
    if (rect.width <= 0 || rect.height < MIN_INTERACTIVE_PX) return false;
    const widget = el.parentElement?.closest('[data-baseweb="select"], [role="combobox"]');
    if (!widget || widget === el) return false;
    const style = win.getComputedStyle(widget);
    if (style.display === "none" || style.visibility !== "visible" || parseFloat(style.opacity) === 0) return false;
    const box = widget.getBoundingClientRect();
    return box.width >= MIN_INTERACTIVE_PX && box.height >= MIN_INTERACTIVE_PX &&
      rect.left >= box.left && rect.right <= box.right &&
      rect.top >= box.top && rect.bottom <= box.bottom;
  }

  function hiddenReason(el, win, doc) {
    const style = win.getComputedStyle(el);
    if (style.display === "none") return "display";
    if (style.visibility === "hidden" || style.visibility === "collapse") return "visibility";
    if (parseFloat(style.opacity) === 0) return "opacity";
    if (el.closest('[aria-hidden="true"]')) return "aria-hidden";
    if (el.hasAttribute("hidden")) return "hidden-attr";

    const rect = el.getBoundingClientRect();
    if ((rect.width < MIN_INTERACTIVE_PX || rect.height < MIN_INTERACTIVE_PX) &&
        !isAutosizedInput(el, rect, win)) {
      return "too-small";
    }

    // Fully outside the document, the usual trick for off-screen injected text
    // (left: -9999px and friends).
    const root = doc.documentElement;
    const pageWidth = Math.max(root.scrollWidth, doc.body ? doc.body.scrollWidth : 0);
    const pageHeight = Math.max(root.scrollHeight, doc.body ? doc.body.scrollHeight : 0);
    const absLeft = rect.left + win.scrollX;
    const absTop = rect.top + win.scrollY;
    if (
      absLeft + rect.width <= 0 ||
      absTop + rect.height <= 0 ||
      absLeft >= pageWidth ||
      absTop >= pageHeight
    ) {
      return "off-document";
    }
    return null;
  }

  function coverage(el, win, doc) {
    // elementFromPoint takes viewport coordinates, so anything scrolled out of
    // view cannot be probed. Those are reported as not covered; in_viewport
    // already tells the planner it must scroll first.
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    if (x < 0 || y < 0 || x > win.innerWidth || y > win.innerHeight) {
      return { covered: false, byModal: false };
    }

    const top = doc.elementFromPoint(x, y);
    if (!top || top === el || el.contains(top) || top.contains(el)) {
      return { covered: false, byModal: false };
    }
    return { covered: true, byModal: isModal(top) };
  }

  // A form's own controls stay listed, marked covered, when a popup such as a
  // date picker sits on them. A field: what it holds is the state of the form,
  // and when MakeMyTrip's calendar opened over the destination just chosen the
  // agent concluded the choice was lost and typed it again. And the button
  // that submits the form: IRCTC's Search sat under the calendar the agent had
  // just used, so it went scrolling for a button that was right there.
  // Clicking a covered target closes the popup first, so listing it is enough.
  function keptUnderPopup(el) {
    if (FIELD_TAGS.has(el.tagName)) return true;
    return el.tagName === "BUTTON" && el.type === "submit" && el.form !== null;
  }

  function describe(el, win, doc, frameId, offsetX, offsetY) {
    const rect = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute("type");

    const expanded = el.getAttribute("aria-expanded");
    const ariaChecked = el.getAttribute("aria-checked");
    const ariaSelected = el.getAttribute("aria-selected");
    const ariaCurrent = el.getAttribute("aria-current");
    const isSelected =
      el.selected === true ||
      ariaSelected === "true" ||
      ariaCurrent === "page" ||
      ariaCurrent === "true" ||
      ariaCurrent === "step" ||
      (el.classList && (el.classList.contains("active") || el.classList.contains("selected")));

    return {
      element: el,
      document: doc,
      tag,
      role: el.getAttribute("role") || null,
      name: accessibleName(el),
      text: clip(el.innerText || el.textContent || ""),
      placeholder: el.getAttribute("placeholder") || null,
      input_type: tag === "input" ? (type || "text").toLowerCase() : null,
      value: valueOf(el),
      href: hrefOf(el),
      bbox: {
        x: Math.round(rect.left + offsetX),
        y: Math.round(rect.top + offsetY),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      },
      in_viewport:
        rect.bottom > 0 &&
        rect.right > 0 &&
        rect.top < win.innerHeight &&
        rect.left < win.innerWidth,
      frame_id: frameId,
      css_path: cssPath(el),
      xpath: xpathOf(el),
      disabled: el.disabled === true || el.getAttribute("aria-disabled") === "true",
      checked:
        el.checked === true ? true : ariaChecked === "true" ? true : ariaChecked === "false" ? false : null,
      selected: isSelected ? true : ariaSelected === "false" ? false : null,
      expanded: expanded === "true" ? true : expanded === "false" ? false : null,
      cross_origin: false,
    };
  }

  function collect(doc, win, frameId, offsetX, offsetY, out) {
    const semantic = new Set(doc.querySelectorAll(SEMANTIC_SELECTOR));

    // tabindex >= 0 is focusable, so it is reachable and therefore clickable.
    // Kept apart when that is its only claim, because such an element is as
    // often a container (a date picker's month, a scroll region) as a control.
    const focusableOnly = new Set();
    for (const el of doc.querySelectorAll("[tabindex]")) {
      const value = parseInt(el.getAttribute("tabindex"), 10);
      if (Number.isFinite(value) && value >= 0 && !semantic.has(el)) {
        semantic.add(el);
        focusableOnly.add(el);
      }
    }

    // Anything the page styles as clickable, even without semantics.
    const pointer = new Set();
    for (const el of doc.querySelectorAll("*")) {
      if (semantic.has(el)) continue;
      if (win.getComputedStyle(el).cursor === "pointer") pointer.add(el);
    }

    // Does this ancestor make a pointer element inside it part of itself?
    // A control does, and so does a pointer element: cursor is inherited, so
    // the text inside a clickable div is pointer-styled too. A container that
    // is focusable and nothing more does not, unless it is pointer-styled
    // itself: what sets its own pointer inside one is a target of its own,
    // like a day in a calendar whose month grid holds the focus.
    const subsumes = (node) =>
      pointer.has(node) ||
      (semantic.has(node) &&
        (!focusableOnly.has(node) || win.getComputedStyle(node).cursor === "pointer"));

    // Keep the outermost meaningful target. A pointer wrapper around a real
    // button is noise, and a chain of nested pointer divs is worse.
    const candidates = [];
    for (const el of doc.querySelectorAll("*")) {
      if (semantic.has(el)) {
        candidates.push(el);
        continue;
      }
      if (!pointer.has(el)) continue;

      let ancestor = el.parentElement;
      let nested = false;
      while (ancestor) {
        if (subsumes(ancestor)) {
          nested = true;
          break;
        }
        ancestor = ancestor.parentElement;
      }
      if (nested) continue;
      if (el.querySelector(SEMANTIC_SELECTOR)) continue;
      candidates.push(el);
    }

    const modal = modalScope(doc);
    for (const el of candidates) {
      if (el.tagName === "IFRAME") continue;
      if (hiddenReason(el, win, doc)) continue;

      const { covered, byModal } = coverage(el, win, doc);
      // Covered means unreachable, so it is dropped. Two exceptions. Behind a
      // modal: the recovery ladder dismisses overlays, so it needs to see what
      // is there. And a form's own controls under a popup (see keptUnderPopup).
      if (covered && !byModal && !modal && !keptUnderPopup(el)) continue;

      const info = describe(el, win, doc, frameId, offsetX, offsetY);
      info.in_modal = Boolean(modal && modal.contains(el));
      info.covered = covered || Boolean(modal && !info.in_modal);
      out.push(info);
    }

    // Same-origin frames are walked with their offset applied so every bbox is
    // in top-level viewport coordinates. Cross-origin frames cannot be read at
    // all, so they are listed as opaque: the planner can still target the
    // frame itself.
    for (const frame of doc.querySelectorAll("iframe")) {
      if (hiddenReason(frame, win, doc)) continue;
      const rect = frame.getBoundingClientRect();
      const childId = `${frameId}/${out.length}`;
      let childDoc = null;
      try {
        childDoc = frame.contentDocument;
      } catch (err) {
        childDoc = null;
      }

      if (childDoc && frame.contentWindow) {
        collect(
          childDoc,
          frame.contentWindow,
          childId,
          offsetX + rect.left,
          offsetY + rect.top,
          out,
        );
      } else {
        const opaque = describe(frame, win, doc, frameId, offsetX, offsetY);
        opaque.covered = false;
        opaque.cross_origin = true;
        opaque.role = "iframe";
        out.push(opaque);
      }
    }
  }

  // Stale stamps from a previous run would make an old index resolve to the
  // wrong element after the page re-rendered.
  for (const doc of [document]) {
    for (const stamped of doc.querySelectorAll("[data-agent-idx]")) {
      stamped.removeAttribute("data-agent-idx");
    }
  }
  for (const frame of document.querySelectorAll("iframe")) {
    try {
      const childDoc = frame.contentDocument;
      if (childDoc) {
        for (const stamped of childDoc.querySelectorAll("[data-agent-idx]")) {
          stamped.removeAttribute("data-agent-idx");
        }
      }
    } catch (err) {
      /* cross-origin, nothing to clean */
    }
  }

  const collected = [];
  collect(document, window, "main", 0, 0, collected);

  return collected.map((info, index) => {
    info.element.setAttribute("data-agent-idx", String(index));
    const { element, document: _doc, ...rest } = info;
    return { index, ...rest };
  });
}
