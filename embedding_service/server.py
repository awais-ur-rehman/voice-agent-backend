import asyncio
import hmac
import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from embedding_service.settings import Settings
from embedding_service.supervisor import Supervisor
from knowledge.embedding import EmbeddingUnavailable, validate_texts
from knowledge.profile import PROFILE


def create_app(settings=None, supervisor=None):
    settings = settings or Settings.from_environment()
    worker = supervisor or Supervisor(settings.assets)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if settings.enabled:
                await asyncio.to_thread(worker.start)
        except EmbeddingUnavailable:
            pass
        yield
        worker.close()

    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/health/live")
    async def live():
        return {"live": True}

    @app.get("/health/ready")
    async def ready():
        available = settings.enabled and worker.ready
        return JSONResponse({"ready": available}, status_code=200 if available else 503)

    @app.post("/internal/embed")
    async def embed(request: Request):
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(),
            ("Bearer " + settings.secret).encode(),
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not settings.enabled:
            return JSONResponse({"error": "embedding_unavailable"}, status_code=503)
        deadline = time.monotonic() + 10
        try:
            async with asyncio.timeout(10):
                body = bytearray()
                async for part in request.stream():
                    if len(body) + len(part) > 131072:
                        return JSONResponse({"error": "invalid_input"}, status_code=413)
                    body.extend(part)
                data = json.loads(body)
                if not isinstance(data, dict) or set(data) != {"texts", "profile"}:
                    raise ValueError()
                if data["profile"] != PROFILE.fingerprint:
                    return JSONResponse({"error": "profile_mismatch"}, status_code=409)
                validate_texts(data["texts"])
                vectors = await asyncio.to_thread(worker.embed, data["texts"], deadline=deadline)
                return {"vectors": vectors, "profile": PROFILE.fingerprint}
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "invalid_input"}, status_code=422)
        except (EmbeddingUnavailable, TimeoutError):
            return JSONResponse({"error": "embedding_unavailable"}, status_code=503)

    return app
