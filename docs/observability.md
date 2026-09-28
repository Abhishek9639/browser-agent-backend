# Observability

What this service publishes, where to look when something is wrong, and the
alert rules worth having. Spec section 9 lists the KPIs; this page is how they
are measured and what to do when they move.

## The three surfaces

| Surface | Path | Who can read it |
|---|---|---|
| Prometheus exposition | `GET /metrics` | An ADMIN bearer token, or `X-Internal-Token` when `METRICS_INTERNAL_TOKEN` is set |
| Task statistics | `GET /api/v1/stats/overview` | Any user, for their own tasks; an ADMIN may pass `?scope=all` |
| Structured logs | stderr, JSON in production | Whoever collects them |

`/metrics` is at the root rather than under `/api/v1`, per decision A9. It is
not public: the exposition says how many tasks fail, how long the model takes
and what it costs, which is commercial information even though none of it is
personal.

## Metrics

### The API

| Metric | Type | Labels |
|---|---|---|
| `http_request_duration_seconds` | histogram | `route`, `method`, `status` |
| `http_errors_total` | counter | `route`, `method`, `status` |

`route` is the route template with parameters put back (`/api/v1/tasks/{task_id}`),
and an unmatched request is labelled `unmatched` rather than by its path. Both
rules exist for the same reason: a label taken from a URL grows one time series
per task, or per probe from whatever is scanning the internet today.

### The agent

| Metric | Type | Labels |
|---|---|---|
| `tasks_total` | counter | `status` |
| `task_duration_seconds` | histogram | `status` |
| `steps_per_task` | histogram | `status` |
| `verification_total` | counter | `result`, `method` |
| `recovery_attempts_total` | counter | `strategy`, `outcome` |
| `recovery_episodes_total` | counter | — |
| `recovery_rescued_total` | counter | — |
| `recovery_success_ratio` | gauge | — |
| `vision_invocations_total` | counter | `reason`, `outcome` |
| `approvals_total` | counter | `outcome` |
| `cache_lookups_total` | counter | `result` |
| `active_tasks` | gauge | — |
| `queued_tasks` | gauge | — |

**Recovery is counted per failed step, not per attempt.** A step rescued on the
third rung is one success, not one success and two failures. The episode
counters are the honest denominator for the section 9 recovery rate, and
`recovery_success_ratio` is derived from them for convenience; prefer the
counters for any window other than "since this process started".

`cache_lookups_total` distinguishes `hit`, `miss` and `ineligible`. A flow that
exists but is below the success-rate threshold is not a hit: counting it as one
would make the rate describe how often a signature matched rather than how often
the cache helped.

### The model

| Metric | Type | Labels |
|---|---|---|
| `llm_request_duration_seconds` | histogram | `provider`, `model`, `purpose` |
| `llm_tokens_total` | counter | `provider`, `model`, `direction` |
| `llm_errors_total` | counter | `provider`, `model`, `error` |

`purpose` is `planner` or `vision`. Tokens are counts: nothing about a prompt or
a completion is published anywhere. A call that raised is timed as well as
counted, because dropping timeouts from a latency histogram makes a provider
look better the worse it gets.

### The live view

`ws_delivery_latency_seconds` measures from the bus stamping a step event to a
client being sent it. Spec section 9 wants the live view within one to two
seconds of the action, and this is the half of that number this service
controls. Replayed events are excluded: their timestamp is when the replay was
built, so including them would fill the histogram with near-zero values that
mean nothing.

## Logs

Every line carries `request_id` when it belongs to a request and `task_id` when
it belongs to a run, both bound through contextvars rather than passed by hand,
so a log statement added in a later part gets them without anyone remembering.
Inside a step, `step_number` is bound too. A decision carries `model_name`,
`provider` and `prompt_version`; a failed action carries `error_code`; a status
change carries `status`.

What never appears, per spec section 10: page content, the compact page summary,
screenshots, typed passwords or card numbers, JWTs and API keys. This is
enforced centrally by a redaction processor and checked by
`tests/unit/test_log_hygiene.py`, which runs a whole mock task against a page
full of personal information and a password field, captures every line through
the real processor chain, and searches it.

Useful queries, in whatever collector you use:

```
event="action_failed" AND task_id="<id>"        # what went wrong on one task
event="planned" AND confidence<0.6              # where the model was unsure
event="recovery_succeeded"                      # which rung is earning its place
event="cache_hit"                               # what replayed, and how much of it
```

## Alert rules

Examples, not a tuned production set: the thresholds assume the section 9
targets and a deployment small enough that one process is the whole service.

```yaml
groups:
  - name: browser-agent
    rules:
      # A page the agent cannot act on. Verification failures are normal in
      # ones and twos, because the ladder exists for them; a sustained rate
      # means a site has changed under us.
      - alert: ActionsFailingRepeatedly
        expr: |
          sum(rate(verification_total{result="failed"}[10m]))
            / clamp_min(sum(rate(verification_total[10m])), 0.001) > 0.4
        for: 15m
        labels: {severity: warning}
        annotations:
          summary: "More than 40% of steps are failing their check"
          runbook: "Check recovery_attempts_total by strategy: one rung carrying
            everything usually means a selector convention changed."

      # The API itself, excluding agent work.
      - alert: HighApiErrorRate
        expr: |
          sum(rate(http_errors_total{status=~"5.."}[5m]))
            / clamp_min(sum(rate(http_request_duration_seconds_count[5m])), 0.001) > 0.05
        for: 10m
        labels: {severity: critical}
        annotations:
          summary: "Over 5% of API requests are returning 5xx"

      # The model provider is down, rate limiting us, or timing out.
      - alert: LlmDependencyFailing
        expr: sum(rate(llm_errors_total[5m])) by (provider) > 0.2
        for: 10m
        labels: {severity: critical}
        annotations:
          summary: "{{ $labels.provider }} is failing more than one call in five minutes"
          runbook: "Vision degrades to DOM-only on its own (section 4.9). A
            failing planner stops tasks, so consider switching LLM_PROVIDER."

      # Decision latency. The p95 target for the API excludes model work, so
      # this is the model's own budget and is deliberately generous.
      - alert: DecisionLatencyAbnormal
        expr: |
          histogram_quantile(
            0.95,
            sum(rate(llm_request_duration_seconds_bucket{purpose="planner"}[10m])) by (le, provider)
          ) > 15
        for: 15m
        labels: {severity: warning}
        annotations:
          summary: "Planner p95 above 15s on {{ $labels.provider }}"

      # The headline KPI: section 9 asks for 80% end to end.
      - alert: CompletionRateBelowBenchmark
        expr: |
          sum(rate(tasks_total{status="COMPLETED"}[1h]))
            / clamp_min(sum(rate(tasks_total[1h])), 0.001) < 0.8
        for: 30m
        labels: {severity: warning}
        annotations:
          summary: "Completion rate under the 80% benchmark for half an hour"

      # Recovery, the other section 9 target.
      - alert: RecoveryRateBelowBenchmark
        expr: |
          sum(rate(recovery_rescued_total[1h]))
            / clamp_min(sum(rate(recovery_episodes_total[1h])), 0.001) < 0.7
        for: 30m
        labels: {severity: warning}
        annotations:
          summary: "Fewer than 70% of failed steps are being rescued"

      # Capacity. Three concurrent tasks is the default cap, so a queue that
      # never drains means the cap is wrong or a task is stuck.
      - alert: TasksQueueing
        expr: queued_tasks > 0
        for: 15m
        labels: {severity: warning}
        annotations:
          summary: "Tasks have been waiting for a slot for 15 minutes"
```

### One rule that cannot be a Prometheus rule

"Repeated action failures **on one task**" is a per-task question, and no metric
here is labelled by task id: that label is exactly the unbounded one, and adding
it to catch this would trade a memory leak for an alert.

Three things cover it instead, in the order they act:

1. **The step budget.** A task that keeps failing runs out of steps and ends
   `EXECUTION_FAILED` with a reason naming the action, the target and every rung
   that was tried. The task does not spin.
2. **The recovery ladder's own budget**, three attempts per step, so one bad
   selector cannot consume a run.
3. **A log query**, which is where a per-task question belongs:
   `event="action_failed"` grouped by `task_id`, alerting above a handful in a
   run. The fields are there because every line inside a run carries its task id.

The aggregate rule above is the Prometheus-shaped version of the same signal: if
several tasks are failing steps at once, it is the site, not the task.
