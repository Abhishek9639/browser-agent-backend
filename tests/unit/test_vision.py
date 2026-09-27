"""Visual grounding (spec section 4.9).

The model is faked throughout. What is being tested is everything around it:
which pages trigger grounding, what the picture looks like, whether an answer
is safe to act on, and that a provider being down never fails a task.
"""

import io
from typing import Any

import pytest
from PIL import Image

from app.agent.llm.base import LLMResponse, LLMUnavailable, LLMUsage
from app.agent.perception.dom_extractor import BoundingBox, ElementInfo
from app.agent.perception.state import PageState
from app.agent.planner.schema import Decision
from app.agent.vision.grounding import (
    ACCEPT_ABOVE,
    GROUNDING_JSON_SCHEMA,
    GroundReason,
    VisionGrounder,
    VisionTally,
    VisualGroundingService,
    ground_reason,
)
from app.agent.vision.marks import MAX_MARKS, annotate

THRESHOLD = 0.6
MIN_ELEMENTS = 5


def screenshot(width: int = 1280, height: int = 800) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (245, 246, 250)).save(buffer, format="PNG")
    return buffer.getvalue()


def element(
    index: int,
    *,
    name: str = "Select",
    x: int = 10,
    y: int = 10,
    width: int = 100,
    height: int = 30,
    in_viewport: bool = True,
    tag: str = "button",
) -> ElementInfo:
    return ElementInfo(
        index=index,
        tag=tag,
        name=name,
        bbox=BoundingBox(x=x, y=y, width=width, height=height),
        in_viewport=in_viewport,
    )


def page(*elements: ElementInfo, text: str = "", title: str = "SkyFare") -> PageState:
    return PageState(
        url="https://skyfare.test/results",
        title=title,
        elements=list(elements),
        text=text,
        summary_text=" ".join(e.name for e in elements),
    )


def decision(**overrides: Any) -> Decision:
    payload: dict[str, Any] = {
        "action": "click",
        "targetIndex": 0,
        "target": "Select",
        "reasoning": "pick the cheapest",
        "expectedOutcome": {"type": "dom_changed", "value": ""},
        "confidence": 0.9,
    }
    payload.update(overrides)
    return Decision.model_validate(payload)


class FakeLLM:
    """Answers with a scripted payload, or raises."""

    provider = "fake"
    model_name = "fake-vision-1"

    def __init__(
        self, parsed: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self._parsed = parsed or {}
        self._error = error
        self.calls: list[Any] = []

    async def generate_json(
        self, system: str, messages: Any, schema: Any, *, timeout_s: float = 60.0
    ) -> LLMResponse:
        self.calls.append((system, messages, schema))
        if self._error is not None:
            raise self._error
        return LLMResponse(
            parsed=self._parsed,
            raw_text="{}",
            usage=LLMUsage(input_tokens=900, output_tokens=40),
            latency_ms=1200,
            model_name=self.model_name,
            provider=self.provider,
        )


def service(llm: FakeLLM, **kwargs: Any) -> VisualGroundingService:
    return VisualGroundingService(llm, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------- triggers


def test_the_planner_asking_for_vision_is_a_reason() -> None:
    reason = ground_reason(
        True,
        0.95,
        page(*[element(i) for i in range(9)]),
        threshold=THRESHOLD,
        min_elements=MIN_ELEMENTS,
    )

    assert reason is GroundReason.PLANNER_ASKED


def test_low_confidence_is_a_reason() -> None:
    state = page(*[element(i, name=f"Button {i}") for i in range(9)])

    reason = ground_reason(False, 0.41, state, threshold=THRESHOLD, min_elements=MIN_ELEMENTS)

    assert reason is GroundReason.LOW_CONFIDENCE


def test_identical_names_are_a_reason() -> None:
    # The SkyFare results page exactly: eight Select buttons, identical in the
    # accessibility tree and obviously different on screen.
    state = page(
        element(0, name="Select"),
        element(1, name="Select"),
        *[element(i, name=f"Other {i}") for i in range(2, 9)],
    )

    reason = ground_reason(False, 0.9, state, threshold=THRESHOLD, min_elements=MIN_ELEMENTS)

    assert reason is GroundReason.DUPLICATE_NAMES


def test_a_page_with_almost_no_markup_is_a_reason() -> None:
    state = page(element(0, name="Open"), text="A seat map drawn on a canvas. " * 6)

    reason = ground_reason(False, 0.9, state, threshold=THRESHOLD, min_elements=MIN_ELEMENTS)

    assert reason is GroundReason.NO_MARKUP


def test_a_genuinely_blank_page_is_not_a_reason() -> None:
    # Otherwise an agent that navigated somewhere empty spends its budget
    # photographing nothing.
    state = page(text="", title="")

    assert ground_reason(False, 0.9, state, threshold=THRESHOLD, min_elements=MIN_ELEMENTS) is None


def test_an_ordinary_confident_step_is_not_a_reason() -> None:
    state = page(*[element(i, name=f"Button {i}") for i in range(9)])

    assert ground_reason(False, 0.9, state, threshold=THRESHOLD, min_elements=MIN_ELEMENTS) is None


# ------------------------------------------------------------------ marks


def test_the_marked_frame_is_downscaled_and_bounded() -> None:
    marked = annotate(screenshot(1600, 1000), [element(0)], max_width=1024)

    assert marked.width == 1024
    assert marked.scale == pytest.approx(0.64)
    assert marked.size_bytes < 1_500_000
    assert marked.jpeg_bytes.startswith(b"\xff\xd8\xff")


def test_labels_run_from_one_in_reading_order() -> None:
    marked = annotate(
        screenshot(),
        [element(7, y=400), element(3, y=100), element(9, y=100, x=500)],
    )

    assert [mark.label for mark in marked.marks] == [1, 2, 3]
    # Down the page, then across, which is how a model reads it.
    assert [mark.element_index for mark in marked.marks] == [3, 9, 7]


def test_elements_off_screen_are_not_marked() -> None:
    marked = annotate(screenshot(), [element(0), element(1, in_viewport=False)])

    # A box drawn for something below the fold lands on whatever happens to be
    # at those coordinates, which is worse than not drawing it.
    assert [mark.element_index for mark in marked.marks] == [0]


def test_the_number_of_marks_is_capped() -> None:
    crowd = [element(i, y=(i % 25) * 30, x=(i // 25) * 130) for i in range(90)]

    marked = annotate(screenshot(), crowd)

    assert len(marked.marks) == MAX_MARKS


def test_a_label_never_falls_outside_the_picture() -> None:
    marked = annotate(screenshot(400, 300), [element(0, x=380, y=0, width=40)])

    mark = marked.marks[0]
    assert 0 <= mark.x < marked.width
    assert 0 <= mark.y < marked.height


# --------------------------------------------------------------- validation


async def test_a_chosen_box_resolves_to_its_element() -> None:
    llm = FakeLLM({"markLabel": 2, "point": None, "confidence": 0.82, "justification": "cheapest"})
    state = page(element(5, y=10), element(8, y=200))
    grounding = service(llm)

    context = grounding.build_evidence_context(state, screenshot(), "the cheaper Select")
    result = await grounding.resolve_target(context)

    assert result.resolved is True
    assert result.element_index == 8
    assert result.confidence == pytest.approx(0.82)
    assert result.used_vision is True
    assert result.tokens == 940


async def test_a_box_that_is_not_on_the_picture_is_refused() -> None:
    llm = FakeLLM({"markLabel": 99, "point": None, "confidence": 0.99, "justification": "sure"})
    grounding = service(llm)

    context = grounding.build_evidence_context(page(element(0)), screenshot(), "anything")
    result = await grounding.resolve_target(context)

    # However confident the model is, a number that is not on the picture
    # cannot be acted on. Same guard the planner has.
    assert result.resolved is False
    assert result.error is not None and "99" in result.error


async def test_a_point_outside_the_image_is_refused() -> None:
    llm = FakeLLM(
        {"markLabel": None, "point": {"x": 9999, "y": 5}, "confidence": 0.7, "justification": "x"}
    )
    grounding = service(llm)

    context = grounding.build_evidence_context(page(element(0)), screenshot(), "anything")
    result = await grounding.resolve_target(context)

    assert result.resolved is False
    assert result.error is not None and "outside the image" in result.error


async def test_a_point_inside_the_image_is_returned_in_page_coordinates() -> None:
    llm = FakeLLM(
        {"markLabel": None, "point": {"x": 512, "y": 320}, "confidence": 0.7, "justification": "x"}
    )
    grounding = service(llm, max_width=1024)

    context = grounding.build_evidence_context(page(element(0)), screenshot(1280, 800), "x")
    result = await grounding.resolve_target(context)

    # The marks were drawn on a 0.8 scale image; everything downstream works
    # in the page's own space.
    assert result.point == (640, 400)


async def test_the_model_saying_it_cannot_tell_is_respected() -> None:
    llm = FakeLLM(
        {"markLabel": None, "point": None, "confidence": 0.2, "justification": "two look alike"}
    )
    grounding = service(llm)

    context = grounding.build_evidence_context(page(element(0)), screenshot(), "x")
    result = await grounding.resolve_target(context)

    assert result.resolved is False
    assert result.confidence == pytest.approx(0.2)


async def test_a_page_with_nothing_to_mark_is_not_sent() -> None:
    llm = FakeLLM({"markLabel": 1, "point": None, "confidence": 1.0, "justification": "x"})
    grounding = service(llm)

    context = grounding.build_evidence_context(page(), screenshot(), "x")
    result = await grounding.resolve_target(context)

    assert result.resolved is False
    assert llm.calls == [], "nothing to point at, so no call worth paying for"


async def test_an_oversized_frame_is_refused_before_the_round_trip() -> None:
    llm = FakeLLM({"markLabel": 1, "point": None, "confidence": 1.0, "justification": "x"})
    grounding = service(llm, max_image_bytes=10)

    context = grounding.build_evidence_context(page(element(0)), screenshot(), "x")
    result = await grounding.resolve_target(context)

    assert result.resolved is False
    assert result.error is not None and "too large" in result.error
    assert llm.calls == []


# -------------------------------------------------------- graceful failure


async def test_a_provider_that_is_down_does_not_raise() -> None:
    """TEST-010. Vision being unavailable degrades to DOM-only."""
    llm = FakeLLM(error=LLMUnavailable("connection refused"))
    grounding = service(llm)

    context = grounding.build_evidence_context(page(element(0)), screenshot(), "x")
    result = await grounding.resolve_target(context)

    assert result.resolved is False
    assert result.error is not None and "unavailable" in result.error.lower()
    assert result.used_vision is False


async def test_the_grounder_hands_the_decision_back_untouched_when_vision_fails() -> None:
    grounder = VisionGrounder(service(FakeLLM(error=LLMUnavailable("down"))))
    original = decision()

    outcome = await grounder.resolve(original, page(element(0)), screenshot(), "asked for")

    # The planner's own target may well be right. Grounding was a second
    # opinion, not a veto, and it must never fail the task.
    assert outcome.decision is original
    assert outcome.error is not None
    assert outcome.reason == "asked for"


async def test_the_grounder_corrects_the_target_when_vision_disagrees() -> None:
    llm = FakeLLM({"markLabel": 2, "point": None, "confidence": 0.9, "justification": "cheaper"})
    grounder = VisionGrounder(service(llm))
    state = page(element(0, y=10), element(4, y=200))

    outcome = await grounder.resolve(decision(targetIndex=0), state, screenshot(), "duplicates")

    assert outcome.decision.target_index == 4
    assert outcome.used_vision is True
    assert outcome.bbox is not None
    assert outcome.tokens == 940


# ----------------------------------------------------------------- tallying


def test_the_tally_counts_calls_failures_and_cost() -> None:
    from app.agent.vision.grounding import GroundingResult

    tally = VisionTally()
    tally.record(GroundingResult(element_index=1, latency_ms=900, tokens=500), "asked for")
    tally.record(GroundingResult(error="down", latency_ms=50), "low confidence")

    assert tally.calls == 2
    assert tally.failures == 1
    assert tally.latency_ms == 950
    assert tally.tokens == 500
    assert tally.reasons == ["asked for", "low confidence"]


def test_the_schema_asks_for_exactly_what_is_validated() -> None:
    properties = GROUNDING_JSON_SCHEMA["properties"]

    assert set(properties) == {"markLabel", "point", "confidence", "justification"}
    assert GROUNDING_JSON_SCHEMA["additionalProperties"] is False


# ------------------------------------------------- against real pages


@pytest.mark.browser
async def test_two_identical_buttons_trigger_grounding(driver: Any, html_server: str) -> None:
    """The case the DOM cannot answer.

    Both buttons are called "Select" and nothing in the accessibility tree
    tells them apart. The prices beside them are painted onto canvases, so
    they exist only in the picture.
    """
    from app.agent.perception.perceive import perceive

    await driver.page.goto(f"{html_server}/vision_identical.html")
    state = await perceive(driver.page, driver)

    names = [item.name for item in state.elements if item.tag == "button"]
    assert names.count("Select") == 2

    reason = ground_reason(False, 0.95, state, threshold=THRESHOLD, min_elements=MIN_ELEMENTS)
    assert reason is GroundReason.DUPLICATE_NAMES


@pytest.mark.browser
async def test_the_marked_frame_covers_the_real_buttons(driver: Any, html_server: str) -> None:
    from app.agent.browser.screenshots import capture
    from app.agent.perception.perceive import perceive

    await driver.page.goto(f"{html_server}/vision_identical.html")
    state = await perceive(driver.page, driver)
    shot = await capture(driver.page)

    grounding = service(FakeLLM())
    context = grounding.build_evidence_context(state, shot.jpeg_bytes, "the cheaper Select")

    assert context.has_marks
    # The text list is what lets the model tie a number to a name, and it has
    # to agree with the boxes drawn on the picture.
    assert context.candidates.count("Select") >= 2
    assert all(mark.width > 0 and mark.height > 0 for mark in context.marked.marks)
    assert context.marked.size_bytes < 1_500_000


@pytest.mark.browser
async def test_a_canvas_only_page_triggers_grounding(driver: Any, html_server: str) -> None:
    from app.agent.perception.perceive import perceive

    await driver.page.goto(f"{html_server}/vision_canvas.html")
    state = await perceive(driver.page, driver)

    # Almost nothing to read: the seat map is painted, not marked up.
    assert len(state.elements) < MIN_ELEMENTS
    reason = ground_reason(False, 0.95, state, threshold=THRESHOLD, min_elements=MIN_ELEMENTS)
    assert reason is GroundReason.NO_MARKUP


@pytest.mark.browser
async def test_a_coordinate_click_reaches_a_canvas(driver: Any, browser_settings: Any) -> None:
    from app.agent.browser.agent_browser import DriverBrowser

    browser = DriverBrowser(driver, browser_settings)
    await driver.page.goto("about:blank")
    await driver.page.set_content(
        "<canvas id='c' width='300' height='200' style='background:#eee'></canvas>"
        "<p id='out'></p>"
        "<script>document.getElementById('c').addEventListener('click', function (e) {"
        "  var r = this.getBoundingClientRect();"
        "  document.getElementById('out').textContent ="
        "    Math.round(e.clientX - r.left) + ',' + Math.round(e.clientY - r.top); });</script>"
    )

    assert await browser.click_at(60, 50) is True
    assert await driver.page.inner_text("#out") != ""


@pytest.mark.browser
async def test_a_coordinate_outside_the_viewport_is_refused(
    driver: Any, browser_settings: Any
) -> None:
    from app.agent.browser.agent_browser import DriverBrowser

    browser = DriverBrowser(driver, browser_settings)
    await driver.page.goto("about:blank")

    # A coordinate that came from a model naming a point on a picture is
    # exactly the input that should not reach the mouse unchecked.
    assert await browser.click_at(99_999, 5) is False
    assert await browser.click_at(-4, 5) is False


# ------------------------------------------- when a correction is accepted


async def test_a_shaky_correction_is_declined_when_names_merely_collide() -> None:
    """Overruling a specific choice on a shaky reading swaps a good target for
    a bad one, and duplicate names are the case where the planner may well
    have been right all along."""
    llm = FakeLLM({"markLabel": 2, "point": None, "confidence": 0.5, "justification": "maybe"})
    grounder = VisionGrounder(service(llm))
    state = page(element(0, y=10), element(4, y=200))

    outcome = await grounder.resolve(
        decision(targetIndex=0), state, screenshot(), GroundReason.DUPLICATE_NAMES
    )

    assert outcome.decision.target_index == 0, "the planner's target should stand"
    assert outcome.used_vision is True
    # The disagreement is still recorded even though it was not acted on.
    assert outcome.bbox is not None
    assert outcome.justification == "maybe"


async def test_a_confident_correction_is_accepted_even_for_colliding_names() -> None:
    llm = FakeLLM({"markLabel": 2, "point": None, "confidence": 0.88, "justification": "cheaper"})
    grounder = VisionGrounder(service(llm))
    state = page(element(0, y=10), element(4, y=200))

    outcome = await grounder.resolve(
        decision(targetIndex=0), state, screenshot(), GroundReason.DUPLICATE_NAMES
    )

    assert outcome.decision.target_index == 4


async def test_any_answer_is_accepted_when_the_planner_had_no_idea() -> None:
    llm = FakeLLM({"markLabel": 2, "point": None, "confidence": 0.2, "justification": "a guess"})
    grounder = VisionGrounder(service(llm))
    state = page(element(0, y=10), element(4, y=200))

    outcome = await grounder.resolve(
        decision(targetIndex=0), state, screenshot(), GroundReason.PLANNER_ASKED
    )

    # The planner said it could not tell, so a guess beats the target it
    # admits it is unsure of.
    assert outcome.decision.target_index == 4


async def test_recovery_accepts_whatever_vision_offers() -> None:
    llm = FakeLLM({"markLabel": 2, "point": None, "confidence": 0.1, "justification": "last hope"})
    grounder = VisionGrounder(service(llm))
    state = page(element(0, y=10), element(4, y=200))

    outcome = await grounder.resolve(
        decision(targetIndex=0), state, screenshot(), GroundReason.RECOVERY
    )

    # Everything that works by name has already failed.
    assert outcome.decision.target_index == 4


def test_every_reason_has_an_acceptance_floor() -> None:
    assert set(ACCEPT_ABOVE) == set(GroundReason)
    assert ACCEPT_ABOVE[GroundReason.DUPLICATE_NAMES] > ACCEPT_ABOVE[GroundReason.PLANNER_ASKED]
