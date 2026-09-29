# ADR-007: A mock LLM provider, in the application rather than in the tests

- **Status:** accepted, implemented
- **Date:** 2026-08 (part B12)
- **Spec:** sections 3 and 11

## Context

Every test that exercises the loop needs a model. Real providers cost money, need
a key, rate-limit, and answer differently each time — which makes them unusable as
a regression gate. Mocking at the call site instead means every test invents its
own fake and none of them agree.

## Decision

`app/agent/llm/mock.py`, a first-class provider behind the same `LLMClient`
protocol as Anthropic, OpenAI and Gemini, selected by `LLM_PROVIDER=mock`, which
is the **default**.

Two modes. **Scripted** returns decisions from a queue, for tests that state what
the model says and assert on what the loop does with it. **Rule-based** reads the
goal and the compact page summary and picks something sensible, which is what lets
the whole loop run end to end — perceive, decide, act, verify, recover — with no
key and no quota.

It can also be told to fail, or to answer with malformed JSON, which is how the
retry policy and the repair path are tested.

## Consequences

**Good.** `make check` needs no key. CI needs no secret and cannot be broken by
somebody else's rate limit. The chaos suite runs ten configurations for nothing.
The rule-based mode consumes exactly what a real model is shown, so a change to
the summary format that would confuse a model breaks these tests too.

**Bad.** The mock is not a model, and the numbers it produces measure the loop
rather than any judgement. The prompt eval cannot distinguish prompt versions
under it, which is why B24's v1-against-v2 comparison was identical by
construction and had to be reported as unvalidated. Its rules have been tuned for
the SkyFare flow, so its 32% action accuracy on the wider eval set is its own
ceiling and not a regression.

**Consequence that mattered.** Because the default is `mock`, a deployment that
forgets to configure a provider gets a rule-based agent rather than an error. That
was judged unacceptable and the factory now refuses `mock` in production.

## Verification

`tests/unit/test_llm_mock.py`, and the mock is the provider under which the whole
of `tests/unit/`, `tests/chaos/` and the headline benchmark run.
