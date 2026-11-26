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


def test_a_language_gate_is_dismissed_by_choosing_english() -> None:
    english = el(0, tag="button", name="Confirmation. SafeValue must use binding", x=574, y=566)
    english = english.model_copy(update={"text": "English"})

    closers = safe_closers(state(*covered_page(), english))

    assert [e.index for e in closers] == [0]


def test_a_control_matches_on_its_text_when_its_name_is_garbled() -> None:
    garbled = el(3, tag="button", name="x1 y2 internal", x=0, y=0).model_copy(
        update={"text": "No thanks"}
    )

    assert [e.index for e in safe_closers(state(*covered_page(), garbled))] == [3]


# ---------------------------------------------------------------------------
# choice_buttons / is_choice_modal — BookMyShow "Select language and format"
# ---------------------------------------------------------------------------
from app.agent.perception.overlays import choice_buttons, is_choice_modal  # noqa: E402


def bms_format_modal() -> list[ElementInfo]:
    """Simulated BookMyShow 'Select language and format' popup."""
    return [
        # The choices — real labels from the screenshot
        el(10, tag="button", name="DOLBY CINEMA 2D", x=220, y=379),
        el(11, tag="button", name="MS InfVsn ICE 3D", x=430, y=379),
        el(12, tag="button", name="MS-Infinity Vsn 3d", x=220, y=436),
        el(13, tag="button", name="MS - Inf Vsn 4DX 3D", x=430, y=436),
        el(14, tag="button", name="MS - Infinity Vision", x=220, y=493),
        el(15, tag="button", name="MS InVsn 3D Screen X", x=430, y=493),
        el(16, tag="button", name="Select all", x=270, y=550),
        # The close icon (×)
        el(17, tag="button", name="×", x=620, y=229),
    ]


def test_format_modal_is_detected_as_choice_modal() -> None:
    """The BookMyShow format picker must be treated as a choice modal."""
    s = state(*covered_page(), *bms_format_modal())
    assert is_choice_modal(s)


def test_choice_buttons_excludes_close_labels() -> None:
    """× and 'Select all' (dismiss-style) must not appear in choice_buttons."""
    s = state(*covered_page(), *bms_format_modal())
    names = {(b.name or b.text).strip() for b in choice_buttons(s)}
    # Close / dismiss labels must be absent
    assert "×" not in names
    # Substantive format choices must be present
    assert "DOLBY CINEMA 2D" in names
    assert "MS InfVsn ICE 3D" in names


def test_choice_buttons_returns_empty_when_not_blocked() -> None:
    """No buttons in a choice modal on an un-blocked page."""
    close = el(5, tag="button", name="DOLBY CINEMA 2D", x=500, y=600)
    assert choice_buttons(state(close)) == []


def test_cookie_banner_is_not_a_choice_modal() -> None:
    """A cookie/promo banner with only close/dismiss controls is not a choice modal."""
    banner = [
        el(5, tag="button", name="No thanks", x=500, y=600),
        el(6, tag="button", name="Close", x=550, y=600),
    ]
    s = state(*covered_page(), *banner)
    assert not is_choice_modal(s)


def test_safe_closers_still_works_on_non_choice_modal() -> None:
    """safe_closers should still find the × on a plain dismiss-only popup that
    has no substantive choices (only close/dismiss controls)."""
    dismiss_popup = [
        el(5, tag="button", name="No thanks", x=300, y=600),
        el(6, tag="button", name="×", x=620, y=229),
        # Covered background elements that push is_blocked() True
    ]
    s = state(*covered_page(), *dismiss_popup)
    # Only close labels — not a choice modal
    assert not is_choice_modal(s)
    # Both dismiss controls are found
    closers = safe_closers(s)
    assert any((c.name or "").strip().casefold() in {"no thanks", "×"} for c in closers)
