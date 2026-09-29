# CI and deployment

What runs on every push, how to verify a change to it, and the branch
protection settings to apply by hand.

## The workflow

`.github/workflows/ci.yml`, on every push to `main`, every pull request, and on
demand. Seven jobs; the first six run at once and `gate` waits for all of them.

| Job | What it does | Typical time |
|---|---|---|
| `lint` | `ruff check`, `ruff format --check`, `mypy` | ~1 min |
| `unit` | The unit suite, with coverage, against a real Chromium | ~5 min |
| `integration` | The integration suite against a Postgres service container | ~4 min |
| `chaos` | Ten chaos configurations plus one benchmark round | ~15 min |
| `audit` | `pip-audit --strict` and a gitleaks scan of the history | ~1 min |
| `image` | Builds both images, starts the stack, checks it is healthy | ~6 min |
| `gate` | Fails unless every job above succeeded | seconds |

The jobs are separate rather than one script because the feedback that matters
is "lint is wrong" arriving in a minute rather than after the chaos suite.

### Why `gate` exists

Branch protection names required checks by job name, so every job added later
would mean editing the protection rule, and a job somebody forgot to add would
be silently optional. One required check that fails unless everything else
succeeded keeps that in the repository rather than in a settings page.

It checks the results explicitly rather than relying on `needs`. A job that only
lists dependencies does **not** fail when one of them is skipped or cancelled,
and "cancelled" is what a timeout looks like.

### Caching

Two caches, both keyed on something that actually invalidates them.

**Dependencies** are cached by `setup-uv` on `uv.lock`. The resolved set is what
the cache holds and the lockfile is the only thing that changes it.

**The Chromium build** is cached on the resolved Playwright version, not on the
lockfile. It is about 400 MB and takes a couple of minutes to download, and the
version is the thing that must invalidate it: the Python package and the browser
build are released together, and a cache keyed on anything else would eventually
pair a new package with last week's browser. On a hit the job still runs
`playwright install-deps`, because the system libraries live outside the cached
directory.

The `image` job is deliberately **not** cached. It runs `docker compose build`,
the same command `make up` runs, so it cannot pass against an image assembled
differently from the one a developer gets.

### The LLM provider

`LLM_PROVIDER: mock` at the workflow level. The mock is rule-based and offline,
so CI costs nothing, needs no secret, and cannot be broken by somebody else's
rate limit. It measures the loop, the verifier and the recovery ladder rather
than a model's judgement, which is the right thing for a regression gate; the
model's judgement is measured by the benchmark with `--provider` and a key,
deliberately by hand.

### Secrets

The workflow needs none to pass. `GITLEAKS_LICENSE` is referenced because the
gitleaks action requires it for organisation-owned private repositories; on a
personal public repository it is unset and ignored.

## Verifying a change to the workflow

`act` is not installed here, so the workflow has not been executed locally.
Three things that can be checked without pushing, in increasing usefulness:

**1. Lint it.** `actionlint` catches the majority of real mistakes, including
expression syntax, unknown contexts, and shell problems inside `run` blocks:

```bash
actionlint .github/workflows/ci.yml
```

That is how the one bug in this workflow was found: `join(needs.*.result, " ")`
with double quotes, which GitHub's expression parser rejects because only single
quotes delimit a string there. It would have failed at the gate, at the end of
the run, on every push.

**2. Run the same commands locally.** Every job is a `make` target or a one-line
equivalent, on purpose:

```bash
make check-all     # lint, typecheck, unit, integration
make audit         # pip-audit and gitleaks
make chaos         # the chaos suite
make up            # what the image job does
```

If those pass locally and `actionlint` is clean, what is left is the runner's
own environment: the service container, the caches and the action versions.

**3. Push to a branch and read the run.** The remaining failures are the ones
only a runner can produce. Watch for:

- the Postgres service container not being ready — the health options cover it,
  and the `Create the test database` step fails immediately if not;
- `playwright install` needing a system library the runner lacks, which is what
  `--with-deps` is for;
- the first run being slow, because both caches are cold. The second run is the
  one to judge.

`act` would cover part of this and not the service containers. Installing it is
`brew install act`; it needs Docker and it does not reproduce the cache actions.

## Branch protection for `main`

To apply by hand, in **Settings → Branches → Add branch ruleset** (or the older
**Branch protection rules**), targeting `main`:

1. **Require a pull request before merging.**
   - Required approvals: 1 for a team, 0 for a solo project. The review is worth
     more than the number.
   - **Dismiss stale approvals when new commits are pushed** — on. An approval
     of the diff somebody read, not of whatever arrives later.
2. **Require status checks to pass before merging.**
   - **Require branches to be up to date before merging** — on. Two changes that
     each pass alone and fail together are otherwise merged in sequence.
   - Required check: **`Quality gate`** (the `gate` job). One entry, deliberately:
     see above.
3. **Require conversation resolution before merging** — on.
4. **Require linear history** — on, if you merge with squash or rebase. It keeps
   `git log` on `main` readable, which is most of what makes a bisect possible.
5. **Do not allow bypassing the above settings** — on, including for
   administrators. A rule you can click past is a rule that gets clicked past at
   the worst moment.
6. **Restrict force pushes and deletions** — on. Both are the default for a
   protected branch and both are worth confirming.

What not to turn on: **require signed commits**, unless every machine that will
push has a signing key configured. It fails confusingly and at the last step.

### Checking it worked

Push a branch with a deliberately failing test, open a pull request, and confirm
the merge button is blocked with `Quality gate` listed as the reason. A rule
nobody has seen refuse a merge is a rule nobody knows is on.

## Images

Two targets in one `Dockerfile`, both from the official Playwright Python image
pinned to the same version as `pyproject.toml`:

- `api` — the service. Runs migrations and generates a signing key on first
  start, then uvicorn. Non-root (`pwuser`, from the base image), with
  `/app/storage` as a volume.
- `skyfare` — the demo site. Same layers, no database, no keys, no browser.

The pinning is not cosmetic. The Playwright Python package and the browser build
it drives are released together, and a mismatch fails at the first `page.goto`
with a protocol error rather than at build time. When `playwright` moves in
`pyproject.toml`, the `FROM` tag moves with it, and the cache key in CI follows
the resolved version so it cannot lag behind.

### Running it

```bash
make up      # build and start postgres, api, skyfare
make logs    # follow
make down    # stop, keeping the volumes
```

Then `http://localhost:8000/docs`, `http://localhost:8000/api/v1/health`, and
SkyFare on `http://localhost:8200`.

Compose reads the repository's `.env` by itself, so a developer with a real
provider configured there gets that provider in the container rather than the
mock. That is usually what you want and occasionally surprising; `LLM_PROVIDER=mock
make up` overrides it for one run.

The full stack, with the frontend and payments service as sibling checkouts, is
`deploy/docker-compose.full.yml`. Both of those repositories are still to be
written, and their service blocks are arranged so each can be commented out in
one piece until they exist.

## Known limitations

- **No deployment.** CI builds images and throws them away: there is no registry
  and nowhere to deploy to. Pushing to one is a `docker/login-action` and two
  lines in the `image` job when there is somewhere to push.
- **The chaos suite cannot run twice at once.** Two runs share the test database
  and collide. `concurrency` cancels superseded runs on a branch, which covers
  the common case and not two different branches at the same moment.
- **Coverage is uploaded, not enforced.** No minimum, so nothing fails when it
  drops. A threshold is one line in `pyproject.toml` and worth having once the
  number has settled.
- **The `image` job builds from scratch every run**, for the reason above. Around
  six minutes, most of it pulling the Playwright base image.
