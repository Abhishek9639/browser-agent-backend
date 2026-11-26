# Browser Automation Agent — local run guide

Backend repository: `~/browser-agent-backend`

GitHub branch: `codex/backend-reliability`

## Current configuration

The ignored `.env` uses **Gemini** with `GOOGLE_API_KEY` and
`LLM_MODEL=gemini-3.6-flash`, plus configured Gemini fallback models. The saved
key passed live validation; do not paste it into chat or commit `.env`.
`AGENT_HEADLESS=false` shows the browser for manual login/OTP, and verified-flow
caching is enabled again after testing.

PostgreSQL is local on port 5432, with `browser_agent_demo` for the application
and a separate `browser_agent_test` for tests. JWT signing keys remain private
in the ignored `keys/` directory. Python dependencies are installed in `.venv`.
Playwright is pinned to 1.65.2.

The backend currently passes its health check at port 8000. If starting it
prints `Address already in use`, check the existing health endpoint before
starting another copy; an already running backend can be reused.

There is no forced SkyFare landing page for prompt-only tasks. For predictable
demos, pass `startUrl` explicitly. The local configuration now uses
`AGENT_ALLOWED_DOMAINS=*` for the requested general web browsing. Previously it
only allowed local hosts and Uber, which blocked the first GSoC navigation.
The example configuration keeps its demo-only allow-list; configure intended
hosts explicitly for a restricted deployment. Only HTTP(S) navigation without
credentials in the URL is accepted, including in wildcard mode.

## Start the backend

In a normal Terminal:

```sh
cd ~/browser-agent-backend
.venv/bin/python run.py
```

Keep the Terminal open. If the server is already running, use that instance.

- API docs: <http://127.0.0.1:8000/docs>
- API/database health: <http://127.0.0.1:8000/api/v1/health>

Chromium works when launched by this normal Terminal server. This Codex chat's
macOS sandbox cannot launch Chromium directly. The local Firefox profile
startup issue is separate; use Chromium for the demonstration. Linux browser
checks run in GitHub CI.

## Repeatable presentation test

In a second Terminal:

```sh
cd ~/browser-agent-backend
.venv/bin/uvicorn demo_sites.skyfare.app:app --host 127.0.0.1 --port 8200
```

Then run:

```sh
cd ~/browser-agent-backend
.venv/bin/python scripts/smoke_backend.py
```

This uses the configured Gemini model and consumes API quota. It creates a
unique demo account and task, searches for a future Delhi–Bengaluru flight,
checks the returned fare against the separate scoring endpoint, and verifies
authentication, refresh, WebSocket events, stored steps, protected screenshots,
history, stats, JWKS, logout and refresh revocation.

The live Gemini smoke passed with flight QP-654 at INR 5,740 on its tested date.
The local cab fixture also passed with PST Hostel and Polaris School of
Technology, reporting the selected Demo ride from its visible confirmation.
Neither fixture is evidence that a real Uber ride was booked.

## Show the API during your presentation

1. Open the API docs, register an account with `POST /api/v1/auth/register`,
   then log in with `POST /api/v1/auth/login`.
2. Use the returned `accessToken` in Swagger's **Authorize** dialog.
3. Submit `POST /api/v1/tasks` with a future travel date and an explicit demo URL:

   ```json
   {
     "instruction": "Find the cheapest Delhi to Bengaluru flight on 2026-12-16 on SkyFare. Report its flight number and fare.",
     "startUrl": "http://127.0.0.1:8200/"
   }
   ```

4. Show the task status, stored `/steps`, screenshots, task history and
   `/api/v1/stats/overview`. The automated smoke above checks these same APIs.

## Frontend connection

Frontend code was not modified. Its backend URL should be
`BACKEND_URL=http://127.0.0.1:8000`; its WebSocket URL should be
`NEXT_PUBLIC_BACKEND_WS_URL=ws://localhost:8000`. Restart Next.js after changing
its environment. The frontend's hardcoded Chrome connection card is not a
backend health check. The backend uses its own Chromium contexts.

## Test and operating limits

- Run one API worker: scheduling, events and stream tickets are process local.
- Keep test databases separate. Automated tests must never target real user data.
- Provider availability and quota can interrupt tasks; fallback models help but
  cannot guarantee service availability.
- Login, OTP, CAPTCHA, ambiguous locations and payment require human action.
- Payment-service integration is disabled locally; contract tests use a fake
  payment service. No real payment or ride booking was made during testing.
- Passing the tested scenarios does not guarantee every website or instruction
  will work. Use the verified SkyFare task for a repeatable college demo.

See [the live-testing phase note](phase-notes/gemini-live-task-reliability.md) for evidence and remaining blockers.
