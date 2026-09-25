"""B11: the compact summary format and its budget (spec section 4.6)."""

from app.agent.perception.dom_extractor import MASK, BoundingBox, ElementInfo
from app.agent.perception.summarizer import (
    CHARS_PER_TOKEN,
    SUMMARY_TOKEN_BUDGET,
    build_header,
    build_summary,
    format_element,
)


def element(index: int = 0, **overrides: object) -> ElementInfo:
    defaults: dict[str, object] = {
        "index": index,
        "tag": "button",
        "name": "Search flights",
        "bbox": BoundingBox(x=0, y=0, width=100, height=30),
        "in_viewport": True,
    }
    return ElementInfo.model_validate(defaults | overrides)


# ------------------------------------------------------------------ format


def test_button_line_matches_the_spec() -> None:
    line = format_element(element(14, tag="button", name="Search flights"))

    assert line == '[14] button "Search flights"'


def test_link_line_matches_the_spec() -> None:
    line = format_element(element(16, tag="a", name="Cheapest", href="/results?sort=price"))

    assert line == '[16] a "Cheapest" href=/results?sort=price'


def test_input_line_carries_type_name_and_value() -> None:
    line = format_element(element(15, tag="input", input_type="text", name="From", value="Delhi"))

    assert line == '[15] input[type=text] "From" value="Delhi"'


def test_placeholder_is_added_only_when_it_says_something_new() -> None:
    """Spec 4.6 shows placeholder=, the brief shows the name. Same rule."""
    both = format_element(
        element(
            15,
            tag="input",
            input_type="text",
            name="From",
            placeholder="Departure city",
            value="Delhi",
        )
    )
    same = format_element(
        element(15, tag="input", input_type="text", name="From", placeholder="From")
    )

    assert both == '[15] input[type=text] "From" placeholder="Departure city" value="Delhi"'
    assert same == '[15] input[type=text] "From"'


def test_disabled_and_covered_are_flagged() -> None:
    """Two states the planner would otherwise waste a step discovering."""
    assert "(disabled)" in format_element(element(disabled=True))
    assert "(covered)" in format_element(element(covered=True))


def test_a_masked_value_stays_masked() -> None:
    line = format_element(element(tag="input", input_type="password", value=MASK))

    assert MASK in line


def test_a_quote_in_the_text_cannot_break_the_line() -> None:
    line = format_element(element(name='Book "AI-812"'))

    assert line.count('"') == 2


# ------------------------------------------------------------------ header


def test_header_carries_everything_spec_asks_for() -> None:
    header = build_header(
        url="http://site.test/results",
        title="Results",
        tabs=["t1", "t2"],
        active_tab_id="t2",
        scroll_y=400,
        scroll_max_y=1200,
    )

    assert "URL: http://site.test/results" in header
    assert "Title: Results" in header
    assert "t2 (active)" in header
    assert "Scroll: 400 of 1200" in header


# ------------------------------------------------------------------ budget


def test_everything_fits_under_a_normal_budget() -> None:
    elements = [element(index) for index in range(30)]

    summary = build_summary(elements)

    assert summary.count("\n") == 29
    assert "more elements" not in summary


def test_the_budget_is_respected_on_a_huge_page() -> None:
    elements = [element(index, name=f"Result number {index}") for index in range(5000)]

    summary = build_summary(elements, token_budget=500)

    assert len(summary) <= 500 * CHARS_PER_TOKEN


def test_truncation_reports_how_many_were_dropped() -> None:
    elements = [element(index) for index in range(5000)]

    summary = build_summary(elements, token_budget=200)

    assert "more elements" in summary.splitlines()[-1]


def test_below_fold_omissions_say_scroll_to_see() -> None:
    elements = [element(index, in_viewport=False) for index in range(400)]

    summary = build_summary(elements, token_budget=100)

    assert summary.splitlines()[-1].endswith("below the fold (scroll to see)")


def test_in_viewport_omissions_do_not_say_scroll() -> None:
    """Telling a planner to scroll for something already on screen wastes a step."""
    elements = [element(index, in_viewport=True) for index in range(400)]

    summary = build_summary(elements, token_budget=100)

    assert "scroll to see" not in summary
    assert "budget reached" in summary.splitlines()[-1]


def test_lines_are_emitted_in_index_order() -> None:
    """Selected by priority, printed in reading order."""
    elements = [
        element(0, tag="a", name="A link"),
        element(1, tag="input", input_type="text", name="A field"),
        element(2, tag="button", name="A button"),
    ]

    indices = [line.split("]")[0] for line in build_summary(elements).splitlines()]

    assert indices == ["[0", "[1", "[2"]


# ------------------------------------------------------------------ priority


def test_covered_elements_survive_a_tight_budget() -> None:
    """An overlay is in the way, and the planner has to be told."""
    elements = [element(index, name=f"Filler {index}") for index in range(200)]
    elements.append(element(999, name="Behind the modal", covered=True))

    summary = build_summary(elements, token_budget=120)

    assert "Behind the modal" in summary


def test_form_fields_outrank_links() -> None:
    elements = [element(index, tag="a", name=f"Link {index}") for index in range(200)]
    elements.append(element(999, tag="input", input_type="text", name="Departure"))

    summary = build_summary(elements, token_budget=120)

    assert "Departure" in summary


def test_in_viewport_outranks_below_fold() -> None:
    below = [element(index, in_viewport=False, name=f"Below {index}") for index in range(200)]
    visible = element(999, name="On screen")

    summary = build_summary([*below, visible], token_budget=120)

    assert "On screen" in summary


def test_an_empty_page_produces_an_empty_summary() -> None:
    assert build_summary([]) == ""


def test_the_header_is_kept_with_the_lines() -> None:
    summary = build_summary([element(0)], "URL: http://x.test")

    assert summary.startswith("URL: http://x.test\n\n")


def test_default_budget_matches_the_spec() -> None:
    assert SUMMARY_TOKEN_BUDGET == 3000
