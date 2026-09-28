# Billing integration

The contract between this service and `browser-agent-payments`. The payments
repository implements the endpoints below; this document is what it implements
against, and `tests/unit/test_billing_client.py` is the executable version of
it: a fake payments app in that file answers exactly these shapes.

Nothing here is a description of the payments service's internals. Wallets,
ledgers, packs, Razorpay and the expiry job are spec section 7's business and
belong in that repository.

## What this side does

| Moment | Call | How |
|---|---|---|
| A user registers | `POST /internal/v1/grants/signup` | Queued in the outbox, delivered by a worker |
| A task is submitted | `POST /internal/v1/reservations` | **Synchronous**, on the request |
| A task reaches a terminal state and used credits | `POST /internal/v1/reservations/{id}/settle` | Queued in the outbox |
| A task reaches a terminal state and used none | `POST /internal/v1/reservations/{id}/release` | Queued in the outbox |

One of the four is synchronous, and the asymmetry is the design. A user
submitting a task has to be told now whether they can afford it, so a refusal
has to reach them as a refusal. Everything else is money owed in one direction
or the other, and losing it because payments happened to be restarting is not
acceptable, so it is written to a table in the same transaction as the business
change and delivered afterwards.

## Headers on every call

```
Content-Type: application/json
X-Internal-Token: <INTERNAL_API_TOKEN>
Idempotency-Key: <see below>
```

`X-Internal-Token` is a shared secret. It is omitted entirely when
`INTERNAL_API_TOKEN` is unset, rather than sent empty, so a payments service
that requires it fails loudly rather than accepting a blank one.

`Idempotency-Key` is not decoration. This client retries on 5xx and on network
failures, and a timeout means the answer was lost rather than that nothing
happened, so every call must be safe to receive twice. The keys are derived,
never random:

| Call | Key |
|---|---|
| Signup grant | `signup:<userId>` |
| Reservation | `reserve:<taskId>` |
| Settle | `settle:<reservationId>` |
| Release | `release:<reservationId>` |

A grant applied twice is free credits; a settle applied twice is a double
charge. The payments side is expected to store the key (spec section 7 gives
`ledger_entries.idempotency_key` a unique constraint) and return the original
outcome on a repeat rather than applying it again.

## The calls

### Signup grant

```http
POST /internal/v1/grants/signup
Idempotency-Key: signup:11111111-1111-4111-8111-111111111111

{"userId": "11111111-1111-4111-8111-111111111111"}
```

```http
201 Created
{"granted": 200}
```

The response body is not read. A 2xx means the grant is somebody else's problem
now; the outbox row is marked delivered and never sent again.

### Reserve

```http
POST /internal/v1/reservations
Idempotency-Key: reserve:22222222-2222-4222-8222-222222222222

{
  "userId": "11111111-1111-4111-8111-111111111111",
  "taskId": "22222222-2222-4222-8222-222222222222",
  "amount": 50
}
```

```http
201 Created
{"reservationId": "33333333-3333-4333-8333-333333333333", "amount": 50}
```

`amount` is `AGENT_MAX_STEPS x RESERVE_MULTIPLIER`, 50 with the defaults. It is
an upper bound rather than an estimate: generous enough that a task grounding on
every step is still covered by its own hold, and settled down to the truth when
the task ends.

`reservationId` is required. A 201 without one is treated as the contract being
broken, not as a feature that is missing: this side answers 503 and the task
does not run.

When the wallet cannot cover it:

```http
402 Payment Required
{"available": 12, "required": 50}
```

Both numbers, at the top level of the body rather than wrapped. They reach the
user:

```http
402 Payment Required
{
  "error": {
    "code": "INSUFFICIENT_CREDITS",
    "message": "This task needs 50 credits and you have 12.",
    "requestId": "req_ab12cd34ef56",
    "details": {"available": 12, "required": 50}
  }
}
```

A 402 is never retried. It is an answer, and asking again reaches the same one
while the user waits.

### Settle

```http
POST /internal/v1/reservations/33333333-3333-4333-8333-333333333333/settle
Idempotency-Key: settle:33333333-3333-4333-8333-333333333333

{"actualAmount": 9}
```

```http
200 OK
{"settled": 9}
```

`actualAmount` is what the run used, priced as below. The difference between the
hold and the charge is released by the payments side.

### Release

```http
POST /internal/v1/reservations/33333333-3333-4333-8333-333333333333/release
Idempotency-Key: release:33333333-3333-4333-8333-333333333333

{}
```

```http
200 OK
{"released": true}
```

Used when a task ends having used nothing. A release rather than a settle of
zero: both leave the wallet in the same place, and the ledger keeps "this task
cost nothing" and "this task was measured at nothing" apart.

## Pricing

Spec section 7, implemented in `app/domain/credits.py`:

- 1 credit per step (`CREDITS_PER_STEP`)
- 2 credits for a vision-grounded step (`CREDITS_PER_VISION_STEP`) — the whole
  price of that step, not a surcharge on top of one
- half a credit for a replayed step, when `CREDITS_REPLAY_STEP_HALF` is on
- **rounded up once, over the whole task**

The rounding matters. Three replayed steps cost two credits, not three: rounding
each half up separately would charge full price for every replayed step, which
is the same as not discounting them at all.

The three kinds of step are disjoint. A replayed step never grounds, because a
cached step names its target with a locator and has nothing for vision to
correct.

Both sides hold these figures. They are settings here so a disagreement is a
deployment mistake rather than a code change in two repositories, and a
disagreement shows up as a settle that does not match what was reserved.

## Failure handling

| What happened | This side does |
|---|---|
| 2xx | Done. An outbox row is marked delivered |
| 402 on reserve | 402 to the user, with both numbers. Not retried |
| Other 4xx | 503 to the user, or the outbox row is retried later. Not retried immediately: the far side has decided |
| 5xx | Retried up to `PAYMENTS_MAX_ATTEMPTS` with a growing delay, then 503 or a later outbox attempt |
| Timeout or connection error | Same as 5xx. This is what the idempotency key is for: the call may well have landed |
| Enabled with no client configured | 503, logged as a deployment error. A task that ran unpaid because of a missing setting is worse |

The outbox backs off exponentially from `BILLING_OUTBOX_BACKOFF_SECONDS`, capped
at an hour, and a row that reaches `BILLING_OUTBOX_MAX_ATTEMPTS` is logged
loudly and **left undone**. It is not deleted: the row is the evidence that
somebody's credits are unaccounted for, and removing it would hide that.

## Disabled mode

With `BILLING_ENABLED=false`, which is the default, none of this runs. No
reservation, no outbox rows, no calls. The agent behaves exactly as it does
without a payments service, and the one visible difference is that
`creditsUsed` on the stats endpoint is null.

`tasks.credits_used` is still written for every finished task, because it is the
section 9 cost-per-task figure as well as the amount a settle would charge, and
a deployment that turns billing on later still wants the history.

## Settings

| Setting | Default | What it is |
|---|---|---|
| `BILLING_ENABLED` | `false` | Off means off |
| `PAYMENTS_BASE_URL` | `http://localhost:8100` | Where the payments service is |
| `INTERNAL_API_TOKEN` | unset | Shared secret for `X-Internal-Token` |
| `PAYMENTS_TIMEOUT_SECONDS` | `3.0` | Per attempt. A person is waiting for the reserve |
| `PAYMENTS_MAX_ATTEMPTS` | `3` | Including the first |
| `CREDITS_PER_STEP` | `1` | |
| `CREDITS_PER_VISION_STEP` | `2` | |
| `CREDITS_REPLAY_STEP_HALF` | `true` | |
| `RESERVE_MULTIPLIER` | `2` | Times `AGENT_MAX_STEPS` |
| `BILLING_OUTBOX_POLL_SECONDS` | `5.0` | How often the worker looks |
| `BILLING_OUTBOX_BACKOFF_SECONDS` | `10.0` | First retry delay, doubling |
| `BILLING_OUTBOX_MAX_ATTEMPTS` | `12` | After which a row is logged and left |

## What this side expects of the other

1. **The four endpoints above**, with those bodies and those status codes.
2. **Idempotency by key**, storing the key and returning the original outcome on
   a repeat.
3. **`reservations.task_id` unique**, so a retried reserve returns the same hold
   rather than making a second one.
4. **An expiry job** for held reservations, which is the only thing that
   recovers a reservation whose task never started: this side holds the credits
   before it commits the task row, so a rollback after a successful reserve
   leaves a hold with nothing attached to it. Spec section 7 puts the window at
   thirty minutes.
5. **A 402 that carries `available` and `required`**, because the user is shown
   both and a wallet page is a worse place to find out.

## Known limitations

- The reserve happens before the task row is committed, so a rollback in between
  leaves a hold that only the expiry job clears. The other order is worse: the
  client would be told the task was accepted and then charged separately, or not
  at all.
- The outbox worker is per process. Several workers share the backlog safely
  (`FOR UPDATE SKIP LOCKED`), but nothing outside a running process delivers it:
  a deployment with no API process running does not settle anything until one
  starts.
- Nothing reconciles. If a settle is delivered and the payments side records
  something different, no job here notices. The evidence exists on both sides;
  comparing them is work nobody has done yet.
