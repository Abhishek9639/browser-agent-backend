# ADR-006: Three repositories rather than one

- **Status:** accepted; one of the three is built
- **Date:** 2026-08 (project shape, plan section 2)
- **Spec:** sections 2 and 7

## Context

AI-07 is a backend agent, a web frontend, and a credits wallet that takes real
payments in Razorpay's test mode. Those could be one repository with three
directories, or three repositories.

## Decision

Three: `browser-agent-backend` (this one), `browser-agent-frontend`,
`browser-agent-payments`. Each owns its own database — `agent_core` and
`payments` — and neither service reads the other's tables. They meet at two
documented interfaces and nowhere else:

- the backend calls payments over an internal API with a shared secret, specified
  in `docs/billing-integration.md`;
- payments verifies the backend's RS256 tokens by fetching
  `/.well-known/jwks.json`, specified in `docs/auth.md`.

## Consequences

**Good.** The money is in a separate process with a separate database, which is
the property worth having: a bug in the agent loop cannot corrupt a wallet, and
the payments service can be reviewed on its own. The contract had to be written
down before it could be implemented, which produced
`docs/billing-integration.md` rather than a shared module nobody documented.

**Bad.** Two of the three do not exist yet, so the backend has been developed
against a specification rather than a running service, and the first integration
will find things the specification got wrong. Local development needs three
checkouts side by side, which is what `deploy/docker-compose.full.yml` is for.
Nothing can be changed atomically across the boundary.

**Rejected: one repository, three packages.** It would have made the first
integration easier and the boundary meaningless. A shared database is the specific
thing to avoid: `SELECT` across it is too easy to write and impossible to take
back.

## Verification

`docs/billing-integration.md` is the contract, and
`tests/unit/test_billing_client.py` implements a fake payments service that
answers exactly those shapes — the executable version of the document.
`deploy/docker-compose.full.yml` describes the three-service stack, with the two
unbuilt services arranged to be commented out until they exist.
