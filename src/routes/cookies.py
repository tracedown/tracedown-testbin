"""Cookie set/echo/delete — observability for every cookieJar mode
(inherit, fresh, named, selective_clear): later calls in a chain reveal
exactly which cookies the executor's jar sent."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/cookies")
async def echo_cookies(request: Request) -> dict:
    return {"cookies": dict(request.cookies)}


@router.get("/cookies/set")
async def set_cookies(request: Request) -> JSONResponse:
    """Sets every query param as a session cookie: /cookies/set?a=1&b=2."""
    response = JSONResponse({"set": dict(request.query_params)})
    for name, value in request.query_params.items():
        response.set_cookie(name, value)
    return response


@router.get("/cookies/delete")
async def delete_cookies(request: Request) -> JSONResponse:
    """Expires the named cookies: /cookies/delete?a=&b=."""
    response = JSONResponse({"deleted": list(request.query_params.keys())})
    for name in request.query_params.keys():
        response.delete_cookie(name)
    return response
