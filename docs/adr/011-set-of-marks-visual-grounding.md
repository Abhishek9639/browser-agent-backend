# ADR-011: Set-of-marks for visual grounding, not raw coordinates

- **Status:** accepted, implemented
- **Date:** 2026-09 (part B22)
- **Spec:** section 4.9

## Context

Sometimes the DOM is not enough: two controls share an accessible name, a page is
clearly interactive and lists almost nothing, or the layout carries meaning the
text does not. A vision model can look at the screenshot, and the question is what
to ask it for.

Asking "where should I click" returns a pixel coordinate. A coordinate is a number
with no relationship to anything the rest of the system knows about: it cannot be
verified against the element list, it cannot be stored as a selector, and if the
page has moved by a few pixels it is simply wrong.

## Decision

Draw numbered boxes over the interactive elements the DOM already found — the
set-of-marks technique — using Pillow, and ask the model **which number** is the
target. The answer is an index into the element list the planner was already
looking at, so it is validated against that list and refused if it names something
that is not there.

Grounding runs only when a trigger fires (`GroundReason`), and **acceptance
depends on which trigger it was**: a correction is cheap when the planner had no
idea (`PLANNER_ASKED`, floor 0.0) and expensive when two controls collide
(`DUPLICATE_NAMES`, floor 0.7). A cheap question gets a cheap answer accepted; an
expensive one has to be confident.

Vision failing degrades to DOM-only and never fails the task.

## Consequences

**Good.** The answer is in the same vocabulary as everything else: an element
index, which becomes a stamped selector, which the verifier can check. A model that
picks a number that does not exist is refused rather than clicked. The per-trigger
floors mean the feature helps where the DOM was genuinely ambiguous and stays out
of the way otherwise.

**Bad.** A control the DOM never found gets no mark, so set-of-marks cannot rescue
a canvas-rendered button — the case where raw coordinates would have been the only
option. `click_at` exists for that and is bounds-checked, and it is the one path
where a model's number reaches the mouse. Annotating costs a Pillow pass per
grounded step, and a screenshot is the expensive half of a vision call.

**Bad, and worth saying.** The vision model reads a page that can carry text aimed
at the agent. Grounding can only return an index, so the worst a hostile page can
do through it is point at a different one of its own controls — which the risk
policy and the approval gate then see exactly as they would any other target.

## Verification

`tests/unit/test_vision.py` (marks drawn over the real elements, index validation,
the per-trigger acceptance floors, degradation when vision is down), and the
`iframe` and `shuffle` chaos configurations.
