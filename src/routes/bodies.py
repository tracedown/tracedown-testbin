"""Response bodies: exact sizes, byte-exact raw strings, schema-testing JSON
variants, changing values for .store() writeback, counters and a two-call
auth chain."""

import uuid as uuidlib

from fastapi import APIRouter, Header, HTTPException, Request, Response

from config import MAX_BYTES
from state import store

router = APIRouter()

FILLER = b"0123456789abcdef"

# Reference payloads for body: schema($s) testing. The README documents the
# JSON schema that `exact` satisfies strictly; `extra` adds fields at two
# levels (loose passes, strict fails); `wrong` breaks a type; `nested` goes
# deep for "at every level" strictness checks.
JSON_VARIANTS: dict[str, dict] = {
    "exact": {
        "service": {"name": "testbin", "healthy": True, "checks": 3},
        "tags": ["alpha", "beta"],
    },
    "extra": {
        "service": {"name": "testbin", "healthy": True, "checks": 3, "region": "eu"},
        "tags": ["alpha", "beta"],
        "debug": {"trace": True},
    },
    "wrong": {
        "service": {"name": "testbin", "healthy": True, "checks": "three"},
        "tags": ["alpha", "beta"],
    },
    "nested": {
        "service": {
            "name": "testbin",
            "healthy": True,
            "checks": 3,
            "meta": {"owner": {"team": "core", "oncall": False}},
        },
        "tags": ["alpha", "beta"],
    },
}


@router.get("/bytes/{n}")
async def bytes_endpoint(n: int) -> Response:
    """Exactly n deterministic bytes — size:eq, bodySize thresholds,
    bodyTooLarge capture, body-storage checksums."""
    if n < 0 or n > MAX_BYTES:
        raise HTTPException(status_code=400, detail=f"n must be 0..{MAX_BYTES}")
    content = (FILLER * (n // len(FILLER) + 1))[:n]
    return Response(content=content, media_type="application/octet-stream")


@router.get("/raw")
async def raw(body: str = "OK", content_type: str = "text/plain") -> Response:
    """Byte-exact raw body — literal/variable body matching."""
    return Response(content=body, media_type=content_type)


@router.get("/json/{variant}")
async def json_variant(variant: str) -> dict:
    payload = JSON_VARIANTS.get(variant)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"variants: {sorted(JSON_VARIANTS)}")
    return payload


@router.get("/uuid")
async def uuid_endpoint() -> dict:
    """Fresh value each call — proves .store() writeback persists per run."""
    return {"uuid": str(uuidlib.uuid4())}


@router.get("/counter/{key}")
async def counter(key: str) -> dict:
    """Incrementing count — metric-variable presets and prev-based logic."""
    n = await store.incr(f"counter:{key}")
    return {"key": key, "count": n}


@router.get("/token")
async def token() -> dict:
    """First half of the login chain: returns a bearer token to .store()."""
    return {"token": uuidlib.uuid4().hex}


@router.post("/login")
async def login(request: Request) -> dict:
    """Credential-checked login: JSON or form {username, password} -> token.

    Any credentials pass EXCEPT password "invalid" or missing fields (401) —
    deterministic success and failure paths for bearer-chain probes.
    """
    username = password = None
    content_type = request.headers.get("content-type", "")
    raw = await request.body()
    if raw and "application/json" in content_type:
        import json

        try:
            data = json.loads(raw)
            username, password = data.get("username"), data.get("password")
        except ValueError:
            pass
    elif raw and "application/x-www-form-urlencoded" in content_type:
        from urllib.parse import parse_qsl

        data = dict(parse_qsl(raw.decode("utf-8", "replace")))
        username, password = data.get("username"), data.get("password")
    if not username or not password or password == "invalid":
        raise HTTPException(status_code=401, detail="bad credentials")
    return {"token": uuidlib.uuid4().hex, "user": username}


@router.get("/protected")
async def protected(authorization: str | None = Header(default=None)) -> dict:
    """Second half: any `Authorization: Bearer <x>` passes, else 401."""
    if not authorization or not authorization.startswith("Bearer ") or len(authorization) <= 7:
        raise HTTPException(status_code=401, detail="missing bearer token")
    return {"authorized": True, "token": authorization[7:]}
