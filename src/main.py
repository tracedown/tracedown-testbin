"""tracedown-testbin — deterministic HTTP target for Lace/Tracedown testing.

Three listeners:
  HTTP_PORT   the FastAPI app (this module's `app`)
  TLS_PORT    the same app behind a self-signed certificate
  CHAOS_PORT  raw-TCP connection-failure modes (chaos.py)

Run: `uvicorn main:app --host 0.0.0.0 --port 20780`. The TLS and chaos
listeners are spawned from the app lifespan, so a single uvicorn entrypoint
brings up all three.
"""

import asyncio
import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import config
from chaos import start_chaos_server
from ratelimit import RateLimiter
from routes import admin, bodies, cookies, echo, redirects, status, timing
from state import store

log = logging.getLogger("testbin")

limiter = RateLimiter(
    rps=config.RATE_LIMIT_RPS,
    ban_seconds=config.RATE_LIMIT_BAN_SECONDS,
    max_tracked=config.RATE_LIMIT_MAX_TRACKED,
)


def _client_ip(request: Request) -> str:
    if config.TRUST_FORWARDED_FOR:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _seed_csv(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()]


async def _refresh_rules() -> None:
    rules = await store.ip_rules()
    limiter.set_rules(allow=rules["allow"], deny=rules["deny"])


async def _rule_reloader() -> None:
    while True:
        await asyncio.sleep(config.RATE_LIMIT_RELOAD_SECONDS)
        try:
            await _refresh_rules()
        except Exception as e:  # never let the reloader die on a transient error
            log.warning("rule reload failed: %s", e)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tasks: list[asyncio.Task] = []
    chaos_server = None

    # Seed the SQLite allow/deny lists from env (insert-if-absent), then load
    # the snapshot and start the periodic reloader.
    for cidr in _seed_csv(config.RATE_LIMIT_WHITELIST):
        await store.add_ip_rule(cidr, "allow")
    for cidr in _seed_csv(config.RATE_LIMIT_BLACKLIST):
        await store.add_ip_rule(cidr, "deny")
    await _refresh_rules()
    tasks.append(asyncio.create_task(_rule_reloader()))

    if config.CHAOS_ENABLED:
        chaos_server = await start_chaos_server(config.CHAOS_PORT)
        log.info("chaos listener on :%d", config.CHAOS_PORT)

    if config.TLS_ENABLED:
        try:
            import uvicorn

            from tlscert import generate_self_signed

            cert, key = generate_self_signed()
            tls_config = uvicorn.Config(
                app,
                host="0.0.0.0",
                port=config.TLS_PORT,
                ssl_certfile=cert,
                ssl_keyfile=key,
                lifespan="off",  # this lifespan already ran; don't recurse
                log_level="warning",
            )
            tasks.append(asyncio.create_task(uvicorn.Server(tls_config).serve()))
            log.info("tls listener on :%d (self-signed)", config.TLS_PORT)
        except Exception as e:  # missing openssl — degrade, don't die
            log.warning("tls listener disabled: %s", e)

    yield

    for task in tasks:
        task.cancel()
    if chaos_server is not None:
        chaos_server.close()
        await chaos_server.wait_closed()


app = FastAPI(
    title="tracedown-testbin",
    description="Deterministic httpbin-style target covering every Lace/Tracedown HTTP behavior.",
    lifespan=lifespan,
)

# Expose the limiter to the admin router (view/manage bans).
app.state.limiter = limiter


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    # /health is exempt so a PaaS healthcheck never trips the limiter.
    if config.RATE_LIMIT_ENABLED and request.url.path != "/health":
        allowed, reason, retry_after = limiter.check(_client_ip(request))
        if not allowed:
            headers = {"Retry-After": str(retry_after)} if retry_after else {}
            code = 403 if reason == "blacklisted" else 429
            return JSONResponse({"error": reason}, status_code=code, headers=headers)
    return await call_next(request)


app.include_router(echo.router)
app.include_router(status.router)
app.include_router(timing.router)
app.include_router(bodies.router)
app.include_router(cookies.router)
app.include_router(redirects.router)
app.include_router(admin.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.HTTP_PORT)
