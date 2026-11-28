"""The SkyFare demo site (spec section 12).

Driven over ASGI rather than through a browser: what matters here is that the
right HTML comes out for a given query, and a real Chromium adds seconds per
case to tell us nothing extra. The pages are exercised in a browser by the
perception and agent suites, which is where that is worth paying for.
"""

import re
import time
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from tests.skyfare.app import ORACLE_HEADER, ORACLE_TOKEN, app
from tests.skyfare.chaos import CHAOS_NAMES, INJECTION_TEXT, parse_chaos
from tests.skyfare.flights import CITIES, cheapest, generate

TRIP = {"from": "Delhi", "to": "Bengaluru", "date": "2026-10-20"}
ORACLE = {ORACLE_HEADER: ORACLE_TOKEN}


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://skyfare.test") as http:
        yield http


def flight_ids(html: str) -> list[str]:
    return re.findall(r'data-flight="([^"]+)"', html)


# ------------------------------------------------------------------ pages


async def test_the_search_page_renders(client: AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    body = response.text
    assert "Find your next flight" in body
    assert 'name="from"' in body and 'name="to"' in body
    assert 'name="date"' in body and 'name="passengers"' in body
    assert "Search flights" in body


async def test_every_city_is_offered_as_a_suggestion(client: AsyncClient) -> None:
    body = (await client.get("/")).text

    for city in CITIES:
        assert f'data-value="{city.name}"' in body
        assert city.code in body


async def test_the_results_page_renders_flights(client: AsyncClient) -> None:
    response = await client.get("/results", params=TRIP)

    assert response.status_code == 200
    body = response.text
    assert "Delhi to Bengaluru" in body
    assert flight_ids(body)
    assert "₹" in body
    assert "Select" in body


async def test_the_detail_page_renders(client: AsyncClient) -> None:
    listed = flight_ids((await client.get("/results", params=TRIP)).text)

    response = await client.get(f"/flight/{listed[0]}", params=TRIP)

    assert response.status_code == 200
    assert listed[0] in response.text
    assert "Book this flight" in response.text


async def test_the_booking_page_asks_for_passengers_and_offers_to_pay(
    client: AsyncClient,
) -> None:
    listed = flight_ids((await client.get("/results", params=TRIP)).text)

    response = await client.get(f"/book/{listed[0]}", params=TRIP)

    assert response.status_code == 200
    body = response.text
    assert "Passenger details" in body
    assert 'name="first_name"' in body and 'name="email"' in body
    # The button the agent must never press without a human saying so.
    assert "Pay now" in body


async def test_the_payment_page_is_clear_that_nothing_happened(
    client: AsyncClient,
) -> None:
    response = await client.get("/pay/6E-100", params=TRIP)

    assert response.status_code == 200
    assert "Nothing was charged" in response.text


async def test_an_unknown_flight_is_404(client: AsyncClient) -> None:
    response = await client.get("/flight/ZZ-999", params=TRIP)

    assert response.status_code == 404
    assert "could not find that flight" in response.text


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ------------------------------------------------------------ determinism


async def test_the_same_search_always_returns_the_same_flights(
    client: AsyncClient,
) -> None:
    first = flight_ids((await client.get("/results", params=TRIP)).text)
    second = flight_ids((await client.get("/results", params=TRIP)).text)

    assert first == second
    assert len(first) > 0


async def test_a_different_date_returns_different_flights(client: AsyncClient) -> None:
    first = flight_ids((await client.get("/results", params=TRIP)).text)
    other = flight_ids((await client.get("/results", params={**TRIP, "date": "2026-10-21"})).text)

    assert first != other


async def test_the_generator_matches_what_the_page_shows(client: AsyncClient) -> None:
    body = (await client.get("/results", params={**TRIP, "sort": "price"})).text
    generated = sorted(
        generate(**{"origin": "Delhi", "destination": "Bengaluru"}, on="2026-10-20"),
        key=lambda f: (f.price, f.duration_minutes, f.id),
    )

    # The page is a view of the generator, not a second source of truth.
    assert flight_ids(body)[0] == generated[0].id


async def test_the_result_count_is_between_fifteen_and_thirty() -> None:
    for city in CITIES[1:]:
        flights = generate("Delhi", city.name, "2026-10-20")
        assert 15 <= len(flights) <= 30, city.name


async def test_an_unknown_route_shows_no_flights(client: AsyncClient) -> None:
    response = await client.get("/results", params={**TRIP, "to": "Atlantis"})

    assert response.status_code == 200
    assert "No flights match this search" in response.text


# ----------------------------------------------------------- ground truth


async def test_the_oracle_is_invisible_without_its_header(client: AsyncClient) -> None:
    response = await client.get("/api/cheapest", params=TRIP)

    # 404 rather than 403: an agent that guesses the path learns nothing from
    # trying it, which is the point of keeping ground truth away from it.
    assert response.status_code == 404


async def test_the_oracle_rejects_a_wrong_token(client: AsyncClient) -> None:
    response = await client.get("/api/cheapest", params=TRIP, headers={ORACLE_HEADER: "guessed"})

    assert response.status_code == 404


async def test_the_oracle_returns_the_cheapest_flight(client: AsyncClient) -> None:
    response = await client.get("/api/cheapest", params=TRIP, headers=ORACLE)

    assert response.status_code == 200
    body = response.json()
    expected = cheapest(generate("Delhi", "Bengaluru", "2026-10-20"))
    assert expected is not None
    assert body["id"] == expected.id
    assert body["price"] == expected.price


async def test_the_oracle_agrees_with_the_page(client: AsyncClient) -> None:
    listed = flight_ids((await client.get("/results", params={**TRIP, "sort": "price"})).text)
    body = (await client.get("/api/cheapest", params=TRIP, headers=ORACLE)).json()

    # If these ever disagree, every evaluation score is measured against the
    # wrong answer.
    assert listed[0] == body["id"]


async def test_the_oracle_404s_on_an_unknown_route(client: AsyncClient) -> None:
    response = await client.get("/api/cheapest", params={**TRIP, "to": "Atlantis"}, headers=ORACLE)

    assert response.status_code == 404


# ------------------------------------------------------- sorting and filters


async def test_sorting_by_price_puts_the_cheapest_first(client: AsyncClient) -> None:
    body = (await client.get("/results", params={**TRIP, "sort": "price"})).text

    prices = [int(value) for value in re.findall(r'data-price="(\d+)"', body)]

    assert prices == sorted(prices)


async def test_sorting_by_departure_orders_by_time(client: AsyncClient) -> None:
    body = (await client.get("/results", params={**TRIP, "sort": "departure"})).text

    times = re.findall(r'<div class="t">(\d\d:\d\d)</div>', body)
    departures = times[::2]

    assert departures == sorted(departures)


async def test_the_non_stop_filter_removes_every_connection(client: AsyncClient) -> None:
    body = (await client.get("/results", params={**TRIP, "stops": "0"})).text

    shown = flight_ids(body)
    nonstop = {f.id for f in generate("Delhi", "Bengaluru", "2026-10-20") if f.stops == 0}

    assert shown
    assert set(shown) <= nonstop


async def test_the_airline_filter_keeps_only_that_airline(client: AsyncClient) -> None:
    every = generate("Delhi", "Bengaluru", "2026-10-20")
    airline = every[0].airline

    body = (await client.get("/results", params={**TRIP, "airline": airline})).text

    wanted = {f.id for f in every if f.airline == airline}
    assert set(flight_ids(body)) <= wanted


async def test_results_are_paged_and_load_more_shows_the_rest(
    client: AsyncClient,
) -> None:
    first = await client.get("/results", params=TRIP)
    shown = flight_ids(first.text)
    total = len(generate("Delhi", "Bengaluru", "2026-10-20"))

    assert len(shown) < total
    assert "more of" in first.text

    more = await client.get("/results", params={**TRIP, "shown": total})
    assert len(flight_ids(more.text)) == total


# ------------------------------------------------------------------ chaos


async def test_no_toggle_is_on_by_default(client: AsyncClient) -> None:
    body = (await client.get("/results", params=TRIP)).text

    # Matched on the markup, not the name: the script that wires these up is
    # on every page and mentions both by id.
    assert '<div class="modal" id="cookie-consent"' not in body
    assert '<div class="card popup" id="newsletter"' not in body
    assert INJECTION_TEXT not in body
    assert "<iframe" not in body
    assert 'target="_blank"' not in body
    assert 'data-slowtype="0"' in body


async def test_cookie_puts_a_modal_over_the_page(client: AsyncClient) -> None:
    body = (await client.get("/results", params={**TRIP, "chaos": "cookie"})).text

    assert '<div class="modal" id="cookie-consent"' in body
    assert "Accept all cookies" in body


async def test_popup_adds_a_newsletter_that_arrives_late(client: AsyncClient) -> None:
    body = (await client.get("/results", params={**TRIP, "chaos": "popup"})).text

    assert 'id="newsletter"' in body
    # Hidden at first: it appears after a couple of seconds, which is after a
    # fast agent has already read the page.
    assert '<div class="card popup" id="newsletter" hidden>' in body


async def test_inject_puts_instructions_on_the_page_visibly_and_hidden(
    client: AsyncClient,
) -> None:
    body = (await client.get("/results", params={**TRIP, "chaos": "inject"})).text

    assert body.count(INJECTION_TEXT) == 3
    assert "injection-banner" in body
    assert 'class="sr-only"' in body
    assert 'style="display:none"' in body


async def test_shuffle_renames_everything_on_every_load(client: AsyncClient) -> None:
    first = (await client.get("/", params={"chaos": "shuffle"})).text
    second = (await client.get("/", params={"chaos": "shuffle"})).text

    assert 'id="from"' not in first
    assert re.search(r'id="x[a-z0-9]{7}"', first)
    # Different on every load, so nothing can memorise a selector.
    assert set(re.findall(r'id="(x[a-z0-9]{7})"', first)) != set(
        re.findall(r'id="(x[a-z0-9]{7})"', second)
    )


async def test_layoutb_moves_the_filters_and_renames_the_button(
    client: AsyncClient,
) -> None:
    search = (await client.get("/", params={"chaos": "layoutb"})).text
    results = (await client.get("/results", params={**TRIP, "chaos": "layoutb"})).text

    # The button label, not the page title, which still says "Search flights".
    assert ">Find flights</button>" in search
    assert ">Search flights</button>" not in search
    assert "layout topfilters" in results


async def test_iframe_moves_the_results_into_a_frame(client: AsyncClient) -> None:
    outer = (await client.get("/results", params={**TRIP, "chaos": "iframe"})).text

    assert "<iframe" in outer
    assert not flight_ids(outer), "the flights must not be in the outer document"

    inner = (await client.get("/results-frame", params={**TRIP, "chaos": "iframe"})).text
    assert flight_ids(inner)


async def test_newtab_makes_the_details_open_in_a_new_tab(client: AsyncClient) -> None:
    body = (await client.get("/results", params={**TRIP, "chaos": "newtab"})).text

    assert 'target="_blank"' in body
    assert 'rel="noopener"' in body


async def test_slowtype_marks_the_document_for_the_debounced_script(
    client: AsyncClient,
) -> None:
    body = (await client.get("/", params={"chaos": "slowtype"})).text

    assert 'data-slowtype="1"' in body


async def test_delay_actually_delays(client: AsyncClient) -> None:
    started = time.monotonic()
    await client.get("/results", params={**TRIP, "chaos": "delay"})
    elapsed = time.monotonic() - started

    assert elapsed >= 0.5


async def test_toggles_survive_every_link_on_the_page(client: AsyncClient) -> None:
    body = (await client.get("/results", params={**TRIP, "chaos": "cookie,inject"})).text

    links = re.findall(r'href="(/(?:flight|results)[^"]*)"', body)
    assert links
    # A toggle that switches itself off on the first click tests nothing.
    assert all("chaos=cookie%2Cinject" in link or "chaos=cookie,inject" in link for link in links)


@pytest.mark.parametrize("name", CHAOS_NAMES)
def test_every_toggle_can_be_named_individually(name: str) -> None:
    assert parse_chaos(name).active == (name,)


def test_unknown_toggles_are_ignored_rather_than_rejected() -> None:
    # A demo site refusing to load because of a typo helps nobody.
    assert parse_chaos("cookie,nonsense").active == ("cookie",)


def test_all_turns_everything_on() -> None:
    assert parse_chaos("all").active == CHAOS_NAMES


def test_the_environment_is_the_fallback() -> None:
    assert parse_chaos(None, env="iframe").active == ("iframe",)
    # An explicit query wins, including an empty one.
    assert parse_chaos("", env="iframe").active == ()
