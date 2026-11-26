# General web task configuration and failure reporting

## Diagnosis

The user's GSoC research task ended with zero persisted steps and the generic
browser failure sentence. The active local `AGENT_ALLOWED_DOMAINS` setting
contained only localhost and Uber. Checking the same URL policy against
`https://summerofcode.withgoogle.com/` returned false. A missing start URL was
not the problem: the planner can choose an initial site from the instruction.

The local instance now uses the existing wildcard mode for the user's requested
general web browsing. Repository defaults stay restricted to demo hosts. The
HTTP(S), hostname and credential-in-URL checks continue to apply in wildcard
mode. This is a local operator configuration change, not automatic widening
of the domain policy by model output.

## Changes

- Translate `DomainNotAllowedError` before the generic browser-error case.
  The task record and final event explain the website allow-list and how the
  operator can configure it. Never echo the attempted URL or a query token.
- Avoid logging that exception's raw traceback, which includes its URL.
- Add an orchestrator regression covering the persisted reason, final event,
  lack of executed steps, and absence of a private URL in client-visible data.
- In the local frontend checkout, show `failureReason` in expanded session
  history instead of hiding it behind “No result summary available”. This is
  a small companion UI change outside this backend repository.
- Research answers must cite visited sources and distinguish an observed
  older-year listing from unverified future-year participation. The first live
  rerun returned organizations from a 2026 page without clarifying the requested
  2027 year; both planner prompt versions now explicitly cover that case.

## Validation

- 1,576 non-browser unit/integration tests passed (209 browser/live tests
  deselected), followed by 60 planner tests after the research prompt update.
- Ruff lint/format passed for 314 files; strict mypy passed for 125 source files.
- The companion frontend file passed ESLint; the page returned HTTP 200.
- Live Gemini/Chromium task `a619de66-d1be-4590-8087-611a0c81733b` repeated the
  user's exact prompt without a supplied start URL. It completed four verified
  steps in 162 seconds: navigate to GSoC, open the 2026 organizations list,
  dismiss cookies and finish with three suggestions. It used a configured
  fallback model for the initial call and the primary for later calls.
- Final live task `7d1f8eab-4ade-42f8-bcea-5fe2c5519f87` completed four verified
  steps in 131 seconds. Three cached browser actions replayed successfully,
  and a fresh Gemini answer explicitly described 2026 candidates and said
  2027 participation must be checked again. Apache Software Foundation,
  PostgreSQL and Blender Foundation were independently confirmed in the
  official 2026 organizations listing.
- The history display fix is pushed to the frontend branch
  `codex/show-task-failure-reasons` (commit `52f6ce7`).
- [CI on ab49d4e](https://github.com/Abhishek9639/browser-agent-backend/actions/runs/45784044352) passed every job: 1,808 tests (1,436 unit, 348 integration, 24 chaos), with one live-provider test intentionally skipped. Benchmark, Docker build/smoke, lint, formatting, mypy, dependency audit and secret scan all passed. The subsequent handoff commit changes documentation only.
- After the backend process stopped, the user restarted it in normal Terminal.
  The frontend was restarted with Webpack polling to avoid this Mac's file
  watcher limit. Its page returned HTTP 200 and its proxied backend health
  check reported application/database `ok`.

No result here guarantees that arbitrary tasks will complete: provider quotas,
site access controls, account login and task feasibility remain constraints.
Historical failed task records are preserved rather than relabelled as success.

## Viva questions

1. Why did the flight/Uber tests pass but the GSoC task fail?
   The local allow-list covered the demo and Uber hosts, not other websites.
   A working browser driver does not imply permission to navigate every site.
2. Why not return the exception message directly?
   It can include URLs with private query tokens. A fixed, actionable message
   identifies the configuration problem without exposing that data.
3. Why distinguish 2026 candidates from confirmed 2027 participants?
   Successful navigation is not evidence for a claim the visited source does
   not establish. Research answers must preserve that distinction.
