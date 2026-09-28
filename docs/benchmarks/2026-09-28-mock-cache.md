# Benchmark 2026-09-28 (mock)

4 runs, 4 per configuration, against SkyFare on darwin.

## Against the targets

| Metric | Result | Target | |
| --- | --- | --- | --- |
| End-to-end success | 100% | ≥80% | PASS |
| Recovery success | no data | >70% | no data |
| Verification accuracy | 100% | — | — |
| Median time per task | 5.1s | — | — |
| p90 time per task | 5.4s | — | — |

Success means the task completed *and* reported the fare the oracle says is cheapest. A run that completes with the wrong number counts as a failure, because a confident wrong answer is the one outcome a user cannot detect.

## Everything else

| Metric | Value |
| --- | --- |
| Ran without crashing | 100% |
| Average steps per task | 7.0 |
| Steps that needed rescuing | 0 |
| Recovery attempts made | 0 |
| Vision invocation rate | 0.21 per step |
| Approvals requested | 0 |
| Cache hit rate | 75% |
| Tokens in / out | 1272 / 0 |
| Estimated cost | 0.0318 INR |

## By configuration

| Chaos | Runs | Correct | Clean | Needed rescue | Rescued | Attempts | Median | Steps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `none` | 4 | 4 | 4 | 0 | 0 | 0 | 5.1s | 7.0 |

## How to read this

Provider `mock`, prices 25.0/100.0 INR per million tokens in and out. Cost is derived from those settings rather than from anything written into the script.

The mock provider is rule-based, so these numbers measure the loop, the verifier and the recovery ladder rather than a model's judgement. Vision always declines under the mock, which is the correct degradation path but makes the vision figures meaningless here.
