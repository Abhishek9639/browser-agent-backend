# Gemini live-task reliability

## Findings and changes

The real Gemini cab-form run filled both addresses and selected the ride, but its
final answer said it could not read the confirmation. The backend nevertheless
marked it completed. The planner received only interactive elements: ordinary
confirmation text and result prices were missing from its context.

The planner now receives up to 4,000 characters of visible page text alongside
its control summary, inside the same injection-resistant untrusted-data fence.
Planning, clarification and outline creation use the same representation. Page
content is not added to logs.

The provider decision schema now requires `goalAchieved`. False on a terminal
decision produces `EXECUTION_FAILED`, reports the blocker, and cannot cache the
run as a successful flow. Existing stored decisions remain readable. This is a
structured model report, not a guarantee that any model's success claim is true;
step verification and explicit human approval gates still apply.

Invalid or restricted Gemini credentials now receive a clear, non-retryable
error. SDK exception bodies are suppressed from the visible traceback. During
live testing the primary also returned HTTP 503. Configured backup models now
handle transient unavailability, with a short cooldown for the failed model.

## Validation

- Real Gemini text generation passed with the user's locally configured key.
- 1,566 non-browser unit/integration tests passed before the final 503 fallback adjustment.
- 237 focused tests passed after that adjustment; lint, formatting and mypy passed.
- Real Gemini cab fixture passed: both addresses and Demo ride were reported
  from the visible confirmation in six steps. The backup model handled a 503
  from the primary. One click forecast was unverified; the next reading of the
  confirmation let the planner recover and answer correctly.
- Real Gemini SkyFare smoke passed: QP-654 at INR 5,740 matched the oracle,
  with auth/refresh, WebSocket, persisted steps, protected screenshots, history,
  stats, JWKS, logout and refresh revocation all checked.
- CI on `6992996`: lint, unit tests, integration tests and Docker smoke passed.
  Audit flagged a historic dummy token; only that exact placeholder is now
  allow-listed. The chaos recovery case repeated typing until its step budget.
- Random shuffled IDs can contain secret-like substrings (such as `pwd`), which
  conservatively mask ordinary fields. The offline mock now honors verified
  typing history for masked fields. A deterministic browser regression forces
  that ID pattern; masking itself is retained. This reproduces a possible cause
  of the CI loop; the original random ID was not captured.
- 135 mock/orchestrator tests pass after this follow-up. The full CI recheck
  and live Uber pre-booking probe are pending.
- No real ride has been requested and no payment has been made.

## Viva questions

1. Why are static page results needed? Interactive controls describe actions,
   but prices and confirmation messages often have no clickable element.
2. Why use an explicit completion flag? Ending an agent loop and achieving its
   goal are different outcomes; parsing English failure phrases is unreliable.
3. Why suppress provider exception context? Vendor errors can echo request
   content or credentials; a translated error should expose only safe guidance.

## Full CI and the live Uber finding

[CI on d41952c](https://github.com/Abhishek9639/browser-agent-backend/actions/runs/45617652798)
passed all jobs: 1,428 unit tests (one intentional live-provider skip),
348 integration tests and 24 chaos tests. Lint, type checking, dependency/secret
audit, benchmark and Docker build/smoke passed too. Firefox tests pass in Linux;
this does not resolve the previously observed local macOS Firefox issue.

The actual Uber browser probe then showed a separate input-perception problem:
the two native address inputs were 2px wide and 20px high inside full-size
Base Web select widgets. Both were excluded as too small. A temporary local
geometry-only diagnostic established this; it recorded no values or page text
and was removed from the code afterward.

The collector now recognizes this specific auto-sizing input pattern while
retaining the other hidden-control exclusions. A native input's own combobox
role must not hide its enclosing widget during the ancestor lookup. A fixture
covers lookup, actual typing, zero-size, aria-hidden, transparent and unrelated
tiny controls. This follow-up's live and full CI results are recorded below
when complete. No real ride was requested.
