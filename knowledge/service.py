"""Short database snapshots around external embedding work; no inference under locks."""

import time
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from sqlalchemy import exists, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from knowledge.embedding import EmbeddingUnavailable, validate_vectors
from knowledge.models import Generation, Source
from knowledge.privacy import derive_query_key, prior_event, query_fingerprint, record_event
from knowledge.profile import PROFILE
from knowledge.retrieval import KnowledgeScope, Reason, RetrievalResult, retrieve_chunks
from scheduler.db import tenant_context
from scheduler.errors import AppError


class Embeddings(Protocol):
    def embed(self, texts: list[str], *, deadline: float) -> list[list[float]]: ...
    def available(self) -> bool: ...


class KnowledgeService:
    def __init__(
        self, factory: Callable[[], Session], embeddings: Embeddings | None, *, audit_secret: str
    ):
        self.factory, self.embeddings = factory, embeddings
        self.audit_key = derive_query_key(audit_secret)

    def availability(self, scope: KnowledgeScope) -> Reason | None:
        with self.factory() as db, db.begin():
            tenant_context(db, scope.tenant_id)
            db.execute(text("SET LOCAL statement_timeout = '3s'"))
            db.execute(text("SET LOCAL lock_timeout = '1s'"))
            configured, ready = corpus_state(db, scope)
        if not configured:
            return "no_sources"
        if not ready:
            return "processing_pending"
        if self.embeddings is None or not self.embeddings.available():
            return "service_unavailable"
        return None

    def retrieve(
        self, scope: KnowledgeScope, query: str, language: str, *, query_id: UUID, deadline: float
    ) -> RetrievalResult:
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > 4000
            or language not in {"en", "ur"}
        ):
            raise AppError("invalid_knowledge_query", 422)
        started = time.monotonic()
        fingerprint = query_fingerprint(scope, query, language, key=self.audit_key)
        with self.factory() as db, db.begin():
            tenant_context(db, scope.tenant_id)
            db.execute(text("SET LOCAL statement_timeout = '3s'"))
            db.execute(text("SET LOCAL lock_timeout = '1s'"))
            prior_event(db, scope, query_id, fingerprint)
            configured, ready = corpus_state(db, scope)
        if not configured:
            result = RetrievalResult("no_sources")
        elif not ready:
            result = RetrievalResult("processing_pending")
        else:
            result = self._search(scope, query, deadline)
        with self.factory() as db, db.begin():
            tenant_context(db, scope.tenant_id)
            db.execute(text("SET LOCAL statement_timeout = '3s'"))
            db.execute(text("SET LOCAL lock_timeout = '1s'"))
            record_event(
                db,
                scope,
                query_id,
                fingerprint,
                language,
                result,
                int((time.monotonic() - started) * 1000),
            )
        return result

    def _search(self, scope: KnowledgeScope, query: str, deadline: float) -> RetrievalResult:
        try:
            if self.embeddings is None or time.monotonic() >= deadline:
                raise EmbeddingUnavailable("unavailable")
            vector = validate_vectors(self.embeddings.embed([query], deadline=deadline))[0]
            if time.monotonic() >= deadline:
                raise EmbeddingUnavailable("deadline")
            with self.factory() as db, db.begin():
                tenant_context(db, scope.tenant_id)
                timeout = max(1, min(5000, int((deadline - time.monotonic()) * 1000)))
                db.execute(
                    text("SELECT set_config('statement_timeout',:timeout,true)"),
                    {"timeout": str(timeout)},
                )
                chunks = retrieve_chunks(db, scope, vector, query)
            if time.monotonic() >= deadline:
                raise EmbeddingUnavailable("deadline")
            return RetrievalResult("ready" if chunks else "below_threshold", chunks)
        except (EmbeddingUnavailable, SQLAlchemyError):
            return RetrievalResult("service_unavailable")


def corpus_state(db, scope):
    configured = db.scalar(
        select(
            exists().where(
                Source.tenant_id == scope.tenant_id,
                Source.agent_id == scope.agent_id,
                Source.deleted_at.is_(None),
            )
        )
    )
    ready = db.scalar(
        select(
            exists().where(
                Source.tenant_id == scope.tenant_id,
                Source.agent_id == scope.agent_id,
                Source.deleted_at.is_(None),
                Source.active_generation_id == Generation.id,
                Generation.tenant_id == scope.tenant_id,
                Generation.agent_id == scope.agent_id,
                Generation.source_id == Source.id,
                Generation.sealed_at.is_not(None),
                Generation.profile == PROFILE.fingerprint,
            )
        )
    )
    return configured, ready
