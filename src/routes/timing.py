"""Latency control: total delay, TTFB-only delay, slow body streaming and
baseline-spike simulation — drives totalDelayMs/ttfb/transfer scopes,
timeout outcomes and laceBaseline spike detection."""

import asyncio

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse

from config import HANG_SECONDS, MAX_BYTES, MAX_DELAY_MS, MAX_DRIP_CHUNKS
from state import store

router = APIRouter()

FILLER = b"0123456789abcdef"


def _body(n: int) -> bytes:
    return (FILLER * (n // len(FILLER) + 1))[:n]


def _cap_ms(ms: int) -> float:
    if ms < 0:
        raise HTTPException(status_code=400, detail="delay must be >= 0")
    return min(ms, MAX_DELAY_MS) / 1000


@router.get("/delay/{ms}")
async def delay(ms: int, status: int = 200, bytes: int = 0) -> Response:
    """Sleeps before responding — total-latency and timeout testing."""
    await asyncio.sleep(_cap_ms(ms))
    content = _body(min(bytes, MAX_BYTES)) if bytes > 0 else b'{"delayed":true}'
    media = "application/octet-stream" if bytes > 0 else "application/json"
    return Response(content=content, status_code=status, media_type=media)


@router.get("/ttfb/{ms}")
async def ttfb(ms: int, bytes: int = 64) -> StreamingResponse:
    """Holds before the first byte, then sends the body at once —
    isolates the ttfb phase from transfer."""
    wait = _cap_ms(ms)
    size = min(max(bytes, 1), MAX_BYTES)

    async def stream():
        await asyncio.sleep(wait)
        yield _body(size)

    return StreamingResponse(stream(), media_type="application/octet-stream")


@router.get("/drip")
async def drip(duration_ms: int = 2000, bytes: int = 1024, chunks: int = 10) -> StreamingResponse:
    """Flushes headers immediately, then drips the body in slow chunks —
    makes transfer time dominate over ttfb."""
    total = min(max(bytes, 1), MAX_BYTES)
    parts = min(max(chunks, 1), MAX_DRIP_CHUNKS)
    pause = _cap_ms(duration_ms) / parts
    chunk = _body(total)

    async def stream():
        sent = 0
        for i in range(parts):
            end = total * (i + 1) // parts
            await asyncio.sleep(pause)
            yield chunk[sent:end]
            sent = end

    return StreamingResponse(stream(), media_type="application/octet-stream")


@router.get("/hang")
async def hang() -> Response:
    """Never responds within any sane probe timeout (capped server-side)."""
    await asyncio.sleep(HANG_SECONDS)
    return Response(status_code=204)


@router.get("/spike/{key}")
async def spike(
    key: str,
    after: int = 5,
    fast_ms: int = 10,
    slow_ms: int = 2000,
    grow_bytes: int = 0,
) -> Response:
    """Fast (and small) for the first `after` hits, then slow (and bigger) —
    feeds laceBaseline enough calm runs to build an average, then spikes
    responseTimeMs/ttfbMs (and sizeBytes when grow_bytes is set)."""
    n = await store.incr(f"spike:{key}")
    spiking = n > after
    await asyncio.sleep(_cap_ms(slow_ms if spiking else fast_ms))
    size = 64 + (min(grow_bytes, MAX_BYTES - 64) if spiking and grow_bytes > 0 else 0)
    return Response(content=_body(size), media_type="application/octet-stream")
