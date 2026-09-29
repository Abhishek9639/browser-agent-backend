# ADR-004: Verification is deterministic and never calls the model

- **Status:** accepted, implemented
- **Date:** 2026-09 (part B20)
- **Spec:** section 4.7, which cites this ADR by name

## Context

After an action, something has to decide whether it worked. The obvious cheap
option is to ask the model: it has the page, it has the goal, and it is already
being paid for.

## Decision

Nothing in `app/agent/verification/verifier.py` calls a model. Every check is a
comparison between two `PageState` snapshots — the one the decision was made
against and the one after the action — and every comparison is reproducible from
the two rows that get stored.

Eleven checks, mapped onto the six storable outcome types from spec section 4.5,
with the precise check recorded in `actual["check"]` so the enum column does not
have to grow for a detail that belongs in JSON.

The planner's declared expectation is used **only when it is usable**. A
`text_present` with an empty value passes on every page in the world, so the
verifier substitutes the default for that action instead.

## Consequences

**Good.** A step is arguable after the fact from the rows alone. A model that has
been persuaded by the page cannot also be the thing that certifies the result.
The check is free and instant.

**Bad.** A deterministic check cannot see meaning. The agent can fill the right
field with the wrong value and pass `value_equals`; it can navigate to a page that
looks correct and answer the wrong question. That class of error is caught by the
benchmark comparing against SkyFare's oracle, not by the verifier.

**The reasoning, stated once.** A model that is wrong about what a click did is
exactly the model that will confidently say it worked. Letting it mark its own
homework converts one error into a silent one, and a silent wrong answer is the
only failure a user cannot detect.

## Verification

`tests/unit/test_verifier.py`, including
`test_the_verifier_never_reaches_for_a_model`, which reads the module's own source and
fails if the word appears: a comment saying "does not call the LLM" is not
enforcement. `tests/unit/test_verification_accuracy.py` scores 18 labelled cases
at precision 1.00, recall 1.00.
