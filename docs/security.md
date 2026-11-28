# Security

Every rule in spec section 10, where it is enforced, and the test that proves
it. Then the prompt-injection layers from section 4.11, in the order a page's
text meets them.

A row here is a claim about the code, so each one names a file and a test by
name. If a test is renamed and this page is not, the page is wrong: it is
checked by hand at the end of each part rather than by a tool, and that is the
one weakness in it.

## Spec section 10, rule by rule

| Rule | Where it is enforced | Test |
|---|---|---|
| Secrets only via env | `app/core/config.py` — every secret is `SecretStr` and defaults to `None`; `.env.example` is kept in sync and holds no values | `test_config.py::test_secrets_do_not_appear_in_repr`, `test_config.py::test_env_example_documents_every_setting` |
| Never log page content | `app/core/redaction.py` masks by key, and no call site passes the summary | `test_log_hygiene.py::test_the_page_summary_is_never_logged`, `::test_a_whole_run_logs_nothing_forbidden` |
| Never log screenshots | `screenshot` is a sensitive key in `SENSITIVE_KEY_PARTS` | `test_log_hygiene.py::test_screenshot_bytes_are_never_logged`, `test_logging.py::test_sensitive_keys_are_masked` |
| Never log credentials or typed passwords | Masked at collection in `app/agent/perception/dom_extractor.py`, then again by the log processor | `test_dom_extractor.py::test_secret_values_are_masked`, `test_log_hygiene.py::test_a_whole_run_logs_nothing_forbidden` |
| Redact password and card-like values everywhere, including DB and WS | `task_steps.value_redacted` and every event carry the masked form (decision A16) | `tests/integration/test_orchestrator.py::test_a_password_typed_into_a_real_page_is_never_stored` |
| Redaction in the static fallback path too | `app/agent/perception/bs4_fallback.py` | `test_bs4_fallback.py::test_password_input_values_are_masked`, `::test_a_card_number_in_body_text_is_masked` |
| RBAC on approvals | `app/services/approvals.py` — APPROVER or ADMIN, or the owner when configured | `tests/integration/test_security.py::test_an_ordinary_user_cannot_answer_an_approval_they_do_not_own`, `tests/integration/test_tasks_api.py` approval cases |
| Rate limiting on auth | `rate_limit` dependency on `POST /auth/register` and `/auth/login` | `test_rate_limit.py::test_blocks_past_the_limit` (the limiter), and the route wiring in `app/api/v1/auth.py` |
| Rate limiting on task creation | `rate_limit("task_create", ...)` on `POST /tasks` | `test_rate_limit.py`, and `tests/integration/test_security.py` for the two added in this part |
| Rate limiting on approvals and stream tickets | `rate_limit("approval", ...)`, `rate_limit("stream_ticket", ...)` | `tests/integration/test_security.py::test_approvals_are_rate_limited`, `::test_stream_tickets_are_rate_limited` |
| CORS restricted to the frontend origin | `app/main.py` — `CORS_ORIGINS`, credentials allowed, methods and headers named rather than `*` | `test_security_headers.py::test_cors_refuses_an_origin_that_is_not_configured`, `::test_cors_names_its_methods_and_headers_rather_than_allowing_everything` |
| Secure headers | `app/core/security_headers.py` — nosniff, `DENY`, `no-referrer`, CSP, CORP, HSTS in production | `test_security_headers.py::test_every_response_carries_the_header`, `::test_hsts_is_sent_in_production_only` |
| Screenshots served only to the owner | `app/services/task_service.py::screenshot` checks the task's owner, then pins the step to the task | `tests/integration/test_security.py::test_a_screenshot_is_served_only_to_its_owner`, `::test_a_step_from_another_task_cannot_be_read_through_your_own` |
| No production credentials or real PII in tests | `tests/skyfare` generates its own data from a seed; no live provider is reachable from it | `test_skyfare.py` (deterministic generation), `.gitleaks.toml` allowlist documents the test markers |
| No CAPTCHA bypass | Not implemented and not attempted. The action allow-list has twelve actions and none of them solves a challenge | `test_enums.py::test_action_allow_list_matches_section_4_4_exactly` |
| Only sites that permit automation | `AGENT_ALLOWED_DOMAINS`, checked before the browser is touched and again after a redirect | `test_browser_driver.py::test_an_empty_allow_list_blocks_everything`, `tests/integration/test_security.py::test_a_start_url_outside_the_allow_list_is_rejected` |

Two additions in this part that section 10 does not name but assumes:

| Rule | Where | Test |
|---|---|---|
| A request body cannot be unbounded | `BodySizeLimitMiddleware`, `MAX_REQUEST_BYTES` | `test_security_headers.py::test_a_body_larger_than_the_limit_is_refused`, `::test_a_lying_content_length_is_still_caught` |
| A screenshot filename is not derivable from a task id | `app/services/storage.py::build_ref` adds eight random bytes | `test_storage.py::test_a_ref_cannot_be_guessed_from_the_task_and_step` |

## Prompt-injection defence (spec section 4.11)

Eight layers, in the order a hostile sentence on a page meets them. The first
three reduce what the model sees; the rest hold whatever it decides. That split
is the design: layers four to eight do not care what the model was told, which
is what makes them defences rather than mitigations.

### 1. Hidden elements never reach the model

`display:none`, zero size, off-screen, `aria-hidden`, and anything covered
completely are excluded from the element list, so the classic invisible
instruction block is not in the prompt at all.

- Code: `app/agent/perception/dom_extractor.js`, `app/agent/perception/dom_extractor.py`, `app/agent/perception/bs4_fallback.py`
- Tests: `test_dom_extractor.py::test_no_hidden_element_is_reported`, `test_perceive.py::test_hidden_injection_never_reaches_the_summary`, `test_bs4_fallback.py::test_hidden_elements_are_dropped`

### 2. Page text is fenced and labelled untrusted

Everything read from the page goes inside `<page_state>` delimiters, after the
goal and the history, so the model has been told what it is looking at before
it looks.

- Code: `app/agent/planner/planner.py`
- Tests: `test_planner.py::test_the_page_state_is_fenced_as_untrusted`, `::test_the_goal_and_history_precede_the_page_state`

### 3. The system prompt says whose instructions count

A section of the prompt names the specific tricks — claims of system authority,
urgency, "ignore your previous instructions", requests to visit another site or
reveal the prompt — and says that a visible button whose label is an
instruction is still just a button.

- Code: `app/agent/planner/prompts/planner_v2.md` (and `planner_v1.md`)
- Test: `test_planner.py::test_the_page_state_is_fenced_as_untrusted` covers the
  envelope; the prompt text itself is asserted only by the chaos suite's
  `inject` configuration, which is the honest position: a prompt is evidence, not
  a guarantee.

### 4. Output schema validation

The model's answer is parsed into a Pydantic model with cross-field rules. An
action outside the enum, a missing target index, a `done` with no answer, or a
`switch_tab` with no tab id is refused before anything runs.

- Code: `app/agent/planner/schema.py`
- Tests: `test_planner.py` (the repair path), `test_actions.py::test_unknown_actions_are_rejected`

### 5. The action allow-list

Twelve actions, checked by name before the browser is involved. Anything else
raises rather than executing, so an invented action cannot reach a page even if
every layer above it has been fooled.

- Code: `app/agent/browser/actions.py`, `app/agent/browser/action_builder.py`
- Tests: `test_enums.py::test_action_allow_list_matches_section_4_4_exactly`, `test_browser_driver.py::test_an_action_outside_the_allow_list_never_reaches_the_browser`, `test_actions.py::test_allow_list_failure_is_distinct_from_validation_failure`

### 6. The target-existence check

An action naming an element index that is not in the state is refused,
immediately upstream of execution rather than merely somewhere upstream. It is
checked twice on purpose: grounding sits between the planner and the driver and
can change the index.

- Code: `app/agent/orchestrator.py::_resolve_target`, `app/agent/perception/state.py::element_at`
- Tests: `test_perceive.py::test_element_at_backs_the_target_existence_check`, `test_orchestrator.py::test_grounding_choosing_a_missing_element_is_refused`

### 7. The deterministic risk policy

Rules over the target's label, the field type and the destination path, in
English and Hinglish. Final risk is the higher of the rules and the model's own,
so a model that has been persuaded to call a payment low risk cannot lower it.

- Code: `app/agent/safety/risk.py`
- Tests: `test_risk.py::test_a_dangerous_label_is_high_risk`, `::test_the_hinglish_variants_are_high_risk`, `::test_typing_into_a_password_field_is_high_risk`, `test_orchestrator.py::test_a_high_risk_action_is_refused_rather_than_waved_through`

### 8. Human approval

A HIGH-risk action parks the task and asks a person, with a screenshot and a
one-sentence description of what is about to happen. Nothing in the page can
answer it: the answer comes through an authenticated endpoint with RBAC.

- Code: `app/agent/safety/gate.py`, `app/services/approvals.py`, `app/api/v1/tasks.py`
- Tests: `test_approvals.py::test_an_approval_is_announced_with_everything_needed_to_answer_it`, `tests/integration/test_security.py::test_an_ordinary_user_cannot_answer_an_approval_they_do_not_own`

### What the chaos suite adds

The `inject` configuration puts instructions on a SkyFare page, visibly and
hidden, and the suite asserts both that the agent still reports the right fare
and that no high-risk action was proposed at all. The first assertion is the
defence working; the second is what catches it when the defence does not.

- Code: `tests/skyfare/chaos.py`
- Tests: `tests/chaos/test_chaos.py` (`inject`), `test_skyfare.py::test_inject_puts_instructions_on_the_page_visibly_and_hidden`

## Tooling

`make audit` runs two things:

- **pip-audit** over the locked dependencies, with `--strict`, so an
  unfixable advisory fails the target rather than printing a warning nobody
  reads.
- **gitleaks** with `.gitleaks.toml`, when it is installed. The target says so
  and carries on when it is not, because a developer without it should still be
  able to run the audit; CI installs it, so the scan is not optional there.

Ruff's `S` rule set (bandit) runs as part of `make lint`, on every file.

### The security suppressions, and why each one is there

Every `noqa: S...` in the repository, listed so that adding one is a visible
decision rather than a quiet habit.

| Suppression | Where | Why |
|---|---|---|
| `S105` | `app/core/security.py` | `token_type = "Bearer"` is a scheme name from RFC 6750, not a credential |
| `S105` | `app/api/ops.py` | The header *name* `X-Internal-Token`; the value is in settings |
| `S105` ×2 | `app/services/audit.py` | Audit action names, `TOKEN_REFRESHED` and `TOKEN_REUSE_DETECTED` |
| `S311` | `app/agent/llm/base.py` | Retry jitter. Predictable jitter is a thundering-herd problem, not a security one, and `secrets` would be the wrong tool |
| `S603` | `tests/integration/test_migrations.py` | Runs Alembic in a subprocess with a fixed argument list, no shell |
| `S101`, `S105`, `S106`, `S311` | `tests/**` | `assert` is how pytest works, and the fake digests and seeded randomness are fixture data. This covers the SkyFare test site's oracle marker and its reproducible flights |
| `N818` | `app/core/security_headers.py` | A private marker exception raised and caught inside one middleware |

None of these is a finding that was silenced. pip-audit currently reports no
known vulnerabilities in the lockfile.

## Known limitations

- **The rate limiter is per process and in memory.** Two workers allow twice the
  configured rate, and a restart forgets every bucket. Redis is the fix and is
  out of scope here; the limiter is behind an interface for it.
- **`/metrics` and the stats endpoint are not rate limited.** Both are read-only
  and one of them is scraped on a schedule, so a limit would be more likely to
  break monitoring than to stop an attack.
- **Cached flows are shared between users.** Keyed by instruction and start
  domain, so two users with the same instruction on the same site replay each
  other's flow. Recorded in the B25 phase note with the fix: add the user id to
  the signature material.
- **This page is maintained by hand.** A renamed test makes it wrong, and
  nothing fails when that happens. Generating it from markers in the tests would
  fix that and would also make the tests read worse, which is why it has not
  been done.
