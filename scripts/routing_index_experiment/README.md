# Packed routing index experiment

Stacked on frozen routing. This layer tests a data-oriented representation without
changing FastAPI's default implementation or adding a runtime native dependency.

## Design

Build using the existing tree's eligibility rules, then discard that temporary
object graph. The serving representation uses **one immutable byte buffer per
included-router index**, with node/edge fields in structure-of-arrays columns,
interned segment bytes, and integer candidate IDs. Links are offsets/indices rather
than Python object pointers. Node numbering is breadth-first. Route objects remain
in an external tuple: handlers, dependency graphs and regexes are not in the buffer.

Three variants use exactly the same route candidate semantics:

1. **object**: parent PR's dataclass/dict/list tree.
2. **packed**: Python traversal over memoryviews of the SoA buffer.
3. **native**: C traversal of the same buffer through ctypes. It scans UTF-8 path
   bytes without allocating per-segment Python strings and returns sorted IDs.

This explicitly separates compilation from the hot query operation. Literal edges
are contiguous ranges, wildcard edges are integer IDs, and labels are interned.
The native implementation uses linear edge search, per-call scratch allocation,
qsort and a ctypes boundary. Those are deliberate simple baselines, not claims of
optimal native performance. The object version uses C-implemented hash tables;
therefore this comparison changes both layout and edge-search implementation.

The buffer is native-endian, 32-bit, process-local data for a trusted compiler output.
The C routine is not an untrusted-input deserializer. The native library is built
only when explicitly running the experiment, outside the package build. No binaries
are committed. Mutation of the byte buffer is impossible through the public views.
The layout context manager is setup-only and must not run concurrently with setup
in other threads. Native lookup scratch/output buffers are owned by each call.

## Reproduction

Requires a POSIX C compiler (`cc`) on macOS/Linux:

```bash
uv run --extra all python -m scripts.routing_index_experiment.measure \
  --output scripts/routing_index_experiment/results
uv run --extra all pytest tests/test_packed_routing_experiment.py
```

The runner records compiler/version/flags, Python/platform, all seven timing batches,
setup time, traced retained/peak index allocations and exact buffer bytes. It runs
nine shuffled configurations serially in one process. Workstation background load
is uncontrolled; small timing differences are inconclusive. Tracemalloc excludes
native scratch allocations and excludes preexisting route contexts.

Workload: 80 routers, 2,000 operations, include depths 1–3, uneven counts 5–60.
Three shapes exercise distinguishing literals, overlapping parameterized paths and
all-catchall fallback routes. Request timings include the ASGI stack and lean response,
with 80% seeded distributed success paths, 10% misses and 10% slash variants. For
catchalls a slash variant can be a success, not a redirect; compare layouts within
each shape. Lookup-only timing fully consumes candidate output for a 60-operation
root bucket: 75% successful paths, 25% missing/slash probes. No profiler in timings.

Before timing, the runner compares **candidate identity and order** for every index
on the probe corpus and verifies successful dispatch of all 2,000 operations. Unit
tests also compare Unicode, empty/trailing segments, literal/wildcard overlap and
catchall results; native randomized differential tests run with UBSan enabled.
Overlapping endpoints intentionally share a handler; candidate identity comparisons
are the check for selection precedence, not merely response status.

## Initial findings

CPython 3.11 on macOS ARM64, seven batches per configuration. The confirmation run
shown below is retained in `results/results.json`.

| Shape | Layout | Lookup µs | Full request µs | Retained index bytes |
| --- | --- | ---: | ---: | ---: |
| Distinct | object | 0.74 | 25.81 | 1,825,504 |
| Distinct | packed Python | 6.58 | 46.65 | 275,592 |
| Distinct | packed native | 0.84 | 25.09 | 287,760 |
| Overlap | object | 2.32 | 26.79 | 269,176 |
| Overlap | packed Python | 3.82 | 36.18 | 122,672 |
| Overlap | packed native | 2.31 | 25.54 | 134,840 |
| Fallback | object | 1.75 | 97.33 | 68,520 |
| Fallback | packed Python | 2.15 | 101.30 | 105,152 |
| Fallback | packed native | 2.58 | 100.32 | 117,320 |

**Result: strong memory reduction for the distinct tree, no demonstrated native
request-time win, and a clear regression from interpreting packed data in Python.**
The distinct packed payload totals 183,048 bytes across all 80 indexes; Python/native
wrappers and the external candidate-reference tuples add overhead. When everything
is a fallback, the original index is already tiny and packing increases memory.

Do not infer CPU-cache residency from size. No cache-miss counters were collected,
and the full routing/handler graph is much larger. C vs Python measurements cannot
attribute improvements solely to locality. Setup includes temporary object trees
and native initialization. A future experiment could add compact hashed edges,
radix compression and a lower-overhead native API; this layer supplies a reproducible
baseline rather than enabling a new production backend prematurely.
