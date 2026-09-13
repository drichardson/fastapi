import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import _FrozenRouteIndex, _IncludedRouter

from scripts.routing_index_experiment.packed import (
    NativeIndex,
    PackedIndex,
    load_native,
)


@pytest.mark.parametrize(
    "path",
    [
        "/api/users/me",
        "/api/users/42",
        "/api/users/no",
        "/api/files/a/b",
        "/api/files/",
        "/api/é/東京",
        "/api/",
        "/unknown",
    ],
)
def test_packed_candidates_preserve_order(path):
    router = APIRouter()

    async def endpoint():
        return None

    for template in (
        "/users/{name}",
        "/users/me",
        "/users/{id:int}",
        "/files/{rest:path}",
        "/é/{name}",
        "/",
    ):
        router.add_api_route(template, endpoint)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    branch = next(r for r in app.routes if isinstance(r, _IncludedRouter))
    candidates = branch.effective_candidates()
    reference = _FrozenRouteIndex(candidates)
    packed = PackedIndex(candidates)
    assert [id(c) for c in packed.select(path)] == [
        id(c) for c in reference.select(path)
    ]
    assert packed.words.readonly
    assert packed.labels.obj is packed.buffer


def test_native_and_packed_branching(tmp_path):
    compiler = shutil.which("cc")
    if compiler is None or sys.platform not in ("darwin", "linux"):
        pytest.skip("Native experiment requires a POSIX C compiler")
    source = Path(__file__).parents[1] / "scripts/routing_index_experiment/native.c"
    library = tmp_path / "packed.so"
    subprocess.run(
        [
            compiler,
            "-shared",
            "-fPIC",
            "-O2",
            "-fsanitize=undefined",
            "-fno-sanitize-recover=all",
            str(source),
            "-o",
            str(library),
        ],
        check=True,
    )
    router = APIRouter()

    async def endpoint():
        return None

    rng = random.Random(19)
    for i in range(100):
        pieces = [rng.choice(["a", "é", f"{{p{j}}}"]) for j in range(4)]
        router.add_api_route("/" + "/".join(pieces), endpoint, name=f"route_{i}")
    router.add_api_route("/{rest:path}", endpoint)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    branch = next(r for r in app.routes if isinstance(r, _IncludedRouter))
    candidates = branch.effective_candidates()
    reference = _FrozenRouteIndex(candidates)
    packed = PackedIndex(candidates)
    native = NativeIndex(candidates, load_native(library))
    for _ in range(300):
        path = "/api/" + "/".join(
            rng.choice(["a", "é", "東京", "", "z"]) for _ in range(rng.randrange(7))
        )
        expected = [id(c) for c in reference.select(path)]
        assert [id(c) for c in packed.select(path)] == expected
        assert [id(c) for c in native.select(path)] == expected
