"""Status-code control: fixed, request-driven, random, and stateful
(flap / sequence / fail-then-succeed) — the transition generators behind
notification, silentOnRepeat/prev and retry testing."""

import asyncio
import random

from fastapi import APIRouter, HTTPException, Request, Response

from state import store

router = APIRouter()


def _parse_codes(raw: str) -> list[int]:
    try:
        codes = [int(c) for c in raw.split(",") if c.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="codes must be integers")
    if not codes or any(c < 100 or c > 599 for c in codes):
        raise HTTPException(status_code=400, detail="codes must be 100..599")
    return codes


def _status_response(code: int, body: str | None = None) -> Response:
    return Response(
        content=body if body is not None else f'{{"status":{code}}}',
        status_code=code,
        media_type="text/plain" if body is not None else "application/json",
    )


async def status(code: int, body: str | None = None, location: str | None = None) -> Response:
    if code < 100 or code > 599:
        raise HTTPException(status_code=400, detail="code must be 100..599")
    response = _status_response(code, body)
    if location is not None and location.startswith("/"):
        response.headers["Location"] = location
    return response


# One route per method → distinct operationIds (see echo.py).
for _method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
    router.add_api_route(
        "/status/{code}", status, methods=[_method],
        operation_id=f"status_{_method.lower()}",
    )


def _parse_success(value) -> bool | None:
    """Accepts true/false, 0/1, and their string forms (case-insensitive)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
    return None


async def flip(request: Request) -> Response:
    """200 or 500 driven by a `success` param — JSON body, form, or query;
    accepts true/false, 0/1 and their string forms.

    The body carries `next` (the negation), so a probe can flip itself every
    run by writing it back: `.store({ "$n": this.body.next })` and posting
    `success: $n` — n = 1 - n without arithmetic on injected (string) vars.
    """
    value = None
    if request.method == "POST":
        content_type = request.headers.get("content-type", "")
        raw = await request.body()
        if raw and "application/json" in content_type:
            import json

            try:
                value = json.loads(raw).get("success")
            except ValueError:
                pass
        elif raw and "application/x-www-form-urlencoded" in content_type:
            from urllib.parse import parse_qsl

            value = dict(parse_qsl(raw.decode("utf-8", "replace"))).get("success")
    if value is None:
        value = request.query_params.get("success")

    success = True if value is None else _parse_success(value)
    if success is None:
        raise HTTPException(status_code=400, detail="success must be true/false or 0/1")
    return Response(
        content=f'{{"success":{str(success).lower()},"next":{str(not success).lower()}}}',
        status_code=200 if success else 500,
        media_type="application/json",
    )


for _method in ("GET", "POST"):
    router.add_api_route(
        "/flip", flip, methods=[_method], operation_id=f"flip_{_method.lower()}",
    )


@router.get("/random")
async def random_status(p: float = 0.5, codes: str = "200,500") -> Response:
    """Returns codes[0] with probability p, else a random one of the rest."""
    parsed = _parse_codes(codes)
    if len(parsed) == 1:
        return _status_response(parsed[0])
    code = parsed[0] if random.random() < p else random.choice(parsed[1:])
    return _status_response(code)


@router.get("/flap/{key}")
async def flap(key: str, codes: str = "200,500") -> Response:
    """Cycles through `codes` one step per request — state transitions on demand."""
    parsed = _parse_codes(codes)
    n = await store.incr(f"flap:{key}")
    return _status_response(parsed[(n - 1) % len(parsed)])


@router.get("/sequence/{key}")
async def sequence(key: str, codes: str = "200,200,500") -> Response:
    """Plays `codes` once, then holds the last — precise transition scripting."""
    parsed = _parse_codes(codes)
    n = await store.incr(f"seq:{key}")
    return _status_response(parsed[min(n - 1, len(parsed) - 1)])


@router.get("/fail-then-succeed/{key}")
async def fail_then_succeed(
    key: str, fails: int = 2, code: int = 500, slow_ms: int | None = None
) -> Response:
    """First `fails` hits fail (or respond after slow_ms when set), then 200.

    With slow_ms this models retry-on-timeout: attempts 1..N are slow, the
    next is instant — a `timeout: {action: "retry"}` call recovers.
    """
    from config import MAX_DELAY_MS

    n = await store.incr(f"fts:{key}")
    if n <= fails:
        if slow_ms is not None:
            await asyncio.sleep(min(slow_ms, MAX_DELAY_MS) / 1000)
            return _status_response(200)
        return _status_response(code)
    return _status_response(200)
