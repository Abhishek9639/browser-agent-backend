"""Where the agent stops and the person pays.

The agent books, buys and orders on its own up to the moment money moves, and
no further. Paying is handed to the person in the visible browser: the agent
never chooses how to pay, never types a card, UPI or bank detail, and never
presses the button that charges anyone. The person pays, presses Approve, and
the agent reads the confirmation.

Deterministic, like the risk policy beside it, and for the same reason: "is
this the moment to pay" is not a question to put to a model that is reading a
page somebody else wrote.

Two checks, because either alone has a gap. :func:`is_payment_page` looks at
the page, so the agent stops as soon as a payment form is in front of it.
:func:`pays` looks at the action, so whatever the page looked like, the agent
does not press "Pay now" or type into a card field.
"""

import re
from typing import Final
from urllib.parse import urlsplit

from app.agent.perception.dom_extractor import ElementInfo
from app.agent.perception.state import PageState
from app.agent.planner.schema import Decision
from app.agent.safety.risk import phrase_pattern
from app.domain.enums import ActionType

__all__ = ["PAYMENT_PHRASES", "is_payment_page", "pays", "wants_a_purchase"]

# Controls that move money when pressed, or open the step that does. Narrower
# than the risk policy's list on purpose: "Book now" and "Checkout" start a
# booking or a basket, and the agent carries on through those behind an
# approval (spec section 4.10). These are where it stops and hands over.
PAYMENT_PHRASES: Final[tuple[str, ...]] = (
    "pay",
    "pay now",
    "make payment",
    "complete payment",
    "confirm payment",
    "proceed to pay",
    "proceed to payment",
    "continue to payment",
    "pay and book",
    "confirm and pay",
    "place order",
    "place your order",
    "confirm purchase",
    "complete purchase",
    "bhugtan karein",
    "abhi bhugtan karein",
)

_PAYING: Final = phrase_pattern(PAYMENT_PHRASES)

# Field labels only a payment form has. Not "PIN", which on an Indian address
# form is the postal code, and not "expiry", which a passport has too.
_PAYMENT_FIELD: Final = re.compile(
    r"(?<![\w])(card ?(number|no)|name on (the )?card|card ?holder|cvv|cvc|upi ?id|vpa)(?![\w])",
    re.IGNORECASE,
)
# A card number that is an identity document or a loyalty scheme, not a payment.
_NOT_PAYMENT: Final = re.compile(
    r"aadhaa?r|(?<![\w])pan(?![\w])|passport|membership|loyalty|frequent|gift|voucher",
    re.IGNORECASE,
)
# A hosted card form is a cross-origin iframe, which perception reports opaque.
# Its name is all there is to go on.
_GATEWAY_FRAME: Final = re.compile(
    r"card|payment|checkout|razorpay|stripe|paypal|payu|cashfree|juspay|billdesk|ccavenue",
    re.IGNORECASE,
)
# A host or path on its way to a till: payments.example.com, /pay/, /payment.
# Bounded by non-letters so "paytm" and "display" do not count.
_PAYMENT_URL: Final = re.compile(r"(?<![a-z])(pay|payments?|payselect)(?![a-z])", re.IGNORECASE)
_PAYMENT_METHODS: Final = re.compile(
    r"(?<![\w])(credit card|debit card|net ?banking|upi|wallets?"
    r"|cash on delivery|pay later)(?![\w])",
    re.IGNORECASE,
)
# A URL hint alone is a page about payments. Two ways to pay on it is a till.
_MIN_METHODS: Final = 2

_PURCHASE_GOAL: Final = re.compile(
    r"(?<![\w])(book|booking|buy|order|purchase|pay|reserve|checkout|check out|subscribe|rent"
    r"|recharge|kharid\w*|bhugtan)(?![\w])",
    re.IGNORECASE,
)


def wants_a_purchase(instruction: str) -> bool:
    """Is the goal one that ends at a payment?

    The page check is only worth running for one. A lookup that happens to
    pass a page with a card field on it has no payment to hand over.
    """
    return _PURCHASE_GOAL.search(instruction) is not None


def is_payment_page(state: PageState) -> bool:
    """Does the page in front of the agent ask for payment?

    A usable card, CVV or UPI field, or a payment gateway's frame, is enough on
    its own. Otherwise it takes both a payment address and at least two ways
    to pay named on the page: either alone turns up on ordinary pages, a
    "payments" help article or a results page advertising card offers.
    """
    usable = [e for e in state.elements if not e.disabled and not e.covered]
    if any(_is_payment_field(element) for element in usable):
        return True

    parts = urlsplit(state.url)
    if not _PAYMENT_URL.search(f"{parts.hostname or ''}{parts.path}"):
        return False
    text = " ".join([state.text, *(element.label for element in usable)])
    methods = {
        match.group(0).lower().replace(" ", "").removesuffix("s")
        for match in _PAYMENT_METHODS.finditer(text)
    }
    return len(methods) >= _MIN_METHODS


def pays(decision: Decision, target: ElementInfo | None) -> bool:
    """Would this action pay, or type something only the payer has?"""
    if decision.action in {ActionType.CLICK, ActionType.SELECT}:
        haystacks = [decision.target or ""]
        if target is not None:
            haystacks += [target.name, target.text]
        return any(_PAYING.search(haystack) for haystack in haystacks)
    if decision.action is ActionType.TYPE and target is not None:
        return _is_payment_field(target)
    return False


def _is_payment_field(element: ElementInfo) -> bool:
    if element.cross_origin:
        return _GATEWAY_FRAME.search(element.name) is not None
    if element.tag not in {"input", "select", "textarea"}:
        return False
    label = f"{element.name} {element.placeholder or ''}"
    return _PAYMENT_FIELD.search(label) is not None and not _NOT_PAYMENT.search(label)
