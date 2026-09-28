# Changelog

All notable changes to the backend. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); parts are tracked by their id.

## [Unreleased]

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
