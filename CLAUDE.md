# CLAUDE.md

Permanent working rules for this repository. These apply to every part and every session.

## Source of truth

`docs/PROJECT_SPEC.md` is the source of truth. If a request conflicts with it, stop and say so before writing anything.

`docs/BACKEND_PLAN.md` records the agreed plan and the 18 resolved decisions. Where a part prompt and the plan disagree, the part prompt wins and the plan is updated to match.

## Language and tooling

- Python 3.12, managed with uv.
- All I/O is async. No blocking calls in request or agent paths.
- Type hints everywhere. mypy runs strict on `app/`.
- ruff for both lint and format.

## Hard constraints

- Never import, vendor, or copy code from the `browser-use` library. It is a reference only.
- Never commit secrets. All config goes through pydantic-settings, and `.env.example` stays in sync with every new setting.
- Never log page content, screenshots, typed values of password or card-like fields, JWTs, or API keys.
- Ask before adding any dependency that is not in the spec tech stack (section 3).

## Architecture

```text
api (routers) -> services -> repositories -> models
```

Dependencies point one way only. A router never touches a repository directly, and a repository never imports a service.

Agent code lives in `app/agent/` and never imports FastAPI. It is driven by `app/services/`, so the agent loop stays testable without an HTTP stack.

## Definition of done

- Every new module ships with unit tests.
- A part is done only when `make check` passes (lint, typecheck, unit tests).
- Conventional Commits. One part is one or a few focused commits.

## After each part

1. Update `docs/CHANGELOG.md`.
2. Write `docs/phase-notes/<part-id>.md` covering what was built, the key decisions, and three viva questions with answers.
3. After committing, always run `git push`, and report the pushed commit hash.

## Pace

Work on ONE part at a time. Never start the next part unprompted. Finish the current part, report, and stop.
