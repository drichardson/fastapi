import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

import pytest
from fastapi import APIRouter, FastAPI, Path, Query
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from pydantic import BaseModel

if "--codspeed" not in sys.argv:
    pytest.skip(
        "Benchmark tests are skipped by default; run with --codspeed.",
        allow_module_level=True,
    )

ROUTE_COUNT = 100
LAST_ROUTE_INDEX = ROUTE_COUNT - 1


class EncodableItem(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    tags: list[str]
    values: dict[str, float]


ENCODABLE_ITEMS = [
    EncodableItem(
        id=UUID(int=i),
        name=f"item-{i}",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        tags=[f"tag-{i % 7}", f"tag-{i % 3}"],
        values={f"v{j}": j * 1.5 for j in range(10)},
    )
    for i in range(100)
]


def build_router() -> APIRouter:
    router = APIRouter()

    for index in range(ROUTE_COUNT):

        @router.get(f"/resources/{index}/items/{{item_id}}")
        def endpoint(item_id: int, index: int = index) -> dict[str, int]:
            return {"index": index, "item_id": item_id}

        endpoint.__name__ = f"endpoint_{index}"

    return router


def build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(build_router())

    @app.get("/validated/{item_id}")
    def validated(
        item_id: Annotated[int, Path(ge=0, le=1000)],
        name: Annotated[str, Query(min_length=1, max_length=50)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        tags: Annotated[list[str] | None, Query()] = None,
    ) -> dict[str, Any]:
        return {
            "item_id": item_id,
            "name": name,
            "limit": limit,
            "tags": tags or [],
        }

    return app


app = build_app()


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def test_route_resolution_with_many_routes(benchmark, client: TestClient) -> None:
    path = f"/resources/{LAST_ROUTE_INDEX}/items/42"
    assert client.get(path).status_code == 200

    def do_request() -> tuple[int, bytes]:
        response = client.get(path)
        return response.status_code, response.content

    status_code, body = benchmark(do_request)
    assert status_code == 200
    assert body == b'{"index":99,"item_id":42}'


def test_path_and_query_parameter_validation(benchmark, client: TestClient) -> None:
    path = "/validated/7"
    params = {"name": "benchmark", "limit": "25", "tags": ["a", "b", "c"]}
    assert client.get(path, params=params).status_code == 200

    def do_request() -> tuple[int, bytes]:
        response = client.get(path, params=params)
        return response.status_code, response.content

    status_code, body = benchmark(do_request)
    assert status_code == 200
    assert body == b'{"item_id":7,"name":"benchmark","limit":25,"tags":["a","b","c"]}'


def test_jsonable_encoder_nested_models(benchmark) -> None:
    encoded = benchmark(jsonable_encoder, ENCODABLE_ITEMS)
    assert len(encoded) == len(ENCODABLE_ITEMS)
    assert encoded[0]["name"] == "item-0"


@pytest.mark.timeout(120)
def test_router_creation_with_many_routes(benchmark) -> None:
    created_router = benchmark(build_router)
    assert len(created_router.routes) == ROUTE_COUNT
