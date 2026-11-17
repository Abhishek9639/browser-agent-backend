"""Where the agent stops and the person pays.

Deterministic rules, so the tests are about which fire and, as much, which do
not: a hand-off on every passport field or "Payment history" link would stop a
booking for nothing, and somebody would turn it off.
"""

from typing import Any

import pytest

from app.agent.perception.dom_extractor import BoundingBox, ElementInfo
from app.agent.perception.state import PageState
from app.agent.planner.schema import Decision
from app.agent.safety.payment import is_payment_page, pays, wants_a_purchase


def element(
    index: int = 0,
    *,
    name: str = "Continue",
    text: str = "",
    tag: str = "button",
    placeholder: str | None = None,
    cross_origin: bool = False,
    covered: bool = False,
) -> ElementInfo:
    return ElementInfo(
        index=index,
        tag=tag,
        name=name,
        text=text,
        placeholder=placeholder,
        bbox=BoundingBox(x=0, y=0, width=80, height=24),
        in_viewport=True,
        cross_origin=cross_origin,
        covered=covered,
    )


def field(name: str, *, placeholder: str | None = None, index: int = 0) -> ElementInfo:
    return element(index, name=name, tag="input", placeholder=placeholder)


def page(
    *elements: ElementInfo, url: str = "https://skyfare.test/review", text: str = ""
) -> PageState:
    return PageState(url=url, title="SkyFare", elements=list(elements), text=text)


def decision(action: str = "click", **overrides: Any) -> Decision:
    payload: dict[str, Any] = {
        "action": action,
        "targetIndex": 0,
        "reasoning": "because",
        "expectedOutcome": {"type": "dom_changed", "value": ""},
        "confidence": 0.9,
    }
    payload.update(overrides)
    return Decision.model_validate(payload)


# ------------------------------------------------------------ the goal


@pytest.mark.parametrize(
    "goal",
    [
        "Book a flight from Delhi to Mumbai tomorrow",
        "buy the cheapest 1TB SSD on Amazon",
        "Order a pizza",
        "reserve a table for two",
        "mobile recharge 299",
    ],
)
def test_a_goal_that_ends_in_a_payment_is_recognised(goal: str) -> None:
    assert wants_a_purchase(goal)


@pytest.mark.parametrize(
    "goal",
    ["What is the cheapest flight to Goa?", "find internships in Pune", "notebook prices"],
)
def test_a_lookup_is_not_a_purchase(goal: str) -> None:
    assert not wants_a_purchase(goal)


# ------------------------------------------------------------ the page


@pytest.mark.parametrize(
    "usable",
    [
        field("Card number"),
        field("", placeholder="Enter card no"),
        field("Name on card"),
        field("Cardholder name"),
        field("CVV"),
        field("Enter UPI ID"),
        field("VPA"),
        element(name="Secure card payment input frame", tag="iframe", cross_origin=True),
    ],
)
def test_a_payment_field_makes_a_payment_page(usable: ElementInfo) -> None:
    assert is_payment_page(page(usable))


@pytest.mark.parametrize(
    "ordinary",
    [
        # An Indian address form's PIN is the postal code.
        field("PIN code"),
        # A passport has an expiry date too.
        field("Passport expiry date"),
        field("Aadhaar card number"),
        field("Frequent flyer card number"),
        field("Gift card number"),
        # A label is not a field.
        element(name="Card number", tag="label"),
        # A cross-origin frame that is not a gateway.
        element(name="reCAPTCHA", tag="iframe", cross_origin=True),
    ],
)
def test_an_ordinary_field_does_not(ordinary: ElementInfo) -> None:
    assert not is_payment_page(page(ordinary))


def test_a_covered_payment_field_does_not_count() -> None:
    """Behind a modal, it is not yet in front of the person either."""
    covered = element(name="Card number", tag="input", covered=True)

    assert not is_payment_page(page(covered))


def test_a_payment_address_with_ways_to_pay_is_a_payment_page() -> None:
    """The tabs come before the fields: UPI, cards, net banking, nothing typed yet."""
    tabs = page(
        element(0, name="UPI"),
        element(1, name="Credit / Debit Card"),
        url="https://payments.skyfare.test/checkout",
    )
    by_text = page(
        url="https://www.shop.test/gp/buy/payselect/display",
        text="Choose a payment method: Net Banking, Cash on Delivery",
    )

    assert is_payment_page(tabs)
    assert is_payment_page(by_text)


def test_either_half_alone_is_not() -> None:
    """Card offers on a results page; a payments help article with one method in it."""
    offers = page(text="10% off with HDFC credit card, extra off with UPI")
    help_page = page(url="https://help.shop.test/payments", text="How UPI refunds work")
    paytm = page(url="https://paytm.test/flights", text="Credit card, UPI, wallets")

    assert not is_payment_page(offers)
    assert not is_payment_page(help_page)
    assert not is_payment_page(paytm)


# ------------------------------------------------------------ the action


@pytest.mark.parametrize(
    "label",
    [
        "Pay now",
        "Pay ₹4,500",
        "Proceed to pay",
        "Proceed to Payment",
        "Continue to payment",
        "Place order",
        "Place your order",
        "Confirm and pay",
        "Abhi bhugtan karein",
    ],
)
def test_pressing_a_pay_control_pays(label: str) -> None:
    assert pays(decision(), element(name=label))
    # The planner's own description is enough: grounding may have moved the index.
    assert pays(decision(target=label), None)


@pytest.mark.parametrize(
    "label",
    [
        # These start a booking or a basket. They need approval, not a hand-off.
        "Book now",
        "Checkout",
        "Buy now",
        "Confirm booking",
        "Continue",
        # Whole words only.
        "Payment history",
        "Display options",
        "Paytm offers",
    ],
)
def test_the_steps_before_paying_do_not(label: str) -> None:
    assert not pays(decision(), element(name=label))


def test_typing_a_card_or_upi_detail_pays() -> None:
    assert pays(decision("type", value="4111"), field("Card number"))
    assert pays(decision("type", value="me@bank"), field("UPI ID"))


def test_typing_a_traveller_detail_does_not() -> None:
    assert not pays(decision("type", value="Asha Rao"), field("Full name"))
    assert not pays(decision("type", value="110001"), field("PIN code"))


def test_an_action_with_no_control_never_pays() -> None:
    assert not pays(decision("scroll", targetIndex=None, value="down"), None)
    assert not pays(decision("navigate", targetIndex=None, value="https://pay.test/"), None)
