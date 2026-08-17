"""Redirect chains — the redirects scope (match first/last/any), follow/max
config and the over-limit hard-fail path.

Public-hosting note: /redirect-to only accepts relative targets, so the
service can't be abused as an open redirector.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from config import MAX_REDIRECT_HOPS

router = APIRouter()


@router.get("/redirect/{n}")
async def redirect_chain(n: int, request: Request, absolute: bool = False, code: int = 302) -> RedirectResponse:
    """n-hop chain ending 200 at /anything; hop URLs are inspectable
    (/redirect/3 → /redirect/2 → /redirect/1 → /anything)."""
    if n < 1 or n > MAX_REDIRECT_HOPS:
        raise HTTPException(status_code=400, detail=f"n must be 1..{MAX_REDIRECT_HOPS}")
    target = "/anything" if n == 1 else f"/redirect/{n - 1}"
    if absolute:
        target = str(request.base_url).rstrip("/") + target + ("?absolute=true" if n > 1 else "")
    elif n > 1:
        target += "?absolute=false"
    if code not in (301, 302, 303, 307, 308):
        raise HTTPException(status_code=400, detail="code must be a 3xx redirect code")
    return RedirectResponse(url=target, status_code=code)


@router.get("/redirect-to")
async def redirect_to(url: str, code: int = 302) -> RedirectResponse:
    """Single hop to an arbitrary RELATIVE target (match first/last/any)."""
    if not url.startswith("/") or url.startswith("//"):
        raise HTTPException(status_code=400, detail="url must be a relative path")
    if code not in (301, 302, 303, 307, 308):
        raise HTTPException(status_code=400, detail="code must be a 3xx redirect code")
    return RedirectResponse(url=url, status_code=code)


@router.get("/redirect-loop")
async def redirect_loop() -> RedirectResponse:
    """Self-loop — exceeds any redirects.max and triggers the hard fail."""
    return RedirectResponse(url="/redirect-loop", status_code=302)
