"""Task signatures and portable flows (spec section 4.12).

The signature is the whole cache: two tasks that mean the same thing have to
land on the same one, and two that mean different things must never. Most of
what follows is about the second half of that, because the failure it prevents
is replaying somebody else's flow.
"""

import pytest

from app.agent.cache.flow import Locator, ReplayStep, decode_steps, encode_steps, hint_for
from app.agent.cache.signature import normalize, start_domain, task_signature
from app.agent.perception.dom_extractor import BoundingBox, ElementInfo
from app.agent.perception.state import PageState
from app.domain.enums import ActionType, VerificationMethod

GOAL = "Find the cheapest flight from Delhi to Bengaluru on 20 Oct"


def element(
    index: int = 0,
    *,
    name: str = "From",
    tag: str = "input",
    role: str | None = None,
    text: str = "",
    css_path: str = "#from",
) -> ElementInfo:
    return ElementInfo(
        index=index,
        tag=tag,
        role=role,
        name=name,
        text=text,
        css_path=css_path,
        bbox=BoundingBox(x=0, y=0, width=50, height=20),
        in_viewport=True,
    )


def page(*elements: ElementInfo) -> PageState:
    return PageState(url="https://skyfare.test/", elements=list(elements))


# --------------------------------------------------------------- normalising


@pytest.mark.parametrize(
    "variant",
    [
        GOAL,
        GOAL.upper(),
        f"  {GOAL}  ",
        GOAL.replace(" ", "  "),
        f"{GOAL}?",
        f"{GOAL}.",
        GOAL.replace("from", "from" + "\u00a0"),
    ],
)
def test_the_same_request_typed_differently_gets_one_signature(variant: str) -> None:
    """Case, spacing, trailing punctuation and unicode width are all noise.

    The non-breaking space matters in practice: it is what a phone keyboard and
    a copied web page produce, and it is a different byte from a space.
    """
    assert task_signature(variant) == task_signature(GOAL)


@pytest.mark.parametrize(
    "different",
    [
        "Find the cheapest flight from Bengaluru to Delhi on 20 Oct",
        "Find the cheapest flight from Delhi to Bengaluru on 21 Oct",
        "Find the most expensive flight from Delhi to Bengaluru on 20 Oct",
        "Find the cheapest hotel from Delhi to Bengaluru on 20 Oct",
    ],
)
def test_a_different_request_gets_a_different_signature(different: str) -> None:
    """Nothing inside the sentence is normalised away.

    A cache that matched "to Delhi" with "from Delhi" would replay a flow that
    books the wrong direction, which is worse than never hitting at all.
    """
    assert task_signature(different) != task_signature(GOAL)


def test_the_start_domain_is_part_of_the_key() -> None:
    assert task_signature(GOAL, "https://skyfare.test/") != task_signature(
        GOAL, "https://other.test/"
    )


def test_the_port_is_not_part_of_the_key() -> None:
    """A demo server on a fresh port each run still hits its own cache."""
    assert task_signature(GOAL, "http://127.0.0.1:8200/") == task_signature(
        GOAL, "http://127.0.0.1:9100/"
    )


def test_a_task_with_no_start_url_is_keyed_on_its_instruction() -> None:
    assert start_domain(None) == ""
    assert len(task_signature(GOAL)) == 64


def test_the_two_halves_cannot_be_confused_for_each_other() -> None:
    """The join is a null byte, not concatenation.

    Without it, an instruction ending in a hostname and a shorter one that
    started on that host would produce the same material.
    """
    assert task_signature("book flights skyfare.test") != task_signature(
        "book flights", "https://skyfare.test/"
    )


def test_normalising_keeps_a_full_stop_inside_the_sentence() -> None:
    assert normalize("pay Rs 4,299. now") == "pay rs 4,299. now"


# ---------------------------------------------------------------- locators


def test_a_control_is_found_again_by_role_and_name() -> None:
    locator = Locator.of(element(3, name="Search flights", tag="button"))
    # A fresh read of the page: different index, same control.
    found = locator.resolve(page(element(0), element(9, name="Search flights", tag="button")))

    assert found is not None
    assert found.index == 9


def test_a_renamed_class_does_not_lose_the_control() -> None:
    """The CSS path is the last rung, not the first.

    Ids and classes are what a shuffling page changes, which is exactly the
    chaos case, so a locator that led with them would be the least durable
    thing available.
    """
    locator = Locator.of(element(0, name="Search flights", tag="button", css_path="#search-a1b2"))
    found = locator.resolve(page(element(0, name="Search flights", tag="button", css_path="#zzz")))

    assert found is not None


def test_a_control_with_no_name_is_found_by_its_text() -> None:
    locator = Locator.of(element(0, name="", tag="a", text="Cheapest first"))
    found = locator.resolve(page(element(4, name="", tag="a", text="Cheapest first")))

    assert found is not None
    assert found.index == 4


def test_a_control_that_is_gone_resolves_to_nothing() -> None:
    """Which is what hands the page back to the planner."""
    locator = Locator.of(element(0, name="Search flights", tag="button", css_path="#search"))
    replaced = element(0, name="Book now", tag="button", css_path="#book")

    assert locator.resolve(page(replaced)) is None


def test_the_css_rung_can_find_the_wrong_control_and_the_verifier_catches_it() -> None:
    """Worth stating rather than hiding.

    The stored path is the weakest rung for a reason: a page that keeps the
    selector and changes the button behind it will resolve to something the
    flow never meant. Nothing here can tell, which is why a replayed step is
    verified exactly like a planned one instead of being trusted.
    """
    locator = Locator.of(element(0, name="Search flights", tag="button", css_path="#action"))
    reused = element(0, name="Delete account", tag="button", css_path="#action")

    assert locator.resolve(page(reused)) is reused


# -------------------------------------------------------------- the sequence


def step(**overrides: object) -> ReplayStep:
    base: dict[str, object] = {
        "action": ActionType.TYPE,
        "locator": Locator(role="input", name="From", css_path="#from"),
        "value": "Delhi",
        "target": "From",
        "expected_type": VerificationMethod.VALUE_EQUALS,
        "expected_value": "Delhi",
    }
    base.update(overrides)
    return ReplayStep(**base)  # type: ignore[arg-type]


def test_a_sequence_survives_a_round_trip() -> None:
    original = (step(), step(action=ActionType.CLICK, value=None, target="Search flights"))

    assert decode_steps(encode_steps(original)) == original


def test_an_unreadable_stored_step_is_dropped_rather_than_fatal() -> None:
    """A row written by an older version must not be able to fail a task.

    The flow replays as far as it can and the planner takes the rest, which is
    the same path a stale flow takes.
    """
    stored = encode_steps((step(),))
    stored.insert(0, {"action": "teleport", "locator": None})
    stored.append({"nonsense": True})

    decoded = decode_steps(stored)

    assert len(decoded) == 1
    assert decoded[0].action is ActionType.TYPE


def test_a_stored_step_with_a_broken_outcome_falls_back_to_dom_changed() -> None:
    stored = encode_steps((step(),))
    stored[0]["expected"] = {"type": "vibes", "value": ""}

    decoded = decode_steps(stored)

    assert decoded[0].expected_type is VerificationMethod.DOM_CHANGED


def test_a_replayed_step_keeps_the_check_that_verified_it() -> None:
    """Held to the standard it passed last time, not to its action's default."""
    decision = step().as_decision(element(2))

    assert decision.expected_outcome.type is VerificationMethod.VALUE_EQUALS
    assert decision.expected_outcome.value == "Delhi"
    assert decision.target_index == 2


def test_a_replayed_decision_does_not_claim_confidence() -> None:
    """A cached step is evidence it worked once, which is worth less than it looks."""
    assert step().as_decision(element(0)).confidence == 0.5


def test_the_hint_describes_the_remaining_steps() -> None:
    text = hint_for((step(), step(action=ActionType.CLICK, value=None, target="Search flights")))

    assert text is not None
    assert "'Delhi'" in text
    assert "Search flights" in text
    # Phrased as history, because the prompt tells the planner a hint describes
    # what worked before and must be checked against the page it can see.
    assert text.startswith("A previous run")


def test_a_long_remainder_is_summarised_rather_than_recited() -> None:
    text = hint_for(tuple(step() for _ in range(9)))

    assert text is not None
    assert "then 5 more" in text


def test_nothing_remaining_means_no_hint() -> None:
    assert hint_for(()) is None
