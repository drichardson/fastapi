"""Fixed, uneven route graph with include depths one through three."""

import asyncio
import sys

import pytest
from fastapi import APIRouter, FastAPI, Response

if "--codspeed" not in sys.argv:
    pytest.skip(
        "Benchmark tests are skipped by default; run with --codspeed.",
        allow_module_level=True,
    )


def create_app(frozen: bool) -> FastAPI:
    app = FastAPI(openapi_url=None)
    routers = [APIRouter() for _ in range(80)]

    async def endpoint() -> Response:
        return Response(b"ok")

    # Ten routers per domain, 250 operations per domain; 2,000 overall.
    counts = [60, 45, 35, 30, 25, 20, 15, 10, 5, 5]
    for domain in range(8):
        offset = domain * 10
        for index, count in enumerate(counts):
            router = routers[offset + index]
            for operation in range(count):
                router.add_api_route(f"/resource-{operation}/{{item_id:int}}", endpoint)
        for index in reversed(range(1, 10)):
            parent = 0 if index < 4 else 1 + (index - 4) // 2
            routers[offset + parent].include_router(
                routers[offset + index], prefix=f"/group-{index}"
            )
        app.include_router(routers[offset], prefix=f"/domain-{domain}")
    if frozen:
        app.freeze_routes()
    return app


@pytest.mark.timeout(60)
@pytest.mark.parametrize("frozen", [False, True], ids=["dynamic", "frozen"])
@pytest.mark.parametrize(
    "path,status",
    [
        ("/domain-7/group-3/group-9/resource-4/42", 200),
        ("/missing", 404),
        ("/domain-7/group-3/group-9/resource-4/42/", 307),
    ],
)
def test_nested_routing(benchmark, frozen: bool, path: str, status: int) -> None:
    app = create_app(frozen)

    async def run():
        actual_status = None

        async def send(message):
            nonlocal actual_status
            if message["type"] == "http.response.start":
                actual_status = message["status"]

        async def receive():
            return {"type": "http.request", "body": b""}

        await app(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "root_path": "",
                "scheme": "http",
                "query_string": b"",
                "headers": [],
                "server": ("testserver", 80),
            },
            receive,
            send,
        )
        return actual_status

    loop = asyncio.new_event_loop()
    try:

        def run_request():
            return loop.run_until_complete(run())

        # Initialize middleware and lazy route contexts outside the measurement.
        assert run_request() == status
        assert benchmark(run_request) == status
    finally:
        loop.close()
