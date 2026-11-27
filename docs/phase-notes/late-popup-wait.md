# Popups that appear after the page has settled

## Scope and cause

This change only addresses one reported behaviour: on BookMyShow the agent kept
clicking a movie card instead of choosing a city in the location picker that
covered it.

Perceiving the page with the real driver shows the cause. Right after load the
summary has no popup at all: `modal_open` is false, none of the city controls
are listed, and the movie cards are plain targets. One second later the same
page reports `modal_open` true, the city controls in the popup and every card
covered. The site builds the page, goes quiet, and only then puts the picker up
on a timer.

`settle()` treats "quiet for 300 ms" as finished, so on such a page it returns
before the popup exists. The first read after a start URL is not settled at
all. Either way the planner chooses a click on a page that, as far as it knows,
has no popup. The card is covered by the time the click runs, and recovery
re-clicks it until the step times out.

Modal detection, scoping and the planner's popup rule were all working: once the
popup is in the page they behave as intended. Only the moment of reading was
wrong.

Sending a screenshot to the model on every step was considered and not done. A
screenshot taken at the same moment would not contain the popup either, it
would add a vision call to every step, and spec sections 4.6 and 4.9 make the
DOM summary the planner's input with vision used on specific triggers.

## Implementation

- `await_late_overlays()` in `app/agent/browser/stability.py`. On a document
  younger than `AGENT_LOAD_POPUP_GRACE_MS` (default 2000) it polls for a popup
  until the document reaches that age, and returns as soon as one appears, after
  one quiet period for it to finish drawing.
- Age is the page's own `performance.now()`, measured from the load the popup is
  timed against. A document that has been up longer than the grace costs a single
  round trip, which is nearly every read after the first on a page. A slow page
  that took longer than the grace to settle pays nothing extra.
- "A popup appeared" is the shared detector in `modal_scope.js`, passed in as
  source, so the wait agrees with perception's `modal_open`. `stability.py` keeps
  no dependency on perception.
- `DriverBrowser.perceive()` calls it before every read. That covers the first
  read after a start URL and the read after each action with one call site.
- The setting is in `app/core/config.py` and `.env.example`. 0 turns it off.

No frontend, provider, authentication, database, billing or local environment
configuration changes are included.

## Verification

A fixture, `late_popup.html`, builds a page of cards, goes quiet, then shows a
city picker after a delay. It is the plain panel inside a dimmed backdrop, with
no dialog role, which is the harder of the two shapes for the detector.

Before the change, a live run on that page (Gemini, real Chromium) planned a
click on the covered card as its first step and was still recovering two minutes
later. After it, the same task clicked Mumbai in the popup first and the movie
card second, both verified (task `8b506c82-0191-4768-abec-b56b75008cef`).

On BookMyShow itself, through the frontend with no start URL, the agent
navigated, then clicked Mumbai with the reasoning "I need to select a city from
the location popup before I can search for showtimes". The run later stopped on
the site's own "Search currently unavailable" message. No booking was made.

Verification before pushing:

- Stability tests: **12 passed**, 6 of them new. A pair runs the agent browser's
  real read path on the fixture with the grace set to 0 and to 2000: the first
  reads past the popup, the second sees it. The others reproduce the miss with
  `settle()` alone, show the wait finding the popup, and bound its cost on an old
  page and on a new page with no popup.
- Unit suite: **1,448 passed**, 1 skipped, 1 failed. The failure is
  `test_a_site_that_refuses_this_browser_is_tried_in_another`, which needs the
  Edge or Firefox Playwright browsers. They are not installed on the machine
  this ran on, and the test fails the same way with this change removed.
- Integration suite against the separate test database: **348 passed** (1 minute
  53 seconds).
- Ruff lint and format checks passed; strict mypy passed for 125 source files.

## Viva questions

1. Why is waiting for the page to go quiet not enough?
   A site can schedule a popup after it has finished loading. The page is quiet
   and not yet finished, so settling returns before the popup exists.
2. Why measure the wait from the document's own clock?
   The popup is timed from the page load, not from when the agent happens to look.
   Measuring from load makes every read of an older page free and keeps the extra
   wait on a new page bounded by the grace.
3. Why not send a screenshot to the model on every step instead?
   It would be taken at the same moment and miss the popup too. It would also
   add a vision call to every step, against the spec's DOM-first design.
