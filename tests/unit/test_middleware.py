"""B02: request id correlation."""

import re

from httpx import AsyncClient

from app.core.middleware import REQUEST_ID_HEADER, new_request_id

ID_PATTERN = re.compile(r"^req_[0-9a-f]{12}$")


def test_generated_ids_match_the_spec_shape() -> None:
    assert ID_PATTERN.match(new_request_id())


def test_generated_ids_are_unique() -> None:
    assert len({new_request_id() for _ in range(500)}) == 500


async def test_response_carries_a_generated_id(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert ID_PATTERN.match(response.headers[REQUEST_ID_HEADER])


async def test_incoming_id_is_honoured(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={REQUEST_ID_HEADER: "req_from_frontend"})

    assert response.headers[REQUEST_ID_HEADER] == "req_from_frontend"


async def test_blank_incoming_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={REQUEST_ID_HEADER: "   "})

    assert ID_PATTERN.match(response.headers[REQUEST_ID_HEADER])


async def test_ids_differ_between_requests(client: AsyncClient) -> None:
    first = await client.get("/api/v1/health")
    second = await client.get("/api/v1/health")

    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]
