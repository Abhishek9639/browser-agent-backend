# ADR-008: A demo site we control, instead of live flight sites

- **Status:** accepted, implemented
- **Date:** 2026-09 (part B18)
- **Spec:** section 12

## Context

The agent has to be tested against something. A real airline site is the realistic
target and is unusable as a test fixture: its prices change, its markup changes
without notice, it rate-limits, it serves CAPTCHAs, and automating it is against
its terms of service. Spec section 10 is explicit that only sites permitting
automation may be used.

There is also no ground truth. Without knowing the right answer, a run can only be
scored on whether it finished, which is the one thing that does not matter.

## Decision

`demo_sites/skyfare/`, a flight search site inside this repository, served on
:8200 as its own ASGI app — never mounted into the API (decision A12), so chaos
behaviour cannot leak into real routes.

Flights are generated deterministically from a seed over the route and date, so
the same search returns the same flights on any machine on any day. Nine chaos
toggles reproduce what real sites do: random delay, cookie modal, newsletter
popup, shuffled class names, a second layout, results in an iframe, details in a
new tab, and hidden prompt-injection text.

An oracle endpoint returns the cheapest fare behind a header the agent is never
given, and answers 404 without it so probing tells the agent nothing.

## Consequences

**Good.** Every run is scored against the right answer, which is what turns
"completed" into "completed correctly" and makes a confident wrong answer a
failure. The chaos toggles are reproducible, so a recovery strategy can be shown
to beat a specific failure rather than to have helped once. No terms of service,
no rate limits, no CAPTCHAs, and nothing to apologise for.

**Bad.** A site we wrote is a site whose quirks we chose. Real pages are worse in
ways nobody thought to simulate: shadow DOM, which is a recorded limitation;
canvas-rendered text; anti-bot fingerprinting. Passing the chaos suite is evidence
about the agent, not a promise about `makemytrip.com`.

## Verification

`tests/unit/test_skyfare.py` (generation, chaos toggles, the oracle's header
gate), `tests/chaos/test_chaos.py` (ten configurations), and
`scripts/benchmark.py`, which scores every run against the oracle.
