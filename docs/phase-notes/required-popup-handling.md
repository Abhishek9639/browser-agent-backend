# Required popups before background actions

## Scope and cause

This change only addresses the reported popup loop. A language/format dialog
opens after Book tickets; the old overlay cleanup could close it with Close or
Escape, then click Book tickets again without ever selecting the required
options. Cached steps could repeat the same background click.

## Implementation

- Share visible modal detection between perception, scrolling and the driver.
  Recognize accessible dialogs and plain panels above a full-screen backdrop.
- Mark background controls as covered and identify controls inside the popup.
  Give the planner the popup text and its available control indices first.
- Required/unknown popups are not automatic dismissal candidates. Explicit
  optional/consent dismissals remain supported.
- Reject background click/type/select proposals while a popup is active, even
  on the planner's final repair turn. Grounding cannot override that boundary.
- The driver refuses stale background targets before Escape, scrolling or
  clicking. Recovery cannot close a required popup just to retry that target.
- Stop cached replay at a visible popup and return to planning. If a required
  popup appears between planning and acting, re-read it instead of recovering
  the same background click.

No frontend, provider, authentication, database, billing or local environment
configuration changes are included.

## Verification

A fixture reproduces the loop: Book tickets opens a required language/format
popup; Close/Escape dismiss it without selecting anything, so the same button
reopens it. Tests cover semantic dialogs and plain div/backdrop versions,
rejecting a stale background click, filling the popup and proceeding only after
selection. A scripted orchestrator regression covers a cached background click
and a rejected planner proposal, with no Close or Escape action.

The initial live Chromium/Gemini run executed the intended order: Book tickets,
fill people count, select English 3D, then Book tickets after the popup closed.
Its final answer was `Screenings ready: English 3D for 2` (task
`9b7b6d02-eb37-45ef-8733-225ec333000c`). This used the representative local
fixture with real Chromium and Gemini; no real ticket booking was made.

Verification before pushing:

- Focused Chromium regressions: **5 passed** (11.04 seconds), including semantic
  and plain popups, background click protection, filling and scoped scrolling.
- Non-browser unit suite: **1,238 passed**, 205 deselected. One existing
  Starlette/AnyIO deprecation warning remains.
- Ruff lint and format checks passed; strict mypy passed for 125 source files.
- `git diff --check` passed.

Full GitHub CI on implementation commit `b0fe06b` also passed all seven jobs:
[verification run](https://github.com/Abhishek9639/browser-agent-backend/actions/runs/45941940297).
The suites passed **1,814 tests**: 1,442 unit (one live-provider test skipped),
348 integration and 24 chaos. The benchmark, Docker build/startup smoke test,
lint/typecheck, dependency/secret audit and final quality gate passed as well.

## Viva questions

1. Why is Escape not always a safe recovery step?
   It can cancel required choices. Repeating the triggering button then
   recreates the same dialog rather than making progress.
2. Why guard both the planner and the driver?
   Planner validation prevents bad proposals, while the driver handles stale
   cached targets and popups that appear after the page was perceived.
3. Why stop cache replay at a popup?
   A previous sequence does not prove that today's popup has the same choices
   or already contains the user's required input. The planner must read it.
