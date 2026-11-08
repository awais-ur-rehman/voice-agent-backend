"""Bounded ASGI perimeter, safe diagnostics and schema-declared error locations."""

import json
import logging
import re
import time
from typing import get_args
from uuid import uuid4

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .errors import AppError

logger = logging.getLogger("scheduler.requests")
BODY_LIMIT = 64 * 1024
QUERY_LIMIT = 8 * 1024
HEADER_LIMIT = 32 * 1024
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_BINARY_UPLOAD = re.compile(rf"/dashboard/agents/{_UUID}/knowledge/sources/upload\Z")
_BINARY_REPLACE = re.compile(rf"/dashboard/agents/{_UUID}/knowledge/sources/{_UUID}/content\Z")
SECURITY_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


def validation_fields(request, errors) -> list[str]:
    """Accept declared paths only; never echo unknown JSON/dict keys or input."""
    route = request.scope.get("route")
    declared: set[tuple[str, ...]] = set()

    def model_paths(annotation, prefix, depth=0):
        if depth > 12:
            return
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            for name, field in annotation.model_fields.items():
                path = (*prefix, field.alias or name)
                declared.add(path)
                model_paths(field.annotation, path, depth + 1)
        else:
            for arg in get_args(annotation):
                model_paths(arg, prefix, depth + 1)

    dependant = getattr(route, "dependant", None)
    seen = set()

    def collect(dep):
        if dep is None or id(dep) in seen:
            return
        seen.add(id(dep))
        for kind in ("body", "query", "path", "header", "cookie"):
            for field in getattr(dep, kind + "_params", []):
                annotation = field.field_info.annotation
                if (
                    kind == "body"
                    and isinstance(annotation, type)
                    and issubclass(annotation, BaseModel)
                ):
                    model_paths(annotation, (kind,))
                else:
                    declared.add((kind, field.alias))
        for child in dep.dependencies:
            collect(child)

    collect(dependant)
    output = []
    for error in errors:
        loc = error.get("loc", ())
        if not loc or loc[0] not in {"body", "query", "path", "header", "cookie"}:
            continue
        safe = [str(loc[0])]
        structural: tuple[str, ...] = (str(loc[0]),)
        for part in loc[1:]:
            if type(part) is int and 0 <= part <= 100000:
                safe.append(str(part))
            elif isinstance(part, str) and (*structural, part) in declared:
                structural = (*structural, part)
                safe.append(part)
            else:
                break
        value = ".".join(safe)
        if value not in output:
            output.append(value)
    return output


class RequestLimits:
    """Outer user middleware; binary uploads retain their own streamed bounds."""

    def __init__(self, app, cors_origins=()):
        self.app = app
        self.cors_origins = cors_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started, request_id = time.perf_counter(), str(uuid4())
        status, response_started = 500, False

        async def secure_send(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started, status = True, message["status"]
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower().decode("latin1") not in SECURITY_HEADERS
                    and k.lower() != b"x-request-id"
                ]
                headers.extend((k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items())
                headers.append((b"x-request-id", request_id.encode()))
                # Rejections before routing never reach CORSMiddleware. Apply the
                # same explicit origin list so the dashboard can read the error.
                origins = [
                    v.decode("latin1")
                    for k, v in scope.get("headers", [])
                    if k.lower() == b"origin"
                ]
                if (
                    len(origins) == 1
                    and origins[0] in self.cors_origins
                    and not any(k.lower() == b"access-control-allow-origin" for k, v in headers)
                ):
                    headers.extend(
                        [
                            (b"access-control-allow-origin", origins[0].encode("latin1")),
                            (b"access-control-expose-headers", b"Retry-After, X-Request-ID"),
                            (b"vary", b"Origin"),
                        ]
                    )
                message = {**message, "headers": headers}
            await send(message)

        async def reject(error):
            await JSONResponse(error.body(), status_code=error.status, headers=error.headers())(
                scope, receive, secure_send
            )

        try:
            headers = scope.get("headers", [])
            if sum(len(k) + len(v) for k, v in headers) > HEADER_LIMIT:
                raise AppError("headers_too_large", 431)
            if len(scope.get("query_string", b"")) > QUERY_LIMIT:
                raise AppError("query_too_large", 414)
            lengths = [v for k, v in headers if k.lower() == b"content-length"]
            if len(lengths) > 1 or lengths and (not lengths[0].isdigit() or len(lengths[0]) > 10):
                raise AppError("invalid_content_length", 400)
            if lengths and any(k.lower() == b"transfer-encoding" for k, v in headers):
                raise AppError("invalid_content_length", 400)
            length = int(lengths[0]) if lengths else None
            path = scope.get("path", "")
            binary = (scope["method"] == "POST" and _BINARY_UPLOAD.fullmatch(path)) or (
                scope["method"] == "PUT" and _BINARY_REPLACE.fullmatch(path)
            )
            if binary:
                await self.app(scope, receive, secure_send)
            else:
                if length is not None and length > BODY_LIMIT:
                    raise AppError("request_too_large", 413)
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > BODY_LIMIT:
                        raise AppError("request_too_large", 413)
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
                if length is not None and len(body) != length:
                    raise AppError("invalid_content_length", 400)
                consumed = False

                async def buffered_receive():
                    nonlocal consumed
                    if not consumed:
                        consumed = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, buffered_receive, secure_send)
        except AppError as exc:
            if not response_started:
                await reject(exc)
        except Exception:
            # Do not log exc_info/type/message: upstream exception text can contain
            # credentials, signed URLs and patient input. Never rethrow to Uvicorn.
            if not response_started:
                await reject(AppError("internal_error", 500))
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            duration = time.perf_counter() - started
            app = scope.get("app")
            if app is not None:
                try:
                    app.state.analytics_metrics.observe(route, duration, status)
                except Exception:
                    pass
            method = scope.get("method")
            logger.info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "method": method
                        if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                        else "OTHER",
                        "route": route,
                        "status": status,
                        "duration_ms": round(duration * 1000, 2),
                    }
                )
            )
