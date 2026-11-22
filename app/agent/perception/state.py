"""The full perception result (spec section 4.6).

Distinct from :class:`app.agent.browser.driver.BrowserSnapshot`, which is the
cheap before/after snapshot the verifier compares. This is the expensive one,
built once per step: the element list, the compact summary the planner reads,
and a hash of what the page looked like.
"""

import hashlib
from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from app.agent.perception.dom_extractor import ElementInfo

__all__ = ["DOM_HASH_LENGTH", "PageState", "ScrollState", "element_signature"]

# Half a sha256 is far more than enough to tell two page states apart, and it
# is short enough to read in a log line.
DOM_HASH_LENGTH: Final = 16


class ScrollState(BaseModel):
    model_config = ConfigDict(frozen=True)

    y: int = 0
    max_y: int = 0

    @property
    def at_bottom(self) -> bool:
        return self.y >= self.max_y


def element_signature(element: ElementInfo) -> str:
    """A stable fingerprint of one element.

    Position is deliberately excluded. The hash answers "did the page change",
    and scrolling changes every bbox while changing nothing about the content.
    Including coordinates would make ``dom_changed`` fire on every scroll,
    which would make the section 4.7 verifier useless.
    """
    return "|".join(
        (
            element.tag,
            element.role or "",
            element.name,
            element.input_type or "",
            element.href or "",
            element.value or "",
            "d" if element.disabled else "",
            {True: "c", False: "u", None: ""}[element.checked],
        )
    )


def compute_dom_hash(url: str, elements: list[ElementInfo]) -> str:
    """Hash the URL and the element signatures, in order.

    Order matters: two pages with the same controls rearranged are different
    pages, and the agent should notice.
    """
    digest = hashlib.sha256()
    digest.update(url.encode("utf-8"))
    for element in elements:
        digest.update(b"\x1f")
        digest.update(element_signature(element).encode("utf-8"))
    return digest.hexdigest()[:DOM_HASH_LENGTH]


class PageState(BaseModel):
    """Everything the planner is told about the page, plus the raw elements."""

    model_config = ConfigDict(frozen=True)

    url: str
    title: str = ""
    tabs: list[str] = Field(default_factory=list)
    # What each tab is showing, by tab id. Only tabs that have been
    # perceived at least once have one, which is why this is not keyed off
    # `tabs` directly.
    tab_labels: dict[str, str] = Field(default_factory=dict)
    active_tab_id: str = "main"
    scroll: ScrollState = Field(default_factory=ScrollState)
    elements: list[ElementInfo] = Field(default_factory=list)
    # The compact section 4.6 rendering. This, not `elements`, is what reaches
    # the model.
    summary_text: str = ""
    # Visible text for verification and bounded planner context, including
    # static results and confirmations that have no interactive element.
    text: str = ""
    dom_hash: str = ""
    # Every frame's URL, main frame first. A link inside an iframe navigates
    # that frame and leaves the top-level URL alone, so a URL check that only
    # looked at `url` would call a working click a failure.
    frame_urls: list[str] = Field(default_factory=list)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def planner_summary(self) -> str:
        """Controls plus bounded readable results; callers must fence as untrusted."""
        if not self.text.strip():
            return self.summary_text
        return (
            f"{self.summary_text}\n\nVISIBLE PAGE TEXT (up to 4000 characters)\n{self.text[:4000]}"
        )

    @property
    def element_count(self) -> int:
        return len(self.elements)

    def element_at(self, index: int) -> ElementInfo | None:
        """Look up by the index the planner quoted.

        Backs the target-existence check in spec section 4.11: an action naming
        an index that is not here is rejected before it reaches the browser.
        """
        return next((item for item in self.elements if item.index == index), None)
