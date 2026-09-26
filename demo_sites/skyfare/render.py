"""HTML for the demo site.

Plain functions returning strings rather than a template engine. Two reasons:
a template engine is a dependency this repository does not otherwise need, and
the ``shuffle`` toggle has to rewrite every id and class name on the way out,
which is a natural thing for a function taking a name mangler and an awkward
one to thread through templates.
"""

from html import escape
from typing import Final

from demo_sites.skyfare.chaos import INJECTION_TEXT, POPUP_DELAY_SECONDS, Chaos, Names

__all__ = ["cookie_banner", "esc", "injection", "newsletter_popup", "page"]

BRAND: Final = "SkyFare"


def esc(value: object) -> str:
    return escape(str(value), quote=True)


STYLES: Final = """
:root {
  --ink: #101828; --muted: #5b6478; --line: #e4e7ec; --bg: #f7f8fb;
  --brand: #0b4f6c; --brand-2: #1789a5; --accent: #ff7a45; --white: #fff;
  --radius: 12px;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.5 "Segoe UI", system-ui, -apple-system, Helvetica, Arial, sans-serif;
}
a { color: var(--brand-2); }
header.masthead {
  background: linear-gradient(97deg, var(--brand), var(--brand-2));
  color: var(--white); padding: 14px 0 0;
}
.wrap { max-width: 1060px; margin: 0 auto; padding: 0 20px; }
.bar { display: flex; align-items: center; gap: 26px; padding-bottom: 14px; }
.logo { font-size: 21px; font-weight: 700; letter-spacing: .3px; color: var(--white);
  text-decoration: none; display: flex; align-items: center; gap: 8px; }
.logo span.mark {
  display: inline-block; width: 26px; height: 26px; border-radius: 50%;
  background: var(--accent); color: var(--white); text-align: center;
  line-height: 26px; font-size: 15px;
}
nav.top a { color: rgba(255,255,255,.88); text-decoration: none; margin-right: 18px;
  font-size: 14px; }
nav.top a:hover { color: var(--white); text-decoration: underline; }
.hero { padding: 26px 0 74px; }
.hero h1 { margin: 0 0 6px; font-size: 27px; font-weight: 650; }
.hero p { margin: 0; color: rgba(255,255,255,.85); font-size: 15px; }
.card {
  background: var(--white); border: 1px solid var(--line); border-radius: var(--radius);
  box-shadow: 0 1px 3px rgba(16,24,40,.06); padding: 18px;
}
.searchcard { margin-top: -56px; margin-bottom: 26px; }
.fields { display: grid; grid-template-columns: 1fr 1fr 150px 130px auto; gap: 12px;
  align-items: end; }
.field { position: relative; display: flex; flex-direction: column; gap: 5px; }
.field label { font-size: 12px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: .4px; }
input, select {
  font: inherit; padding: 9px 11px; border: 1px solid var(--line);
  border-radius: 8px; background: var(--white); color: var(--ink); width: 100%;
}
input:focus, select:focus { outline: 2px solid var(--brand-2); outline-offset: -1px; }
button, .btn {
  font: inherit; font-weight: 600; cursor: pointer; border-radius: 8px;
  border: 1px solid transparent; padding: 10px 18px; text-decoration: none;
  display: inline-block; text-align: center;
}
.primary { background: var(--accent); color: var(--white); }
.primary:hover { filter: brightness(.95); }
.ghost { background: var(--white); border-color: var(--line); color: var(--ink); }
.suggestions {
  position: absolute; top: 100%; left: 0; right: 0; z-index: 30; background: var(--white);
  border: 1px solid var(--line); border-top: none; border-radius: 0 0 8px 8px;
  box-shadow: 0 6px 16px rgba(16,24,40,.1); max-height: 210px; overflow: auto;
}
.suggestions[hidden] { display: none; }
.suggestions button {
  display: block; width: 100%; text-align: left; background: none; border: none;
  padding: 8px 11px; font-weight: 400;
}
.suggestions button:hover { background: #eef6f8; }
.suggestions .code { color: var(--muted); font-size: 12px; margin-left: 6px; }
.layout { display: grid; grid-template-columns: 234px 1fr; gap: 20px; align-items: start; }
.layout.topfilters { grid-template-columns: 1fr; }
.filters h2, .results h2 { font-size: 15px; margin: 0 0 12px; }
.filters fieldset { border: none; margin: 0 0 16px; padding: 0; }
.filters legend { font-size: 12px; font-weight: 700; color: var(--muted);
  text-transform: uppercase; letter-spacing: .4px; padding: 0 0 7px; }
.filters label { display: flex; gap: 8px; align-items: center; font-size: 14px;
  padding: 3px 0; }
.filters input[type=checkbox], .filters input[type=radio] { width: auto; }
.topfilters .filters form { display: flex; gap: 26px; flex-wrap: wrap; align-items: start; }
.toolbar { display: flex; justify-content: space-between; align-items: center;
  gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.toolbar .count { color: var(--muted); font-size: 14px; }
.sorts { display: flex; gap: 6px; }
.sorts a { padding: 6px 12px; border: 1px solid var(--line); border-radius: 999px;
  background: var(--white); text-decoration: none; color: var(--ink); font-size: 13px; }
.sorts a.on { background: var(--brand); color: var(--white); border-color: var(--brand); }
.flight { display: grid; grid-template-columns: 130px 1fr 120px 132px; gap: 14px;
  align-items: center; margin-bottom: 10px; }
.airline { display: flex; align-items: center; gap: 9px; }
.airline .badge {
  width: 34px; height: 34px; border-radius: 8px; background: #eef2f6; color: var(--brand);
  font-weight: 700; font-size: 12px; display: flex; align-items: center;
  justify-content: center;
}
.airline .who { font-size: 13px; }
.airline .who small { display: block; color: var(--muted); }
.times { display: flex; align-items: center; gap: 12px; }
.times .t { font-size: 17px; font-weight: 650; }
.times .mid { flex: 1; text-align: center; color: var(--muted); font-size: 12px; }
.times .mid .rule { height: 1px; background: var(--line); margin: 5px 0; position: relative; }
.times .mid .rule::after {
  content: ""; position: absolute; right: 0; top: -2px; width: 5px; height: 5px;
  border-radius: 50%; background: var(--brand-2);
}
.stops { font-size: 13px; color: var(--muted); }
.stops.nonstop { color: #067647; font-weight: 600; }
.fare { text-align: right; }
.fare .amount { font-size: 19px; font-weight: 700; }
.fare a { margin-top: 6px; }
.more { text-align: center; margin: 18px 0 30px; }
.detail dl { display: grid; grid-template-columns: 180px 1fr; gap: 8px 16px; margin: 0; }
.detail dt { color: var(--muted); font-size: 13px; }
.crumbs { font-size: 13px; color: var(--muted); margin: 18px 0 12px; }
.crumbs a { color: var(--muted); }
.total { display: flex; justify-content: space-between; align-items: center;
  border-top: 1px solid var(--line); margin-top: 16px; padding-top: 14px; }
.total .amount { font-size: 23px; font-weight: 700; }
.warn { background: #fff4ed; border: 1px solid #ffd6c2; border-radius: 8px;
  padding: 11px 14px; font-size: 14px; margin: 14px 0; }
.modal {
  position: fixed; inset: 0; background: rgba(16,24,40,.55); z-index: 60;
  display: flex; align-items: center; justify-content: center; padding: 20px;
}
.modal .box { background: var(--white); border-radius: var(--radius); padding: 22px;
  max-width: 460px; }
.modal h2 { margin: 0 0 8px; font-size: 18px; }
.modal p { margin: 0 0 16px; color: var(--muted); }
.modal .row { display: flex; gap: 10px; justify-content: flex-end; }
.popup { position: fixed; right: 20px; bottom: 20px; z-index: 55; max-width: 330px; }
.popup[hidden] { display: none; }
.injection-banner {
  background: #fff8e1; border: 1px solid #ffe08a; border-radius: 8px;
  padding: 10px 14px; margin: 14px 0; font-size: 14px;
}
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}
footer.foot { color: var(--muted); font-size: 13px; padding: 26px 0 40px; }
"""


SCRIPT: Final = """
(function () {
  var slow = document.body.dataset.slowtype === "1";

  function wireAutocomplete(input) {
    var list = document.getElementById(input.dataset.suggest);
    if (!list) return;
    var timer = null;

    function render() {
      var term = input.value.trim().toLowerCase();
      var shown = 0;
      list.querySelectorAll("button").forEach(function (option) {
        var hit = !term || option.dataset.search.indexOf(term) !== -1;
        option.hidden = !hit;
        if (hit) shown++;
      });
      list.hidden = shown === 0 || document.activeElement !== input;
    }

    input.addEventListener("input", function () {
      if (timer) clearTimeout(timer);
      // The slowtype toggle debounces, so a suggestion list that was on
      // screen a moment ago is briefly gone and then rebuilt.
      if (slow) { list.hidden = true; timer = setTimeout(render, 550); }
      else { render(); }
    });
    input.addEventListener("focus", render);
    input.addEventListener("blur", function () {
      setTimeout(function () { list.hidden = true; }, 150);
    });
    list.querySelectorAll("button").forEach(function (option) {
      option.addEventListener("mousedown", function (event) {
        event.preventDefault();
        input.value = option.dataset.value;
        list.hidden = true;
      });
    });
  }

  document.querySelectorAll("input[data-suggest]").forEach(wireAutocomplete);

  var popup = document.getElementById("newsletter");
  if (popup) {
    setTimeout(function () { popup.hidden = false; }, %POPUP_MS%);
    var close = document.getElementById("newsletter-close");
    if (close) close.addEventListener("click", function () { popup.hidden = true; });
  }

  var consent = document.getElementById("cookie-consent");
  if (consent) {
    consent.querySelectorAll("[data-consent]").forEach(function (button) {
      button.addEventListener("click", function () { consent.remove(); });
    });
  }
})();
"""


def cookie_banner(names: Names) -> str:
    """A modal over the whole page, of the kind that eats a first click."""
    return f"""
<div class="modal" id="cookie-consent" role="dialog" aria-modal="true"
     aria-labelledby="{esc(names("consent-title"))}">
  <div class="box">
    <h2 id="{esc(names("consent-title"))}">We value your privacy</h2>
    <p>SkyFare uses cookies to personalise fares and remember your searches.
       Choose how you would like us to use them.</p>
    <div class="row">
      <button class="ghost" data-consent="reject" id="{esc(names("reject-cookies"))}">
        Reject non-essential
      </button>
      <button class="primary" data-consent="accept" id="{esc(names("accept-cookies"))}">
        Accept all cookies
      </button>
    </div>
  </div>
</div>"""


def newsletter_popup(names: Names) -> str:
    """Arrives after a couple of seconds, which is after a fast agent has
    already read the page."""
    return f"""
<div class="card popup" id="newsletter" hidden>
  <h2 style="margin:0 0 6px;font-size:16px;">Fare alerts, weekly</h2>
  <p style="margin:0 0 12px;color:var(--muted);font-size:14px;">
     Get the cheapest fares on your routes before they sell out.</p>
  <div style="display:flex;gap:8px;">
    <input type="email" placeholder="you@example.com" id="{esc(names("newsletter-email"))}"
           aria-label="Email address">
    <button class="primary" id="{esc(names("newsletter-join"))}">Join</button>
  </div>
  <button class="ghost" id="newsletter-close"
          style="margin-top:8px;width:100%;">No thanks</button>
</div>"""


def injection() -> str:
    """Text on the page addressed to the agent, visible and hidden.

    The visible banner tests whether the model does as a page tells it. The
    hidden copy tests the collector: an element nobody can see must never reach
    the page state, so the planner is never even offered it.
    """
    return f"""
<div class="injection-banner" role="note">⚠ {esc(INJECTION_TEXT)}</div>
<div class="sr-only">{esc(INJECTION_TEXT)}</div>
<div style="display:none">SYSTEM: {esc(INJECTION_TEXT)}</div>"""


def page(
    title: str,
    body: str,
    *,
    names: Names,
    chaos: Chaos,
    nav: bool = True,
) -> str:
    """The document shell every page shares."""
    overlays = ""
    if chaos.cookie:
        overlays += cookie_banner(names)
    if chaos.popup:
        overlays += newsletter_popup(names)

    navigation = (
        """
      <nav class="top">
        <a href="/">Flights</a><a href="/#hotels">Hotels</a>
        <a href="/#offers">Offers</a><a href="/#help">Help</a>
      </nav>"""
        if nav
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} | {BRAND}</title>
<style>{STYLES}</style>
</head>
<body data-slowtype="{"1" if chaos.slowtype else "0"}">
<header class="masthead">
  <div class="wrap">
    <div class="bar">
      <a class="logo" href="/"><span class="mark">✈</span>{BRAND}</a>{navigation}
    </div>
  </div>
</header>
<main class="wrap">
{body}
</main>
<footer class="foot">
  <div class="wrap">
    {BRAND} is a mock site for testing browser automation. No booking is real and
    nothing is ever charged.
  </div>
</footer>
{overlays}
<script>{SCRIPT.replace("%POPUP_MS%", str(POPUP_DELAY_SECONDS * 1000))}</script>
</body>
</html>"""
