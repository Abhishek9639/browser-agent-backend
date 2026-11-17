# This file is actually the security + validation layer of your browser automation agent.
"""The action allow-list as typed, validated models (spec section 4.4).

These are plain data: no Playwright import, nothing that touches a page. The
planner produces them, the risk policy inspects them, the driver executes them,
and the step row stores them, so they stay free of any one layer's dependencies.

Anything not in this module cannot be executed. The union below *is* the
allow-list, which means an invented action fails to parse rather than reaching
a real browser.
"""

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.browser.errors import ActionNotAllowedError
from app.domain.enums import ActionType

__all__ = [
    "MAX_WAIT_SECONDS",
    "Action",
    "ClickAction",
    "CloseTabAction",
    "DoneAction",
    "ExtractAction",
    "GoBackAction",
    "NavigateAction",
    "NewTabAction",
    "ScrollAction",
    "SelectAction",
    "SwitchTabAction",
    "TypeAction",
    "WaitAction",
    "parse_action",
]

# A plan that wants longer than this is stalling. The task budget is 300s
# (spec section 4.2); letting one step eat a twentieth of it is plenty.
MAX_WAIT_SECONDS: Final = 10
MAX_SCROLL_PIXELS: Final = 10_000
ALLOWED_URL_SCHEMES: Final = frozenset({"http", "https"})


class BaseAction(BaseModel):
    """Common configuration.

    ``extra="forbid"`` so a typo in a model's output is a validation error
    rather than a silently ignored field. ``frozen`` because an action is a
    record of what was decided: mutating it after the fact would desync the
    step row from what actually ran.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class NavigateAction(BaseAction):
    action: Literal[ActionType.NAVIGATE] = ActionType.NAVIGATE
    url: Annotated[str, Field(min_length=1, max_length=2048)]

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        """Only http and https.

        Blocks ``file://`` reading the host filesystem, ``javascript:``
        executing in page context, and ``data:`` smuggling a whole document
        past the domain allow-list.
        """
        url = value.strip()
        scheme, separator, _ = url.partition("://")
        if not separator or scheme.lower() not in ALLOWED_URL_SCHEMES:
            raise ValueError("URL must start with http:// or https://")
        return url


class ClickAction(BaseAction):
    action: Literal[ActionType.CLICK] = ActionType.CLICK
    # A Playwright selector. Resolution from the perception layer's element
    # index arrives with the DOM extractor.
    selector: Annotated[str, Field(min_length=1, max_length=1024)]


class TypeAction(BaseAction):
    action: Literal[ActionType.TYPE] = ActionType.TYPE
    selector: Annotated[str, Field(min_length=1, max_length=1024)]
    value: Annotated[str, Field(max_length=4096)]
    clear_first: bool = True
    press_enter: bool = False


class ScrollAction(BaseAction):
    action: Literal[ActionType.SCROLL] = ActionType.SCROLL
    direction: Literal["up", "down"] = "down"
    pixels: Annotated[int, Field(gt=0, le=MAX_SCROLL_PIXELS)] = 600


class SelectAction(BaseAction):
    action: Literal[ActionType.SELECT] = ActionType.SELECT
    selector: Annotated[str, Field(min_length=1, max_length=1024)]
    value: Annotated[str, Field(min_length=1, max_length=1024)]


class ExtractAction(BaseAction):
    action: Literal[ActionType.EXTRACT] = ActionType.EXTRACT
    # Omitted means the whole page; BeautifulSoup handles that path per
    # spec section 4.6.
    selector: Annotated[str, Field(max_length=1024)] | None = None


class WaitAction(BaseAction):
    action: Literal[ActionType.WAIT] = ActionType.WAIT
    seconds: Annotated[float, Field(gt=0, le=MAX_WAIT_SECONDS)] = 1.0


class GoBackAction(BaseAction):
    action: Literal[ActionType.GO_BACK] = ActionType.GO_BACK


class NewTabAction(BaseAction):
    action: Literal[ActionType.NEW_TAB] = ActionType.NEW_TAB
    url: Annotated[str, Field(max_length=2048)] | None = None

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return NavigateAction(url=value).url


class SwitchTabAction(BaseAction):
    action: Literal[ActionType.SWITCH_TAB] = ActionType.SWITCH_TAB
    tab_id: Annotated[str, Field(min_length=1, max_length=32)]


class CloseTabAction(BaseAction):
    action: Literal[ActionType.CLOSE_TAB] = ActionType.CLOSE_TAB
    # Omitted closes the active tab.
    tab_id: Annotated[str, Field(max_length=32)] | None = None


class DoneAction(BaseAction):
    action: Literal[ActionType.DONE] = ActionType.DONE
    final_answer: Annotated[str, Field(max_length=8192)] | None = None


Action = Annotated[
    NavigateAction
    | ClickAction
    | TypeAction
    | ScrollAction
    | SelectAction
    | ExtractAction
    | WaitAction
    | GoBackAction
    | NewTabAction
    | SwitchTabAction
    | CloseTabAction
    | DoneAction,
    Field(discriminator="action"),
]

# Kept in sync with the union by a test, so adding a model without listing it
# here (or the reverse) fails the suite rather than silently half-working.
ACTION_MODELS: Final[dict[ActionType, type[BaseAction]]] = {
    ActionType.NAVIGATE: NavigateAction,
    ActionType.CLICK: ClickAction,
    ActionType.TYPE: TypeAction,
    ActionType.SCROLL: ScrollAction,
    ActionType.SELECT: SelectAction,
    ActionType.EXTRACT: ExtractAction,
    ActionType.WAIT: WaitAction,
    ActionType.GO_BACK: GoBackAction,
    ActionType.NEW_TAB: NewTabAction,
    ActionType.SWITCH_TAB: SwitchTabAction,
    ActionType.CLOSE_TAB: CloseTabAction,
    ActionType.DONE: DoneAction,
}


def parse_action(payload: dict[str, object]) -> BaseAction:
    """Build a typed action from raw data, rejecting anything unrecognised.

    The allow-list check happens here, before any model is constructed and long
    before a browser is involved: an unknown ``action`` raises
    :class:`ActionNotAllowedError` rather than a validation error, because the
    two mean different things to the caller. A bad field is worth one repair
    retry; an invented action never is.
    """
    name = payload.get("action")
    if not isinstance(name, str):
        raise ActionNotAllowedError("An action must carry an 'action' name.")

    try:
        action_type = ActionType(name)
    except ValueError as exc:
        raise ActionNotAllowedError(f"{name!r} is not in the action allow-list.") from exc

    return ACTION_MODELS[action_type].model_validate(payload)
