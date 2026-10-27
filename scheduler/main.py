import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from . import auth, catalog
from .config import Settings, load_settings
from .errors import AppError

logger = logging.getLogger("scheduler.requests")


def create_app(settings: Settings | None = None, clock=None) -> FastAPI:
    settings = settings or load_settings()
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        hide_parameters=True,
        pool_size=10,
        max_overflow=20,
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            with engine.connect() as conn:
                role = conn.execute(
                    text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
                ).one()
                owner = conn.execute(
                    text(
                        "SELECT tableowner=current_user FROM pg_tables WHERE schemaname='public' AND tablename='tenants'"
                    )
                ).scalar()
                if role.rolsuper or role.rolbypassrls or owner or owner is None:
                    raise RuntimeError(
                        "Application database role must be non-owner, NOSUPERUSER and NOBYPASSRLS"
                    )
        except Exception:
            engine.dispose()
            raise
        yield
        engine.dispose()

    app = FastAPI(title="Voice Agent Scheduling API", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.engine = settings, engine
    app.state.session_factory = sessionmaker(engine, expire_on_commit=False)
    app.state.clock = clock or (lambda: datetime.now(UTC))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        fields = [".".join(str(v) for v in err["loc"]) for err in exc.errors()]
        return JSONResponse(
            AppError("validation_error", 422, details={"fields": fields}).body(), status_code=422
        )

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        return JSONResponse(AppError("conflict", 409).body(), status_code=409)

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        started, request_id = time.perf_counter(), str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        route = request.scope.get("route")
        logger.info(
            json.dumps(
                {
                    "request_id": request_id,
                    "method": request.method,
                    "route": getattr(route, "path", "unmatched"),
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
        )
        return response

    @app.get("/health/live", tags=["Health"])
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["Health"])
    def ready():
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready"}

    app.include_router(auth.router)
    app.include_router(catalog.router)
    return app
