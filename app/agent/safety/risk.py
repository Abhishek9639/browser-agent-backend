"""The deterministic half of the risk assessment (spec section 4.10).

Rules, not judgement. The final risk is the higher of this and whatever the
planner declared, so a model that calls a purchase low risk cannot talk its way
past a rule that says otherwise. That asymmetry is the whole point: the model
proposes, and a fixed list decides whether a human has to look first.

Everything here errs towards asking. A false positive costs somebody a click;
a false negative spends their money.
"""

import re
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from app.agent.perception.dom_extractor import ElementInfo
from app.agent.perception.state import PageState
from app.agent.planner.schema import Decision
from app.core.logging import get_logger
from app.domain.enums import ActionType, RiskLevel

__all__ = [
    "DANGEROUS_PATHS",
    "HIGH_RISK_PHRASES",
    "SECRET_INPUT_TYPES",
    "DeterministicRiskPolicy",
    "RiskAssessment",
    "phrase_pattern",
]

logger = get_logger(__name__)

# Phrases that make an action irreversible, expensive, or both. English plus
# the Hinglish a real Indian booking site actually uses, because a site that
# labels its button "Abhi kharidein" is not less of a purchase.
HIGH_RISK_PHRASES: Final[tuple[str, ...]] = (
    # Paying
    "pay",
    "pay now",
    "make payment",
    "proceed to pay",
    "confirm purchase",
    "confirm payment",
    "place order",
    "buy now",
    "buy",
    "checkout",
    "check out",
    "bhugtan",
    "bhugtan karein",
    "abhi bhugtan karein",
    "paisa bhejo",
    "paise bhejein",
    "kharido",
    "kharidein",
    "abhi kharidein",
    "order karein",
    # Committing
    "confirm booking",
    "confirm and book",
    "book now",
    "book this flight",
    "submit application",
    "confirm order",
    "booking pakka karein",
    "abhi book karein",
    # Destroying
    "delete",
    "delete account",
    # "remove account", not bare "remove": "Remove filter" and "Remove item"
    # are ordinary controls, and a policy that stops the agent on every one of
    # them is a policy somebody turns off. The list is configurable for a
    # deployment that wants it stricter.
    "remove account",
    "remove card",
    "deactivate",
    "close account",
    "hata dein",
    "khata band karein",
    # Moving money
    "transfer",
    "send money",
    "transfer funds",
    "withdraw",
    "paisa transfer",
)

# A URL that is already on the way to a till.
DANGEROUS_PATHS: Final[tuple[str, ...]] = (
    "checkout",
    "payment",
    "/pay",
    "billing",
    "purchase",
)

# Typing into one of these is handling something that must never be replayed.
SECRET_INPUT_TYPES: Final[frozenset[str]] = frozenset({"password"})

# Field names that mean money even when the input type does not say so.
SECRET_FIELD_WORDS: Final[tuple[str, ...]] = (
    "card",
    "cardnumber",
    "cvv",
    "cvc",
    "csc",
    "otp",
    "pin",
    "security code",
    "account number",
    "upi",
)


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """What the rules concluded, and why.

    The reasons are not decoration. They go into the approval a human reads,
    and "this button says Pay now" is the difference between an approval
    prompt somebody can answer and one they have to investigate.
    """

    level: RiskLevel
    reasons: list[str] = field(default_factory=list)

    @property
    def is_high(self) -> bool:
        return self.level is RiskLevel.HIGH


def phrase_pattern(phrases: tuple[str, ...]) -> re.Pattern[str]:
    """Match whole words only.

    A substring rule would find "pay" in "display" and "buy" in "buyer
    reviews", and this repository has been bitten three times by exactly that
    on security checks. Multi-word phrases allow any run of whitespace so
    "Pay  now" across a line break still matches.
    """
    parts = sorted(phrases, key=len, reverse=True)
    alternatives = "|".join(r"\s+".join(re.escape(word) for word in part.split()) for part in parts)
    # The group around the alternation is load-bearing. Without it the
    # lookarounds bind only to the first and last branches, every phrase in
    # between matches as a bare substring, and "Payment history" reads as
    # "pay".
    return re.compile(rf"(?<![\w])(?:{alternatives})(?![\w])", re.IGNORECASE)


_PHRASES: Final = phrase_pattern(HIGH_RISK_PHRASES)


class DeterministicRiskPolicy:
    """Spec section 4.10. Implements the `RiskPolicy` seam."""

    def __init__(self, phrases: tuple[str, ...] | None = None) -> None:
        self._pattern = phrase_pattern(phrases) if phrases else _PHRASES

    def assess(self, decision: Decision, state: PageState) -> RiskLevel:
        """The seam the loop calls. See :meth:`evaluate` for the reasons."""
        return self.evaluate(decision, state).level

    def evaluate(self, decision: Decision, state: PageState) -> RiskAssessment:
        """The full assessment, with what triggered it."""
        reasons: list[str] = []
        target = self._target(decision, state)

        reasons += self._target_reasons(decision, target)
        reasons += self._typing_reasons(decision, target)
        reasons += self._navigation_reasons(decision)

        if reasons:
            logger.info(
                "risk_high",
                action=decision.action.value,
                target=decision.target,
                reasons=reasons,
            )
            return RiskAssessment(RiskLevel.HIGH, reasons)

        return RiskAssessment(RiskLevel.LOW, [])

    @staticmethod
    def _target(decision: Decision, state: PageState) -> ElementInfo | None:
        if decision.target_index is None:
            return None
        return state.element_at(decision.target_index)

    def _target_reasons(self, decision: Decision, target: ElementInfo | None) -> list[str]:
        """Does the thing being acted on say something irreversible?

        Only the target, not the whole page. A results page mentioning "Book"
        beside every flight would otherwise make every click on it high risk,
        and a risk policy that fires on everything is one people switch off.
        """
        if decision.action not in {ActionType.CLICK, ActionType.SELECT}:
            return []

        haystacks = [decision.target or ""]
        if target is not None:
            haystacks += [target.name, target.text]

        found = {
            match.group(0).strip().lower()
            for haystack in haystacks
            for match in self._pattern.finditer(haystack)
        }
        return [f"the control says {phrase!r}" for phrase in sorted(found)]

    def _typing_reasons(self, decision: Decision, target: ElementInfo | None) -> list[str]:
        """Is this filling in something that must never be replayed?"""
        if decision.action is not ActionType.TYPE or target is None:
            return []

        reasons: list[str] = []
        if (target.input_type or "").lower() in SECRET_INPUT_TYPES:
            reasons.append("it types into a password field")

        label = f"{target.name} {target.input_type or ''}".lower()
        hits = sorted({word for word in SECRET_FIELD_WORDS if _whole_word(word, label)})
        reasons += [f"it types into a field about {word!r}" for word in hits]
        return reasons

    def _navigation_reasons(self, decision: Decision) -> list[str]:
        """Is it heading somewhere that takes money?"""
        if decision.action not in {ActionType.NAVIGATE, ActionType.NEW_TAB}:
            return []

        destination = (decision.value or "").lower()
        path = f"{urlsplit(destination).path}?{urlsplit(destination).query}"
        hits = [fragment for fragment in DANGEROUS_PATHS if fragment in path]
        return [f"the destination looks like a payment page ({hit})" for hit in hits]


def _whole_word(word: str, haystack: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", haystack) is not None
