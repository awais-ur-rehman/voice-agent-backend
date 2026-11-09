import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker
from valkey import Valkey
from valkey.exceptions import ValkeyError

from brain import draft_test_routes, voice_routes, voice_worker_routes
from brain import routes as brain_routes
from brain.provider import Nemotron
from brain.store import StateStore

from . import appointments, auth, catalog
from .config import Settings, load_settings
from .errors import AppError
from .http_security import RequestLimits, validation_fields

logger = logging.getLogger("scheduler.requests")


def create_app(settings: Settings | None = None, clock=None) -> FastAPI:
    settings = settings or load_settings()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        hide_parameters=True,
        pool_size=10,
        max_overflow=20,
    )

    # A separate bounded pool prevents lock-holding business requests from
    # exhausting the connections needed to finish their own admission checks.
    admission_engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        hide_parameters=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=1,
        connect_args={
            "connect_timeout": 2,
            "options": "-c statement_timeout=1000 -c lock_timeout=500",
        },
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
                        "SELECT CASE WHEN to_regclass('public.tenants') IS NULL THEN NULL ELSE EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tableowner=current_user) END"
                    )
                ).scalar()
                namespace_owner = conn.scalar(
                    text("""
                    WITH actor AS (SELECT oid FROM pg_roles WHERE rolname=current_user)
                    SELECT EXISTS (
                        SELECT 1 FROM pg_database
                        WHERE datname=current_database() AND datdba=(SELECT oid FROM actor)
                    ) OR EXISTS (
                        SELECT 1 FROM pg_namespace WHERE nspname='public' AND (
                            nspowner=(SELECT oid FROM actor)
                            OR (nspowner='pg_database_owner'::regrole AND EXISTS (
                                SELECT 1 FROM pg_database WHERE datname=current_database()
                                AND datdba=(SELECT oid FROM actor)
                            ))
                        )
                    )
                """)
                )
                if role.rolsuper or role.rolbypassrls or owner or owner is None or namespace_owner:
                    raise RuntimeError(
                        "Application database role must be non-owner, NOSUPERUSER and NOBYPASSRLS"
                    )
        except Exception:
            admission_engine.dispose()
            engine.dispose()
            raise
        cleanup_stop = asyncio.Event()
        cleanup_task = None
        if app.state.brain_store:
            from brain.voice_cleanup import cleanup_loop

            cleanup_task = asyncio.create_task(cleanup_loop(app, cleanup_stop))
        try:
            yield
        finally:
            cleanup_stop.set()
            if cleanup_task is not None:
                await cleanup_task
            if getattr(app.state, "brain_store", None):
                app.state.brain_store.client.close()
            app.state.brain_planner.close()
            try:
                # The provider drain has its own hard deadline; offload the wait
                # so callbacks and concurrent cleanup can progress on this loop.
                completed = await asyncio.to_thread(
                    app.state.brain_planner.drain, settings.shutdown_drain_seconds
                )
                if not completed:
                    logger.warning("provider_shutdown_drain_incomplete")
            except Exception:
                logger.warning("provider_shutdown_drain_failed")
            finally:
                admission_engine.dispose()
                engine.dispose()

    app = FastAPI(title="Voice Agent Scheduling API", version="0.2.0", lifespan=lifespan)
    from analytics.prometheus import Registry
    from analytics.prometheus import router as metrics_router

    app.state.analytics_metrics = Registry()
    app.include_router(metrics_router)
    app.state.settings, app.state.engine = settings, engine
    app.state.admission_factory = sessionmaker(admission_engine, expire_on_commit=False)
    app.state.brain_planner = Nemotron(settings)
    app.state.brain_store = (
        StateStore(
            Valkey.from_url(
                settings.valkey_url.get_secret_value(),
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        )
        if settings.valkey_url
        else None
    )
    app.state.session_factory = sessionmaker(
        engine, expire_on_commit=False, info={"analytics_metrics": app.state.analytics_metrics}
    )
    from knowledge.embedding import EmbeddingClient

    app.state.knowledge_embeddings = (
        EmbeddingClient(
            settings.knowledge_embedding_url, settings.knowledge_embedding_secret.get_secret_value()
        )
        if settings.knowledge_embedding_secret
        else None
    )
    app.state.clock = clock or (lambda: datetime.now(UTC))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        expose_headers=["Retry-After", "X-Request-ID"],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Content-SHA256"],
    )

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers())

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        fields = validation_fields(request, exc.errors())
        return JSONResponse(
            AppError("validation_error", 422, details={"fields": fields}).body(), status_code=422
        )

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        return JSONResponse(AppError("conflict", 409).body(), status_code=409)

    @app.exception_handler(OperationalError)
    async def database_error(request, exc):
        retryable = getattr(exc.orig, "sqlstate", None) in {"40P01", "40001"}
        error = AppError(
            "transaction_conflict" if retryable else "database_unavailable",
            409 if retryable else 503,
        )
        return JSONResponse(error.body(), status_code=error.status)

    app.add_middleware(RequestLimits, cors_origins=settings.cors_origins)

    @app.get("/health/live", tags=["Health"])
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["Health"])
    def ready():
        if settings.drain_mode:
            raise AppError("service_draining", 503, retry_after=3)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        if app.state.brain_store:
            try:
                app.state.brain_store.client.ping()
            except ValkeyError:
                raise AppError("cache_unavailable", 503) from None
        return {"status": "ready"}

    app.include_router(draft_test_routes.router)
    app.include_router(brain_routes.router)
    app.include_router(voice_worker_routes.router)
    app.include_router(voice_routes.router)
    app.include_router(appointments.router)
    app.include_router(auth.router)
    app.include_router(catalog.router)
    from analytics.routes import router as analytics_router
    from knowledge.routes import router as knowledge_router

    from .dashboard import agents, patients, profile, scheduling

    app.include_router(analytics_router)
    app.include_router(knowledge_router)
    app.include_router(agents.router)
    app.include_router(profile.router)
    app.include_router(patients.router)
    app.include_router(scheduling.router)
    return app
