import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import FastAPIError
from fastapi.routing import APIRoute, _EffectiveRouteContext, iter_route_contexts
from fastapi.testclient import TestClient
from starlette.convertors import Convertor
from starlette.responses import PlainTextResponse
from starlette.routing import Match, Mount, Route


def make_app():
    app = FastAPI()
    parent, child, leaf = APIRouter(), APIRouter(), APIRouter()

    async def endpoint(request: Request):
        return {"name": request.scope["route"].name, "params": request.path_params}

    for path, name, methods in [
        ("/overlap/{value}", "dynamic", ["GET"]),
        ("/overlap/fixed", "static", ["GET"]),
        ("/typed/{value:int}", "int", ["GET"]),
        ("/typed/{value}", "str", ["GET"]),
        ("/file-{value}.json", "partial_segment", ["GET"]),
        ("/files/{value:path}", "catchall", ["GET"]),
        ("/duplicate", "first", ["GET"]),
        ("/duplicate", "post", ["POST"]),
        ("/duplicate", "second", ["GET"]),
    ]:
        leaf.add_api_route(path, endpoint, methods=methods, name=name)
    child.include_router(leaf, prefix="/{tenant}")
    parent.include_router(child, prefix="/v1")
    app.include_router(parent, prefix="/api")
    app.include_router(parent, prefix="/alias")
    return app, leaf


def test_frozen_matches_unfrozen():
    app, _ = make_app()
    client = TestClient(app)
    cases = [
        (prefix + suffix, method)
        for prefix in ("/api/v1/acme", "/alias/v1/acme")
        for suffix in (
            "/overlap/fixed",
            "/typed/42",
            "/typed/abc",
            "/file-name.json",
            "/files/",
            "/files/a/b",
            "/duplicate",
            "/duplicate/",
            "/missing",
        )
        for method in ("GET", "POST", "DELETE", "HEAD")
    ]

    def responses():
        return [
            (r.status_code, r.content, dict(r.headers))
            for path, method in cases
            for r in [client.request(method, path, follow_redirects=False)]
        ]

    expected = responses()
    schema = app.openapi()
    app.freeze_routes()
    with (
        patch.object(
            APIRouter,
            "_get_routes_version",
            side_effect=AssertionError("version checked"),
        ),
        patch.object(
            _EffectiveRouteContext,
            "from_api_route",
            side_effect=AssertionError("lazy compilation"),
        ),
    ):
        assert responses() == expected
        assert len(list(iter_route_contexts(app.routes))) == 22
        assert (
            app.url_path_for("int", tenant="acme", value=42) == "/api/v1/acme/typed/42"
        )
    app.openapi_schema = None
    assert app.openapi() == schema


def test_freeze_routes_is_idempotent():
    app, _ = make_app()
    app.freeze_routes()
    contexts = list(iter_route_contexts(app.routes))

    app.freeze_routes()

    assert all(
        before._effective_route is after._effective_route
        for before, after in zip(contexts, iter_route_contexts(app.routes), strict=True)
    )
    assert TestClient(app).get("/api/v1/acme/typed/42").json() == {
        "name": "int",
        "params": {"tenant": "acme", "value": 42},
    }


@pytest.mark.parametrize(
    "operation",
    ["api", "websocket", "route", "ws_route", "include", "mount", "host", "frontend"],
)
def test_registration_rejected_before_mutation(operation, tmp_path):
    app, child = make_app()
    app.freeze_routes()

    async def endpoint(request):
        return PlainTextResponse("ok")

    for router in (app.router, child):
        before = list(router.routes)
        with pytest.raises(FastAPIError, match="Routes are frozen"):
            if operation == "api":
                router.add_api_route("/new", endpoint)
            elif operation == "websocket":
                router.add_api_websocket_route("/new", endpoint)
            elif operation == "route":
                router.add_route("/new", endpoint)
            elif operation == "ws_route":
                router.add_websocket_route("/new", endpoint)
            elif operation == "include":
                router.include_router(APIRouter(), prefix="/new")
            elif operation == "mount":
                router.mount("/new", FastAPI())
            elif operation == "host":
                router.host("example.com", FastAPI())
            else:
                router.frontend("/new", directory=tmp_path)
        assert router.routes == before


def test_custom_match_hooks_preserve_calls_and_scope():
    class CustomRoute(APIRoute):
        def matches(self, scope):
            scope["calls"] = scope.get("calls", 0) + 1
            if scope["path"] == "/surprise":
                return Match.FULL, {"endpoint": self.endpoint, "path_params": {}}
            return super().matches(scope)

    router = APIRouter(route_class=CustomRoute)

    @router.get("/declared")
    async def endpoint(request: Request):
        return request.scope["calls"]

    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    assert client.get("/surprise").json() == 2
    app.freeze_routes()
    assert client.get("/surprise").json() == 2


def test_mounted_app_and_proxy_root():
    child, _ = make_app()
    app = FastAPI(root_path="/proxy")
    app.mount("/internal", child)
    app.freeze_routes()
    assert child.router._routes_frozen
    client = TestClient(app)
    response = client.get("/proxy/internal/api/v1/acme/typed/42")
    assert response.status_code == 200
    assert response.json()["params"] == {"tenant": "acme", "value": 42}


def test_custom_slash_converter(monkeypatch):
    from starlette.convertors import CONVERTOR_TYPES

    class Slash(Convertor):
        regex = ".+"

        def convert(self, value):
            return value

        def to_string(self, value):
            return value

    monkeypatch.setitem(CONVERTOR_TYPES, "slash", Slash())
    router = APIRouter()

    @router.get("/files/{value:slash}/suffix")
    async def endpoint(value: str):
        return value

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.freeze_routes()
    assert TestClient(app).get("/api/files/a/b/suffix").json() == "a/b"


def test_frontend_websocket_mount_and_dependency_override(tmp_path):
    (tmp_path / "index.html").write_text("frontend")
    router = APIRouter()

    async def dependency():
        return "before"

    @router.get("/value")
    async def endpoint(value=Depends(dependency)):
        return value

    from fastapi import WebSocket

    @router.websocket("/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        await ws.send_text("websocket")
        await ws.close()

    async def mounted(request):
        return PlainTextResponse("mounted")

    router.routes.append(Mount("/mounted", routes=[Route("/value", mounted)]))
    router.frontend("/", directory=tmp_path)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.freeze_routes()
    client = TestClient(app)
    assert client.get("/api/value").json() == "before"

    async def replacement():
        return "after"

    app.dependency_overrides[dependency] = replacement
    assert client.get("/api/value").json() == "after"
    assert client.get("/api/mounted/value").text == "mounted"
    assert client.get("/api/", headers={"accept": "text/html"}).text == "frontend"
    with client.websocket_connect("/api/ws") as ws:
        assert ws.receive_text() == "websocket"


def test_finalize_during_lifespan():
    @asynccontextmanager
    async def lifespan(app):
        router = APIRouter()

        @router.get("/value")
        async def endpoint():
            return "ready"

        app.include_router(router, prefix="/api")
        app.freeze_routes()
        yield

    app = FastAPI(lifespan=lifespan)
    with TestClient(app) as client:
        assert client.get("/api/value").json() == "ready"


def test_frozen_concurrent_requests_have_isolated_parameters():
    router = APIRouter()

    @router.get("/value/{number:int}")
    async def endpoint(number: int):
        await asyncio.sleep(0)
        return number

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.freeze_routes()

    async def run():
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            responses = await asyncio.gather(
                *(client.get(f"/api/value/{i}") for i in range(50))
            )
        assert [response.json() for response in responses] == list(range(50))

    asyncio.run(run())
