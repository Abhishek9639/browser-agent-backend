// Find the foremost visible modal without relying on a site's class names.
(doc) => {
  const win = doc.defaultView;
  const controls = 'button,a[href],input:not([type="hidden"]),select,textarea,[role="button"],[role="option"],[role="radio"],[onclick]';
  const visible = el => {
    const r = el.getBoundingClientRect(), s = win.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
      r.top < win.innerHeight && r.left < win.innerWidth &&
      s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) !== 0 &&
      !el.closest('[hidden],[aria-hidden="true"]');
  };
  const painted = el => {
    if (!visible(el)) return false;
    const r = el.getBoundingClientRect();
    const x = Math.max(0, Math.min(win.innerWidth - 1, r.left + r.width / 2));
    const y = Math.max(0, Math.min(win.innerHeight - 1, r.top + r.height / 2));
    const top = doc.elementFromPoint(x, y);
    return top && (top === el || el.contains(top));
  };
  const candidates = [...doc.querySelectorAll('[role="dialog"],[role="alertdialog"],[aria-modal="true"],dialog[open]')].filter(painted);
  // Many booking sites use a plain div panel above a fixed, full-screen veil.
  // A sticky header, small calendar or autocomplete does not meet this test.
  for (const layer of doc.querySelectorAll('div,section,aside')) {
    const s = win.getComputedStyle(layer), r = layer.getBoundingClientRect();
    const rgba = s.backgroundColor.match(/rgba\([^,]+,[^,]+,[^,]+,\s*([\d.]+)\)/);
    const dimmed = (rgba && Number(rgba[1]) > 0 && Number(rgba[1]) < 1) ||
      (Number(s.opacity) > 0 && Number(s.opacity) < 1) ||
      (s.backdropFilter && s.backdropFilter !== 'none');
    if (!['fixed', 'absolute'].includes(s.position) || !dimmed ||
        s.pointerEvents === 'none' || !visible(layer) ||
        r.width < win.innerWidth * .8 || r.height < win.innerHeight * .8 ||
        !(Number.parseInt(s.zIndex, 10) > 0)) continue;
    const top = doc.elementFromPoint(win.innerWidth / 2, win.innerHeight / 2);
    if (!top) continue;
    if (layer.contains(top) && layer.querySelector(controls)) {
      candidates.push(layer);
    } else {
      // A separate panel can be a sibling of the veil.
      for (let panel = top; panel && panel !== doc.body; panel = panel.parentElement) {
        const ps = win.getComputedStyle(panel);
        if (['fixed', 'absolute'].includes(ps.position) && panel.querySelector(controls) &&
            Number.parseInt(ps.zIndex, 10) >= Number.parseInt(s.zIndex, 10) && painted(panel)) {
          candidates.push(panel);
          break;
        }
      }
    }
  }
  // Prefer the inner dialog when an enclosing veil also qualified.
  return candidates.find(el => !candidates.some(other => other !== el && el.contains(other))) || null;
}
