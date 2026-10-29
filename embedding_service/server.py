import asyncio
import hmac
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    admission = threading.BoundedSemaphore(5)
    executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="embedding-rpc")

    @asynccontextmanager
    async def lifespan(app):
        try:
            if settings.enabled:
                await asyncio.to_thread(worker.start)
        except EmbeddingUnavailable:
            pass
        try:
            yield
        finally:
            worker.close()
            await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)

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
        if not admission.acquire(blocking=False):
            return JSONResponse({"error": "embedding_unavailable"}, status_code=503)
        submitted = False
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
                operation = executor.submit(worker.embed, data["texts"], deadline=deadline)
                submitted = True
                # The concurrent future, not the HTTP task, owns release once submitted.
                # Shielding prevents disconnect/timeout from releasing running work's slot.
                operation.add_done_callback(lambda _: admission.release())
                future = asyncio.wrap_future(operation)
                future.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception()
                )
                vectors = await asyncio.shield(future)
                return {"vectors": vectors, "profile": PROFILE.fingerprint}
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "invalid_input"}, status_code=422)
        except (EmbeddingUnavailable, TimeoutError):
            return JSONResponse({"error": "embedding_unavailable"}, status_code=503)
        finally:
            if not submitted:
                admission.release()

    return app
