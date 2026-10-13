"""Finding a blocking popup and the control that closes it."""

from app.agent.perception.dom_extractor import BoundingBox, ElementInfo
from app.agent.perception.overlays import icon_closers, is_blocked, safe_closers
from app.agent.perception.state import PageState


def el(
    index: int,
    *,
    tag: str = "a",
    name: str = "",
    x: int = 0,
    y: int = 0,
    w: int = 100,
    h: int = 30,
    covered: bool = False,
) -> ElementInfo:
    return ElementInfo(
        index=index,
        tag=tag,
        name=name,
        bbox=BoundingBox(x=x, y=y, width=w, height=h),
        in_viewport=True,
        covered=covered,
    )


def covered_page() -> list[ElementInfo]:
    """Ten elements under a popup."""
    return [el(100 + i, name=f"Link {i}", covered=True) for i in range(10)]


def state(*elements: ElementInfo) -> PageState:
    return PageState(url="https://example.test/", elements=list(elements))


def popup(close: ElementInfo) -> list[ElementInfo]:
    return [
        close,
        el(7, name="Terms and Conditions", x=667, y=466),
        el(8, name="Sign up with Google", x=472, y=492, w=336),
        el(9, name="Sign up with Email", x=580, y=575, w=120),
    ]


def test_a_page_with_few_covered_elements_is_not_blocked() -> None:
    assert not is_blocked(state(*covered_page()[:3]))


def test_a_popup_covering_the_page_is_blocked() -> None:
    assert is_blocked(state(*covered_page()))


def test_an_unnamed_icon_at_the_top_right_of_a_popup_is_found() -> None:
    icon = el(6, tag="i", x=793, y=153, w=20, h=20)

    found = icon_closers(state(*covered_page(), *popup(icon)))

    assert [e.index for e in found] == [6]


def test_an_icon_far_from_any_popup_controls_is_ignored() -> None:
    stray = el(6, tag="i", x=5, y=900, w=20, h=20)

    assert icon_closers(state(*covered_page(), *popup(stray))) == []


def test_a_labelled_close_beats_an_unlabelled_icon() -> None:
    icon = el(6, tag="i", x=793, y=153, w=20, h=20)
    labelled = el(5, tag="button", name="No thanks", x=500, y=600)

    closers = safe_closers(state(*covered_page(), *popup(icon), labelled))

    assert [e.index for e in closers] == [5, 6]


def test_accept_is_never_offered_as_a_way_to_dismiss() -> None:
    accept = el(5, tag="button", name="Accept all", x=500, y=600)

    assert safe_closers(state(*covered_page(), accept)) == []


def test_nothing_is_offered_when_the_page_is_not_blocked() -> None:
    close = el(5, tag="button", name="Close", x=500, y=600)

    assert safe_closers(state(close)) == []
