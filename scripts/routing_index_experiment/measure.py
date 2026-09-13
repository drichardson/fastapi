"""Reproduce: python -m scripts.routing_index_experiment.measure --output DIR."""

import argparse
import asyncio
import gc
import json
import platform
import random
import statistics
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

from fastapi import APIRouter, FastAPI, Response
from fastapi.routing import _IncludedRouter

from .packed import layout, load_native


def build(shape):
    app = FastAPI(openapi_url=None)
    routers = [APIRouter() for _ in range(80)]
    paths = []

    async def endpoint() -> Response:
        return Response(b"ok")

    counts = [60, 45, 35, 30, 25, 20, 15, 10, 5, 5]
    for domain in range(8):
        for index, count in enumerate(counts):
            router = routers[domain * 10 + index]
            for operation in range(count):
                if shape == "overlap":
                    path = f"/{{a{operation}}}/{{b{operation}}}"
                    concrete = "/x/y"
                elif shape == "fallback":
                    path = f"/file-{operation}/{{rest:path}}"
                    concrete = f"/file-{operation}/a/b"
                else:
                    path = f"/resource-{operation}/{{item_id:int}}"
                    concrete = f"/resource-{operation}/42"
                router.add_api_route(path, endpoint)
                parent = (
                    ""
                    if index == 0
                    else (
                        f"/group-{index}"
                        if index < 4
                        else f"/group-{1 + (index-4)//2}/group-{index}"
                    )
                )
                paths.append(f"/domain-{domain}{parent}{concrete}")
        for index in reversed(range(1, 10)):
            parent = 0 if index < 4 else 1 + (index - 4) // 2
            routers[domain * 10 + parent].include_router(
                routers[domain * 10 + index], prefix=f"/group-{index}"
            )
        app.include_router(routers[domain * 10], prefix=f"/domain-{domain}")
    return app, paths


def branches(routes):
    for route in routes:
        if isinstance(route, _IncludedRouter):
            yield route
            yield from branches(route.effective_candidates())


async def request(app, path):
    status = None

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

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
            "server": ("test", 80),
        },
        receive,
        send,
    )
    return status


async def measure(name, shape, library, repeats):
    app, paths = build(shape)
    with layout(name, library):
        start = time.perf_counter()
        app.freeze_routes()
        finalize = time.perf_counter() - start
    branch_list = list(branches(app.routes))
    # Compare identical candidates by identity and order against the base tree.
    from fastapi.routing import _FrozenRouteIndex

    probes = paths[::17] + ["/unknown", paths[-1] + "/", "/domain-7/é/東京"]
    for branch in branch_list:
        reference = _FrozenRouteIndex(branch.effective_candidates())
        for path in probes:
            assert [id(c) for c in reference.select(path)] == [
                id(c) for c in branch._frozen_index.select(path)
            ]
    for path in paths:
        assert await request(app, path) == 200
    assert await request(app, "/unknown") == 404
    rng = random.Random(45)
    traffic = rng.sample(paths, 160) + ["/unknown"] * 20 + [paths[-1] + "/"] * 20
    rng.shuffle(traffic)
    times = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        for path in traffic:
            await request(app, path)
        times.append((time.perf_counter_ns() - start) / len(traffic) / 1000)
    # Lookup-only boundary: 60-way root endpoint bucket. Excludes ASGI dispatch.
    index = branch_list[0]._frozen_index
    lookup_paths = paths[:60] + ["/domain-0/missing"] * 15 + [paths[0] + "/"] * 5
    lookup = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        for _ in range(30):
            for path in lookup_paths:
                tuple(index.select(path))
        lookup.append(
            (time.perf_counter_ns() - start) / (30 * len(lookup_paths)) / 1000
        )
    # Isolated index allocations, keeping route objects/contexts alive outside tracing.
    gc.collect()
    tracemalloc.start()
    with layout(name, library):
        from fastapi import routing

        indexes = [
            routing._FrozenRouteIndex(b.effective_candidates()) for b in branch_list
        ]
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    packed_bytes = sum(len(i.buffer) for i in indexes) if name != "object" else None
    return {
        "layout": name,
        "shape": shape,
        "finalize_ms": finalize * 1000,
        "request_us": times,
        "request_median_us": statistics.median(times),
        "lookup_us": lookup,
        "lookup_median_us": statistics.median(lookup),
        "retained_bytes": retained,
        "peak_bytes": peak,
        "packed_bytes": packed_bytes,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    extension = ".dylib" if sys.platform == "darwin" else ".so"
    library_path = output / ("packed" + extension)
    command = [
        "cc",
        "-O3",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-shared",
        "-fPIC",
        str(Path(__file__).with_name("native.c")),
        "-o",
        str(library_path),
    ]
    subprocess.run(command, check=True)
    library = load_native(library_path)
    jobs = [
        (shape, name)
        for shape in ("distinct", "overlap", "fallback")
        for name in ("object", "packed", "native")
    ]
    random.Random(731).shuffle(jobs)
    results = []
    for shape, name in jobs:
        result = asyncio.run(measure(name, shape, library, args.repeats))
        results.append(result)
        print(json.dumps(result), flush=True)
    (output / "results.json").write_text(
        json.dumps(
            {
                "environment": platform.platform(),
                "python": sys.version,
                "compiler": subprocess.check_output(["cc", "--version"], text=True),
                "compile_command": command,
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
