"""What the loop depends on, and the stand-ins it uses until those parts exist.

Every collaborator the orchestrator needs is a protocol here, each with a
default implementation that does the least interesting correct thing. Naming
them now, instead of letting the behaviour grow inline, is what keeps the later
parts from being surgery on the loop: each one swaps a default for a real
implementation and touches nothing else.

The defaults live together rather than in the packages that will own them, so
that "what is not built yet" is one file you can read end to end. As each part
lands, its default moves out to `app/agent/<area>/` and this module keeps only
the protocol.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from app.agent.browser.driver import ActionResult, BrowserSnapshot
from app.agent.browser.screenshots import Screenshot
from app.agent.perception.state import PageState
from app.agent.planner.schema import Decision
from app.domain.enums import RiskLevel, VerificationMethod

__all__ = [
    "AgentBrowser",
    "ApprovalGate",
    "ApprovalOutcome",
    "CachedHint",
    "CancelSignal",
    "DecisionRiskPolicy",
    "DenyApprovals",
    "FlowCache",
    "Grounder",
    "GroundingOutcome",
    "NeverCancelled",
    "NoCache",
    "NoGrounding",
    "NoRecovery",
    "PageStateSessions",
    "RecoveryStrategy",
    "RiskPolicy",
    "SessionManager",
    "Verdict",
    "Verifier",
]


# --------------------------------------------------------------- outcomes


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether a step did what it was supposed to (spec section 4.7).

    ``method`` is one of the six section 4.5 outcome types, because that is
    what ``verifications.verification_method`` stores and what the spec calls
    canonical. The verifier runs a few more checks than there are enum values,
    so the precise one is named in ``actual`` under ``check``, where a
    free-form record belongs.

    ``expected`` and ``actual`` are what the two JSONB columns hold, so a step
    can be read back later and the decision re-argued from the row alone.
    """

    ok: bool
    method: VerificationMethod
    expected: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """A human's answer to a high-risk action (spec section 4.10)."""

    approved: bool
    approval_id: uuid.UUID | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CachedHint:
    """A flow that worked before, offered to the planner (spec section 4.9)."""

    text: str
    flow_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class GroundingOutcome:
    """A decision after vision has had a chance to correct its target."""

    decision: Decision
    used_vision: bool = False
    bbox: dict[str, int] | None = field(default=None)


# -------------------------------------------------------------- protocols


@runtime_checkable
class AgentBrowser(Protocol):
    """Everything the loop asks of a browser.

    Wider than `BrowserDriver` because the loop also needs to look at the page
    and photograph it, and narrower than the Playwright driver because it has
    no business launching or closing anything: the caller owns that lifetime.
    """

    async def perceive(self) -> PageState: ...

    async def settle(self) -> None:
        """Wait for the page to stop changing before it is read.

        Without this, verification races the page: a click that triggers a
        fetch is checked before the response lands, and a perfectly good step
        is recorded as failed.
        """
        ...

    async def screenshot(self) -> Screenshot: ...

    async def capture_state(self) -> BrowserSnapshot: ...

    async def execute(self, action: BaseModel) -> ActionResult: ...


class Verifier(Protocol):
    """Did the action produce the outcome the planner expected?

    Takes page states rather than the cheap snapshot, because half the checks
    need the element list: whether a control is visible, what an input now
    holds, whether a checkbox is ticked.

    Never calls a model. ADR-004 and spec section 4.7: the model proposes
    actions and verification stays deterministic, so that a model which is
    wrong about what happened cannot also be the thing that decides whether it
    happened.
    """

    async def check(
        self,
        decision: Decision,
        before: PageState,
        after: PageState,
        result: ActionResult,
    ) -> Verdict: ...


class FlowCache(Protocol):
    """Has this instruction been solved before?"""

    async def lookup(self, instruction: str) -> CachedHint | None: ...


class Grounder(Protocol):
    """Resolve an ambiguous or low-confidence target by looking at pixels."""

    async def resolve(self, decision: Decision, state: PageState) -> GroundingOutcome: ...


class RiskPolicy(Protocol):
    """The deterministic half of the risk assessment.

    Final risk is the higher of this and the planner's own, per spec section
    4.5, so a model that calls a purchase low risk cannot talk its way past a
    rule that says otherwise.
    """

    def assess(self, decision: Decision, state: PageState) -> RiskLevel: ...


class ApprovalGate(Protocol):
    """Park a high-risk action on a human and wait for the answer."""

    async def request(
        self,
        *,
        task_id: uuid.UUID,
        step_number: int,
        decision: Decision,
        risk: RiskLevel,
        screenshot: str | None,
        bbox: dict[str, int] | None,
    ) -> ApprovalOutcome: ...


class RecoveryStrategy(Protocol):
    """Try to rescue a step whose verification failed (spec section 4.8).

    Returns ``None`` when nothing was attempted, so the caller can tell "no
    recovery ran" apart from "recovery ran and failed". The two mean different
    things to anyone reading the step log.
    """

    async def run(
        self,
        *,
        decision: Decision,
        state: PageState,
        result: ActionResult,
        outcome: Verdict,
    ) -> Verdict | None: ...


class SessionManager(Protocol):
    """Owns the tab registry and the stable ids the client sees."""

    def tab_id(self, state: PageState) -> str: ...


class CancelSignal(Protocol):
    """Has someone asked for this task to stop?

    Checked between phases rather than relied on through task cancellation,
    because ``asyncio.CancelledError`` arriving at an arbitrary await would
    leave the run with no chance to settle its status or publish a final event.
    A flag the loop reads at known points stops it at a known point.
    """

    def is_cancelled(self) -> bool: ...


# --------------------------------------------------------------- defaults


class NeverCancelled:
    """Nothing ever asks this run to stop.

    The default for a run nobody is supervising, such as an evaluation or a
    test. The task runner supplies a real one.
    """

    def is_cancelled(self) -> bool:
        return False


class NoCache:
    """Never a hit. Replaced by the flow cache."""

    async def lookup(self, instruction: str) -> CachedHint | None:
        return None


class NoGrounding:
    """Hands the decision back untouched. Replaced by visual grounding.

    ``used_vision`` stays false, which is what the step event reports and what
    the vision-rate KPI counts.
    """

    async def resolve(self, decision: Decision, state: PageState) -> GroundingOutcome:
        return GroundingOutcome(decision=decision, used_vision=False)


class DecisionRiskPolicy:
    """Adds no rules of its own. Replaced by the deterministic policy.

    Returning the planner's own level makes it the identity element of the
    section 4.5 ``max``, so wiring the real policy in later changes the answer
    without changing the arithmetic around it.
    """

    def assess(self, decision: Decision, state: PageState) -> RiskLevel:
        return decision.risk_level


class DenyApprovals:
    """Refuses every request. Replaced by the approval flow.

    Deliberately not an auto-approve. A no-op that waves high-risk actions
    through is a hole that looks like working software, and the day somebody
    forgets to swap it out is the day an agent buys something. Refusing is
    visible immediately and fails the task rather than the user.
    """

    async def request(
        self,
        *,
        task_id: uuid.UUID,
        step_number: int,
        decision: Decision,
        risk: RiskLevel,
        screenshot: str | None,
        bbox: dict[str, int] | None,
    ) -> ApprovalOutcome:
        return ApprovalOutcome(
            approved=False,
            reason="This action needs human approval, and approvals are not available yet.",
        )


class NoRecovery:
    """Attempts nothing. Replaced by the recovery ladder."""

    async def run(
        self,
        *,
        decision: Decision,
        state: PageState,
        result: ActionResult,
        outcome: Verdict,
    ) -> Verdict | None:
        return None


class PageStateSessions:
    """Takes the tab id perception already worked out.

    Replaced by the session manager, which will hand out ids that stay stable
    across a reload rather than being whatever the driver last called the tab.
    """

    def tab_id(self, state: PageState) -> str:
        return state.active_tab_id
