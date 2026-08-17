"""Keyed-state management. Per-key operations are open (callers only know
their own keys); the global listing/reset are admin-gated when ADMIN_TOKEN
is set — on a public deployment they would otherwise leak or destroy other
tenants' state."""

import ipaddress

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from config import (
    ADMIN_TOKEN, CHAOS_ENABLED, CHAOS_PORT, RATE_LIMIT_ENABLED, RATE_LIMIT_RPS,
    TLS_ENABLED, TLS_PORT,
)
from state import store

router = APIRouter()


class IpRule(BaseModel):
    cidr: str


def _require_admin(authorization: str | None) -> None:
    if not ADMIN_TOKEN:
        return
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=403, detail="admin token required")


@router.get("/state/{key}")
async def get_state(key: str) -> dict:
    count = await store.get(key)
    if count is None:
        raise HTTPException(status_code=404, detail="unknown key")
    return {"key": key, "count": count}


@router.delete("/state/{key}")
async def delete_state(key: str) -> dict:
    return {"deleted": await store.delete(key)}


@router.get("/state")
async def list_state(authorization: str | None = Header(default=None)) -> dict:
    _require_admin(authorization)
    return {"keys": await store.keys()}


@router.delete("/state")
async def clear_state(authorization: str | None = Header(default=None)) -> dict:
    _require_admin(authorization)
    return {"cleared": await store.clear()}


def _validate_cidr(cidr: str) -> str:
    try:
        return str(ipaddress.ip_network(cidr, strict=False))
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid IP or CIDR")


@router.get("/ratelimit")
async def ratelimit_info(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """Limiter config, persisted allow/deny lists, and active bans (admin-gated)."""
    _require_admin(authorization)
    limiter = request.app.state.limiter
    rules = await store.ip_rules()
    return {
        "enabled": RATE_LIMIT_ENABLED,
        "rps": RATE_LIMIT_RPS,
        "allow": rules["allow"],
        "deny": rules["deny"],
        "bans": limiter.bans(),  # ip -> seconds remaining
    }


@router.post("/ratelimit/{kind}")
async def add_ip_rule(kind: str, rule: IpRule, request: Request,
                      authorization: str | None = Header(default=None)) -> dict:
    """Adds an allow/deny entry (persisted; applied immediately on this worker)."""
    _require_admin(authorization)
    if kind not in ("allow", "deny"):
        raise HTTPException(status_code=404, detail="kind must be allow or deny")
    cidr = _validate_cidr(rule.cidr)
    added = await store.add_ip_rule(cidr, kind)
    await _apply_rules(request)
    return {"kind": kind, "cidr": cidr, "added": added}


@router.delete("/ratelimit/{kind}")
async def remove_ip_rule(kind: str, cidr: str, request: Request,
                         authorization: str | None = Header(default=None)) -> dict:
    """Removes an allow/deny entry by ?cidr= (persisted; applied immediately)."""
    _require_admin(authorization)
    if kind not in ("allow", "deny"):
        raise HTTPException(status_code=404, detail="kind must be allow or deny")
    cidr = _validate_cidr(cidr)
    removed = await store.remove_ip_rule(cidr, kind)
    await _apply_rules(request)
    return {"kind": kind, "cidr": cidr, "removed": removed}


@router.delete("/ratelimit/bans/{ip}")
async def unban(ip: str, request: Request, authorization: str | None = Header(default=None)) -> dict:
    """Lifts a ban for one IP (admin-gated)."""
    _require_admin(authorization)
    return {"unbanned": request.app.state.limiter.unban(ip)}


async def _apply_rules(request: Request) -> None:
    """Refreshes this worker's limiter snapshot from SQLite (others reload on cadence)."""
    rules = await store.ip_rules()
    request.app.state.limiter.set_rules(allow=rules["allow"], deny=rules["deny"])


@router.get("/chaos")
async def chaos_info() -> dict:
    """Documents the raw-TCP chaos listener (connection-error testing)."""
    return {
        "enabled": CHAOS_ENABLED,
        "port": CHAOS_PORT,
        "modes": {
            "/rst": "TCP reset after the request arrives (connection reset by peer)",
            "/empty": "connection closed before any response bytes",
            "/partial": "half a response head, then close",
        },
        "note": "connection-refused needs no endpoint — target any unused port",
        "tls": {"enabled": TLS_ENABLED, "port": TLS_PORT, "cert": "self-signed"},
    }
