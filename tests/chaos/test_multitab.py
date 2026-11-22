"""The multi-tab journey against SkyFare (spec sections 4.1 and 4.4).

SkyFare's ``newtab`` chaos configuration puts ``target="_blank"`` on every
Select link, which is how a real airline site behaves and the case the session
manager exists for: the click opens and activates a tab, and the agent reads
its detail before switching back to the original results.

The planner here is deterministic and reads only the compact page summary, the
same text a model would be shown. That is the point: if the ``Tabs:`` header
line stops naming what each tab holds, this test fails, because the planner has
no other way to know t2 is worth switching to.
"""

import re
import time
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.browser.agent_browser import DriverBrowser
from app.agent.browser.driver import PlaywrightDriver
from app.agent.llm.base import LLMMessage, LLMResponse, LLMUsage, MessageRole, elapsed_ms
from app.agent.llm.mock import MockLLM, parse_summary
from app.agent.orchestrator import Orchestrator, RunOutcome
from app.agent.planner.planner import Planner
from app.agent.planner.registry import PromptRegistry
from app.agent.recovery.runner import RecoveryRunner
from app.agent.recovery.strategies import strategies_with_grounding
from app.agent.vision.grounding import VisionGrounder, VisualGroundingService
from app.core.config import Settings
from app.domain.enums import StepStatus, TaskStatus
from app.models.task import Task
from app.repositories.unit_of_work import UnitOfWork
from app.services.events import InMemoryEventBus
from app.services.storage import LocalFileStorage
from tests.chaos.harness import DESTINATION, ORIGIN, demo_date, make_task, session_unit_of_work

pytestmark = pytest.mark.chaos

_TAB_LINE: Final = re.compile(r"^Tabs:\s*(.+)$", re.MULTILINE)
_TAB: Final = re.compile(r"^(t\d+)\s*(.*?)(\s*\(active\))?$")
_PRICE: Final = re.compile(r"(?:(?<![A-Za-z])(?:Rs\.?|INR)|₹)\s?([\d,]+)", re.IGNORECASE)


class Tabs:
    """The header's tab line, read back."""

    def __init__(self, summary: str) -> None:
        self.entries: list[tuple[str, str, bool]] = []
        match = _TAB_LINE.search(summary)
        for chunk in match.group(1).split(", ") if match else []:
            parsed = _TAB.match(chunk.strip())
            if parsed:
                self.entries.append(
                    (parsed.group(1), (parsed.group(2) or "").strip(), bool(parsed.group(3)))
                )

    @property
    def active(self) -> str:
        return next((tab for tab, _, is_active in self.entries if is_active), "")

    def labelled(self, word: str) -> str:
        """The first tab whose label mentions this, if any."""
        return next(
            (tab for tab, label, _ in self.entries if word.casefold() in label.casefold()),
            "",
        )


class TabReadingPlanner:
    """A planner that does one thing: read a detail tab and come back.

    Not intelligence, and not pretending to be. It is the smallest thing that
    exercises the tab machinery through the real loop, and it makes every
    decision from the summary rather than from state it keeps, so a broken
    header breaks it.
    """

    provider_name: Final = "mock"

    def __init__(self) -> None:
        self.actions: list[str] = []
        self.tab_lines: list[str] = []

    @property
    def provider(self) -> str:
        return self.provider_name

    @property
    def model_name(self) -> str:
        return "tab-reader"

    async def generate_json(
        self,
        system: str,
        messages: list[LLMMessage],
        json_schema: dict[str, Any],
        *,
        images: list[bytes] | None = None,
        timeout_s: float = 60.0,
    ) -> LLMResponse:
        import json

        started = time.perf_counter()
        summary = next(
            (m.text for m in reversed(messages) if m.role is MessageRole.USER),
            "",
        )
        history = " ".join(m.text for m in messages)
        seen = _TAB_LINE.search(summary)
        if seen:
            self.tab_lines.append(seen.group(0))
        decision = self._decide(summary, history)
        self.actions.append(str(decision["action"]))

        raw = json.dumps(decision)
        return LLMResponse(
            parsed=decision,
            raw_text=raw,
            usage=LLMUsage(input_tokens=len(history) // 4, output_tokens=len(raw) // 4),
            latency_ms=elapsed_ms(started),
            model_name=self.model_name,
            provider=self.provider_name,
        )

    def _decide(self, summary: str, history: str) -> dict[str, Any]:
        tabs = Tabs(summary)
        read = "extracted:" in history.lower()

        # The detail is in hand and the agent is back where it started.
        if read and tabs.active == "t1":
            prices = _PRICE.findall(history)
            answer = f"Rs {prices[-1]}" if prices else "no fare found"
            return _decision("done", final_answer=f"The fare is {answer}.")

        if read:
            # Read it; now go back to the results, which are still as they were.
            return _decision("switch_tab", tab_id="t1", outcome="new_tab_opened")

        if tabs.active != "t1":
            return _decision("extract", outcome="text_present")

        # A second tab exists, so the Select link opened one. Go and look.
        other = next((tab for tab, _, active in tabs.entries if not active), "")
        if other:
            return _decision("switch_tab", tab_id=other, outcome="new_tab_opened")

        select = next(
            (line for line in parse_summary(summary) if "/flight/" in line.href),
            None,
        )
        if select is None:
            return _decision("done", final_answer="No flight link on the page.")
        return _decision("click", target_index=select.index, outcome="new_tab_opened")


def _decision(
    action: str,
    *,
    target_index: int | None = None,
    tab_id: str | None = None,
    final_answer: str | None = None,
    outcome: str = "dom_changed",
) -> dict[str, Any]:
    return {
        "action": action,
        "targetIndex": target_index,
        "target": tab_id or action,
        "value": None,
        "tabId": tab_id,
        "reasoning": "Reading the flight detail in the tab the link opened.",
        "expectedOutcome": {"type": outcome, "value": ""},
        "riskLevel": "LOW",
        "confidence": 0.9,
        "requiresApproval": False,
        "needsVision": False,
        "finalAnswer": final_answer,
    }


async def journey(
    *,
    base_url: str,
    session: AsyncSession,
    settings: Settings,
    storage_dir: Path,
    task: Task,
    planner_llm: TabReadingPlanner,
) -> RunOutcome:
    """Start on the results page and run the loop until it stops."""
    settings = settings.model_copy(
        update={
            "STORAGE_DIR": str(storage_dir),
            "AGENT_ALLOWED_DOMAINS": ["127.0.0.1", "localhost"],
            "AGENT_HEADLESS": True,
            "AGENT_MAX_STEPS": 8,
        }
    )
    query = f"from={ORIGIN}&to={DESTINATION}&date={demo_date()}&chaos=newtab"

    driver = PlaywrightDriver(settings)
    await driver.launch()
    try:
        await driver.page.goto(f"{base_url}/results?{query}", wait_until="domcontentloaded")

        grounder = VisionGrounder(
            VisualGroundingService(
                MockLLM(),
                version=settings.VISION_PROMPT_VERSION,
                max_width=settings.VISION_MAX_WIDTH,
                max_image_bytes=settings.VISION_MAX_IMAGE_BYTES,
            )
        )
        orchestrator = Orchestrator(
            browser=DriverBrowser(driver, settings),
            planner=Planner(planner_llm, PromptRegistry(settings.PLANNER_PROMPT_VERSION)),
            storage=LocalFileStorage(settings),
            events=InMemoryEventBus(),
            unit_of_work=session_unit_of_work(session),
            settings=settings,
            grounder=grounder,
            recovery=RecoveryRunner(
                max_attempts=settings.AGENT_MAX_RETRIES_PER_STEP,
                strategies=strategies_with_grounding(grounder),
            ),
            sessions=driver.sessions,
        )
        outcome = await orchestrator.run(task.id)
        # Read before the driver goes away: the tab count is part of what
        # this journey is asserting.
        open_tabs = driver.tab_ids
    finally:
        await driver.close()

    assert open_tabs == ["t1", "t2"], open_tabs
    return outcome


async def test_the_agent_reads_a_detail_tab_and_comes_back(
    skyfare: str,
    chaos_session: AsyncSession,
    chaos_settings: Settings,
    tmp_path: Path,
) -> None:
    """Open in a new tab, read it, switch back, finish."""
    task = await make_task(chaos_session)
    planner = TabReadingPlanner()

    outcome = await journey(
        base_url=skyfare,
        session=chaos_session,
        settings=chaos_settings,
        storage_dir=tmp_path,
        task=task,
        planner_llm=planner,
    )

    assert outcome.status is TaskStatus.COMPLETED, outcome.final_answer
    # The driver follows target=_blank automatically, so no redundant switch
    # is required before reading the newly active detail page.
    assert planner.actions == ["click", "extract", "switch_tab", "done"]
    assert "Rs" in (outcome.final_answer or "")

    uow = UnitOfWork(chaos_session)
    steps = await uow.task_steps.list_for_task(task.id)

    # Every step passed its own check, the return switch included. Switches are
    # verified against the tab registry rather than the URL, which is the point:
    # switching tabs leaves the top-level URL of neither tab changed.
    assert [step.status for step in steps] == [StepStatus.VERIFIED] * 4, [
        (step.action.value, step.status.value) for step in steps
    ]
    # The step log distinguishes the tabs, so the activity feed can say where
    # each action happened.
    assert {step.tab_id for step in steps} == {"t1", "t2"}


async def test_the_tab_the_link_opened_is_named_in_the_header(
    skyfare: str,
    chaos_session: AsyncSession,
    chaos_settings: Settings,
    tmp_path: Path,
) -> None:
    """The header has to say what is in the other tab.

    A planner shown "Tabs: t1, t2" cannot tell whether t2 is the flight detail
    or an advertisement, and the whole switch decision rests on that.
    """
    task = await make_task(chaos_session)
    planner = TabReadingPlanner()

    await journey(
        base_url=skyfare,
        session=chaos_session,
        settings=chaos_settings,
        storage_dir=tmp_path,
        task=task,
        planner_llm=planner,
    )

    two_tabs = [Tabs(line) for line in planner.tab_lines if ", t" in line]

    assert two_tabs, planner.tab_lines
    # Both tabs named, and the flight id in the label of the one the link
    # opened. "t2" alone would not tell the planner anything.
    # The results tab keeps its own name while the agent is away from it,
    # and the tab the link opened is named by the flight it shows.
    assert any(tabs.labelled(DESTINATION) for tabs in two_tabs), planner.tab_lines
    assert any(tabs.labelled("-") for tabs in two_tabs), planner.tab_lines
    # Following the popup perceives it immediately. Its label must be present,
    # and the active marker must move back to the results after extraction.
    assert two_tabs[0].active == "t2"
    assert all(label for _, label, _ in two_tabs[0].entries)
    assert two_tabs[-1].active == "t1"
