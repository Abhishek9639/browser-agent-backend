# Browser Automation Agent — local presentation guide

This backend runs FastAPI, PostgreSQL, and a server-side Chromium browser. The
bundled SkyFare website and `LLM_PROVIDER=mock` provide an offline demonstration.
The mock is a deterministic flight-search planner, not a general-purpose AI model.

## This Mac is configured

Repository: `~/browser-agent-backend`
Branch: `codex/backend-reliability`

Dependencies are installed in `.venv`. Playwright is pinned to 1.65.2, with
matching Chromium and Firefox binaries. JWT keys are in `keys/`, with the private
key restricted to its owner. The ignored `.env` uses the existing Homebrew
PostgreSQL server on port 5432, database `browser_agent_demo`, and a separate
`browser_agent_test` database for tests. Docker is not needed for this setup.
Do not replace this `.env` with the example: its database username is specific
to this Mac. Neither the environment file nor the keys should be committed.

## Start the demonstration

In Terminal tab 1:

```sh
cd ~/browser-agent-backend
.venv/bin/python run.py
```

In Terminal tab 2:

```sh
cd ~/browser-agent-backend
.venv/bin/uvicorn demo_sites.skyfare.app:app --host 127.0.0.1 --port 8200
```

Keep both tabs open. Stop each with Control-C. If a port is already in use, use
the server already running there or stop its original Terminal process first.

- API documentation: http://127.0.0.1:8000/docs
- API/database health: http://127.0.0.1:8000/api/v1/health
- SkyFare: http://127.0.0.1:8200/

The local configuration sets `AGENT_DEFAULT_START_URL=http://127.0.0.1:8200/`.
This matters because the linked frontend sends an instruction without `startUrl`.
An explicit API `startUrl` overrides the configured default, and both are checked
against the allow-list. Clear this setting when moving to general-purpose tasks
with a real LLM provider. In Docker the corresponding demo URL is
`http://skyfare:8200/`, not `localhost:8200/`.

## Presentation flow

1. Open `/docs`, register your own account using `POST /api/v1/auth/register`,
   and log in using `POST /api/v1/auth/login`.
2. Copy the returned `accessToken` into Swagger's **Authorize** dialog.
3. Submit a task:

   ```json
   {
     "instruction": "Find the cheapest flight from Delhi to Bengaluru on 2026-12-15 on SkyFare",
     "startUrl": "http://127.0.0.1:8200/"
   }
   ```

4. Poll `GET /api/v1/tasks/{id}` and list its `/steps`. The screenshot endpoint
   returns a JPEG only with the owner's token or an administrator's token.
5. Show the task history and `GET /api/v1/stats/overview`. For the live view,
   obtain a `/stream-ticket` and use the WebSocket route, or use the frontend.
6. Explain that each action is verified against the actual page, and that
   approval gates protect high-risk actions.

The smoke-test script performs this flow automatically, verifies the reported
fare against SkyFare's separate scoring endpoint, and checks authentication,
WebSocket delivery, persisted steps, screenshots, history, stats, JWKS and logout:

```sh
.venv/bin/python scripts/smoke_backend.py
.venv/bin/python scripts/smoke_backend.py --use-default-start
```

Each run creates a unique smoke account and a task in the demo database. The
script does not print or save its generated credentials. Use a separate account
of your own for the presentation UI.

## Frontend connection (no frontend files were changed)

The supplied frontend is `https://github.com/ishaan-khandelwal/Browser-Automation-Agent`.
Its current HTTP rewrite uses `BACKEND_URL=http://127.0.0.1:8000`, while its live
stream uses `NEXT_PUBLIC_BACKEND_WS_URL=ws://localhost:8000` by default. Local CORS
allows `http://localhost:3000` and `http://127.0.0.1:3000` in this backend's `.env`.
Restart Next.js after changing its environment variables.

The frontend's Chrome connection card currently displays hardcoded extension,
port and latency values. It is not a backend health check. The default backend
uses its own isolated Chromium contexts and does not require a Chrome extension.
Frontend implementation and visual fixes remain outside this backend-only work.

## Validation commands

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest tests/unit tests/integration tests/chaos
.venv/bin/pip-audit --strict --progress-spinner off
```

The full tests use the separate test database and disposable migration databases;
they do not clear the demo database. Never point `POSTGRES_TEST_DB` at real data.
The configuration rejects a test database matching the application's database.
The migration tests explicitly override `DATABASE_URL` to prevent accidental use
of a managed application database.

Run browser tests from a normal Terminal on this Mac. This Codex chat's macOS
sandbox cannot launch Chromium's Mach port service. That restriction is separate
from the backend; a browser launched by the normal Terminal can run the demo.

## Scope and remaining configuration

- No external AI-provider credentials were supplied. General web automation needs
  a configured real provider and its API key; live provider calls were not tested.
- Allow only the sites you intend to demonstrate through `AGENT_ALLOWED_DOMAINS`.
  The current configuration allows only local sites. The `*` option still rejects
  invalid URLs, credentials in URLs, and non-HTTP navigation schemes.
- Payments are a separate service and are disabled here. Billing contract tests
  exercise a local fake payments API, not a real wallet or payment provider.
- Use one API worker. Scheduling, event delivery and stream tickets are process
  local; this change does not turn the project into a distributed service.
- PostgreSQL 14 on this Mac is exercised by the database suite; the repository's
  Docker deployment continues to use PostgreSQL 16. Docker was not running here,
  so container builds were not validated during this session.
- Passing tests and a successful demo do not guarantee arbitrary sites or every
  natural-language instruction will work. Use the verified SkyFare task for a
  repeatable presentation.

## Final verification and the remaining Firefox issue

Before the subsequent input/scroll follow-up, the 21 November 2026 full unit,
integration and chaos run completed with
**1,777 passed, one failed, and one skipped**. The skipped test requires a live
AI-provider key. All chaos and integration tests passed. Ruff, formatting, mypy,
and the dependency vulnerability audit passed. The Chromium API/WebSocket smoke
flow also passed after the Playwright update.

The one failure is `test_a_site_that_refuses_this_browser_is_tried_in_another`.
A direct Firefox launch outside the application still exits with
`Could not find profile folder` on this Mac (macOS 27.2.1, Firefox 159). Updating
Playwright to 1.65.2 did not resolve it here. This is consistent with the
[upstream macOS profile-access issue](https://github.com/microsoft/playwright/issues/42768);
the exact OS permission cause has not been independently confirmed. The test has
been left enabled so this limitation stays visible.

The presentation uses Chromium and does not require Firefox. If you need Firefox
fallback, resolve its profile access in the terminal application you use, then run:

```sh
.venv/bin/python scripts/check_browsers.py firefox
.venv/bin/pytest tests/unit/test_browser_driver.py::test_a_site_that_refuses_this_browser_is_tried_in_another
```

The upstream issue reports granting the launching terminal Full Disk Access as
one workaround. This is a broad permission and is optional for this project.
Review it yourself under **System Settings → Privacy & Security → Full Disk
Access**, following [Apple's guidance](https://support.apple.com/en-us/149699).
If you choose to change it, quit and reopen that terminal before the recheck;
restart the two demo servers afterward. No macOS permissions were changed by
this repair. A supported Linux environment is another option, but its Firefox
behavior has not been verified in this session.

The subsequent input/scroll follow-up passed all 20 focused browser cases across
two runs, including the corrected local cab-form test. Its lint, formatting,
type checks, non-browser unit tests and live SkyFare smoke passed. Live Uber
remains unverified. See `docs/phase-notes/input-and-scroll-reliability.md`.
