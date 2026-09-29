# ADR-002: Structured output with schema validation and exactly one repair turn

- **Status:** accepted, implemented
- **Date:** 2026-08 (parts B12–B14)
- **Spec:** sections 4.5, 11 (TEST-006)

## Context

The planner's answer has to become an action. Free text does not, reliably: a
model that returns prose, or JSON wrapped in a code fence, or a plausible action
name that does not exist, is a model whose output has to be parsed defensively at
every call site.

Three options: parse free text with regular expressions, ask for JSON and hope,
or constrain the output and validate it.

## Decision

Every provider is asked for JSON against a hand-written JSON Schema
(`app/agent/planner/schema.py`), using whatever the provider offers to constrain
it: tool-use for Anthropic, response format for OpenAI, response schema for
Gemini. The answer is then validated into a Pydantic model with cross-field rules
that a schema cannot express — `done` needs a final answer, `switch_tab` needs a
tab id, an action that targets an element needs an index.

A decision that fails validation earns **one** repair turn: the problem is
described back to the model and it is asked again. One, not three: a model that
cannot produce a valid decision twice is not going to on the third attempt, and
each attempt is real money and real latency.

## Consequences

**Good.** Downstream code can assume a valid `Decision`. The repair turn recovers
the common case, which is a missing field rather than a misunderstanding. The
prompt version and the attempt count are recorded per step, so a bad afternoon is
attributable.

**Bad.** The schema is hand-written rather than generated from the Pydantic model,
so the two can drift; a test compares them. Three providers means three ways of
saying the same thing, and a new provider is a new adapter.

## Verification

`tests/unit/test_planner.py` (validation, the repair path, the refusal after two
failures), `tests/unit/test_llm_providers.py` (each adapter constrains output),
`scripts/eval_planner.py` (22 labelled cases).
