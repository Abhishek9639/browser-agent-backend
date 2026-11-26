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
- A second live run is checking the final year/source guidance. Full browser
  CI is being rerun. Results will be recorded after completion.

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
