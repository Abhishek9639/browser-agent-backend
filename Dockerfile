# The API image.
#
# Based on Microsoft's official Playwright Python image, pinned to the same
# Playwright version as pyproject.toml. That pairing is not cosmetic: the Python
# package and the browser build it drives are released together, and a mismatch
# fails at the first `page.goto` with a protocol error rather than at build time.
# When `playwright` moves in pyproject.toml, the tag here moves with it.
#
# The image brings Chromium and its system libraries, which is the reason to use
# it: installing those on a slim base is about forty packages of guesswork that
# Microsoft has already done.

# ----------------------------------------------------------------- base
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble AS base

# Bytecode on import is wasted work in a container that is replaced rather than
# restarted; unbuffered output is what makes `docker logs` useful during a hang.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # uv installs into this, and every stage below puts it on PATH.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Pinned rather than `latest`: a resolver that changes under CI is a build that
# is not reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /usr/local/bin/

WORKDIR /app


# ------------------------------------------------------------ dependencies
# Its own stage so the layer is cached on the lockfile alone. Application code
# changes on every commit; dependencies change on the rare one, and rebuilding
# them each time costs minutes.
FROM base AS deps

COPY pyproject.toml uv.lock ./

# --frozen: install exactly the lockfile, and fail rather than resolve. A
# container that quietly upgraded a dependency is a container running something
# nobody tested.
# --no-install-project: the project itself is copied in the next stage, so
# installing it here would invalidate this layer on every code change.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project


# -------------------------------------------------------------------- api
FROM base AS api

# The virtualenv, already built.
COPY --from=deps /opt/venv /opt/venv

# Ownership set on copy rather than with a later `chown -R`, which would double
# the size of the layer by rewriting every file.
COPY --chown=pwuser:pwuser alembic.ini ./
COPY --chown=pwuser:pwuser migrations ./migrations
COPY --chown=pwuser:pwuser scripts ./scripts
COPY --chown=pwuser:pwuser app ./app
COPY --chown=pwuser:pwuser docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# The project has no build-system table, so there is nothing to install: the
# code is found on the working directory, which is how uvicorn and alembic are
# already run in development.
RUN chmod +x /usr/local/bin/entrypoint.sh \
    # Created here so the directories exist and are writable even when no volume
    # is mounted over them: the agent writes a screenshot every step, and a
    # permission error on step one is a confusing way to learn about a volume.
    && mkdir -p /app/storage /app/keys \
    && chown -R pwuser:pwuser /app/storage /app/keys

# pwuser comes with the Playwright image and owns its browser cache. Running as
# root would mean every screenshot written to a mounted volume is root-owned on
# the host, and a browser running as root is a sandbox with the door open.
USER pwuser

ENV STORAGE_DIR=/app/storage \
    JWT_PRIVATE_KEY_PATH=/app/keys/jwt_private.pem \
    JWT_PUBLIC_KEY_PATH=/app/keys/jwt_public.pem

VOLUME ["/app/storage"]
EXPOSE 8000

# Uses the health endpoint the frontend and compose already use (decision A9).
# `start-period` covers migrations and the Chromium launch; without it the
# container is reported unhealthy for the first few seconds of every start.
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
