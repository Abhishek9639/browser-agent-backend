"""Deterministic flight generation.

The same origin, destination and date always produce the same flights, on any
machine and in any process. That is what makes the agent evaluation suite
possible: a run can be scored against a known cheapest fare, and a failure is
reproducible rather than "it was different that time".

Determinism comes from hashing the query rather than from ``hash()``, which is
salted per process and would give different results on every restart.
"""

import hashlib
import random
from dataclasses import dataclass
from datetime import date
from typing import Final

__all__ = [
    "AIRLINES",
    "CITIES",
    "City",
    "Flight",
    "cheapest",
    "city_by_code",
    "city_by_name",
    "generate",
]


@dataclass(frozen=True, slots=True)
class City:
    name: str
    code: str
    # Rough position, only ever used to make durations and fares feel related
    # to distance. Not meant to be accurate.
    lat: float
    lon: float


CITIES: Final[tuple[City, ...]] = (
    City("Delhi", "DEL", 28.56, 77.10),
    City("Mumbai", "BOM", 19.09, 72.87),
    City("Bengaluru", "BLR", 13.20, 77.71),
    City("Chennai", "MAA", 12.99, 80.17),
    City("Kolkata", "CCU", 22.65, 88.45),
    City("Hyderabad", "HYD", 17.24, 78.43),
    City("Pune", "PNQ", 18.58, 73.92),
    City("Ahmedabad", "AMD", 23.07, 72.63),
    City("Goa", "GOI", 15.38, 73.83),
    City("Jaipur", "JAI", 26.82, 75.80),
)


@dataclass(frozen=True, slots=True)
class Airline:
    name: str
    code: str


AIRLINES: Final[tuple[Airline, ...]] = (
    Airline("IndiGo", "6E"),
    Airline("Air India", "AI"),
    Airline("Vistara", "UK"),
    Airline("SpiceJet", "SG"),
    Airline("Akasa Air", "QP"),
    Airline("Air India Express", "IX"),
)

MIN_FLIGHTS: Final = 15
MAX_FLIGHTS: Final = 30

# The first departure and the last, in minutes from midnight.
FIRST_DEPARTURE: Final = 5 * 60
LAST_DEPARTURE: Final = 22 * 60


@dataclass(frozen=True, slots=True)
class Flight:
    """One row in the results list."""

    id: str
    airline: str
    airline_code: str
    flight_number: str
    origin: str
    destination: str
    depart_minutes: int
    duration_minutes: int
    stops: int
    price: int

    @property
    def arrive_minutes(self) -> int:
        return self.depart_minutes + self.duration_minutes

    @property
    def depart(self) -> str:
        return _clock(self.depart_minutes)

    @property
    def arrive(self) -> str:
        """Arrival time, with a day marker when it lands after midnight."""
        arrival = self.arrive_minutes
        suffix = f" +{arrival // (24 * 60)}" if arrival >= 24 * 60 else ""
        return _clock(arrival % (24 * 60)) + suffix

    @property
    def duration(self) -> str:
        hours, minutes = divmod(self.duration_minutes, 60)
        return f"{hours}h {minutes:02d}m"

    @property
    def stops_label(self) -> str:
        if self.stops == 0:
            return "Non-stop"
        return f"{self.stops} stop" if self.stops == 1 else f"{self.stops} stops"

    @property
    def price_label(self) -> str:
        return f"₹{self.price:,}"


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def city_by_name(value: str) -> City | None:
    wanted = value.strip().casefold()
    return next((city for city in CITIES if city.name.casefold() == wanted), None)


def city_by_code(value: str) -> City | None:
    wanted = value.strip().upper()
    return next((city for city in CITIES if city.code == wanted), None)


def resolve(value: str) -> City | None:
    """Accept either a city name or an airport code."""
    return city_by_name(value) or city_by_code(value)


def _seed(origin: str, destination: str, on: str) -> int:
    """A stable seed for one query.

    sha256 rather than ``hash()``: the built-in is salted per process, so the
    same search would give different flights after a restart and no evaluation
    run would ever be comparable to another.
    """
    key = f"{origin.strip().upper()}|{destination.strip().upper()}|{on.strip()}"
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")


def _separation(origin: City, destination: City) -> float:
    """A crude distance, in degrees. Only used to shape durations and fares."""
    return float(((origin.lat - destination.lat) ** 2 + (origin.lon - destination.lon) ** 2) ** 0.5)


def generate(origin: str, destination: str, on: str | date) -> list[Flight]:
    """Every flight for one query, cheapest last is not assumed anywhere.

    Returned in departure order, which is the order an airline site would show
    before any sort is applied.
    """
    departure_city = resolve(origin)
    arrival_city = resolve(destination)
    if departure_city is None or arrival_city is None or departure_city == arrival_city:
        return []

    on_text = on.isoformat() if isinstance(on, date) else str(on)
    rng = random.Random(_seed(departure_city.code, arrival_city.code, on_text))

    separation = _separation(departure_city, arrival_city)
    base_minutes = int(70 + separation * 9)
    base_price = int(2600 + separation * 190)

    flights: list[Flight] = []
    used: set[str] = set()

    for _ in range(rng.randint(MIN_FLIGHTS, MAX_FLIGHTS)):
        airline = rng.choice(AIRLINES)

        number = f"{rng.randint(100, 999)}"
        identifier = f"{airline.code}-{number}"
        if identifier in used:
            continue
        used.add(identifier)

        # Two stops are rare on a domestic route, one is common enough to make
        # the "non-stop only" filter worth having.
        stops = rng.choices((0, 1, 2), weights=(62, 32, 6))[0]
        duration = base_minutes + rng.randint(-15, 25) + stops * rng.randint(75, 210)

        price = base_price + rng.randint(-600, 4200) + (900 if stops == 0 else 0)
        price -= stops * rng.randint(150, 500)

        flights.append(
            Flight(
                id=identifier,
                airline=airline.name,
                airline_code=airline.code,
                flight_number=number,
                origin=departure_city.code,
                destination=arrival_city.code,
                depart_minutes=rng.randint(FIRST_DEPARTURE, LAST_DEPARTURE),
                duration_minutes=duration,
                stops=stops,
                # Rounded to ten rupees, the way a fare is actually quoted.
                price=max(1990, round(price, -1)),
            )
        )

    flights.sort(key=lambda flight: flight.depart_minutes)
    return flights


def cheapest(flights: list[Flight]) -> Flight | None:
    """The ground truth an evaluation run is scored against.

    Ties break on the shorter flight, then on the identifier, so the answer is
    a single flight rather than whichever one happened to sort first.
    """
    if not flights:
        return None
    return min(flights, key=lambda flight: (flight.price, flight.duration_minutes, flight.id))
