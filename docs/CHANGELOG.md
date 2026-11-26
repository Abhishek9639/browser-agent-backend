# Changelog

All notable changes to the backend. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); parts are tracked by their id.

## [Unreleased]

### B45 - A planner that keeps to its plan, and code problems

Changed

- The planner is now shown the outline it made at the start (`AGENT_PLAN_PREVIEW`) on every turn, so it works through those steps instead of improvising each turn from the page alone.
- A failed step in the history now says why ("The URL did not change"), and three identical actions in a row add a warning to do something different. Before, the planner saw only "failed" and tried the same thing again.

Added

- Programming problems (CodeChef, LeetCode, HackerRank, Codeforces and similar): a goal that mentions them gets a short guide in the planner's turn: read the statement with `extract`, write the whole program in one `type`, press Run rather than Submit, read the result, fix and retry up to three times, and submit only if asked.
- A `type` whose value has line breaks is inserted into the editor in one piece, replacing its contents. Typed key by key, a program is mangled by auto-indent and auto-closed brackets. The verifier checks that the first lines of the code are visible on the page, since a code editor does not expose its text as a field value.

### B44 - Questions before the task starts

Added

- With `AGENT_CLARIFY=true` the agent asks the person a few short questions before it starts, when the task leaves out something that changes the result (for a train: the day, the time of day, the class, the budget). One extra model call decides what to ask, at most `AGENT_CLARIFY_MAX_QUESTIONS` (3), each with two to six likely answers; the person can also type their own. It never asks for a password, a card or a one-time code.
- A question rides the existing approval channel, so no new endpoint or event type: `approval_required` arrives with `proposedAction.action == "wait"`, `proposedAction.target == "question"`, the options in `proposedAction.value` separated by `|`, and the question in `reason`. The answer is the `reason` sent to `POST /tasks/{id}/approve`; `POST /tasks/{id}/reject` skips the question (no preference) and does not stop the task. Answers are added to the task text the planner reads. Nobody answering within `AGENT_CLARIFY_WAIT_SECONDS` times the task out, and the wait does not count against the task time limit.

### B43 - Follow a click into the tab it opens

Changed

- A click that opens a new tab (a `target="_blank"` link, a script's `window.open`, a sign-in popup) now takes the agent into that tab, as a person looking at the screen would. Before, the agent kept looking at the page it started on, so a listing that opened correctly was verified against the old page's unchanged URL and reported as a failed click. A link that asks for a new tab is waited for (up to 3 seconds); any other click only gets a short look (0.4 seconds). A new tab outside the allowed domains is closed, the agent returns to where it was, and the breach is reported.
- This reverses an earlier rule, that opening a tab does not move the agent and switching is always the planner's decision. The planner can still `switch_tab` back to the page it came from, and its prompt now says so.

### B42 - Typing, scrolling and login waits on any site

Added

- Typing handles the widgets real sites use: a selector that lands on a wrapper finds the field inside it; a field that discards a programmatic fill (a mask, an autocomplete that watches key events) is retyped at the keyboard; something with nothing editable in it is focused and typed at. The planner is refused when it targets something that is not a field while fields exist (an icon button between two inputs was taken for a date box), and is told which fields there are.
- Scrolling and scroll position use whatever actually scrolls: the window, or the largest inner panel of an app shell. A smooth scroll waits until the position stops changing.
- Before a click or type, if something covers the target (an open calendar, an autocomplete list), Escape is pressed once to close it.
- `AGENT_LOGIN_WAIT_SECONDS` (default 900): how long to wait for a person to sign in or sign up. The wait is taken out of the task's time limit (`TaskBudget.exclude`), so a slow login no longer leaves the task out of time. A page that says it needs a login ("please login to continue") now starts the wait too, not only a visible password field.

Fixed

- Playwright's "Element is not an <input>" was classed UNKNOWN, so recovery had nothing to try; it is now NOT_INTERACTABLE.

### B41 - Full screen, a visible pointer, and autocomplete

Added

- `AGENT_FULLSCREEN` (default off): the visible browser opens maximised and the page fills the window instead of a fixed 1280x800 box. The real size is measured from the page (`PlaywrightDriver.viewport_size`), because a maximised window reports none, and screenshots, snapshots and coordinate clicks all use it.
- A visible pointer in a visible browser: a blue arrow that follows every mouse event and rings on a click. Before each click or type the real pointer glides to the target in a short arc, and a scroll is smooth and has landed before the step is checked.

Fixed

- Choosing an autocomplete suggestion ("NEW DELHI - NDLS") was marked failed: the verifier looked for the value on the clicked list item, which disappears when it works. A `value_equals` check on something that is not a field now looks for a field holding the value, ignoring case and punctuation.
- A replayed cached flow that failed a step ended the task with `IllegalTransition` (RECOVERING to PLANNING). The return to planning now walks the legal path from wherever the task is.
- The "prefer a named input" planner check no longer offers a field that already holds a value (the From and To boxes are named and full, and the date box the model wanted is not), and is advisory: on the last attempt the model's choice stands.

### B40 - Language gates, popup verification

Changed

- A step is no longer failed for an expectation that is not meaningful: an `element_visible` or `text_present` value with fewer than three letters or digits ("7", from a date in the goal) falls back to the action's default check, and a click whose target is a popup closer is verified by the page changing rather than by a guess about the page underneath. A real run had marked a working "close confirmation modal" click as failed this way.
- The popup closer also matches on a control's text when its accessible name is garbled, accepts "English" so a language gate in front of a site is passed, and presses Escape once when a popup blocks the page but nothing on it says "close".

### B39 - Plan preview

Added

- `AGENT_PLAN_PREVIEW` (default off). When on, one extra model call before the first action outlines the steps a task is expected to take. The outline is stored on the task (`tasks.plan`, migration `b3c1d9a47e20`), returned as `plan` on the task, sent once as a `plan` WebSocket event, and replayed first on reconnect.
- The outline is a forecast. The agent still chooses each real step from the page (spec section 4.5), so a popup or a changed layout cannot break a plan it never depended on. A failed outline call is not an error: the task simply has no outline.
- Spec sections 1, 5 and 6.1 amended to record the field and the event.

### B38 - Free-tier quotas, request pacing and model rotation

Added

- `GEMINI_FALLBACK_MODELS` (default `gemini-3-flash-preview,gemini-3.1-flash-lite,gemini-3.5-flash-lite`): when a Gemini model is used up or not available to the account, the next answers, and a used-up model is skipped until its quota returns. Found because the free tier allows only 20 requests a day per model, and a new account gets a 404 on `gemini-2.5-flash`.
- `GEMINI_REQUESTS_PER_MINUTE` and `GROQ_REQUESTS_PER_MINUTE` (default 0, off): requests are spaced so a per-minute limit is never hit, sharing one pacer per provider.
- When the fallback is also rate limited and the primary asked for 30 seconds or less, the primary is waited for once.
- Gemini network failures (a failed DNS lookup, a dropped connection) count as "provider unreachable", so they retry and fall back instead of ending the task. A 429 keeps the wait Google asks for.
- Grounding may no longer move a `type` onto an unnamed or covered input that the planner was already refused for.

### B37 - Popups, unnamed inputs and rate limits

Added

- `app/agent/perception/overlays.py`: finds a popup that covers the page (eight or more covered elements) and the control that closes it, including an unnamed icon in the popup's top-right corner. Before planning, the loop closes such a popup by clicking a dismissal (never "Accept"). The recovery ladder's popup step now recognises the same icons.
- The planner refuses a `type` into a covered or unnamed input when a named one exists, and says which and whether to scroll first, using the existing repair turn.
- `GROQ_FALLBACK_MODELS` (default `openai/gpt-oss-20b,qwen/qwen3.8-27b`): when the Groq model is rate limited, the next is tried, since daily limits are per model.
- The fallback provider skips a rate-limited primary until its cooldown ends instead of retrying it every step.

Changed

- A rate limit that asks for more than 90 seconds is no longer slept through. It fails at once, so the fallback or the person decides. A real run lost 12 minutes to a 756 second Groq daily limit.
- Groq: a 413 (request over the model's per-minute token limit) moves to the next model, and the output allowance is capped at 1024 tokens, because Groq counts the whole allowance against that limit.
- `element_visible` accepts a planner's description that differs from the page's label ("search input field" for "Search here..."): words that only name the kind of control are dropped, every other word must match (or half of them when the description names a kind of control), and the element must be the right kind (an input, not the Search button beside it).

### B36 - Sign-in hand-off and a working indicator

Added

- When the page needs a sign-in or sign-up (a usable password field, or a login-looking path with an email field), the agent stops and hands the browser to the person through the existing approval flow. The person signs in in the visible window and presses Approve; the agent re-reads the page and carries on. Declining stops the task, an unanswered prompt times it out, and after three hand-offs on the same task it fails with "The site still needs you to sign in or sign up." A goal that mentions a password or credentials is left to the agent.
- A frame around every tab the agent uses in a visible browser: blue with "Agent is working", amber with "Waiting for you to sign in" while handed off. It ignores clicks, survives navigation, and is hidden while a screenshot is taken so the model never sees it.
- The frontend approval card reads "Sign-in needed" with an "I've signed in, continue" button for a hand-off.

Changed

- Tests that type into a password field now state a goal about signing in, because otherwise the agent correctly hands over.

### B35 - Run in installed Chrome, and say why an action failed

Added

- `AGENT_BROWSER_CHANNEL` (default empty = bundled Chromium). `chrome` or `msedge` launches the browser installed on the machine, for both the shared task browser and a standalone driver.

Changed

- A step whose action did not run now reports why, e.g. `The action did not run: the page could not be loaded; check the address (Page.goto: net::ERR_NAME_NOT_RESOLVED at ...)`. Before, every such failure read "The action did not run."
- Groq default models replaced with ones that exist: `openai/gpt-oss-120b` for text, `qwen/qwen3.8-27b` for images.
- Planner prompt: guidance not to `wait` when the answer is already on the page. A `wait` that expected the DOM to change failed a finished page.

### B34 - Tolerate a racing refresh

Added

- `REFRESH_REUSE_GRACE_SECONDS` (default 10, 0 disables). A refresh token rotated within this window and presented again gets an access token but no new refresh cookie, and the session chain survives. Previously two tabs (or a double mount) refreshing at once made the loser look like a thief, and every session for the user was revoked.

Unchanged

- A token revoked by logout, or presented after the window, or when the user has no live session, still destroys the chain and is audited as `TOKEN_REUSE_DETECTED`.

Tests

- The four existing replay tests now age the rotation past the window first; new tests cover a tolerated race and a logged-out token never being forgiven.

### B33 - Groq provider and fast rate-limit fallback

Added

- `app/agent/llm/groq.py`: a Groq adapter over Groq's OpenAI-compatible endpoint, using the existing `openai` SDK (no new dependency). JSON mode with the schema in the system prompt; a separate vision model is used when images are attached.
- `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_VISION_MODEL` settings. `groq` is now a valid `LLM_PROVIDER` and `LLM_FALLBACK_PROVIDER`. Groq never reads `LLM_MODEL`, so a Groq fallback is not sent the primary's model name.
- `RetryingLLM(retry_rate_limits=...)`.

Changed

- When `LLM_FALLBACK_PROVIDER` is set, a rate-limited primary hands over to the fallback immediately instead of sleeping out its retry-after. Other transient failures are still retried first.

### B32 - Drive the user's own browser

Added

- `AGENT_CDP_URL` (default empty). When set, the driver and the task runner attach to the user's running Chrome over CDP instead of launching an isolated Chromium. The agent opens its own tabs in that browser, closes only those, and never closes the user's context or browser.
- `AGENT_SLOW_MO_MS` (default 0): a pause between browser operations so a visible window can be followed.
- The driver raises the active tab to the front when it opens, switches or closes one, in any visible mode (`PlaywrightDriver._focus`).
- `scripts/chrome_debug.ps1`: starts Chrome with a remote debugging port and a dedicated profile.
- ADR-013, and an exception noted in spec section 4.13.

Changed

- In CDP mode the task runner allows one task at a time.

## [1.0.0] - 2026-09-29

The backend, feature-complete against `docs/PROJECT_SPEC.md`. Release notes in
`docs/RELEASE_NOTES_v1.0.0.md`.

### B31 - Architecture, ADRs, traceability and the v1.0.0 release

Added

- `docs/architecture.md`: five Mermaid diagrams — system context across the three repositories, the agent's layers, one loop iteration as a sequence, the task state machine, and deployment — each with prose and each marking what is implemented against what is future work. The state diagram is generated against `app/domain/state_machine.py` and checked to match it edge for edge.
- `docs/adr/`: twelve architecture decision records. ADR-001 to 005 are the decisions the architecture rests on, written from what was built; ADR-004 is the one the specification cites by name. ADR-006 to 012 are decisions this project made and named itself: three repositories, the mock provider, SkyFare instead of live sites, WebSocket tickets, reserve-plus-outbox, set-of-marks grounding, and an asyncio runner before a job queue. Each names the alternative it rejected and the tests that hold it in place.
- `docs/traceability.md`: BR-001 to BR-010 and FR-1 to FR-19 mapped to real modules, tables and test names, plus the specification's ten test ids. Six requirements are flagged as having no test or a weak one, with the reason for each.
- `docs/api.md` and `docs/openapi.json`, generated from the application's own schema: all 23 endpoints, and every one of the eight WebSocket event types with a payload generated from the event models themselves.
- `docs/viva-prep.md`: all 90 questions from the phase notes, grouped beginner, intermediate and advanced, with the answers unchanged from when each part was built.
- `docs/RELEASE_NOTES_v1.0.0.md`, and a rewritten `README.md` with a quick start for Docker and local, an environment variable table, the benchmark, the three-repository map and a disclaimer.

Fixed, from the accuracy pass

- `docs/traceability.md` cited `test_a_cancel_stops_the_loop_at_a_phase_boundary`, which does not exist; the test is `test_a_cancel_between_iterations_stops_the_loop`.
- ADR-004 cited `test_the_verifier_never_calls_a_model`; the test is `test_the_verifier_never_reaches_for_a_model`.
- `docs/architecture.md` referenced `app/agent/perception/collector.js`; the file is `dom_extractor.js`. (The same stale name was fixed in `docs/security.md` during B28.)
- The README claimed 1,147 unit tests, which is the number that pass; 1,148 are collected and one is skipped.
- TEST-004 and TEST-005 were covered by tests that did not cite the id. The citations were added to `tests/unit/test_planner.py` and `tests/integration/test_orchestrator.py`, so all ten of the specification's test ids are now greppable rather than only claimed in a document.

The live benchmark

- Run with `--provider gemini`, five runs of the primary configuration, per instruction and without retrying. It produced no usable measurement: `gemini-3.6-flash` answered every request with `503 "This model is currently experiencing high traffic"`, so all five runs failed with `LLMUnavailable`. The report is kept as `docs/benchmarks/2026-09-29-gemini.md` with a note explaining that its 0% is a statement about provider availability rather than about the agent. What it does show is the degradation path working: five clean failures with a readable reason and no crash. The mock run remains the headline.

## [Unreleased]

### Changed: branch protection is documented, not enabled

- GitHub does not enforce rulesets or branch protection on a private repository on the free plan: the settings can be saved and are then ignored, which is worse than not having them because the repository looks protected and is not. `docs/cicd.md` now says so plainly and keeps the configuration as the intended one.
- The two ways it becomes enforceable are recorded: making the repository public, or moving it to a GitHub organization. Neither needs a change to the workflow, since `gate` already exists and is already the single check a rule would name.
- The interim practice is written down as well: every part goes through a pull request, and a pull request is merged only when `Quality gate` is green. A red gate is fixed rather than merged around. This is a weaker guarantee than a ruleset and the document says so.

### Fixed: the first CI run

Three failures, none of which could have been reproduced locally.

- `psql -c "... \gexec"` is a syntax error. `\gexec` is a psql meta-command and `-c` sends its argument straight to the server, which answers `syntax error at or near "\"`. The pattern had been copied from `scripts/db/init.sql`, where psql reads it as a file and it works. The step now runs that script with `-f`, so CI and the compose stack create the same three databases from one source rather than from a duplicated one-liner. This failed both the integration and the chaos jobs.
- `gitleaks-action` requires `GITHUB_TOKEN` on a pull request: a breaking change in its v2, where it lists the pull request's commits through the API and refuses to scan one without a token. Added, along with `pull-requests: read` on that job. Nothing local would have caught it, because `make audit` runs the binary against a working tree with no pull request involved.
- The quality gate reported `success success failure failure failure success` and failed, which is the gate doing its job rather than a fourth bug.
- The second run failed on one test for the same class of reason: `test_live_database_is_at_migration_head` asserts that the live `agent_core` database is at head, which is the state `make migrate` leaves a developer's machine in and which nothing in CI had done. The suite builds `agent_core_test` from the model metadata and never touches the live database. The integration job now runs `alembic upgrade head`, which also means CI proves the migrations apply from nothing rather than only that the metadata does.

`docs/cicd.md` records all three under verifying a workflow change, because two of them are the kind of thing that recurs.

### B30 - Docker images and GitHub Actions

Added

- `Dockerfile`: multi-stage, two targets, on `mcr.microsoft.com/playwright/python` pinned to the same Playwright version as `pyproject.toml`. Dependencies are their own stage so the layer is cached on the lockfile alone. Runs as the base image's `pwuser`: a browser running as root is a sandbox with the door open, and screenshots written to a mounted volume would be root-owned on the host.
- `docker/entrypoint.sh`: applies migrations, then generates a signing key if the volume has none, then starts uvicorn. Alembic serialises on its own version row, so several replicas starting at once is safe; `RUN_MIGRATIONS=0` and `JWT_GENERATE_KEYS=0` exist for a deployment that would rather do either itself.
- `HEALTHCHECK` on `/api/v1/health` with a 40 second start period, which is what covers the migrations and the Chromium launch.
- A second target for SkyFare: same layers, no database, no keys, no entrypoint.
- `.dockerignore`, `docker-compose.yml` (postgres, api, skyfare, with health checks and `depends_on: service_healthy`), and `make up` / `down` / `logs` / `build`.
- `deploy/docker-compose.full.yml` and `.env.full.example` for the three-repository stack with the frontend and payments service as sibling checkouts. Both of those repositories are still to be written, so their blocks are arranged to be commented out in one piece each; removing both leaves a valid file, which was checked.
- `.github/workflows/ci.yml`: lint and typecheck, unit tests with coverage, integration tests against a Postgres service container, the chaos suite and one benchmark round, `pip-audit --strict` and gitleaks, and a Docker build that starts the stack and checks its health. Dependencies cached on `uv.lock`, the Chromium build cached on the resolved Playwright version.
- `gate`, a single job that fails unless every other job succeeded, so branch protection names one check and adding a job later does not mean editing the rule. It reads the results explicitly rather than relying on `needs`, because a job that only lists dependencies does not fail when one is skipped or cancelled.
- `docs/cicd.md`: the jobs and their timings, why the caches are keyed the way they are, how to verify a workflow change without pushing, and the branch protection settings for `main` to apply by hand.

Verified

- `docker compose up --build` from deleted volumes: all three containers healthy, migrations applied from empty, and a task run end to end inside the stack that reached SkyFare by its service name and answered "The cheapest option is Rs 4,870" — which is what SkyFare's oracle endpoint says the cheapest fare is. `GET /metrics` answers 401 anonymously and the B28 security headers are present, so that work survives containerisation.
- The workflow was not executed: `act` is not installed here. `actionlint` was, and found a real bug before it could fail at the gate on every push.

Fixed

- The compose file passed the Gemini key as `GEMINI_API_KEY`; the setting is `GOOGLE_API_KEY`. Compose loads the repository's `.env` by itself, so the container inherited `LLM_PROVIDER=gemini` from a developer machine and then failed the first task with a missing key. The inheritance is kept and documented, because a developer with a provider configured probably wants it; `LLM_PROVIDER=mock make up` overrides it.
- `join(needs.*.result, " ")` in the gate used double quotes, which GitHub's expression parser rejects.
- `.gitignore` excluded `.env.full.example` along with every other `.env.*`.

### B29 - Billing client with outbox

Added

- `app/services/billing_client.py`: the internal client for the payments service. Four calls, a three second timeout, retries on 5xx and network failures only, and an `Idempotency-Key` on every request derived from the thing it is about rather than generated.
- `billing_outbox` table, with a migration. A grant, a settle and a release are written to it **in the same transaction as the business change they belong to**: the user row and its signup grant commit together, and the terminal task status and its settle commit together. A worker delivers them afterwards with an exponential backoff capped at an hour.
- `app/services/billing.py`: `BillingService`, the one place `BILLING_ENABLED` is checked, and `OutboxWorker`, started in the lifespan.
- `app/domain/credits.py`: the section 7 pricing in one place. 1 credit per step, 2 for a vision-grounded step, half for a replayed one, rounded up once over the whole task.
- Wiring: registration queues a grant; `POST /tasks` reserves `AGENT_MAX_STEPS x RESERVE_MULTIPLIER` synchronously and answers 402 with `available` and `required` or 503 if payments cannot be reached; a terminal state queues a settle, or a release when the run used nothing.
- `tasks.credits_used`, written for every finished task whether or not billing is enabled, and `creditsUsed` on the task detail response.
- Settings: `PAYMENTS_BASE_URL`, `INTERNAL_API_TOKEN`, `PAYMENTS_TIMEOUT_SECONDS`, `PAYMENTS_MAX_ATTEMPTS`, `CREDITS_PER_STEP`, `CREDITS_PER_VISION_STEP`, `CREDITS_REPLAY_STEP_HALF`, `RESERVE_MULTIPLIER`, and three for the outbox worker.
- `details` on the error envelope, so a 402 carries the two numbers a user needs rather than sending them to a wallet page to find out how short they are.
- `docs/billing-integration.md`: every request and response, the idempotency keys, the pricing, the failure handling, and what this side expects of the payments repository.
- 48 tests, driven against a fake payments app in the same process so the HTTP contract is exercised rather than mocked.

Changed

- `BILLING_BASE_URL` and `BILLING_INTERNAL_TOKEN` are renamed to `PAYMENTS_BASE_URL` and `INTERNAL_API_TOKEN`, the names the part brief uses. Both were unused scaffolding.
- The credit arithmetic that was duplicated in the orchestrator and the stats service now comes from `app/domain/credits.py`. A settle that disagreed with what the agent reported is the one bug here that costs somebody money.
- `tests/unit/test_models_metadata.py` now lists the two tables that are not in spec section 5 with the reason each exists, rather than naming one.

With billing disabled, which is the default

- No reservation, no outbox rows, no calls, and the agent behaves exactly as before. The only visible difference is that `creditsUsed` on the stats endpoint is null.

### B28 - Security hardening

Added

- `app/core/security_headers.py`: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-origin`, a CSP, and HSTS in production only. Two policies: the API gets `default-src 'none'` with framing, base URI and form submission all denied, and the four documentation routes get one that lets Swagger UI load, matched against a fixed set rather than by prefix so `/docsomething` cannot pick it up.
- A request body limit, `MAX_REQUEST_BYTES`, as pure ASGI middleware. `Content-Length` is refused before anything is read, and the body is counted as it arrives because the header is only a claim. The framework turns an exception from the receive channel into a 400 about a malformed body, so the middleware rewrites the response instead: once the counter trips, the first `http.response.start` becomes a 413 and the handler's own body is dropped.
- Rate limits on `POST /tasks/{id}/approve`, `/reject` and `/stream-ticket`, with `RATE_LIMIT_APPROVAL_PER_MINUTE` and `RATE_LIMIT_STREAM_TICKET_PER_MINUTE`.
- Screenshot refs carry eight random bytes, so a filename is not derivable from a task id and a step number. Access is still decided by ownership; this matters for a deployment that later serves the directory statically. The pattern accepts refs written before the suffix existed.
- `make audit`: pip-audit with `--strict` over the lockfile, and gitleaks with `.gitleaks.toml` when it is installed. Ruff's `S` (bandit) rules already run in `make lint`.
- `docs/security.md`: every rule in spec section 10 mapped to where it is enforced and the test that proves it, the eight prompt-injection layers from section 4.11 each linked to its code and tests, and every `noqa: S` in the repository listed with its reason.
- 44 tests.

Fixed, from the input validation review

- A control character in an instruction was a 500, not a 400: Postgres refuses a NUL byte in a text column. Control characters are now stripped before the length check, so an instruction made only of them is rejected rather than stored.
- A start URL could carry credentials. `http://user:secret@host/` passed the scheme check and the allow-list, and the password would have been stored on the task row and repeated in every event about it.
- `{"approved": false}` posted to `/approve` performed an approval. Both endpoints now refuse a body that disagrees with them: the action cannot be undone and either reading might be the one nobody wanted.
- An inverted `from`/`to` range returned an empty page, which reads as "you have no tasks". Now a 400.
- An approval reason is whitespace-normalised, so one made of newlines becomes null rather than a blank string.

Changed

- CORS names its methods and headers instead of allowing `*`. A wildcard with credentials is both refused by browsers and a statement that any method and header from that origin is welcome.

Checked and unchanged

- Pagination caps (20 default, 100 maximum, per decision A17), the enum filter on `?status=`, `%` escaping on the search term, UUID path parameters, the grounding description bounds, and the `after_step` lower bound on the WebSocket.

### B27 - Metrics, stats overview and log hygiene

Added

- `app/core/metrics.py` and `GET /metrics` in Prometheus text, on its own registry and at the root per decision A9. Admin bearer token or `X-Internal-Token` when `METRICS_INTERNAL_TOKEN` is set; the token comparison is constant time. Sixteen collectors covering the API, the agent, the model and the live view, which between them derive every KPI in spec section 9.
- `GET /api/v1/stats/overview`: completion rate, median duration, recovery success rate, cache hit rate, approvals requested, credits used when billing is enabled, and a seven day trend. Own tasks by default; an ADMIN may pass `scope=all`, and anyone else asking for it gets 403 rather than their own numbers under a global heading.
- `app/repositories/stats.py` and `app/services/stats.py`: the aggregates, and the arithmetic over them.
- Log context bound rather than passed: `task_id` for a whole run, `step_number` for a step, alongside the existing `request_id`. A decision logs `model_name`, `provider` and `prompt_version`; a status change logs `status`; a failed action logs `error_code`.
- `docs/observability.md`: every metric and its labels, useful log queries, and seven Prometheus alert rules covering repeated step failures, API error rate, LLM dependency failure, decision latency, and the completion and recovery benchmarks.
- `prometheus-client`, and the `METRICS_INTERNAL_TOKEN` setting. The library is not in the spec section 3 stack list; section 6 requires Prometheus text output and the part brief asked for it by name.
- 30 tests, including a scan of every log line a full mock task produces.

Not labelled, on purpose

- No metric carries a task id, user id or URL. A label taken from user input grows one time series per task, or per probe from whatever is scanning the internet, which is the standard way instrumentation takes down the service it was meant to watch. A test enumerates the registry and fails if any collector gains one.
- The HTTP metrics use the route template with parameters put back, and label an unmatched request `unmatched` rather than by its path.
- "Repeated action failures on one task" is therefore not a Prometheus rule. The step budget and the ladder's own budget already stop a task that keeps failing, and the per-task question is answered by a log query on `task_id`, which the observability page documents. The aggregate rule is the metric-shaped version of the same signal.

Changed

- Recovery is counted per failed step rather than per attempt, matching the chaos harness: a step rescued on the third rung is one success, not one success and two failures.
- `cache_lookups_total` has three outcomes, `hit`, `miss` and `ineligible`. A flow below the success-rate threshold is not a hit; counting it as one would measure how often a signature matched rather than how often the cache helped.
- Structlog no longer caches bound loggers on first use. Caching binds each module's logger to whatever processor chain existed when it first logged, which makes the pipeline unobservable and the section 10 guarantee untestable. The cost is a dictionary lookup per log call.

Fixed

- `metrics.reset()` left unlabelled counters at their old values, because `clear()` only empties a collector's label map. One test's recovery episodes were appearing in the next test's total.

### B25 - Task replay and caching

Added

- `app/agent/cache/signature.py`: `sha256(normalised instruction + start domain)`. Normalisation removes case, surrounding and repeated whitespace, unicode width and trailing punctuation, and nothing inside the sentence: "to Delhi" and "from Delhi" have to stay different signatures. The port is excluded from the domain so a demo server on a fresh port still hits its own cache.
- `app/agent/cache/flow.py`: a cached step names its target by role, accessible name, text and a CSS path as a last resort, never by index. Indices are assigned fresh by every perception pass, so replaying them would aim at whatever now sits at those numbers.
- `app/agent/cache/store.py`: the flow cache over `cached_flows`, with the success rate, the eligibility threshold and the invalidation rule.
- Replay in the orchestrator. Cached steps run through the same action building, risk assessment, approval gate and verifier as planned ones; the first step that fails to resolve or fails to verify ends the replay and hands the live page to the planner with the remaining steps as a hint. The task completes normally either way.
- `GET /api/v1/tasks/{id}/cache-match` returns the signature and, if a flow is stored, its step count, run count, success rate and whether an ordinary run would replay it.
- `POST /api/v1/tasks/{id}/replay` creates a new task bound to a finished task's flow and queues it. The replay ignores the success rate, because the user asked for that sequence rather than for the cache's opinion of it. 409 while the original is still running, 404 when there is no flow.
- Step events carry `replayed`, and the final event's stats carry `replayedSteps` beside `cacheHit`. A hit that replayed nothing is a hit that saved nothing, and the two fields together say so.
- `CACHE_ENABLED` and `CACHE_MIN_SUCCESS_RATE` settings, and a `--cache` flag on the benchmark that writes its own report file.
- 47 tests, including TEST-009 against the real site: the same task twice, the second run replaying and asking the model fewer times.

Not cached, on purpose

- The final answer. `done` is never stored, so a replay can never finish a task by itself and no run can report a stale price. Every replayed run still ends with the planner reading the live page.
- A masked value. The step is kept as a marker that the planner has to do this one, which stops the replay there. Dropping it would produce a flow that silently skips the password field.

Changed

- A replayed step writes no `llm_decisions` row, because there was no model call behind it. That absence is the audit trail for what the cache saved.
- `_record` takes an optional planner result, which is what makes the row above possible.
- The task status moves back to `PLANNING` between replayed steps. The state machine allows `ACTING` only from `PLANNING`, and replaying more than one step failed with an illegal transition until the phase was tracked through the replay.

Known limitation

- Flows are keyed by signature alone, so two users with the same instruction on the same site share one. The sequence is not secret, but `cache-match` reporting a run count for an instruction you have never submitted tells you somebody else has. Single user is this project's scope; the fix when it is not is to add the user id to the signature material, which partitions the keyspace and needs no schema change.

The measured effect

- Same configuration, four runs, with and without the cache. Tokens fall from 2580 to 1272 and the estimated cost from 0.0645 to 0.0318 INR, with three of the four runs replaying. Per run that is about 645 tokens uncached against about 209 replayed. Vision calls per step halve, from 0.43 to 0.21, because a cached step names its target with a locator and never needs grounding. Steps per task and correctness are unchanged at 7.0 and 4 of 4.
- The wall clock barely moves, and that is an artefact of the mock provider answering instantly. Against a real provider the planner is most of a step's latency, so the same replay would save that rather than a rounding error.

### Changed: the recovery ladder is routed by the failure's error code

- `app/agent/recovery/routing.py`: the rung tried first now depends on why the step failed. `ELEMENT_NOT_FOUND` means the locator is wrong, so the re-locate rungs go first. `TIMEOUT` or `NOT_INTERACTABLE` means the locator found something that would not respond, so `DismissOverlay` and `ScrollAndWait` go first, with the order between those two decided by whether the target is on the page and merely off screen.
- Routing reorders and never truncates. The error code comes from reading a message Playwright wrote, so it is a hint rather than a diagnosis, and a misclassified failure still reaches every rung while the budget lasts.
- The effect, measured over the same 30 benchmark runs: p90 time per task falls from 48.4s to 27.4s, and recovery attempts from 24 to 12. The cookie configurations were spending two attempts per rescued step, the first of them re-locating a button that a cookie banner was covering and waiting out the full ten second action timeout to find out. They now spend one. Success and recovery rates are unchanged at 100%.
- 8 tests.

### B24 - Multi-tab session manager

Added

- `app/agent/browser/sessions.py`: `TabSessionManager`, one per task. Stable ids `t1`, `t2`, ... that are never reused, the active tab, capture of pages the site opens itself, the last `PageState` per tab, and a limit of five open tabs.
- `new_tab`, `switch_tab` and `close_tab` are implemented through it; the driver no longer keeps its own tab dictionary. Closing the active tab lands on the most recently opened of the rest rather than the oldest.
- Orphan tabs are closed when the task ends, before the context goes away, so a popup nobody closed does not outlive the run on a shared browser.
- The section 4.6 header names what each tab holds: `Tabs: t1 Delhi to Bengaluru | SkyFare (active), t2 SpiceJet SG-419 | SkyFare`. A tab nobody has perceived yet prints as its bare id.
- `PageState.tab_labels`, so the verifier and the live view see the same names the planner was given.
- Three tab checks in the verifier: a new tab opened, the active tab changed, and a tab closed. All three are decided from the tab registry rather than from a URL, and all three store as the `new_tab_opened` method with `actual["check"]` saying which ran.
- `ActionErrorCode.TAB_LIMIT_REACHED`, returned as a failed action rather than raised, because closing a tab is a recovery the ladder can perform.
- `app/agent/planner/prompts/planner_v2.md`: v1 plus a section on tabs, and the new default. v1 is unchanged and still loadable.
- `AGENT_MAX_TABS` setting, default 5.
- 24 tests, and a SkyFare `newtab` journey in the chaos suite that clicks a `target="_blank"` link, switches to the tab it opened, reads it, switches back and finishes.

Changed

- `PLANNER_PROMPT_VERSION` now reaches the agent. `Planner(llm)` with no registry falls back to the literal `"v1"`, and both the task runner and the chaos harness constructed it that way, so the setting only ever changed the eval script's behaviour. Both now pass the configured version.
- The orchestrator is given the driver's own tab registry instead of taking the tab id from the last page state, so a step row and a step event name a tab the client can still find.
- `new_tab` is no longer verified by the destination host. A new tab that failed to open leaves the old tab active, and if that tab was already on the right host the URL check passed on a page the action never reached.

Note on the prompt eval

- v1 and v2 score identically on the 22 labelled cases (32% action accuracy, 100% groundedness, 0 hallucinated indices, 0 unsafe values), and identically by construction rather than by measurement: the mock provider ignores the system prompt, so no prompt change can move those numbers either way. The eval therefore could not distinguish the two versions, and v2 is unvalidated by that harness. It is the default on the strength of the real-loop coverage instead, and the diff being purely additive. The live comparison is deferred to the benchmark stage; the phase note records the command.

### B23 - Risk policy and human-in-the-loop approval

Added

- `app/agent/safety/risk.py`: deterministic rules returning a level and the reasons for it. A control whose label matches a configurable phrase list, typing into a password or card or CVV or OTP field, or navigating to a path that looks like a till. English plus the Hinglish a real Indian booking site uses, because a button labelled "Abhi kharidein" is not less of a purchase.
- Final risk is the higher of the rules and the planner's own, so a model that calls a purchase low risk cannot talk its way past a rule that says otherwise.
- `app/agent/safety/gate.py`: writes a PENDING approval, commits it, announces it with a screenshot, bbox and a plain-language description, then waits. The row is written before the event so a client reconnecting mid-wait is told about the approval by the replay rather than having missed its only announcement.
- `app/services/approvals.py`: the registry a waiting run blocks on and the service an HTTP request answers through. The answer goes to the database and the event only wakes the run, so losing the event loses the promptness and not the decision.
- `POST /api/v1/tasks/{id}/approve` and `/reject`. APPROVER or ADMIN, or the task's own submitter when `APPROVAL_OWNER_CAN_APPROVE` allows it. A stranger gets 404 and cannot learn the task exists; the owner without the role gets 403, because by then there is nothing left to hide. No pending approval is 404, answering twice is 409, and every decision writes an audit row and publishes `approval_resolved`.
- Restart recovery expires a pending approval rather than leaving it PENDING, since the run that would have received the answer died with the process.
- `RISK_HIGH_PHRASES` and `APPROVAL_OWNER_CAN_APPROVE` settings.
- 54 tests.

Changed

- The chaos injection case now also asserts that no high-risk action was proposed at all. The first assertion is the injection defence working; the second is what catches it when the defence does not.
- The default risk policy is the deterministic one rather than a pass-through, so typing a password now needs a human.

Deviation from the brief

- The brief said an approval nobody answers should make the task `REJECTED`. Spec section 4.3 and decision A4 both say `EXPIRED` for the approval and `TIMED_OUT` for the task, and reserve `REJECTED` for an explicit human rejection. The spec is the source of truth, so a timeout times the task out. The two are different events and a run that nobody looked at should not read as one somebody refused.

### Fixed: iframe targeting

- The driver resolved every selector with `page.locator`, which only ever sees the main frame, so an element inside a same-origin iframe was perceived perfectly well and then could not be clicked, typed into or read. Click, type, select and extract now resolve through a shared frame-aware lookup that tries the main frame first, so a page without frames behaves exactly as before.
- `extract` with no selector reads every frame rather than the top-level body. A page that renders its content into an iframe has almost nothing in its own body, so the old version returned the chrome around the results and none of the results.
- `PageState` carries `frame_urls`, and the section 4.6 summary header gains a `Frames:` line when they differ from the top-level URL. A link inside an iframe navigates that frame and leaves the top-level URL untouched, so a planner told only `URL:` cannot tell that its click worked and clicks again until the step budget runs out.
- The `url_contains` and `url_changed` checks consider frame URLs for the same reason: a click that navigated a frame did what was asked of it.
- The benchmark goes from 90% to 100% end-to-end success. `iframe` was the only failing configuration, failing all three runs and taking 64 seconds each; it now passes in 5.6 seconds with no recovery attempts, the same as a page with nothing wrong with it.

### B26 - Chaos suite and benchmark runner

Added

- `tests/chaos/`: the primary task run against SkyFare under each of ten chaos configurations, in one process with a real Chromium and the real loop. Every configuration asserts the same contract: either the agent reports the fare the oracle says is cheapest, or it fails with a reason a person can read. Completing with the wrong number is never acceptable, because a confident wrong answer is the one outcome a user cannot detect.
- Stronger assertions where they are earned: the three configurations with nothing in the way must all succeed with every step verified, and the ones with a cookie banner must show recovery actually rescuing a step.
- The injection case asserts the agent never reaches the payment page and never targets "Pay now" or "Book this flight". Spec section 4.11's defences measured end to end rather than unit by unit. Once approvals are real this becomes an assertion that an approval was requested.
- `--live` runs the same suite against the configured provider instead of the mock.
- `scripts/benchmark.py`: runs the task N times per configuration and writes a dated Markdown and JSON report with end-to-end success, recovery success, verification accuracy, median and p90 time, average steps, vision invocation rate, approvals, cache hit rate, tokens and estimated cost, marked PASS or FAIL against the spec section 9 targets, plus a per-configuration table.
- `LLM_PRICE_INPUT_PER_MTOK`, `LLM_PRICE_OUTPUT_PER_MTOK` and `LLM_PRICE_CURRENCY`, so cost comes from settings rather than from a number typed into a script.
- `make chaos` and `make bench`.
- `docs/benchmarks/2026-09-27-mock.md`, the first report.

Changed

- Visual grounding only overrules the planner when it is confident enough for the reason it was called. A correction is nearly free when the planner said it had no idea or when the recovery ladder has run out of other options, and expensive when the planner picked a specific element and grounding was called only because two share a name. Each trigger now has its own acceptance floor.
- `ground_reason` returns a value rather than a phrase, since the acceptance floor depends on it.

### B22 - Visual grounding with set-of-marks

Added

- `app/agent/vision/marks.py`: draws numbered, high-contrast boxes over the on-screen candidates with Pillow, downscales to `VISION_MAX_WIDTH`, and keeps the mapping from label back to element. Labels run from 1 in reading order rather than reusing sparse element indices, are capped at 40, and are placed so none falls outside the picture.
- `app/agent/vision/grounding.py`: `VisualGroundingService` with `build_evidence_context`, `resolve_target` and `validate_output`, plus `VisionGrounder` implementing the `Grounder` seam. Uses `LLM_VISION_MODEL` through the existing adapter with a versioned prompt, `vision_v1.md`.
- Asking for a mark number rather than coordinates is the design: a number can be checked against the marks we drew, and a coordinate cannot be checked against anything. A label that is not on the picture is refused however confident the model is, which is the same guard the planner has. A point is accepted only as a fallback and only inside the image.
- Triggers from spec section 4.9, evaluated in the loop so the screenshot is only paid for when one fires: `needsVision`, confidence below `VISION_CONFIDENCE_THRESHOLD`, several elements sharing an accessible name, and fewer than `VISION_MIN_ELEMENTS` on a page that is visibly not blank. The reason is recorded alongside the result, because a vision rate is uninterpretable without knowing what drove it.
- Recovery rung seven is now real: it grounds the target and clicks the bbox centre by coordinate, with the verifier still deciding whether that worked. Without a grounder configured it stays not-applicable rather than reporting a hollow success.
- `POST /api/v1/tasks/{id}/ground` runs grounding against the page a running task is looking at and returns what it found. It never acts.
- Graceful degradation throughout: a provider that is down, times out, returns an unusable answer, or is handed a page with nothing to mark, returns a result carrying an error and the run continues DOM-only. Grounding can never fail a task.
- Per-task vision metrics on the run outcome and in the logs: invocation count, failures, total latency and total tokens.
- `VISION_PROMPT_VERSION`, `VISION_MAX_WIDTH`, `VISION_CONFIDENCE_THRESHOLD`, `VISION_MIN_ELEMENTS` and `VISION_MAX_IMAGE_BYTES` settings.
- Two fixture pages: two buttons that are identical in the accessibility tree and distinguished only by prices painted onto canvases, and a seat map drawn entirely on a canvas.
- 28 tests.

Changed

- `PromptRegistry` takes a package and filename, so the vision prompt gets the same versioned loading and the same eager failure as the planner's rather than a second copy of both.
- `Grounder.resolve` takes the screenshot and the trigger reason, and `GroundingOutcome` carries the point, confidence, justification, latency, tokens and any error.
- The browser gained `click_at`, which bounds-checks the coordinate before it reaches the mouse.

### B21 - Error recovery, and the first end-to-end demo

Added

- `app/agent/recovery/`: the seven ordered strategies from spec section 4.8 behind one `Recovery` collaborator. Re-perceive and re-find by role, name and text; a role and accessible-name locator; a text locator that tries exact, then case-insensitive, then a difflib match at 0.8; the stored CSS path and XPath; scroll into view and retry; dismiss overlays and retry; and a visual grounding placeholder that is deliberately never applicable.
- Every rung works the same way: find a better locator, run the original action against it, and let the verifier decide. A strategy that graded its own attempt would report success whenever Playwright did not raise.
- `RecoveryRunner` walks the ladder until the verifier is satisfied or `AGENT_MAX_RETRIES_PER_STEP` attempts are spent. Skipped rungs do not spend the budget but are still recorded and numbered, because `attempt_number` is the ordering key on the row.
- Each attempt writes a `recovery_attempts` row and publishes a `recovery` event as it happens. The task sits in `RECOVERING` while the ladder is climbed.
- A failure that survives the ladder says what was tried: "Step 5: Could not click 'Search flights': tried scrolling to it and closing a popup."
- `GET /api/v1/tasks/{id}/recovery-attempts`.
- Overlay close buttons are ranked so that "Reject non-essential" beats "Accept all cookies", and matched on the whole label so "Accept" cannot also match "Accept and book this flight".
- `app/agent/browser/action_builder.py`, shared by the loop and recovery, plus `scroll_into_view` and `press` on the browser interface.
- Three fixture pages that each break in one specific way, and 25 tests.

Fixed

- Strategies that re-locate an element now stand down while the target is covered. A cookie banner made them runnable and useless, and they spent the whole retry budget before the rung that closes overlays was reached.
- A recovery attempt is verified against the page as it is immediately before the retry, not the page that failed. Closing a modal changes the DOM, so the old baseline let `dom_changed` pass whether or not the retried click landed.
- The overlay rung clears every overlay it recognises. A cookie banner and a newsletter popup together left the click blocked after one was closed.
- After a successful rescue the loop carries the page the retry ended on. It was carrying the page that failed, so the agent navigated to the results page and then planned its next step against the search form it had already left.
- Provider rate limits, timeouts and outages have their own failure reasons instead of reading as an internal error.
- The mock provider fills any field whose value is not what the goal asks for, rather than only empty ones, and keys the date slot on the input type. A pre-filled date meant the first demo searched the wrong day and returned a confidently wrong answer; "Departure" as a label is the date on one page and the origin city on another.

### B20 - Deterministic verification

Added

- `app/agent/verification/verifier.py`: `DeterministicVerifier.check(decision, before, after, result)` comparing two page states and returning a `Verdict` of `ok`, `method`, `expected`, `actual` and `detail`. It never calls a model, per ADR-004 and spec section 4.7: a model that is wrong about what a click did is exactly the model that will confidently say it worked, so it must not also be what decides whether it did.
- Checks: `url_contains`, `url_changed`, `element_visible` by name, text or role, `text_present`, `value_equals` which re-reads the field, `dom_changed`, `new_tab_opened`, `element_state` for checked, selected and expanded, plus `scrolled` and `extracted` for the actions that need them.
- Defaults when the planner's declared expectation cannot be checked, which is any expectation that needs a value and was given an empty one: navigate to the target host, type and select to the value that was sent, go back to the URL changing, click to something on the page having changed, scroll to the position moving, extract to getting text back, done to always passing.
- `app/agent/browser/stability.py`: waits for network idle or a DOM that has not mutated for 300 ms, capped at 3 seconds. Without it verification races the page, a click that triggers a render is checked before the render happens, and the agent starts recovering from a success.
- `PageState.text`, the page's visible text, so `text_present` can see a result rendered into a paragraph. Not sent to the model, which still gets only the compact summary.
- A labelled set of 18 before and after cases scored as precision and recall, evenly split between steps that worked and steps that did not. Both are 1.00. The two errors are reported separately because they are not equally bad: a false positive builds every later step on a wrong premise, a false negative recovers from a success.
- `AGENT_SETTLE_QUIET_MS` and `AGENT_SETTLE_TIMEOUT_MS` settings.
- 48 tests: 38 over the checks and defaults, 5 driving the settle race against a real browser, 2 scoring the labelled set, and 3 in the loop suite for the verdict reaching the row.

Changed

- The stub verifier is gone. `VerificationOutcome` is now `Verdict`, carrying what was expected as well as what was found, and both JSONB columns on `verifications` come from it, so a step can be argued about later from the row alone.
- The loop perceives once per step instead of twice. The state read after an action is the state the next step plans against, and the cheap browser snapshot is no longer taken at all since the page state carries everything the step event needed from it.
- A failed step's reason now carries the verifier's own account, so a user is told what was checked rather than only that something went wrong.
- `element_state` is the check for a click whose target carries a checked, selected or expanded flag. `expanded` is not part of the DOM hash, so `dom_changed` would have called a working disclosure toggle a failure.

### B19 - WebSocket live stream

Added

- `app/services/ws_tickets.py`: single-use tickets bound to one user and one task, valid for 60 seconds. `POST /api/v1/tasks/{id}/stream-ticket` returns `{ticket, expiresIn}`. A browser cannot set headers on a WebSocket handshake, so the only alternative is an access token in the URL, where it reaches the server log, the proxy log and the browser history. A ticket is redeemed before any other check, so a refused connection still burns it.
- `WS /api/v1/tasks/{id}/stream?ticket=&after_step=`: validates and consumes the ticket, sends the current status, replays every step after the cursor with its screenshot loaded from storage, adds any pending approval, then streams live events. Close codes 4401 invalid ticket, 4403 not the owner, 4404 no such task, 1000 for a task that has already finished.
- `app/services/stream.py`: rebuilds a task's events from its rows. Replayed events carry `seq` 0, because the original sequence number is not stored and inventing one would break the promise that a task's sequence only goes up; steps resume on `stepNumber`, which is stored.
- Deduplication between replay and live, keyed on step number and approval id, so a step published while the replay was being built is not shown twice. The subscription is opened before the database is read, which closes the window where a step could be published and seen by nobody.
- Heartbeat every 15 seconds, and `{"type":"ping"}` from the client is answered with `{"type":"pong"}`. A heartbeat takes no sequence number, since it is not part of the task's history.
- Publish-to-send latency logged per step as `stream_step_latency`, for the spec section 9 live-view target. Replayed events are excluded: their timestamp is when the replay rebuilt them, not when anything happened.
- `scripts/ws_watch.py`: logs in, takes a ticket, and prints the stream. Screenshots are reported by size. `--after-step` reconnects as a returning client would.
- `WS_TICKET_TTL_SECONDS` and `WS_HEARTBEAT_SECONDS` settings.
- 52 tests: 13 for tickets, 13 driving a real WebSocket handshake, 19 for replay against real rows, and 7 pinning the fare pattern.

Fixed

- App-state dependencies took a `Request`, which FastAPI cannot inject into a WebSocket route, so every socket failed at connect. They now take `HTTPConnection`, the base class of both.
- The mock provider read "Apply filters 28 flights" as a fare of 28 rupees, because "filters" ends in "rs" and the currency token was unanchored. It reported that as the cheapest flight on the page.
- Extracted text was truncated on a character count, which turned a fare of Rs 5,260 into Rs 5. The cut now falls back to the last word boundary.

### B18 - SkyFare demo site

Added

- `demo_sites/skyfare/`: a mock airline search site on port 8200, served by `make skyfare`. Standalone, and nothing in `app/` imports it. Search with autocomplete over ten Indian cities, a results list with sort, stop and airline filters and load-more, a flight detail page, a passenger form ending at a "Pay now" button, and a payment page that makes clear nothing was charged.
- Deterministic generation: 15 to 30 flights seeded from the origin, destination and date, hashed with sha256 rather than `hash()`, which is salted per process and would give different flights after every restart. The same search returns the same flights on any machine, which is what makes an agent run scorable.
- Nine chaos toggles, set per request with `?chaos=` or for a session with `SKYFARE_CHAOS`: `delay`, `cookie`, `popup`, `shuffle`, `layoutb`, `iframe`, `newtab`, `inject`, `slowtype`, plus `all`. Toggles carry through every link on the page, so one that switches itself off on the first click cannot happen.
- `GET /api/cheapest`: ground truth for scoring a run, behind a header and returning 404 without it, so an agent that guesses the path learns nothing from trying.
- README section with screenshots of the search, results, detail, booking and chaos pages.
- `127.0.0.1:8200` added to the allowed domains, and SkyFare named in the comment next to them.
- 46 tests over the site, driven through ASGI rather than a browser.

Fixed

- The collector masked any field whose name contained `pass`, `pin` or `card` as a substring, so a passenger count read as `••••` and a shipping address was treated as a secret. Known benign words are now stripped from the hint before the secret check runs, which keeps `cardNumber` and `newCreditCard` masked while leaving `passengers` alone.
- README status was still describing the repository as of B06.

### B17 - Task runner and task API

Added

- `app/services/task_runner.py`: one runner per process, started and stopped by the lifespan. Owns Playwright and the Chromium it launches, a semaphore for `AGENT_MAX_CONCURRENT_TASKS`, and the per-task cancel flags. A queued task stays `SUBMITTED` because the orchestrator writes `PLANNING` as its first act. The browser is shared and the context is per task, so one run's cookies and storage cannot reach another's.
- Cooperative cancellation: `CancelSignal` in `app/agent/contracts.py`, checked by the orchestrator at phase boundaries, plus closing the task's browser context so an action already in flight fails at once. Writing `CANCELLED` to the row is what actually stops the task; the signal only makes it prompt.
- Shutdown cancels running tasks and marks anything that did not settle `CANCELLED` with reason "server shutdown". Startup finishes off tasks abandoned by a crash: `EXECUTION_FAILED` with reason "interrupted by restart", or `TIMED_OUT` for one left `AWAITING_APPROVAL`, since nobody answered.
- `app/services/task_service.py`: instruction normalisation, the start-URL allow-list check, duplicate detection, ownership, and the cancel rules. Routers hold no logic.
- `app/api/v1/tasks.py`: `POST /tasks`, `GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/cancel`, `GET /tasks/{id}/steps`, `GET /tasks/{id}/steps/{stepId}` and `GET /tasks/{id}/steps/{stepId}/screenshot`, each with a summary, description and error responses.
- `app/schemas/task.py`: camelCase request and response models with examples on every field, since the frontend generates its TypeScript from `/openapi.json`.
- Duplicate protection: the same user submitting the same normalised instruction while an earlier one is still live gets 409 with the existing task id in the message.
- Screenshots are served only to the owner or an admin, as `image/jpeg` with `Cache-Control: private, max-age=0, no-store`.
- An after-commit hook list on the request session, so work handed to a background worker starts only once the row it will read has been committed.
- `SUBMITTED -> EXECUTION_FAILED` in the task lifecycle, for a task whose browser will not launch and one left queued by a crash. Without it neither had a legal terminal state.
- `make check-all` (lint, typecheck, unit and integration) as the gate before a pull request, with `make check` unchanged as the fast loop.
- 52 tests.

Fixed

- The loop discarded whatever an `extract` action read. `ActionResult.extracted` now reaches the planner through `StepSummary`, capped so a page of text cannot crowd out the current page state. Before this an agent would read the results list, learn nothing, and read it again until the step budget ran out.
- `GET /tasks/{id}/steps/{stepId}` tried to lazy-load three `lazy="raise"` relationships while serialising. Built explicitly from the pieces the service loaded instead.

### B16 - Orchestrator core loop

Added

- `app/agent/orchestrator.py`: the spec section 4.2 loop. Perceive, plan, ground, assess risk, act, verify, record, repeat, ending at `COMPLETED`, `EXECUTION_FAILED`, `TIMED_OUT`, `REJECTED` or `CANCELLED`. Every status change goes through the state machine and publishes a `status` event; a `final` event is published whatever happens, including when the settling write itself fails.
- `app/agent/contracts.py`: the collaborators the loop depends on, each a protocol with a do-nothing default. Verification, recovery, visual grounding, risk policy, approval gate, flow cache and session manager. The approval gate is the exception: its default refuses, because a stub that waves high-risk actions through is a hole that looks like working software.
- `app/agent/browser/agent_browser.py`: `DriverBrowser`, binding the Playwright driver to what the loop asks for (perceive, screenshot, capture state, execute).
- Per step, in one transaction: the screenshot goes to storage first, then `task_steps`, `llm_decisions` and `verifications`, then commit. The `step` event is published only after the commit, so the database is never behind the live view and a reconnecting client can always be caught up.
- `RiskLevel.rank` and `RiskLevel.highest`, because these are strings and `max(HIGH, MEDIUM)` compares alphabetically and answers `MEDIUM`.
- Values typed into password or masked fields are stored and sent as the mask, in `task_steps.value_redacted` and in `llm_decisions.raw_output`, which carries the model's own copy of the value.
- Failure reasons are written by us, never taken from an exception message: a message can carry a URL with a token in it.
- `docs/agent.md`: a sequence diagram of one iteration, why the write order is what it is, and a table of what is still a stand-in.
- 43 tests, 34 unit against fakes and 9 integration against real Postgres and real Chromium.

Fixed

- The whole integration suite. The settings isolation added in B14 also hid `.env` from `integration_settings`, so every integration test tried to reach a Postgres that is not there. Integration settings now read the real environment through a `real_environment` helper, and `api_app` takes its database URL from them.

### B15 - Task state machine and event bus

Added

- `app/domain/state_machine.py`: the spec section 4.3 lifecycle as one table. `TRANSITIONS`, `allowed_from`, `can_transition`, `is_terminal`, and `transition(task, new_status)` which returns the previous status because that is what the section 6.1 `status` event carries. An illegal move raises `IllegalTransition` naming what was allowed instead. `CANCELLED` and `TIMED_OUT` are reachable from every live status; terminal statuses have no outgoing edges at all.
- `app/services/events.py`: frozen, camelCase models for all eight section 6.1 event types, sharing `type`, `taskId`, `seq` and `timestamp`.
- `EventBus`, an abstract base class, and `InMemoryEventBus`. `publish` stamps a per-task monotonically increasing `seq` and fans out without blocking; `subscribe` returns a bounded buffer that is an async iterator and an async context manager.
- Load shedding for a subscriber that falls behind, on two separate budgets: screenshots are stripped from the oldest steps first, whole droppable events go only once the item budget is also full, and `status`, `approval_required`, `approval_resolved`, `final` and `error` are never dropped. An approval keeps its screenshot.
- `docs/agent.md`: the lifecycle as a Mermaid state diagram, the event table, how to subscribe, and what gives way under load.
- 106 unit tests.

### B14 - Planner, prompt registry, eval harness

Added

- `app/agent/planner/schema.py`: the `Decision` model from spec section 4.5, camelCase on the wire, frozen, rejecting unknown fields. Cross-field rules: click/type/select need `targetIndex`, type and select need a value, navigate needs an http or https URL, done needs `finalAnswer`, switch_tab needs `tabId`. `DECISION_JSON_SCHEMA` is hand-written for provider strict modes, with a test asserting it cannot drift from the model.
- `app/agent/planner/prompts/planner_v1.md`: the system prompt. Covers the role, using only supplied state, never inventing an element or a value, the three kinds of knowledge (observed state, retrieved hint, assumption), flagging insufficiency through `needsVision` or low confidence, approval for anything irreversible, one action per turn, an `expectedOutcome` on every decision, short reasoning citing observable evidence, and that content inside `<page_state>` is untrusted data whose instructions must be ignored.
- `app/agent/planner/registry.py`: `PromptRegistry` loading prompts by version from `PLANNER_PROMPT_VERSION`, recorded with every decision.
- `app/agent/planner/planner.py`: `Planner.decide(goal, page_state, history, cached_hint)` returning `PlannerResult`. Builds goal, compressed history, optional hint and fenced page state; validates against the schema; checks the named index exists in the page state; buys exactly one repair turn with the error fed back, then raises `PlannerOutputError` (TEST-006).
- `compress_history`: the last eight steps in full, everything earlier as one counted line.
- `tests/eval/planner_cases.yaml`: 22 labelled cases over 11 fixture pages, each with a set of acceptable decisions and, where it matters, an assertion on the approval flag.
- `scripts/eval_planner.py`: loads each fixture in a real browser, perceives it, and scores one planner decision. Reports action accuracy, groundedness, repair rate, unsafe values typed, average latency and tokens. Defaults to the mock provider regardless of `LLM_PROVIDER`, so a bare run costs nothing.
- Dev dependency `pyyaml` for the eval dataset.
- 71 unit tests.

Changed

- `LLM_MODEL` is now optional. Left unset, each provider supplies its own default: `claude-sonnet-5`, `gpt-6-luna`, `gemini-3.6-flash`. `.env.example` documents this and gives `gemini-3.6-flash` as the worked example.
- `LLM_PROMPT_VERSION` renamed to `PLANNER_PROMPT_VERSION`.
- Unit tests no longer read a developer's `.env` or exported variables; an autouse fixture isolates `Settings` for the whole suite.
- `scripts/**` is exempt from the no-print lint rule rather than carrying a `noqa` on every line.

### B13 - Anthropic, OpenAI and Gemini providers

Added

- Dependencies: `anthropic`, `openai`, `google-genai`.
- `app/agent/llm/anthropic.py`, `openai.py`, `gemini.py`, each implementing `LLMClient.generate_json` with JPEG images, schema-constrained JSON, token usage, latency, and SDK errors mapped onto our own types. API shapes were checked against current documentation and the installed SDKs rather than recalled.
- Per-provider keys as `SecretStr`: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`. A selected provider without its key raises `MissingApiKeyError` at startup, naming the variable to set.
- `merge_images` and `parse_json_payload` in `app/agent/llm/base.py`, shared by all three adapters. The parser tolerates a fenced code block and rejects anything that is not a JSON object.
- `scripts/llm_smoke.py`: one text prompt and one image prompt against the configured provider, printing parsed JSON, token counts and latency.
- A `live` pytest marker for tests that call a real API, skipped unless `LIVE_LLM=1`.
- 38 unit tests, all against fake SDK clients. Nothing in the suite calls a real API.

Changed

- A mock provider in production is now fatal rather than a logged error. An agent on the mock accepts tasks and reports success while making no real decisions.
- `app/core/redaction.py` gained `SAFE_KEYS`, a small exact-match allowlist so token counts (`input_tokens`, `output_tokens` and similar) are not masked. They contain "token" but are measurements, and they are the column names `llm_decisions` already uses.
- `LLM_API_KEY` replaced by the three per-provider keys.

### B12 - LLM adapter base and mock provider

Added

- `app/agent/llm/base.py`: `LLMMessage` (role, text, JPEG images), `LLMUsage`, `LLMResponse`, and the `LLMClient` protocol with a single `generate_json` method. Errors: `LLMTimeout`, `LLMRateLimited` (carrying a server-supplied `retry_after`), `LLMUnavailable` and `LLMBadOutput`.
- `RetryingLLM`: exponential backoff with full jitter, three attempts by default, for transport failures only. `LLMBadOutput` is never retried, because re-sending an identical prompt buys an identical bad answer. Sleep and RNG are injectable so tests neither wait nor flake.
- `FallbackLLM`: tries a second provider when the first is unreachable, but not when the model merely answered badly.
- `app/agent/llm/mock.py`: `MockLLM` with a scripted mode (a queue of decisions) and a rule-based mode that reads the goal and the page summary and picks a sensible action. It records every call, and can be told to fail N times or to return malformed JSON. Zero network.
- `app/agent/llm/factory.py`: `build_llm(settings)` over `LLM_PROVIDER`, with `LLM_FALLBACK_PROVIDER`. A typo and a not-yet-implemented provider give different errors, both at startup.
- Setting `LLM_FALLBACK_PROVIDER`.
- 64 unit tests.

### B11 - DOM summarizer, BeautifulSoup fallback, untrusted wrapping

Added

- `app/agent/perception/state.py`: `PageState` with url, title, tabs, active tab, scroll, elements, summary text, dom hash and capture time. `compute_dom_hash` fingerprints the URL plus ordered element signatures, deliberately excluding position so scrolling does not change it.
- `app/agent/perception/summarizer.py`: `build_summary` and `build_header` producing the spec section 4.6 line format. Selection is by priority under a token budget (default 3000, roughly four characters per token): modal-covered elements, then in-viewport form fields, buttons and links, then below the fold. Selected lines are emitted in index order.
- `app/agent/perception/bs4_fallback.py`: `extract_main_text`, `extract_tables` (row dictionaries), `extract_list_items` and `summarise_static`, with script and hidden content dropped and the same masking rules as the collector, including card-shaped numbers in body text.
- `app/agent/perception/untrusted.py`: `wrap_page_state`, `neutralize` and `strip_invisible`. A page cannot close its own `<page_state>` fence, whatever the casing or spacing, and zero-width, bidi and control characters are removed first.
- `app/agent/perception/perceive.py`: `perceive(page, session)` running extraction, summarisation and hashing in one pass.
- `scripts/perceive.py <url>`: prints the summary, element counts and dom hash, with `--show-fenced` and `--budget`.
- Dependencies: `beautifulsoup4`, `lxml`.
- 90 unit tests.

Changed

- `app/agent/browser/driver.py`: `PageState` renamed to `BrowserSnapshot`, so the cheap before/after snapshot is not confused with the full perception state. `PlaywrightDriver` gained public `tab_ids` and `active_tab_id`.

### B10 - DOM extractor

Added

- `app/agent/perception/dom_extractor.js`, loaded as a package resource and run through `page.evaluate`. Collects visible interactive elements: semantic controls, ARIA roles, `[onclick]`, `[contenteditable]`, `tabindex >= 0`, and anything the page styles with `cursor: pointer`. Nested pointer wrappers are dropped in favour of the outermost meaningful target.
- Per element: index, tag, role, accessible name, text (collapsed, 80 chars), placeholder, input type, value, href (path and query only), bbox in top-level viewport coordinates, `in_viewport`, `covered`, `frame_id`, `css_path`, `xpath`, and disabled/checked/selected/expanded state.
- Exclusion of everything spec section 4.11 requires: `display:none`, `visibility:hidden`, zero opacity, `aria-hidden` ancestors, the `hidden` attribute, elements off the document, and elements smaller than 8px in either axis.
- Value masking at the point of collection for `type=password` and for fields whose name, id or autocomplete suggests a card number, CVV, OTP, PIN or similar.
- Elements covered at their centre point are dropped, unless the thing covering them is a modal, in which case they are kept with `covered=true` so the recovery ladder can dismiss the overlay.
- Same-origin iframes are walked with their offset applied so every bbox shares one coordinate space; cross-origin iframes are listed as opaque entries.
- `data-agent-idx` stamping, with stale stamps from previous runs cleared first.
- `app/agent/perception/dom_extractor.py`: `extract(page) -> list[ElementInfo]` with a time limit and `PageNotReadyError`, plus `locator_for_index(page, index)` which searches every frame.
- Fixtures: `flight_search`, `results`, `cookie_modal`, `hidden_injection`, `iframe` with `iframe_child`, `password_form`, `dynamic`.
- 46 unit tests.

### B09 - Screenshots, storage, budgets

Added

- `pillow` dependency.
- `app/agent/browser/screenshots.py`: `capture(page)` grabs the viewport as PNG, downscales to `SCREENSHOT_MAX_WIDTH` (1280) preserving aspect ratio, and encodes JPEG at `SCREENSHOT_JPEG_QUALITY` (60), matching spec section 6.1. `Screenshot` carries the image and the viewport dimensions separately so a client can map a bbox onto a scaled frame. `to_base64()` for the WebSocket payload. Compression runs off the event loop.
- `app/services/storage.py`: the `ScreenshotStorage` protocol and `LocalFileStorage` under `STORAGE_DIR/screenshots/{task_id}/{step:04d}.jpg`. Refs are opaque relative strings validated against an anchored pattern, with a containment check behind it for symlinks. Writes go to a temporary name and are renamed, so no reader sees a partial file. All filesystem calls run in a worker thread.
- `app/agent/budget.py`: `TaskBudget` with `check()`, `consume_step()` and `reset()`, raising `BudgetExceeded(reason=MAX_STEPS|MAX_TIME)`. Monotonic, injectable clock.
- Settings `SCREENSHOT_MAX_WIDTH` and `SCREENSHOT_JPEG_QUALITY`.
- `make test-fast` (`pytest tests/unit -m "not browser"`) for a tight edit loop, documented in the README.
- 71 new unit tests (19 budget, 17 screenshot, 35 storage).

Changed

- `STORAGE_LOCAL_PATH` renamed to `STORAGE_DIR`.

### B08 - Playwright driver and action executor

Added

- `playwright` dependency and Chromium. `make browsers` installs it, `make install` now does too, and the README lists it as a prerequisite.
- `app/agent/browser/actions.py`: a frozen, `extra="forbid"` Pydantic model per allow-listed action, as a discriminated union. `navigate` and `new_tab` accept only http/https; `wait` is capped at 10 s; `scroll` at 10000 px. `parse_action()` raises `ActionNotAllowedError` for anything outside spec section 4.4.
- `app/agent/browser/errors.py`: `ActionErrorCode` (TIMEOUT, ELEMENT_NOT_FOUND, NOT_INTERACTABLE, NAVIGATION_FAILED, DOMAIN_NOT_ALLOWED, ACTION_NOT_ALLOWED, TAB_NOT_FOUND, UNKNOWN), `ActionNotAllowedError`, `DomainNotAllowedError`.
- `app/agent/browser/driver.py`: the `BrowserDriver` protocol and `PlaywrightDriver`. One isolated `BrowserContext` per task with `accept_downloads=False`, viewport 1280x800, locale en-IN, timezone Asia/Kolkata, headless and timeouts from settings. `ActionResult` carries ok, error_code, error_message, duration_ms, url_before, url_after. Popups are tracked as switchable tabs.
- `host_is_allowed()`: the spec section 4.13 sandbox check, on the parsed hostname. A bare host matches any port; a port-qualified entry matches only that port; an empty allow-list blocks everything.
- Settings `BROWSER_ACTION_TIMEOUT_MS` and `BROWSER_NAVIGATION_TIMEOUT_MS`; `AGENT_ALLOWED_DOMAINS` now lists the demo port.
- `tests/fixtures/html/`: simple_form, buttons, long_page, select, download; a session-scoped fixture serving them on a random localhost port. No test reaches the internet.
- 96 new unit tests (51 action, 45 browser), marked `browser` where they drive Chromium.

### B07 - Refresh rotation, RBAC, rate limiting

Added

- Login now sets an httpOnly refresh cookie: an opaque 384 bit token, stored only as its SHA-256 hash, `SameSite=Lax`, `Secure` when `APP_ENV=prod`, scoped to `path=/api/v1/auth`.
- `POST /api/v1/auth/refresh`: single-use rotation. The presented token is revoked and points at its successor via `replaced_by_id`.
- Reuse detection: presenting an already-revoked token revokes every session for that user, writes a `TOKEN_REUSE_DETECTED` audit row, and returns 401.
- `POST /api/v1/auth/logout`: revokes the token and clears the cookie, idempotent, always 204.
- `GET /api/v1/auth/me`.
- `require_roles(*roles)` in `app/api/deps.py`: 403 for the wrong role, 401 for no token. ADMIN is not implied; each route lists what it accepts.
- `app/core/rate_limit.py`: a `RateLimiter` interface with an in-memory token bucket, keyed on route plus client address, with idle-bucket eviction. Applied at 5/min to login and 3/min to register; 429 in the error envelope with a `Retry-After` header.
- `scripts/create_admin.py`: creates an ADMIN or APPROVER from the CLI, prompting for the password rather than taking it as an argument.
- `AppError` carries response headers, so 429 can send `Retry-After`.
- Settings: `RATE_LIMIT_ENABLED`, `RATE_LIMIT_LOGIN_PER_MINUTE`, `RATE_LIMIT_REGISTER_PER_MINUTE`, `RATE_LIMIT_TASK_CREATE_PER_MINUTE`, `RATE_LIMIT_TRUST_FORWARDED_FOR`.
- `docs/auth.md` gains the rotation and reuse diagram, the cookie table, the roles section and the rate limiting section.
- 12 unit tests and 39 integration tests.

Changed

- Reuse handling runs in the request's own transaction and the route returns the 401 rather than raising it, so the chain revocation commits. Raising would have rolled it back at exactly the wrong moment.
- Audit metadata uses `session_id` rather than `token_id`: the redaction rule matches any key containing "token" and was masking the identifier a security review needs.

### B06 - Auth: JWT, JWKS, register, login

Added

- Dependencies: `pyjwt[crypto]`, `argon2-cffi`.
- `scripts/gen_keys.py`: generates an RSA 2048 keypair into `keys/` (gitignored, private key 0600), prints the env lines, and refuses to overwrite without `--force`.
- `app/core/jwt_keys.py`: `KeyPair`, `load_key_pair`, and the JWK/JWKS document (`kty`, `kid`, `use`, `alg`, `n`, `e`, base64url unpadded). Keys are loaded once at startup and the service fails to boot without them.
- `app/core/security.py`: argon2id `hash_password` / `verify_password`, and `TokenService.create_access_token` / `decode_access_token`. Tokens carry `sub`, `role`, `iss`, `aud`, `iat`, `exp`, `jti` with `kid` in the header; decode pins `algorithms=["RS256"]` and requires every claim.
- `GET /.well-known/jwks.json` at the root path, unauthenticated, cached five minutes.
- `POST /api/v1/auth/register`: email normalised to lowercase, password policy (min 10, not all digits, max 256), duplicate email 409, role `TASK_SUBMITTER`, response never carries the hash.
- `POST /api/v1/auth/login`: returns `accessToken`, `tokenType`, `expiresIn`, `user`. Unknown email and wrong password fail identically, and an unknown email still runs an argon2 verification so the timing does not differ.
- `get_current_user` bearer dependency, plus `SettingsDep`, `KeyPairDep`, `TokenServiceDep`, `AuthServiceDep`.
- `app/schemas/base.py` (`CamelModel`) and `app/schemas/auth.py`; responses are camelCase on the wire.
- `InvalidCredentials` error (401 `INVALID_CREDENTIALS`) and the `LOGIN_FAILED` audit action.
- `docs/auth.md`: two sequence diagrams, the token contents, what decode rejects, and why the failed-login audit needs its own transaction.
- 23 unit tests and 39 integration tests, including `alg: none`, the RS256-to-HS256 downgrade, tampered payload and signature, wrong issuer and audience, and a token verified using only the published JWKS.

Changed

- Settings: `JWT_ACCESS_TTL_SECONDS` and `JWT_REFRESH_TTL_SECONDS` replaced by `ACCESS_TOKEN_TTL_MIN=15` and `REFRESH_TOKEN_TTL_DAYS=7`, with `access_token_ttl` / `refresh_token_ttl` properties. Added `JWT_AUDIENCE` and `JWT_KID`.
- Integration tests clean committed audit rows before each test, because the failed-login audit deliberately commits outside the test transaction.

### B05 - Repositories and audit service

Added

- `app/repositories/base.py`: generic `BaseRepository[Model]` (`get`, `get_or_404`, `add`, `add_all`, `delete`, `count`, `list`, `paginate`) and a `Page` result carrying `items`, `total`, `page`, `page_size` plus `total_pages`, `has_next`, `has_previous`. Page size is capped at 100 (decision A17).
- `UserRepository` (`get_by_email`, case-insensitive; `email_exists`) and `RefreshTokenRepository` (`get_by_token_hash`, `list_active_for_user`, `revoke` with rotation chain, `revoke_all_for_user`).
- `TaskRepository`: `get_owned` (NotFound, not Forbidden, for other users' tasks; admins exempt), `list_for_user` with status, date range and escaped text search, `get_by_number`, `list_active`, `count_active_for_user`.
- `TaskStepRepository`: `next_step_number`, `list_for_task`, `list_after` (WebSocket replay), `get_by_number`.
- `LlmDecisionRepository`, `VerificationRepository`, `RecoveryAttemptRepository` (including `list_for_task` and `next_attempt_number`), `ApprovalRepository` (`get_pending_for_task`, `list_for_task`, `list_expired`), `AuditEventRepository`, `CachedFlowRepository` (`get_by_signature`, `upsert` via `ON CONFLICT`, `record_run`, `should_invalidate`).
- `app/repositories/unit_of_work.py`: `UnitOfWork` bundling every repository over one session, with a `UnitOfWorkDep` FastAPI dependency. Deliberately has no `commit()`.
- `app/services/audit.py`: `AuditService.record()` plus `AuditAction` constants. Metadata is redacted before it is stored.
- `app/core/redaction.py`: the masking rules, extracted from `app/core/logging.py` now that the audit trail is a second consumer. Logs and audit rows mask identically, asserted by a test.
- 32 integration tests covering every repository method, pagination maths, ownership, filters, and audit redaction.

Fixed

- `CachedFlowRepository.upsert` returned the stale identity-mapped instance after `ON CONFLICT DO UPDATE`; it now uses `populate_existing` so the returned object reflects what the database holds.

### B04 - Data model and initial migration

Added

- `app/domain/enums.py`: `UserRole`, `TaskStatus` (with `is_terminal`), `StepStatus`, `ActionType`, `RiskLevel`, `ApprovalStatus`, `VerificationMethod`, `RecoveryOutcome`. All stored as native Postgres enum types holding the member value, not its name.
- `app/models/`, grouped by aggregate: `user.py` (User, RefreshToken), `task.py` (Task, TaskStep, LLMDecision, Verification, RecoveryAttempt), `approval.py`, `audit.py`, `cache.py`.
- `refresh_tokens`, an addition to spec section 5, now documented there: refresh rotation needs a server-side record so a stolen token can be revoked and reuse of a rotated one detected. Only the hash is stored.
- `task_number` generated by the Postgres sequence `task_number_seq`, formatted `T-000001`.
- Migration `a78013bebd3a`, hand-corrected after autogenerate. Upgrade, downgrade and re-upgrade all verified against a throwaway database.
- `docs/database.md`: Mermaid ER diagram and a table-by-table explanation.
- `CreatedAtMixin` split out of `TimestampMixin`, for the tables that carry only `created_at`; `enum_column()` helper in `app/db/base.py`.
- 48 new tests (99 unit, 32 integration).

Changed

- `docs/PROJECT_SPEC.md` section 5 documents `refresh_tokens` and why it exists.
- The B03 migration test now compares against alembic's head rather than a hardcoded revision.

Notes

- Indexes are exactly those spec section 5 lists, plus `unique(task_id, step_number)` and, for the added table, `refresh_tokens.token_hash` and `user_id`. `approvals.step_id`, `tasks.created_by` and `audit_events.actor_id` are deliberately unindexed.
- All relationships are `lazy="raise"`: nothing loads implicitly.

### B03 - Postgres, async database layer, Alembic

Added

- `docker-compose.dev.yml`: postgres:16 with a named volume, a `pg_isready` healthcheck, and `scripts/db/init.sql` creating `agent_core`, `agent_core_test` and `payments` on first boot. `make db-up` waits for healthy.
- Dependencies: sqlalchemy[asyncio] 2.x, asyncpg, alembic.
- `app/db/base.py`: `Base` with a constraint naming convention, `UUIDPrimaryKeyMixin` (client and server defaults) and `TimestampMixin` (timezone-aware, server defaults).
- `app/db/session.py`: `Database` owning the async engine and session factory, a `session()` context manager that commits or rolls back, a `healthcheck()` that runs `SELECT 1` under a timeout and never raises, and the `get_database` / `get_session` dependencies.
- Alembic in `migrations/` on the async template. `env.py` takes its URL from `Settings` and imports `app.models` for autogenerate. Baseline revision `0001_baseline`.
- Makefile: `revision`, `downgrade`, `db-reset`; `db-up` now waits for the healthcheck and reports the published port.
- Health reports `db: ok | down` and returns 503 when Postgres is unreachable.
- Test fixtures: a stub database for unit tests, a session-scoped engine against `agent_core_test` that builds and drops the schema, and a per-test transactional session using `join_transaction_mode="create_savepoint"`.
- 10 integration tests and 51 unit tests.

Changed

- Database credentials moved to `POSTGRES_*` settings; `database_url` and `test_database_url` are derived from them, with `DATABASE_URL` still available as an override. compose and the app read the same variables, so they cannot drift.
- `migrations/script.py.mako` rewritten so generated revisions satisfy ruff (modern typing, no trailing whitespace); `make revision` formats its output.
- `docs/BACKEND_PLAN.md` section 2: `alembic/` corrected to `migrations/`.
- README quick start covers the database, and documents the port 5432 conflict.

### B02 - App core, errors, logging, health

Added

- Runtime dependencies: fastapi, uvicorn[standard], pydantic, pydantic-settings, structlog, httpx.
- `app/core/config.py`: `Settings` over pydantic-settings with `.env` support, covering app, database, JWT, LLM, agent budget, billing and storage groups. Secrets are `SecretStr`; list settings accept comma-separated values; `get_settings()` is cached.
- `app/core/logging.py`: structlog with JSON rendering in prod and a console renderer in dev, `request_id` merged from contextvars, and a `redact_sensitive` processor that masks password, token, authorization, api_key, secret, card, cvv, screenshot and similar keys at any depth.
- `app/core/errors.py`: `AppError` base plus `ValidationFailed` (400), `Unauthorized` (401), `InsufficientCredits` (402), `Forbidden` (403), `NotFound` (404), `Conflict` (409), `UnprocessableAction` (422), `RateLimited` (429) and `DependencyUnavailable` (503). Handlers render the spec section 6 envelope for `AppError`, request validation, bare `HTTPException` and anything unhandled.
- `app/core/middleware.py`: `RequestContextMiddleware` honours an incoming `X-Request-ID` or mints `req_<12 hex>`, echoes it on the response, binds it for logging, and emits one access line with method, path, status and duration_ms.
- `app/api/deps.py`: `SettingsDep`, reading settings from app state so `create_app(settings)` configures handlers too.
- `app/main.py`: `create_app()` factory with a lifespan hook carrying startup and shutdown placeholders for the database, browser and task runner, CORS from settings, and the v1 router mounted under `API_PREFIX`.
- `GET /api/v1/health` returning status, version, env and time.
- 49 unit tests across config, logging redaction, error envelopes, request-id propagation and health.

Changed

- `.env.example` now documents all 40 settings, enforced by a test.
- `docs/BACKEND_PLAN.md` section 2 corrected: `models/` and `repositories/` are siblings of `db/`, and the api and core subtrees match the repo.

Notes

- Request validation returns **400**, not FastAPI's default 422. Spec section 6 assigns 400 to validation and reserves 422 for an action the agent cannot carry out.

### B01 - Repo scaffolding

Added

- `CLAUDE.md` with the permanent working rules (source of truth, async and typing standards, architecture direction, secret and logging bans, definition of done).
- uv application project pinned to Python 3.12, with ruff, mypy, pytest, pytest-asyncio, pytest-cov and pre-commit as dev dependencies.
- Tooling configuration in `pyproject.toml`: ruff at line length 100, mypy strict over `app/`, pytest with `asyncio_mode=auto`, coverage scoped to `app`.
- Empty package structure for `app/` (api, core, db, models, schemas, repositories, services, domain) and `app/agent/` (browser, perception, vision, planner, llm, safety, verification, recovery, cache).
- Repo-root directories: `demo_sites/`, `tests/` (unit, integration, chaos, eval, fixtures), `scripts/`, `docs/` (adr, phase-notes, benchmarks), and a gitignored `storage/`.
- `Makefile` with install, dev, test, test-unit, test-int, lint, fmt, typecheck, check, migrate, db-up, db-down.
- `.gitignore`, `.editorconfig`, `.env.example`, and `.pre-commit-config.yaml` with ruff, ruff-format, end-of-file, trailing-whitespace and a gitleaks secret scanner.
- `README.md` skeleton and this changelog.
- Smoke tests so the suite runs and the async mode is proven.


## Local reliability review — 2026-11-21

- Fixed verification that treated unchanged pages as successful progress.
- Serialized refresh-token rotation; handled competing registrations and long user-agent headers. Moved password hashing and verification off the event loop.
- Committed request transactions before successful responses, with regression coverage for commit failures.
- Cleaned resources after failed startup, validated provider configuration before accepting tasks, and enforced the configured per-user concurrency limit.
- Added an optional default start URL for prompt-only frontend submissions; rejected malformed navigation URLs without server errors.
- Reserved credits for replays, repaired special-character database credentials and Alembic URL escaping, and protected test/migration databases from application database overrides.
- Updated eight vulnerable locked dependencies, fixed lint issues and browser-test markers, and added concurrency, lifecycle, URL and billing regression coverage.
- Added a runnable API/WebSocket/browser smoke test and a local presentation guide.
- Fixed macOS editor replacement shortcuts and missing-selector extraction failures; kept the current browser intact when a fallback cannot launch. Browser setup now installs Chromium and Firefox.
- Updated multi-tab and overlay tests to check the current behavior, and exercised recovery with a real injected missing-element failure.
- Pinned Playwright 1.65.2 and aligned the Docker image. The local Firefox profile-startup issue persists despite the upstream update and is documented explicitly.
- Final full suite: 1,777 passed, one skipped (live provider credentials), one failed (local Firefox startup). All integration and chaos tests pass; the live Chromium demo passes. Added a direct browser diagnostic.

### Input and scrolling follow-up

- Fixed append semantics, partial-value acceptance, hidden input selection and unsafe keyboard fallback targets.
- Scroll active autocomplete lists, focused panels and visible dialogs before the background page, using the same metrics for verification.
- Added local cab-form regression coverage. Live Uber reproduction remains unverified pending the actual task and real-provider configuration; see `docs/phase-notes/input-and-scroll-reliability.md`.
- Verified all 20 focused input/scroll cases across the focused run and corrected cab-form recheck; the full suite has not been rerun after this follow-up.

### Gemini live-task reliability — 2026-11-22

- Include bounded visible page text inside the planner's untrusted-data fence, so static prices and confirmations can be read alongside interactive controls.
- Add a structured `goalAchieved` result. A planner that stops with an unmet goal now produces `EXECUTION_FAILED` and cannot save a successful replay.
- Treat invalid Gemini keys and denied API access as actionable, non-retryable errors without exposing vendor response bodies in exception tracebacks.
- Try configured Gemini backup models after transient provider unavailability, including HTTP 503; briefly skip the unavailable primary.
- Make the local cab fixture show a visible selection confirmation, and explicitly request flight number and price in the live smoke test.
- Regression checks: 1,566 non-browser unit/integration tests passed before the final HTTP-503 fallback adjustment; 237 focused tests then passed including the fallback regression. Live rechecks and CI results are recorded in the phase note.

### Verification follow-up — 2026-11-25

- Passed real Gemini cab-form confirmation and full SkyFare API/browser smoke tests.
- Allow-list only the exact historical DOM-test dummy token in the secret audit.
- Prevent the offline mock from endlessly retyping a masked field after a verified write; add deterministic coverage for randomly generated IDs containing secret-like substrings.
- Preserve field masking and all other secret-scanning rules.

### Auto-sizing address inputs

- A real Uber pre-booking probe exposed native address inputs only 2px wide inside full-size select widgets. The collector's minimum-size filter hid them from the planner.
- Retain non-zero text/search inputs inside visible Base Web select or ARIA combobox wrappers, including inputs that also carry a combobox role. Keep hidden, zero-size and unrelated tiny controls excluded.
- Add a browser regression that finds and fills this layout and checks the exclusion rules.

### Autocomplete verification

- Real Uber now accepts both pickup and destination input. The successful pre-booking run selected PST hostel and Polaris School of Technology without requesting a ride.
- Verify a disappearing autocomplete option against its declared result instead of requiring its vanished selected-state flag. Unchanged checkbox/disclosure checks still fail correctly.
- Guide the planner to use accessible labels, rather than invented CSS selectors, for visibility expectations.
- Correct the auto-sized-input regression to distinguish the native input from its identically named combobox wrapper. Added positive/negative selected-result verification tests; 221 focused tests pass.

### Login handoff verification

- Recognize Uber's “Log in to see ride options” message before calling the planner.
- Do not treat any mention of passwords/credentials as permission to automate sign-in; explicit sign-in intent is required to bypass the manual login gate.
- Passed 99 orchestrator tests and a real local API/Chromium/WebSocket handoff test using the observed login message, with no model calls or typed credentials.
- The final real Uber probe reached Choose a ride for the correct route, then displayed a login requirement. Its next model call was rate-limited. Actual fares, account login and booking remain unverified.

### Cancellation during human handoff — 2026-11-26

- Wake a task's approval wait on cancellation instead of leaving it alive until the human-response timeout. Expire the pending approval and release waiters.
- Give cancellation priority over a simultaneous approval, so the action cannot execute after cancel and the client receives a cancelled final outcome.
- Added regression coverage for cancellation while waiting, cancellation before registration, and an approval/cancel race. All 128 focused approval, runner and orchestrator tests passed.
- Make the browser integration fixture's scripted sign-in intent explicit. Its password field otherwise correctly triggers the manual-login handoff before the persistence/action assertions can run; password writes still require action approval and remain redacted.
