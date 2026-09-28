"""The deterministic risk policy (spec section 4.10).

Rules, so the tests are about which rules fire and, just as importantly,
which do not. A policy that flags everything gets switched off.
"""

from typing import Any

import pytest

from app.agent.perception.dom_extractor import BoundingBox, ElementInfo
from app.agent.perception.state import PageState
from app.agent.planner.schema import Decision
from app.agent.safety.risk import HIGH_RISK_PHRASES, DeterministicRiskPolicy
from app.domain.enums import RiskLevel

POLICY = DeterministicRiskPolicy()


def element(
    index: int = 0,
    *,
    name: str = "Select",
    text: str = "",
    tag: str = "button",
    input_type: str | None = None,
) -> ElementInfo:
    return ElementInfo(
        index=index,
        tag=tag,
        name=name,
        text=text,
        input_type=input_type,
        bbox=BoundingBox(x=0, y=0, width=80, height=24),
        in_viewport=True,
    )


def page(*elements: ElementInfo, url: str = "https://skyfare.test/results") -> PageState:
    return PageState(url=url, title="SkyFare", elements=list(elements))


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


# ------------------------------------------------------------ what is high


@pytest.mark.parametrize(
    "label",
    [
        "Pay now",
        "PAY NOW",
        "Place order",
        "Buy now",
        "Checkout",
        "Confirm booking",
        "Book now",
        "Book this flight",
        "Delete account",
        "Deactivate",
        "Transfer",
        "Send money",
        "Submit application",
        "Confirm purchase",
    ],
)
def test_a_dangerous_label_is_high_risk(label: str) -> None:
    assessment = POLICY.evaluate(decision(target=label), page(element(0, name=label)))

    assert assessment.level is RiskLevel.HIGH
    assert assessment.reasons


@pytest.mark.parametrize(
    "label",
    ["Abhi kharidein", "Bhugtan karein", "Paise bhejein", "Order karein", "Khata band karein"],
)
def test_the_hinglish_variants_are_high_risk(label: str) -> None:
    """A site that labels its button "Abhi kharidein" is not less of a purchase."""
    assessment = POLICY.evaluate(decision(target=label), page(element(0, name=label)))

    assert assessment.level is RiskLevel.HIGH


def test_the_label_is_read_from_the_element_not_only_the_decision() -> None:
    # The planner's `target` is its own words; the element's name is the page's.
    assessment = POLICY.evaluate(
        decision(target="the orange button"), page(element(0, name="Pay now"))
    )

    assert assessment.level is RiskLevel.HIGH


def test_typing_into_a_password_field_is_high_risk() -> None:
    assessment = POLICY.evaluate(
        decision("type", value="hunter2"),
        page(element(0, name="Password", tag="input", input_type="password")),
    )

    assert assessment.level is RiskLevel.HIGH
    assert "password field" in assessment.reasons[0]


@pytest.mark.parametrize("field", ["Card number", "CVV", "OTP", "UPI id", "Account number"])
def test_typing_into_a_money_field_is_high_risk(field: str) -> None:
    assessment = POLICY.evaluate(
        decision("type", value="4111"),
        page(element(0, name=field, tag="input", input_type="text")),
    )

    assert assessment.level is RiskLevel.HIGH


@pytest.mark.parametrize(
    "url",
    [
        "https://skyfare.test/checkout",
        "https://skyfare.test/pay/UK-547",
        "https://skyfare.test/payment/confirm",
        "https://skyfare.test/account/billing",
    ],
)
def test_navigating_towards_a_till_is_high_risk(url: str) -> None:
    assessment = POLICY.evaluate(decision("navigate", value=url, targetIndex=None), page())

    assert assessment.level is RiskLevel.HIGH


# -------------------------------------------------------- what is not high


@pytest.mark.parametrize(
    "label",
    [
        "Search flights",
        "Select",
        "Apply filters",
        "Cheapest",
        "Display settings",
        "Buyer reviews",
        "Remove filter",
        "Payment history",
    ],
)
def test_an_ordinary_control_is_not_high_risk(label: str) -> None:
    """The false positives that would make the policy unusable.

    "Display" contains "pay", "Buyer" contains "buy", and "Remove filter" is a
    control on nearly every results page. A policy that stops the agent on all
    of those is one somebody turns off, and then it stops nothing at all.
    """
    assessment = POLICY.evaluate(decision(target=label), page(element(0, name=label)))

    assert assessment.level is RiskLevel.LOW, assessment.reasons


def test_typing_an_ordinary_value_is_not_high_risk() -> None:
    assessment = POLICY.evaluate(
        decision("type", value="Delhi"),
        page(element(0, name="From", tag="input", input_type="text")),
    )

    assert assessment.level is RiskLevel.LOW


def test_navigating_to_an_ordinary_page_is_not_high_risk() -> None:
    assessment = POLICY.evaluate(
        decision("navigate", value="https://skyfare.test/results?sort=price", targetIndex=None),
        page(),
    )

    assert assessment.level is RiskLevel.LOW


def test_a_page_mentioning_payment_does_not_make_every_click_high_risk() -> None:
    # Only the target is read. A results page with "Book" beside every flight
    # would otherwise make the sort link high risk.
    state = page(element(0, name="Cheapest"), element(1, name="Book this flight"))

    assessment = POLICY.evaluate(decision(target="Cheapest"), state)

    assert assessment.level is RiskLevel.LOW


# ------------------------------------------------------------- composition


def test_the_reasons_say_what_fired() -> None:
    assessment = POLICY.evaluate(decision(target="Pay now"), page(element(0, name="Pay now")))

    # These reach the approval prompt, so they have to read as an explanation
    # rather than as a rule id.
    assert assessment.reasons == ["the control says 'pay now'"]


def test_the_phrase_list_is_configurable() -> None:
    policy = DeterministicRiskPolicy(phrases=("frobnicate",))

    assert policy.assess(decision(target="Pay now"), page(element(0, name="Pay now"))) is (
        RiskLevel.LOW
    )
    assert (
        policy.assess(decision(target="Frobnicate"), page(element(0, name="Frobnicate")))
        is RiskLevel.HIGH
    )


def test_the_default_list_covers_the_spec_examples() -> None:
    for phrase in ("pay", "checkout", "place order", "buy now", "confirm booking", "delete"):
        assert phrase in HIGH_RISK_PHRASES


def test_the_policy_implements_the_seam() -> None:
    # `assess` is what the loop calls; `evaluate` is the same answer with its
    # reasons, which the approval prompt needs.
    state = page(element(0, name="Pay now"))
    assert POLICY.assess(decision(target="Pay now"), state) is RiskLevel.HIGH
