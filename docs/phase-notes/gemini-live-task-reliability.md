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
- Local fixture and SkyFare live rechecks, followed by GitHub CI, are in progress.
- No real ride has been requested and no payment has been made.

## Viva questions

1. Why are static page results needed? Interactive controls describe actions,
   but prices and confirmation messages often have no clickable element.
2. Why use an explicit completion flag? Ending an agent loop and achieving its
   goal are different outcomes; parsing English failure phrases is unreliable.
3. Why suppress provider exception context? Vendor errors can echo request
   content or credentials; a translated error should expose only safe guidance.
