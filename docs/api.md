# API reference

Generated from the application's own OpenAPI schema, plus the WebSocket events,
which OpenAPI cannot describe. The schema itself is `docs/openapi.json`, written
from the same run that produced this page, and the interactive version is at
`/docs` when the service is not in production.

Base path `/api/v1`, except `/metrics` and `/.well-known/jwks.json`, which are at
the root (decision A9).

## Authentication

An access token in `Authorization: Bearer <token>`, RS256, fifteen minutes. The
refresh token is an httpOnly cookie and is rotated on every use; a reused one
revokes the whole chain. The public key is at `/.well-known/jwks.json` so the
payments service can verify a token without calling this one.

Two endpoints take something else. `GET /metrics` takes an ADMIN token **or**
`X-Internal-Token`, for a Prometheus scraper that has no account. `WS /tasks/{id}/stream`
takes a single-use ticket in the query string, because a browser cannot set a
header on a WebSocket handshake (ADR-009).

## Errors

Every error is the same envelope, with the request id so a user can quote one
string and have it found in the logs.

```json
{
  "error": {
    "code": "INSUFFICIENT_CREDITS",
    "message": "This task needs 50 credits and you have 12.",
    "requestId": "req_ab12cd34ef56",
    "details": {"available": 12, "required": 50}
  }
}
```

`details` is present only where numbers help. The codes in use:

| Status | Code | When |
|---|---|---|
| 400 | `VALIDATION_FAILED` | The body or a query parameter is not acceptable |
| 401 | `UNAUTHORIZED` | No token, a bad token, or an expired one |
| 402 | `INSUFFICIENT_CREDITS` | The wallet cannot cover the task's reservation |
| 403 | `FORBIDDEN` | Authenticated, and the role does not allow it |
| 404 | `NOT_FOUND` | No such thing, or not yours: the two are deliberately alike |
| 409 | `CONFLICT` | A duplicate live task, or an approval already answered |
| 413 | `PAYLOAD_TOO_LARGE` | The request body is over `MAX_REQUEST_BYTES` |
| 429 | `RATE_LIMITED` | Carries `Retry-After` |
| 503 | `BILLING_UNAVAILABLE` | Payments could not be reached while billing is on |
| 503 | `DEPENDENCY_UNAVAILABLE` | The database is not answering |

## Endpoints


## Auth


### `POST /api/v1/auth/login`

**Exchange credentials for an access token and a refresh cookie**


Authenticate, mint a 15 minute access token, and set the refresh cookie.


Body: `LoginRequest`


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 401 | Email or password is incorrect |
| 422 | Validation Error |
| 429 | Too many attempts |


### `POST /api/v1/auth/logout`

**Revoke the refresh token and clear the cookie**


Idempotent.

Logging out is a request to end a session; a client that already lost its
cookie has achieved that, so an unknown token is not an error.

Returns nothing so FastAPI keeps the injected response, which is what
carries the cookie-clearing header.


| Status | Meaning |
|---|---|
| 204 | Successful Response |


### `GET /api/v1/auth/me`

**The authenticated user**


Read from the database, not from the token.

The role is whatever the row says now, so a demotion applies immediately
rather than when the access token happens to expire.


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 401 | Missing or invalid access token |


### `POST /api/v1/auth/refresh`

**Rotate the refresh cookie for a new access token**


Single-use rotation.

The presented token is revoked and replaced. Presenting an already-revoked
token destroys every session for that user: two parties holding one cookie
means one of them stole it, and there is no way to tell which.

Reuse is caught here rather than left to the global handler. The service
revokes the chain in this request's transaction, and an exception escaping
the route would roll that back, throwing away the revocation at exactly the
moment it matters. Returning the 401 lets the request finish and commit.


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 401 | Missing, invalid, expired or already-used token |


### `POST /api/v1/auth/register`

**Create an account**


Register a TASK_SUBMITTER.

The response is built from an explicit schema rather than dumping the
model, so the password hash cannot leak by accident.


Body: `RegisterRequest`


| Status | Meaning |
|---|---|
| 201 | Successful Response |
| 409 | Email already registered |
| 422 | Validation Error |
| 429 | Too many attempts |


## Tasks


### `GET /api/v1/tasks`

**List your tasks**


Newest first. Admins see every task; everyone else sees their own. `status` may be repeated to match any of several states.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `status` | query | no | Repeatable. Matches any of the given states. |
| `from` | query | no | Only tasks created at or after this time. |
| `to` | query | no | Only tasks created at or before this time. |
| `q` | query | no | Case-insensitive search of the instruction. |
| `page` | query | no |  |
| `pageSize` | query | no |  |


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


### `POST /api/v1/tasks`

**Submit a task**


Queues a task and returns it in SUBMITTED. The agent starts when a slot frees, so a task can sit in SUBMITTED for a while on a busy server. Follow it with GET /tasks/{id} or the live stream.


Body: `CreateTaskRequest`


| Status | Meaning |
|---|---|
| 201 | Successful Response |
| 400 | The instruction is missing, too short or too long, or the start URL is not http(s) or not in the allowed domain list |
| 409 | You already have a live task with this instruction |
| 422 | Validation Error |
| 429 | Too many submissions |


### `GET /api/v1/tasks/{task_id}`

**Get one task**


| Parameter | In | Required | Notes |
|---|---|---|---|
| `task_id` | path | yes |  |


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 404 | No such task, or it belongs to someone else |
| 422 | Validation Error |


### `POST /api/v1/tasks/{task_id}/approve`

**Approve a high-risk action**


Lets a task that is parked on a human carry on. Allowed for APPROVER and ADMIN, and for the task's own submitter when the deployment permits it.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `task_id` | path | yes |  |


Body: `ApprovalDecisionRequest`


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 403 | Your role does not allow approving |
| 404 | No such task, or it is not waiting for an approval |
| 409 | That approval has already been answered |
| 422 | Validation Error |
| 429 | Too many approval decisions |


### `GET /api/v1/tasks/{task_id}/cache-match`

**Is there a cached flow for this task?**


The task signature and, if one is stored, the flow's statistics. `eligible` is whether an ordinary run would replay it: a flow below the configured success rate is kept and not used, until three runs have passed judgement on it and it is deleted.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `task_id` | path | yes |  |


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 404 | No such task, or it belongs to someone else |
| 422 | Validation Error |


### `POST /api/v1/tasks/{task_id}/cancel`

**Cancel a task**


Stops a task that is still running. The agent finishes the step it is on, records it, and stops there, so the activity log always matches what the browser actually did.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `task_id` | path | yes |  |


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 404 | No such task, or it belongs to someone else |
| 409 | The task has already finished |
| 422 | Validation Error |


### `POST /api/v1/tasks/{task_id}/ground`

**Ask vision where something is on the current page**


Runs visual grounding against the page the agent is looking at right now and returns what it found. It does not click, type or otherwise act. Only works while the task is running on this worker.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `task_id` | path | yes |  |


Body: `GroundRequest`


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 404 | No such task, or it belongs to someone else |
| 409 | The task is not running, so it has no current page |
| 422 | Validation Error |


### `GET /api/v1/tasks/{task_id}/recovery-attempts`

**List a task's recovery attempts**


Every rung of the recovery ladder this task climbed, across all steps, in order. Empty for a run where nothing needed rescuing.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `task_id` | path | yes |  |


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 404 | No such task, or it belongs to someone else |
| 422 | Validation Error |


### `POST /api/v1/tasks/{task_id}/reject`

**Reject a high-risk action**


Stops the task. A rejection is a person having looked and said no, which ends the run as REJECTED; an approval nobody answers is a different thing and times out instead.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `task_id` | path | yes |  |


Body: `ApprovalDecisionRequest`


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 403 | Your role does not allow approving |
| 404 | No such task, or it is not waiting for an approval |
| 409 | That approval has already been answered |
| 422 | Validation Error |
| 429 | Too many approval decisions |


### `POST /api/v1/tasks/{task_id}/replay`

**Run a finished task's flow again**


Creates a new task bound to the cached flow of this one, and queues it. The replay is unconditional: the success rate that gates ordinary runs is not consulted, because the user has asked for this sequence. The original task is untouched, since it is the record of what happened. Replay stops at the first step that does not verify and the planner carries on from there, so the new task completes on its own terms either way.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `task_id` | path | yes |  |


| Status | Meaning |
|---|---|
| 201 | Successful Response |
| 404 | Not your task, or no cached flow for it |
| 409 | The task is still running, or a duplicate is |
| 422 | Validation Error |


### `GET /api/v1/tasks/{task_id}/steps`

**List a task's steps**


In order. This is the activity log, without the per-step detail.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `task_id` | path | yes |  |


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 404 | No such task, or it belongs to someone else |
| 422 | Validation Error |


### `GET /api/v1/tasks/{task_id}/steps/{step_id}`

**Get one step in full**


The step, the decision the model made, how it was verified, and every recovery attempt against it.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `task_id` | path | yes |  |
| `step_id` | path | yes |  |


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 404 | No such step on this task |
| 422 | Validation Error |


### `GET /api/v1/tasks/{task_id}/steps/{step_id}/screenshot`

**Get a step's screenshot**


The JPEG captured just after the step ran. Served only to the task's owner or an admin, and marked private so no shared cache keeps a copy.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `task_id` | path | yes |  |
| `step_id` | path | yes |  |


| Status | Meaning |
|---|---|
| 200 | The frame |
| 404 | No such step, or it has no screenshot |
| 422 | Validation Error |


### `POST /api/v1/tasks/{task_id}/stream-ticket`

**Get a ticket for the live stream**


Returns a single-use ticket for opening the task's WebSocket. Valid for 60 seconds and for one connection. Pass it as the `ticket` query parameter on `WS /api/v1/tasks/{id}/stream`; never put an access token in that URL.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `task_id` | path | yes |  |


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 404 | No such task, or it belongs to someone else |
| 422 | Validation Error |
| 429 | Too many tickets requested |


## Statistics


### `GET /api/v1/stats/overview`

**Your task statistics**


Your own finished tasks by default. An administrator may pass `scope=all` for the whole deployment; anyone else asking for it gets 403 rather than their own numbers under a global heading.

Rates are null when there is nothing to divide by, because zero would claim that nothing succeeded.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `scope` | query | no |  |


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 403 | Only an administrator can read the global statistics |
| 422 | Validation Error |


## Ops


### `GET /.well-known/jwks.json`

**Public keys for verifying access tokens**


The public half of the signing key, as a JWK Set.

Public by design: anyone may verify a token, only this service may mint one.
Cached for five minutes so payments does not refetch on every request, but
not so long that a key rotation takes hours to propagate.


| Status | Meaning |
|---|---|
| 200 | Successful Response |


### `GET /api/v1/health`

**Liveness and readiness probe**


| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 503 | A dependency is down |


### `GET /metrics`

**Prometheus exposition**


Admin or internal only. Present an ADMIN bearer token, or the configured token in `X-Internal-Token` if the deployment sets one.


| Parameter | In | Required | Notes |
|---|---|---|---|
| `authorization` | header | no |  |
| `x-internal-token` | header | no |  |


| Status | Meaning |
|---|---|
| 200 | Prometheus text format |
| 401 | No usable credential |
| 403 | Authenticated, but not an administrator |
| 422 | Validation Error |


## WebSocket events

`WS /api/v1/tasks/{id}/stream?ticket=...&after_step=N`

Get a ticket from `POST /api/v1/tasks/{id}/stream-ticket` first: single use, sixty
seconds, one task. Ownership is checked when the ticket is issued, on a request
that carries a real token, rather than at connect time where the only credential
is the ticket itself.

`after_step` replays what was missed. Replay comes from the database and is sent
before anything live, so a reconnecting client sees every step exactly once and
in order. Replayed events carry `seq: -1` and, per decision A10, metadata rather
than an inline screenshot above a configurable cutover, so a reconnect cannot
burst a run's worth of images.

Every event shares four fields: `type`, `taskId`, `seq` and `timestamp`. The
samples below are generated from the event models themselves, so they are the
shapes a client actually receives.


### `status`

The task moved between lifecycle states. `previousStatus` is null on the first one.


```json
{
  "taskId": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "seq": 12,
  "timestamp": "2026-09-29T02:51:04.118Z",
  "type": "status",
  "status": "ACTING",
  "previousStatus": "PLANNING"
}
```


### `step`

One executed step, after it has been committed. The screenshot is base64 JPEG, no wider than 1280 and quality about 60. `replayed` is true when the step came from a cached flow rather than the planner, in which case there is no reasoning behind it.


```json
{
  "taskId": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "seq": 12,
  "timestamp": "2026-09-29T02:51:04.118Z",
  "type": "step",
  "stepNumber": 4,
  "action": "click",
  "target": "Cheapest first",
  "value": null,
  "verified": true,
  "screenshot": "/9j/4AAQSkZJRgABAQ...",
  "bbox": {
    "x": 812,
    "y": 310,
    "width": 124,
    "height": 36
  },
  "viewport": {
    "width": 1280,
    "height": 800
  },
  "url": "http://skyfare:8200/results?sort=price",
  "tabId": "t1",
  "reasoning": "Sorting by price puts the cheapest option first.",
  "confidence": 0.9,
  "usedVision": false,
  "replayed": false
}
```


### `recovery`

One rung of the recovery ladder. Several per failed step is normal; the budget is three attempts.


```json
{
  "taskId": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "seq": 12,
  "timestamp": "2026-09-29T02:51:04.118Z",
  "type": "recovery",
  "stepNumber": 4,
  "attemptNumber": 1,
  "strategy": "dismiss_overlay",
  "outcome": "SUCCEEDED"
}
```


### `approval_required`

The run is parked on a human. It carries the screenshot and the bounding box, because somebody is being asked to approve an action and is entitled to see what it is.


```json
{
  "taskId": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "seq": 12,
  "timestamp": "2026-09-29T02:51:04.118Z",
  "type": "approval_required",
  "approvalId": "2f1c8d3a-1111-4222-8333-444455556666",
  "stepNumber": 6,
  "proposedAction": {
    "action": "click",
    "target": "Pay now"
  },
  "riskLevel": "HIGH",
  "reason": "Click 'Pay now' on SkyFare. Flagged because the control says 'pay now'.",
  "screenshot": "/9j/4AAQSkZJRgABAQ...",
  "bbox": {
    "x": 640,
    "y": 520,
    "width": 96,
    "height": 40
  }
}
```


### `approval_resolved`

Somebody answered. `approved` false means an explicit rejection, which ends the task as REJECTED; an approval nobody answers expires instead and the task times out.


```json
{
  "taskId": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "seq": 12,
  "timestamp": "2026-09-29T02:51:04.118Z",
  "type": "approval_resolved",
  "approvalId": "2f1c8d3a-1111-4222-8333-444455556666",
  "approved": true,
  "by": "11111111-1111-4111-8111-111111111111",
  "reason": "Looks right."
}
```


### `final`

The run is over, whatever happened. Always sent, including when the settling write itself failed: a client that never receives one watches a spinner forever.


```json
{
  "taskId": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "seq": 12,
  "timestamp": "2026-09-29T02:51:04.118Z",
  "type": "final",
  "status": "COMPLETED",
  "finalAnswer": "The cheapest option is Rs 4,870.",
  "failureReason": null,
  "stats": {
    "steps": 7,
    "durationMs": 8420,
    "creditsUsed": 10,
    "cacheHit": false,
    "replayedSteps": 0
  }
}
```


### `error`

Something went wrong at the stream level rather than in the run.


```json
{
  "taskId": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "seq": 12,
  "timestamp": "2026-09-29T02:51:04.118Z",
  "type": "error",
  "code": "INTERNAL_ERROR",
  "message": "Something went wrong."
}
```


### `heartbeat`

Sent on an idle connection so a proxy does not close it. Carries nothing.


```json
{
  "taskId": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "seq": 12,
  "timestamp": "2026-09-29T02:51:04.118Z",
  "type": "heartbeat"
}
```


## Close codes

| Code | Meaning |
|---|---|
| 4401 | The ticket is missing, expired, or already used |
| 4403 | The ticket is valid and the task is not yours |
| 4404 | No such task |

The socket closes rather than sending an error event for these, because none of
them is recoverable by the client on the same connection.

## Generating a client

`docs/openapi.json` is the schema this page was generated from. The frontend is
intended to generate its types with `openapi-typescript` (spec section 3); the
WebSocket events are not in the schema and have to be written by hand from the
section above.

