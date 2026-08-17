"""Request echo — the observability half of most Lace capabilities:
header/cookie/body config, URL interpolation, scoped-variable headers."""

import json

from fastapi import APIRouter, Request, Response

router = APIRouter()


async def _echo_payload(request: Request, include_body: bool = True) -> dict:
    raw = await request.body() if include_body else b""
    parsed_json = None
    form = None
    content_type = request.headers.get("content-type", "")
    if raw:
        if "application/json" in content_type:
            try:
                parsed_json = json.loads(raw)
            except ValueError:
                parsed_json = None
        elif "application/x-www-form-urlencoded" in content_type:
            from urllib.parse import parse_qsl

            form = dict(parse_qsl(raw.decode("utf-8", "replace")))
    return {
        "method": request.method,
        "path": request.url.path,
        "url": str(request.url),
        "args": dict(request.query_params),
        "headers": dict(request.headers),
        "cookies": dict(request.cookies),
        "origin": request.client.host if request.client else None,
        "content_type": content_type or None,
        "data": raw.decode("utf-8", "replace") if raw else "",
        "json": parsed_json,
        "form": form,
    }


async def anything(request: Request, rest: str = "") -> dict:
    return await _echo_payload(request)


# Register one route per (path, method) so each OpenAPI operation gets a
# distinct operationId — a single multi-method api_route shares one id across
# all its methods, which FastAPI flags as a duplicate.
for _path, _tag in (("/anything", "root"), ("/anything/{rest:path}", "path")):
    for _method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        router.add_api_route(
            _path, anything, methods=[_method],
            operation_id=f"anything_{_tag}_{_method.lower()}",
        )


@router.get("/get")
async def get_compat(request: Request) -> dict:
    """httpbin-compatible /get (seeds and org bootstrap default to it)."""
    payload = await _echo_payload(request, include_body=False)
    for key in ("data", "json", "form", "content_type"):
        payload.pop(key, None)
    return payload


@router.get("/health")
async def health() -> Response:
    return Response(content='{"status":"ok"}', media_type="application/json")
