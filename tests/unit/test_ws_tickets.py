"""Stream tickets: single use, short lived, bound to one task."""

import time
import uuid

from app.services.ws_tickets import DEFAULT_TTL_SECONDS, InMemoryTicketStore, Ticket

USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER = uuid.UUID("22222222-2222-4222-8222-222222222222")
TASK = uuid.UUID("33333333-3333-4333-8333-333333333333")
OTHER_TASK = uuid.UUID("44444444-4444-4444-8444-444444444444")


def test_a_ticket_is_bound_to_a_user_and_a_task() -> None:
    store = InMemoryTicketStore()

    ticket = store.issue(USER, TASK)

    assert ticket.user_id == USER
    assert ticket.task_id == TASK
    assert ticket.value


def test_tickets_are_unguessable_and_unique() -> None:
    store = InMemoryTicketStore()

    values = {store.issue(USER, TASK).value for _ in range(50)}

    assert len(values) == 50
    assert all(len(value) >= 32 for value in values)


def test_a_ticket_works_once() -> None:
    store = InMemoryTicketStore()
    ticket = store.issue(USER, TASK)

    assert store.consume(ticket.value, TASK) is not None
    # A ticket that could be replayed is a ticket worth stealing.
    assert store.consume(ticket.value, TASK) is None


def test_an_unknown_ticket_is_rejected() -> None:
    store = InMemoryTicketStore()

    assert store.consume("not-a-ticket", TASK) is None


def test_expiry_is_inclusive_of_the_deadline() -> None:
    ticket = Ticket(value="v", user_id=USER, task_id=TASK, expires_at=1000.0)

    assert ticket.is_expired(1000.0) is True
    assert ticket.is_expired(999.9) is False


def test_an_expired_ticket_cannot_be_redeemed() -> None:
    store = InMemoryTicketStore(ttl_seconds=0)
    ticket = store.issue(USER, TASK)
    time.sleep(0.01)

    assert store.consume(ticket.value, TASK) is None


def test_a_ticket_for_another_task_is_rejected() -> None:
    store = InMemoryTicketStore()
    ticket = store.issue(USER, TASK)

    assert store.consume(ticket.value, OTHER_TASK) is None


def test_a_rejected_ticket_is_still_burned() -> None:
    store = InMemoryTicketStore()
    ticket = store.issue(USER, TASK)

    store.consume(ticket.value, OTHER_TASK)

    # Anything else leaves a valid ticket lying around for a second attempt
    # once the caller has worked out which task it was for.
    assert store.consume(ticket.value, TASK) is None


def test_two_users_get_different_tickets_for_the_same_task() -> None:
    store = InMemoryTicketStore()

    mine = store.issue(USER, TASK)
    theirs = store.issue(OTHER_USER, TASK)

    assert mine.value != theirs.value
    assert store.consume(theirs.value, TASK).user_id == OTHER_USER  # type: ignore[union-attr]


def test_expired_tickets_are_swept() -> None:
    store = InMemoryTicketStore(ttl_seconds=0)
    store.issue(USER, TASK)
    time.sleep(0.01)

    # Otherwise a long-lived process keeps one dead entry per unused ticket.
    assert store.purge_expired() == 1
    assert store.outstanding == 0


def test_a_live_ticket_is_left_alone() -> None:
    store = InMemoryTicketStore(ttl_seconds=60)
    for _ in range(5):
        store.issue(USER, TASK)

    assert store.purge_expired() == 0
    assert store.outstanding == 5


def test_issuing_sweeps_as_it_goes() -> None:
    store = InMemoryTicketStore(ttl_seconds=0)
    store.issue(USER, TASK)
    time.sleep(0.01)

    store.issue(USER, TASK)

    assert store.outstanding == 1


def test_the_default_ttl_is_sixty_seconds() -> None:
    assert DEFAULT_TTL_SECONDS == 60
