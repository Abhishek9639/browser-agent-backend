# ADR-009: Single-use tickets for the WebSocket, not the access token

- **Status:** accepted, implemented
- **Date:** 2026-09 (part B19)
- **Spec:** section 6

## Context

The live view is a WebSocket. Browsers cannot set headers on a WebSocket
handshake, so the usual `Authorization: Bearer` is not available, and the obvious
workaround is `?token=<jwt>`.

A JWT in a URL is a JWT in the server log, in the proxy log, in the browser's
history, and in the `Referer` of anything the page loads next. It is also valid
for its whole lifetime, so leaking it leaks an hour of API access rather than one
connection.

## Decision

`POST /api/v1/tasks/{id}/stream-ticket` on an authenticated request returns an
opaque single-use ticket, valid sixty seconds, for one task. The socket is opened
with `?ticket=...`.

Ownership is checked when the ticket is **issued**, on a request that carries a
real token, rather than at connect time where the only credential is the ticket.
The ticket is consumed on use: a second connection with the same one is refused.

## Consequences

**Good.** Nothing long-lived reaches a URL. A leaked ticket is worth one
connection to one task for at most a minute. The expensive check — ownership —
happens on a normal authenticated request, which also means the connect path is
cheap.

**Bad.** One more round trip before the stream opens. The ticket store is in
memory and per process, so a ticket issued by one worker cannot be redeemed on
another; that is the same boundary as the event bus and is documented with it. A
client that reconnects needs a new ticket, which the frontend has to handle.

**Rejected: a cookie.** It would work and would put the live view's auth on a
different mechanism from the rest of the API, with CSRF to reason about on top.

## Verification

`tests/unit/test_ws_tickets.py` (issue, consume, expiry, single use),
`tests/integration/test_stream.py` (the socket refuses a bad, expired, reused, or
someone else's ticket).
