#!/usr/bin/env bash
# Container entrypoint for the API.
#
# Two jobs before the server starts: make sure the schema is current, and make
# sure a signing key exists. Both fail the container rather than being skipped,
# because a service that starts without them fails later and less clearly: a
# missing migration surfaces as an undefined column on somebody's first request,
# and a missing key as every login failing after registration succeeded.
set -euo pipefail

log() { printf '%s entrypoint: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [[ "${RUN_MIGRATIONS:-1}" == "1" ]]; then
  log "applying migrations"
  # Alembic is idempotent: several replicas starting at once serialise on the
  # alembic_version row rather than racing.
  alembic upgrade head
  log "schema is up to date"
else
  log "RUN_MIGRATIONS=0, skipping migrations"
fi

# A key on a mounted volume survives restarts, which is what keeps tokens valid
# across them. Generated here only when the volume is empty, so a deployment
# that mounts real keys is never overwritten.
private_key="${JWT_PRIVATE_KEY_PATH:-/app/keys/jwt_private.pem}"
if [[ ! -f "${private_key}" ]]; then
  if [[ "${JWT_GENERATE_KEYS:-1}" == "1" ]]; then
    log "no signing key at ${private_key}; generating one"
    python scripts/gen_keys.py --dir "$(dirname "${private_key}")"
  else
    log "no signing key at ${private_key} and JWT_GENERATE_KEYS=0"
    exit 1
  fi
fi

log "starting: $*"
exec "$@"
