# First end-to-end demo

The first run where the whole thing works: a plain-language instruction goes in, a browser is driven, and a correct answer comes out, checked against ground truth the agent cannot reach.

Run on 2026-09-27 against SkyFare, with the date thirty days out (2026-10-27).

## Setup

```bash
make db-up && uv run alembic upgrade head
```

```bash
make skyfare
```

```bash
LLM_PROVIDER=mock STORAGE_DIR=/tmp/agent-storage uv run uvicorn app.main:app --port 8000
```

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login -H 'Content-Type: application/json' -d '{"email":"demo@example.test","password":"a-long-passphrase"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['accessToken'])")
```

The answer to check against, which is behind a header the agent has no way to send:

```bash
curl -s -H "x-skyfare-oracle: scoring-only" "http://127.0.0.1:8200/api/cheapest?from=Delhi&to=Bengaluru&date=2026-10-27"
```

```json
{"id":"QP-555","airline":"Akasa Air","price":4420,"priceLabel":"₹4,420","stops":2}
```

## Run one: clean page, mock provider

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/tasks -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"instruction":"Find the cheapest flight from Delhi to Bengaluru on 2026-10-27 on SkyFare","startUrl":"http://127.0.0.1:8200/"}'
```

```bash
uv run python scripts/ws_watch.py $TASK --email demo@example.test --password a-long-passphrase
```

```text
#1 type 'FROM' = 'Delhi'                  verified  screenshot=34.3 KB
#2 type 'TO' = 'Bengaluru'                verified  screenshot=35.5 KB
#3 type 'DEPARTURE' = '2026-10-27'        verified  screenshot=34.2 KB
#4 click 'Search flights'                 verified  screenshot=50.5 KB
#5 click 'Cheapest'                       verified  screenshot=50.4 KB
#6 extract 'the results list'             verified  screenshot=50.4 KB
#7 done 'task complete'                   verified  screenshot=50.4 KB
final  COMPLETED The cheapest option is Rs 4,420.  [7 steps, 4958 ms, 7 credits]
```

**₹4,420 against an oracle of ₹4,420.** Seven steps in five seconds, every step verified against the page rather than taken on the model's word.

## Run two: cookie banner, newsletter popup, shuffled ids

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/tasks -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"instruction":"Find the cheapest SkyFare flight from Delhi to Bengaluru on 2026-10-27","startUrl":"http://127.0.0.1:8200/?chaos=cookie,popup,shuffle"}'
```

```text
#1 type 'FROM' = 'Delhi'                  verified
#2 type 'TO' = 'Bengaluru'                verified
#3 type 'DEPARTURE' = '2026-10-27'        verified
   recovery #4 attempt 1 scrolling to it   -> FAILED
   recovery #4 attempt 2 closing a popup   -> SUCCEEDED
#4 click 'Search flights'                 verified
   recovery #5 attempt 1 scrolling to it   -> FAILED
   recovery #5 attempt 2 closing a popup   -> SUCCEEDED
#5 click 'Cheapest'                       verified
#6 extract 'the results list'             verified
#7 done 'task complete'                   verified
final  COMPLETED The cheapest option is Rs 4,420.  [7 steps, 49467 ms, 7 credits]
```

Same answer through a cookie modal, a newsletter popup and ids that change on every load. Four recovery attempts, all recorded:

```bash
curl -s "http://127.0.0.1:8000/api/v1/tasks/$TASK/recovery-attempts" -H "Authorization: Bearer $TOKEN"
```

```text
attempt 1 scroll_and_wait   FAILED     TIMEOUT
attempt 2 dismiss_overlay   SUCCEEDED
attempt 1 scroll_and_wait   FAILED     TIMEOUT
attempt 2 dismiss_overlay   SUCCEEDED
```

Forty-nine seconds against five. Almost all of it is the two failed scroll attempts: a failed action waits out the ten second browser timeout, so a rung that cannot work still costs ten seconds before the rung that can gets its turn.

## Run three: real provider

One run on Gemini, which is what was authorised.

```bash
STORAGE_DIR=/tmp/agent-storage uv run uvicorn app.main:app --port 8000   # provider from .env
```

```text
#1 type 'FROM input field' = 'Delhi'          verified
#2 type 'TO input field' = 'Bengaluru'        verified
#3 type 'DEPARTURE date input' = '2026-10-27' verified
#4 click 'Search flights button'              verified
#5 click 'Cheapest sort link'                 verified
final  EXECUTION_FAILED  [5 steps, 31952 ms]
```

Five steps, all correct and all verified, with noticeably better target descriptions than the rule-based mock produces. It then stopped on the free-tier rate limit, partway through planning step six.

I did not retry. One run was what was authorised, and the previous time I ran something twice against this key it cost real quota.

The rate limit surfaced as "The task stopped after an internal error (LLMRateLimited)", which is a poor thing to tell a user about something that is ordinary weather and worth retrying in a minute. Provider rate limits, timeouts and outages now have their own reasons.

## What failed on the way

Six things, none of which the test suite caught. Every one came out of running the thing against a real site.

**The agent answered the wrong question, correctly.** The first clean run returned ₹4,940, which is the right cheapest fare for 2026-10-21. SkyFare's date field arrives pre-filled with a default, the mock planner only filled fields that were empty, so the search ran on the form's default date and not the one in the instruction. The mock now fills any field whose value is not what the goal asks for. This is the failure mode that worries me most about agents generally: nothing errored, every step verified, and the answer was confidently wrong.

**"DEPARTURE" matched the origin-city rule.** Once the mock started correcting pre-filled fields, it tried to type "Delhi" into the date picker, because SkyFare labels its date field "Departure" and the practice form labels its origin field "Departure city". Matching on the input type rather than the label fixes it; the labels genuinely collide.

**The cookie banner beat the recovery ladder.** The first chaos run failed with all three retries spent on rungs one to three, none of which can help when something is sitting on top of the target. The ladder never reached the rung that closes it. Strategies that re-locate an element are now not applicable while the target is covered, which is what `applicable` is for: not "can this run" but "can this possibly help".

**Dismissing the overlay verified the click.** Closing a modal changes the DOM, and the retry was being compared against the state from before the dismissal, so `dom_changed` passed whether or not the click landed. The baseline is now the page as it is immediately before the retry.

**One overlay closed, the other did not.** With a cookie banner and a newsletter popup both on screen, closing one still leaves the click blocked. The rung now clears every overlay it recognises before retrying.

**The loop planned against a stale page.** The most interesting one. After recovery succeeded, the next step was planned against the page state captured when the step *failed*, not where the successful retry left the browser. So the agent clicked Search, navigated to the results page, and then planned its next move against the search form it had already left, targeting a stamped index that meant something else entirely. The step 5 screenshot showed the results page while the agent was reasoning about the home page. Recovery now returns the state it ended on.

## Where it stands

The clean run is fast and correct. The chaos run is correct and slow. The real provider drives it correctly and the free tier will not sustain a full run.

The honest limitation is the ten second cost of a failed rung. Three failed attempts is half a minute of a five minute budget, and the recovery ladder currently spends it in strategy order rather than in order of what the failure looked like. Picking the rung by the failure's error code would be the obvious next improvement.
