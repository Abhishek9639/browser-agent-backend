"""Nothing forbidden reaches the logs (spec section 10).

A real run of the whole loop, with every log line captured and searched. The
redaction processor has unit tests of its own; what this adds is the thing
those cannot check, which is whether some call site logs a secret under a key
the processor was never told about.

The forbidden list is section 10's: page content, screenshots, credentials and
typed passwords. The page text here is deliberately distinctive, so a leak
shows up as a match rather than as a judgement call.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from app.agent.perception.dom_extractor import MASK, BoundingBox, ElementInfo
from app.agent.perception.state import PageState
from app.core.config import Settings
from app.core.logging import configure_logging
from app.domain.enums import TaskStatus
from tests.unit.test_orchestrator import (
    TASK_ID,
    ApprovingGate,
    FakeBrowser,
    FakeStore,
    FakeTask,
    build,
    decision_payload,
    done_payload,
)

# Strings that must never appear in a log line. Each is planted somewhere the
# loop will handle it.
SECRET_PASSWORD = "hunter2-should-never-be-logged"
PAGE_SENTENCE = "Mrs Sharma of 14 Nehru Road owes Rs 4,299 on card 4111111111111111"
CARD_NUMBER = "4111111111111111"
FAKE_JPEG = b"\xff\xd8\xff-not-really-a-jpeg"


def secret_page() -> PageState:
    """A page with a password field, a card number and a paragraph of prose."""
    password = ElementInfo(
        index=0,
        tag="input",
        name="Password",
        input_type="password",
        # Already masked by the collector, which is the contract this relies on.
        value=MASK,
        css_path="#password",
        bbox=BoundingBox(x=0, y=0, width=100, height=30),
        in_viewport=True,
    )
    return PageState(
        url="https://skyfare.test/pay",
        title="Payment",
        tabs=["t1"],
        active_tab_id="t1",
        elements=[password],
        summary_text=f"[0] input[type=password] 'Password'\n{PAGE_SENTENCE}",
        text=PAGE_SENTENCE,
        dom_hash="abc123",
    )


@pytest.fixture
def captured(settings: Settings) -> Iterator[list[dict[str, Any]]]:
    """Every structlog event from the run, as the processors left it.

    Built by replacing the renderer at the end of the real chain rather than by
    using ``structlog.testing.capture_logs``, which swaps the whole chain out.
    That distinction is the point of this file: the redaction processor and the
    contextvars merge are exactly what is being tested, and a capture that
    removed them would pass by testing nothing.

    The level is lowered to DEBUG for the duration. The suite runs at CRITICAL
    to keep its output readable, and a filtering bound logger drops an ``info``
    call before any processor sees it, so a scan at the default level would find
    nothing and call that a pass.
    """
    configure_logging(settings.model_copy(update={"LOG_LEVEL": "DEBUG"}))
    entries: list[dict[str, Any]] = []

    def collect(_logger: Any, _name: str, event_dict: Any) -> str:
        entries.append(dict(event_dict))
        return ""

    config = structlog.get_config()
    structlog.configure(processors=[*config["processors"][:-1], collect])
    try:
        yield entries
    finally:
        configure_logging(settings)


def haystack(entries: list[dict[str, Any]]) -> str:
    """Every captured event as one searchable string.

    ``default=str`` so a UUID or a datetime does not stop the serialisation and
    quietly shrink what is being searched.
    """
    return json.dumps(entries, default=str)


async def test_a_whole_run_logs_nothing_forbidden(
    settings: Settings, captured: list[dict[str, Any]]
) -> None:
    """The section 10 rule, end to end.

    The run types a password, reads a page full of personal information and
    takes a screenshot on every step. None of it may reach a log line.
    """
    rig = build(
        script=[
            decision_payload(
                value=SECRET_PASSWORD,
                target="Password",
                expectedOutcome={"type": "value_equals", "value": SECRET_PASSWORD},
            ),
            done_payload(f"Paid. {PAGE_SENTENCE}"),
        ],
        settings=settings,
        # A goal about signing in is left to the agent rather than handed back.
        store=FakeStore(task=FakeTask(instruction="Log in with my password")),
        browser=FakeBrowser(secret_page()),
        approvals=ApprovingGate(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    text = haystack(captured)
    assert captured, "nothing was logged, so this test proves nothing"

    for forbidden in (SECRET_PASSWORD, CARD_NUMBER, PAGE_SENTENCE):
        assert forbidden not in text, f"{forbidden!r} reached the logs"


async def test_the_page_summary_is_never_logged(
    settings: Settings, captured: list[dict[str, Any]]
) -> None:
    """Section 10 forbids page content, and the summary is page content.

    It is the largest thing the loop handles and the most tempting to log while
    debugging, which is why it gets its own assertion rather than being covered
    only by the sentence above.
    """
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
        browser=FakeBrowser(secret_page()),
        approvals=ApprovingGate(),
    )

    await rig.orchestrator.run(TASK_ID)

    text = haystack(captured)
    assert "input[type=password]" not in text


async def test_screenshot_bytes_are_never_logged(
    settings: Settings, captured: list[dict[str, Any]]
) -> None:
    """Every step captures one, and none of them may be in a log line."""
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
        approvals=ApprovingGate(),
    )

    await rig.orchestrator.run(TASK_ID)

    text = haystack(captured)
    assert "not-really-a-jpeg" not in text
    assert FAKE_JPEG.decode("latin-1") not in text
    # Any key that looks like an image is masked rather than rendered, whatever
    # a call site named it.
    for entry in captured:
        for key, value in entry.items():
            if "screenshot" in key.lower():
                assert value == "[REDACTED]", key


async def test_the_task_id_is_on_every_line_of_a_run(
    settings: Settings, captured: list[dict[str, Any]]
) -> None:
    """Section 9 wants a task id on the lines that have one.

    Bound for the whole run rather than passed per call, so a new log statement
    in a later part gets it without anyone remembering to add it. This test is
    what makes that claim checkable.

    Scoped to the run itself. Building the rig loads a prompt and logs that it
    did, which happens before any task exists and correctly has no task id.
    """
    rig = build(script=[decision_payload(), done_payload()], settings=settings)
    captured.clear()

    await rig.orchestrator.run(TASK_ID)

    assert captured
    assert all(entry.get("task_id") == str(TASK_ID) for entry in captured), [
        entry["event"] for entry in captured if entry.get("task_id") != str(TASK_ID)
    ]


async def test_a_step_number_is_bound_while_a_step_runs(
    settings: Settings, captured: list[dict[str, Any]]
) -> None:
    rig = build(script=[decision_payload(), done_payload()], settings=settings)

    await rig.orchestrator.run(TASK_ID)

    numbered = [entry for entry in captured if "step_number" in entry]
    assert numbered, [entry["event"] for entry in captured]
    assert {entry["step_number"] for entry in numbered} == {1, 2}


async def test_the_model_and_prompt_version_are_logged_with_each_decision(
    settings: Settings, captured: list[dict[str, Any]]
) -> None:
    """Section 9: the model name and prompt version on every step."""
    rig = build(script=[decision_payload(), done_payload()], settings=settings)

    await rig.orchestrator.run(TASK_ID)

    planned = [entry for entry in captured if entry["event"] == "planned"]
    assert planned
    for entry in planned:
        assert entry["model_name"]
        assert entry["prompt_version"]
