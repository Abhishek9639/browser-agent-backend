"""Single-use tickets for opening a WebSocket (spec section 6).

A browser cannot set headers on a WebSocket handshake. The only places left to
put a credential are the URL and a cookie, and an access token in a URL is a
token in the server log, the proxy log, the browser history and any Referer
that leaks out of the page. So the token stays where it belongs, on an ordinary
authenticated POST, and that call hands back a ticket good for one connection
in the next minute.

That is a much smaller thing to lose. A stolen ticket buys one read-only stream
of one task for up to sixty seconds, and only if nobody has used it yet.
"""

import secrets
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

from app.core.logging import get_logger

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "InMemoryTicketStore",
    "Ticket",
    "TicketStore",
]

logger = get_logger(__name__)

DEFAULT_TTL_SECONDS: Final = 60

# 32 bytes of urandom. Long enough that guessing is hopeless even though the
# window is short and the ticket dies on first use.
TICKET_BYTES: Final = 32


@dataclass(frozen=True, slots=True)
class Ticket:
    """What a ticket is bound to."""

    value: str
    user_id: uuid.UUID
    task_id: uuid.UUID
    expires_at: float

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


class TicketStore(ABC):
    """Issue and redeem stream tickets.

    An interface because the in-memory implementation is per process, and a
    ticket issued by one worker has to be redeemable by whichever worker the
    WebSocket lands on. That needs Redis, and this is the seam for it.
    """

    @abstractmethod
    def issue(self, user_id: uuid.UUID, task_id: uuid.UUID) -> Ticket:
        """Mint a ticket for one user and one task."""

    @abstractmethod
    def consume(self, value: str, task_id: uuid.UUID) -> Ticket | None:
        """Redeem a ticket, or return None.

        Redeeming removes it whatever the outcome of the caller's own checks,
        so a ticket cannot be replayed even if the connection is then refused.
        """

    @abstractmethod
    def purge_expired(self) -> int:
        """Drop tickets nobody redeemed. Returns how many went."""


class InMemoryTicketStore(TicketStore):
    """Per-process tickets.

    Fine for one worker and wrong for several: a ticket issued by worker A is
    not redeemable on worker B, so a client would see an invalid ticket at
    random. Same boundary as the in-memory event bus, and the same answer.
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._tickets: dict[str, Ticket] = {}

    def issue(self, user_id: uuid.UUID, task_id: uuid.UUID) -> Ticket:
        # Opportunistic sweep. There is no background job here, and without it
        # a long-lived process accumulates one dead entry per unused ticket.
        self.purge_expired()

        ticket = Ticket(
            value=secrets.token_urlsafe(TICKET_BYTES),
            user_id=user_id,
            task_id=task_id,
            expires_at=time.monotonic() + self._ttl,
        )
        self._tickets[ticket.value] = ticket
        logger.info("stream_ticket_issued", task_id=str(task_id), ttl=self._ttl)
        return ticket

    def consume(self, value: str, task_id: uuid.UUID) -> Ticket | None:
        # Popped before anything is checked, so a wrong task or an expiry
        # still burns the ticket. Anything else leaves a valid ticket lying
        # around for a second attempt.
        ticket = self._tickets.pop(value, None)
        if ticket is None:
            return None
        if ticket.is_expired(time.monotonic()):
            logger.info("stream_ticket_expired", task_id=str(task_id))
            return None
        if ticket.task_id != task_id:
            logger.warning(
                "stream_ticket_wrong_task",
                issued_for=str(ticket.task_id),
                used_on=str(task_id),
            )
            return None
        return ticket

    def purge_expired(self) -> int:
        now = time.monotonic()
        dead = [value for value, ticket in self._tickets.items() if ticket.is_expired(now)]
        for value in dead:
            del self._tickets[value]
        return len(dead)

    @property
    def outstanding(self) -> int:
        return len(self._tickets)
