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
- A part is done only when `make check` passes (lint, typecheck, unit tests). That is the fast loop and it does not touch Postgres.
- `make check-all` adds the integration suite and is what CI runs. **It must pass before a pull request is opened.** `make check` alone once let the whole integration suite sit broken across two parts, because nothing in the fast loop connects to a database.
- Conventional Commits. One part is one or a few focused commits.

## After each part

1. Update `docs/CHANGELOG.md`.
2. Write `docs/phase-notes/<part-id>.md` covering what was built, the key decisions, and three viva questions with answers.
3. Commit the part on its branch. Pushing and opening the pull request happen after the second part of the pair; see Git workflow below.

## Git workflow

Two parts ship per branch. Never commit to `main` directly, and never merge the pull request.

**Part IDs are internal.** They never appear in a branch name, a commit message, a pull request title, or a pull request description. Not `B08`, not `B08+B09`, not a trailing `(B10)`, nothing of that form anywhere in git or on GitHub. Git history describes the change; the part numbering is our scaffolding and means nothing to a reader of the repository.

They stay in `docs/phase-notes/<part-id>.md` filenames and in `docs/CHANGELOG.md` headings, which is where they belong.

1. **Pair the parts.** Two consecutive parts share one branch.

2. **Branch from current `main`** before starting the first part of a pair, so the branch never starts from a stale base:

   ```bash
   git fetch origin && git checkout main && git pull
   git checkout -b feat/<topic>
   ```

   The topic describes the work: `feat/browser-driver`, `feat/dom-perception`. No part ids.

3. **Commit each part separately** on that branch, each an ordinary Conventional Commit describing what changed:

   ```
   feat: playwright driver and action executor
   feat: screenshots, storage and budgets
   ```

   No trailing part id.

4. **After the second part of the pair**, and only once `make check` passes:

   ```bash
   git push -u origin <branch>
   gh pr create --base main --head <branch> --title "..." --body "..."
   ```

   If the GitHub CLI is unavailable, print the compare URL to open by hand.

5. **Title and description are plain.** The title summarises what the branch delivers. The description covers the features, the test counts, any spec or plan changes, and anything needing a decision, all in plain language with no part numbering and no "first part / second part" framing.

6. **Stop at the pull request.** It is merged on GitHub by the user, never by me, and nothing is pushed to `main` directly. Once told it is merged:

   ```bash
   git checkout main && git pull
   ```

   and start the next pair from there.

## Pace

Work on ONE part at a time. Never start the next part unprompted. Finish the current part, report, and stop.
