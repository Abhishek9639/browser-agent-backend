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
3. Commit the part on its branch. Pushing and opening the pull request happen after the *second* part of the pair; see Git workflow below.

## Git workflow

Parts ship in **pairs, on a branch**. Never commit to `main` directly, and never merge the pull request.

1. **Pair the parts.** B08+B09 on one branch, B10+B11 on the next, and so on.

2. **Branch from current `main`** before starting the first part of a pair, so the branch never starts from a stale base:

   ```bash
   git fetch origin && git checkout main && git pull
   git checkout -b feat/bXX-bYY-<short-topic>
   ```

   For example `feat/b08-b09-browser-driver-perception`.

3. **Commit each part separately** on that branch, each with its own Conventional Commit message.

4. **After the second part of the pair**, and only once `make check` passes:

   ```bash
   git push -u origin <branch>
   gh pr create --base main --head <branch> --title "..." --body "..."
   ```

   If the GitHub CLI is unavailable, print the compare URL to open by hand.

5. **The pull request description lists**: both parts and what each delivers, the test counts, any spec or plan changes, and anything needing a decision.

6. **Stop at the pull request.** It is merged on GitHub by the user, never by me, and nothing is pushed to `main` directly. Once told it is merged:

   ```bash
   git checkout main && git pull
   ```

   and start the next pair from there.

## Pace

Work on ONE part at a time. Never start the next part unprompted. Finish the current part, report, and stop.
