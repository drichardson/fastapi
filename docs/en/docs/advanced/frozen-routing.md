# Frozen routing

For applications with many nested routers and a fixed route table, call
`app.freeze_routes()` after registering all endpoints, routers and mounts, before
serving requests:

```python
from fastapi import APIRouter, FastAPI

app = FastAPI()
users = APIRouter()


@users.get("/{user_id}")
async def read_user(user_id: int):
    return {"user_id": user_id}


app.include_router(users, prefix="/users")
app.freeze_routes()
```

You can also finalize routing at the end of lifespan startup, before `yield`, if
startup registers endpoints. Finalization must not run concurrently with requests
or route registration. Calling it again is harmless.

## What changes

FastAPI eagerly builds the effective contexts and candidate indexes for included
routers. On each request, it filters out unrelated branches and endpoints before
running their matchers. Candidate order is preserved, including overlapping static
and parameterized routes, method matching, and slash redirects. Custom routes use
the existing matching hooks; selected branches are still matched again during
dispatch, preserving those hooks' invocation behavior.

Path parameters such as `/users/{user_id}` still vary normally. Dependency overrides
can still change. OpenAPI, reverse URL lookup, WebSockets, frontend fallback and
lifespan handling remain available.

## Fixed route definitions

Registration methods raise `FastAPIError` after finalization. This includes
registration on included routers, including instances shared with other apps.
Finish registering all shared routers before freezing any application using them.

Do not directly mutate route lists, paths, converters, prefixes, methods, metadata
or matching hooks afterwards. Such edits bypass registration checks and are
unsupported. Rebuild the application to change a frozen routing table.

Directly mounted FastAPI applications and APIRouters are finalized recursively.
Opaque ASGI applications and applications hidden behind custom middleware wrappers
are not inspected; finalize those applications explicitly before wrapping them.
Starlette-only routers retain their own routing behavior.

## Costs and scope

Finalization moves lazy dependency/context compilation to setup, and retains an
additional routing index. This trades startup time and memory for less request-time
work. It is opt-in: applications that need dynamic route registration can leave
routing unfrozen.

The optimization targets **included routers**. Direct application routes continue
to use their existing top-level ordered scan. Benefits depend on route topology,
path overlap and custom matchers; the number of routes alone does not determine
the speedup. Custom converters and catchalls remain conservative fallback candidates.
