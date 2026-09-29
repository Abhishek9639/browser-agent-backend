# Traceability

Requirement → where it is implemented → what it writes → the test that proves it.
Every path and every test name in this document is real; a script at the end of
part B31 checked all of them, and anything missing a test is flagged rather than
quietly omitted.

## A note on the numbering

`docs/PROJECT_SPEC.md` cites exactly two requirement ids by name: **BR-008** for
human-in-the-loop approval, in the section 1 conflict table, and **ADR-004** in
section 4.7. Decision A18 in `docs/BACKEND_PLAN.md` settled what that means — the
specification's text is the whole requirement and the citations are provenance.

The numbered lists BR-001…BR-010 and FR-1…FR-18 are therefore **assigned here**,
derived from the specification's own text, with BR-008 pinned to the requirement
the specification names. They are stable from this point: the ids are referenced
from `docs/viva-prep.md` and the release notes.

## Business requirements

| ID | Requirement | Spec | Status |
|---|---|---|---|
| BR-001 | A user states a goal in plain language and the agent pursues it in a real browser | §0, §4.2 | Implemented |
| BR-002 | The user watches each step as it happens, with a screenshot and the reasoning | §6.1, §9 | Implemented |
| BR-003 | Every run is auditable afterwards: steps, decisions, verifications, recovery attempts | §5 | Implemented |
| BR-004 | The agent recovers from ordinary page breakage without a human | §4.8, §9 | Implemented |
| BR-005 | The agent never reports success it has not verified | §4.7 | Implemented |
| BR-006 | The LLM is swappable; no provider is load-bearing | §3, §4.1 | Implemented |
| BR-007 | Repeating a task is cheaper than doing it the first time | §4.12 | Implemented |
| BR-008 | Anything irreversible waits for human approval | §1, §4.10 | Implemented |
| BR-009 | Usage is metered and billed in credits | §7 | Client side implemented; payments service not built |
| BR-010 | The system is safe to run: sandboxed, no secrets in logs, only automation-permitted sites | §4.13, §10 | Implemented |

### BR → FR

| BR | Delivered by |
|---|---|
| BR-001 | FR-3, FR-9, FR-10, FR-11 |
| BR-002 | FR-7, FR-8 |
| BR-003 | FR-5, FR-13, FR-14, FR-17 |
| BR-004 | FR-12, FR-14 |
| BR-005 | FR-13 |
| BR-006 | FR-11 |
| BR-007 | FR-16 |
| BR-008 | FR-15 |
| BR-009 | FR-18 |
| BR-010 | FR-1, FR-2, FR-6, FR-8, FR-19 |

FR-19 is the ops and hardening group; the specification does not number it, and it
is listed below with the rest.

## Functional requirements

Each row: the requirement, the modules that implement it, the tables it writes,
and the tests that prove it. `TR` is the test reference, either one of the
specification's ten ids or the suite that covers it.

### FR-1 — Registration and login with RS256 JWTs, refresh rotation

- **Endpoints:** `POST /api/v1/auth/register`, `/login`, `/refresh`, `/logout`
- **Modules:** `app/api/v1/auth.py`, `app/services/auth.py`, `app/core/security.py`, `app/core/jwt_keys.py`
- **Tables:** `users`, `refresh_tokens`, `audit_events`
- **TR:** `tests/integration/test_auth_api.py::test_the_password_is_never_stored_in_clear`, `tests/integration/test_auth_session.py` (rotation and reuse detection), `tests/unit/test_security.py`

### FR-2 — Role-based access control

- **Modules:** `app/api/deps.py` (`require_roles`), `app/domain/enums.py` (`UserRole`)
- **Tables:** `users.role`
- **TR:** **TEST-008** — `tests/integration/test_tasks_api.py::test_someone_else_cannot_approve_your_task`, `::test_a_submitter_without_the_role_is_403_when_owners_may_not_approve`

### FR-3 — Submit a task

- **Endpoint:** `POST /api/v1/tasks`
- **Modules:** `app/api/v1/tasks.py`, `app/services/task_service.py`, `app/schemas/task.py`
- **Tables:** `tasks`, `audit_events`, `billing_outbox` (when billing is on)
- **TR:** **TEST-001** — `tests/integration/test_tasks_api.py::test_a_valid_submission_is_accepted_and_queued`; **TEST-002** — `::test_an_instruction_is_stored_normalised` and `tests/integration/test_security.py::test_an_instruction_of_only_control_characters_is_rejected`

### FR-4 — List, filter and paginate tasks

- **Endpoint:** `GET /api/v1/tasks`
- **Modules:** `app/api/v1/tasks.py`, `app/repositories/task.py`, `app/repositories/base.py`
- **Tables:** `tasks`
- **TR:** `tests/integration/test_repositories_task.py`, `tests/integration/test_security.py::test_pagination_is_capped`, `::test_an_unknown_status_filter_is_rejected`, `::test_a_search_term_with_wildcards_is_matched_literally`

### FR-5 — Task detail, step list, step detail

- **Endpoints:** `GET /api/v1/tasks/{id}`, `/steps`, `/steps/{stepId}`, `/recovery-attempts`
- **Modules:** `app/api/v1/tasks.py`, `app/services/task_service.py`
- **Tables:** `task_steps`, `llm_decisions`, `verifications`, `recovery_attempts`
- **TR:** `tests/integration/test_orchestrator.py::test_the_database_holds_a_full_record_of_each_step`, `tests/integration/test_tasks_api.py`

### FR-6 — Cancel a running task

- **Endpoint:** `POST /api/v1/tasks/{id}/cancel`
- **Modules:** `app/api/v1/tasks.py`, `app/services/task_runner.py`, `app/agent/orchestrator.py`
- **Tables:** `tasks.status`
- **TR:** `tests/integration/test_tasks_api.py` (cancel through the real runner), `tests/unit/test_orchestrator.py::test_a_cancel_between_iterations_stops_the_loop`

### FR-7 — Live stream with replay

- **Endpoints:** `POST /api/v1/tasks/{id}/stream-ticket`, `WS /api/v1/tasks/{id}/stream`
- **Modules:** `app/api/v1/stream.py`, `app/services/stream.py`, `app/services/events.py`, `app/services/ws_tickets.py`
- **Tables:** `task_steps` (replay is rebuilt from rows)
- **TR:** `tests/integration/test_stream.py`, `tests/unit/test_events.py`, `tests/unit/test_ws_tickets.py`

### FR-8 — Step screenshots, owner or admin only

- **Endpoint:** `GET /api/v1/tasks/{id}/steps/{stepId}/screenshot`
- **Modules:** `app/services/storage.py`, `app/services/task_service.py`
- **Tables:** `task_steps.screenshot_ref`
- **TR:** `tests/integration/test_security.py::test_a_screenshot_is_served_only_to_its_owner`, `::test_an_admin_can_read_any_screenshot`, `::test_a_step_from_another_task_cannot_be_read_through_your_own`, `tests/unit/test_storage.py`

### FR-9 — The agent loop, with step and time budgets

- **Modules:** `app/agent/orchestrator.py`, `app/agent/budget.py`, `app/services/task_runner.py`, `app/domain/state_machine.py`
- **Tables:** `tasks`, `task_steps`
- **TR:** `tests/unit/test_orchestrator.py`, `tests/unit/test_budget.py`, `tests/integration/test_orchestrator.py::test_running_out_of_steps_leaves_a_consistent_record`, `tests/unit/test_state_machine.py`

### FR-10 — Perception: interactive elements and a compact summary

- **Modules:** `app/agent/perception/dom_extractor.py`, `dom_extractor.js`, `summarizer.py`, `bs4_fallback.py`, `perceive.py`
- **Tables:** none (perception is not persisted; the hash is, on `verifications`)
- **TR:** `tests/unit/test_dom_extractor.py::test_no_hidden_element_is_reported`, `tests/unit/test_summarizer.py`, `tests/unit/test_perceive.py`, `tests/unit/test_bs4_fallback.py`

### FR-11 — LLM planning with structured output, four providers

- **Modules:** `app/agent/planner/planner.py`, `schema.py`, `registry.py`, `prompts/planner_v2.md`, `app/agent/llm/*`
- **Tables:** `llm_decisions`
- **TR:** **TEST-004** — `tests/unit/test_planner.py::test_a_valid_decision_comes_straight_back`; **TEST-006** — `::test_malformed_json_buys_one_repair_then_fails`; `tests/unit/test_llm_providers.py`, `tests/unit/test_llm_factory.py`, `scripts/eval_planner.py`

### FR-12 — Visual grounding on a trigger, degrading to DOM-only

- **Endpoint:** `POST /api/v1/tasks/{id}/ground`
- **Modules:** `app/agent/vision/grounding.py`, `marks.py`, `prompts/vision_v1.md`
- **Tables:** `llm_decisions.used_vision`, `task_steps.bbox`
- **TR:** **TEST-010** — `tests/unit/test_vision.py::test_a_provider_that_is_down_does_not_raise`; `::test_two_identical_buttons_trigger_grounding`, `::test_a_coordinate_outside_the_viewport_is_refused`

### FR-13 — Deterministic verification of every step

- **Modules:** `app/agent/verification/verifier.py`
- **Tables:** `verifications`
- **TR:** `tests/unit/test_verifier.py::test_the_verifier_never_reaches_for_a_model`, `tests/unit/test_verification_accuracy.py::test_verification_precision_and_recall`

### FR-14 — Recovery ladder, bounded, one row per attempt

- **Endpoint:** `GET /api/v1/tasks/{id}/recovery-attempts`
- **Modules:** `app/agent/recovery/strategies.py`, `runner.py`, `routing.py`
- **Tables:** `recovery_attempts`
- **TR:** **TEST-003** and **TEST-005** — `tests/integration/test_orchestrator.py::test_a_page_that_changes_under_the_agent_fails_the_step`; `tests/unit/test_recovery.py` (each rung against a real page), `::test_a_timeout_on_a_visible_element_goes_to_the_overlay_rung_first`

### FR-15 — Risk policy and human approval

- **Endpoints:** `POST /api/v1/tasks/{id}/approve`, `/reject`
- **Modules:** `app/agent/safety/risk.py`, `gate.py`, `app/services/approvals.py`
- **Tables:** `approvals`, `audit_events`, `tasks.status`
- **TR:** **TEST-007** — `tests/chaos/test_chaos.py::test_a_high_risk_action_stops_for_a_human`; `tests/unit/test_risk.py`, `tests/unit/test_approvals.py`, `tests/integration/test_orchestrator.py::test_a_high_risk_action_never_touches_the_page`

### FR-16 — Flow cache and replay

- **Endpoints:** `GET /api/v1/tasks/{id}/cache-match`, `POST /api/v1/tasks/{id}/replay`
- **Modules:** `app/agent/cache/signature.py`, `flow.py`, `store.py`
- **Tables:** `cached_flows`, `tasks.cache_flow_id`
- **TR:** **TEST-009** — `tests/chaos/test_cache.py::test_the_same_task_twice_replays_and_costs_fewer_calls`; `tests/unit/test_cache.py`, `tests/integration/test_cache.py`

### FR-17 — Metrics and statistics

- **Endpoints:** `GET /metrics`, `GET /api/v1/stats/overview`
- **Modules:** `app/core/metrics.py`, `app/services/stats.py`, `app/repositories/stats.py`
- **Tables:** reads `tasks`, `task_steps`, `llm_decisions`, `recovery_attempts`, `approvals`
- **TR:** `tests/unit/test_metrics.py::test_no_metric_is_labelled_by_anything_unbounded`, `tests/integration/test_observability.py`

### FR-18 — Credits: reserve, settle, release, grant

- **Modules:** `app/services/billing_client.py`, `billing.py`, `app/domain/credits.py`, `app/models/billing.py`
- **Tables:** `billing_outbox`, `tasks.credits_reservation_id`, `tasks.credits_used`
- **TR:** `tests/unit/test_billing_client.py`, `tests/unit/test_credits.py`, `tests/integration/test_billing.py`
- **Note:** the payments service itself is a separate repository and is not built. What is tested here is this side of the contract against a fake payments app.

### FR-19 — Ops and hardening

- **Endpoints:** `GET /api/v1/health`, `GET /.well-known/jwks.json`
- **Modules:** `app/core/security_headers.py`, `app/core/middleware.py`, `app/core/rate_limit.py`, `app/core/redaction.py`, `app/core/logging.py`, `app/api/ops.py`
- **Tables:** `audit_events`
- **TR:** `tests/unit/test_security_headers.py`, `tests/unit/test_log_hygiene.py::test_a_whole_run_logs_nothing_forbidden`, `tests/unit/test_rate_limit.py`, `tests/integration/test_security.py`, `tests/unit/test_health.py`

## The specification's ten test ids

| ID | Scenario | Test | Suite |
|---|---|---|---|
| TEST-001 | Valid task submission | `test_a_valid_submission_is_accepted_and_queued` | integration |
| TEST-002 | Empty or invalid instruction | `test_an_instruction_is_stored_normalised` | integration |
| TEST-003 | Target element not found | `test_a_page_that_changes_under_the_agent_fails_the_step` | integration |
| TEST-004 | Valid page state → valid action | `test_a_valid_decision_comes_straight_back` | unit |
| TEST-005 | Verification fails → recovery logged | `test_a_page_that_changes_under_the_agent_fails_the_step` | integration |
| TEST-006 | Malformed JSON from the model | `test_malformed_json_buys_one_repair_then_fails` | unit |
| TEST-007 | High-risk action detected | `test_a_high_risk_action_stops_for_a_human` | chaos |
| TEST-008 | Unauthorised approval attempt | `test_someone_else_cannot_approve_your_task` | integration |
| TEST-009 | Duplicate signature → replay | `test_the_same_task_twice_replays_and_costs_fewer_calls` | chaos |
| TEST-010 | LLM or vision unavailable | `test_a_provider_that_is_down_does_not_raise` | unit |

All ten map to at least one automated test, which is what section 11 requires.
TEST-004 and TEST-005 were covered by tests that did not cite the id; the
citations were added to those tests in this part, so the mapping is now greppable
rather than only claimed here.

## Requirements with no test, or with a weak one

Flagged rather than omitted.

| Requirement | Gap | Why it is like this |
|---|---|---|
| BR-009 / FR-18 | No test against a real payments service | It is a separate repository and is not built. `tests/unit/test_billing_client.py` implements a fake that answers the documented contract, which tests this side and cannot test theirs. |
| FR-11, prompt quality | The prompt eval cannot distinguish prompt versions | The mock provider ignores the system prompt, so v1 and v2 score identically by construction. Recorded in the B24 phase note; the real comparison needs a live provider. |
| FR-12, vision quality | No test asserts a vision model picks the right mark | Every vision test runs under the mock, which always declines. Degradation is tested; judgement is not. |
| FR-10, shadow DOM | Not traversed | A recorded limitation from part B10. `demo_sites/skyfare` does not use shadow DOM, so nothing in the suite exercises it either way. |
| §9, live-provider KPIs | Measured under the mock | `docs/benchmarks/2026-09-28-mock.md` is the headline. The one live attempt, `2026-09-29-gemini.md`, produced no usable measurement because the provider returned 503 on every call. |
| FR-19, WebSocket frame size | No limit and no test | The HTTP body limit does not cover a WebSocket. Recorded in the B28 phase note. |

## How this document is kept honest

It is maintained by hand and nothing fails when a test is renamed, which is the
same weakness `docs/security.md` has and worth stating in both places. What was
done instead, at the end of part B31, is a script that extracted every
`file::test_name` and every module path from this document and checked each one
exists. It found and fixed several stale names. Running that check again is a
matter of re-running the same extraction over this file.
