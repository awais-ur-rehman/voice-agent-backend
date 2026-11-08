"""Short database snapshots around external embedding work; no inference under locks."""

import time
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import exists, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from knowledge.contracts import RetrievalQuery
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
        self,
        factory: Callable[[], Session],
        embeddings: Embeddings | None,
        *,
        audit_secret: str,
        answers=None,
    ):
        self.factory, self.embeddings = factory, embeddings
        self.answers = answers
        self.audit_key = derive_query_key(audit_secret)

    def availability(self, scope: KnowledgeScope) -> Reason | None:
        with self.factory() as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
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
        self,
        scope: KnowledgeScope,
        query: str,
        language: str,
        *,
        query_id: UUID,
        deadline: float,
        persist: bool = True,
    ) -> RetrievalResult:
        if not isinstance(query, str) or language not in {"en", "ur"}:
            raise AppError("invalid_knowledge_query", 422)
        try:
            # Share the established trimmed 1..1000 contract with public search.
            query = RetrievalQuery(query=query).query
        except ValidationError:
            raise AppError("invalid_knowledge_query", 422) from None
        started = time.monotonic()
        fingerprint = query_fingerprint(scope, query, language, key=self.audit_key)
        with self.factory() as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            tenant_context(db, scope.tenant_id)
            db.execute(text("SET LOCAL statement_timeout = '3s'"))
            db.execute(text("SET LOCAL lock_timeout = '1s'"))
            if persist:
                prior_event(db, scope, query_id, fingerprint)
            configured, ready = corpus_state(db, scope)
        if not configured:
            result = RetrievalResult("no_sources")
        elif not ready:
            result = RetrievalResult("processing_pending")
        else:
            result = self._search(scope, query, deadline)
        if persist:
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

    def prepare(self, scope, query, language, *, command_id, expected_revision, deadline):
        from uuid import uuid5

        from brain.provider import ProviderError
        from knowledge.grounding import GroundingRejected, validate_answer
        from knowledge.runtime import PreparedKnowledge

        started = time.monotonic()
        deadline = min(deadline, started + 15)
        # RetrievalQuery is checked before HMAC, RPC or audit. Runtime never logs
        # in the corpus scope; its single outcome belongs to the final command.
        try:
            query = RetrievalQuery(query=query).query
        except ValidationError:
            raise AppError("invalid_knowledge_query", 422) from None
        fingerprint = query_fingerprint(scope.log, query, language, key=self.audit_key)
        result = self.retrieve(
            scope.corpus,
            query,
            language,
            query_id=uuid5(scope.log.session_id, command_id),
            deadline=deadline,
            persist=False,
        )
        answer = None
        if result.reason == "ready":
            try:
                if self.answers is None or time.monotonic() >= deadline:
                    raise ProviderError("unavailable")
                content = self.answers.ground(query, language, result.chunks, deadline=deadline)
                answer = validate_answer(content, result.chunks, language)
                if time.monotonic() >= deadline:
                    raise ProviderError("deadline")
            except GroundingRejected:
                result = RetrievalResult("rejected")
            except ProviderError:
                result = RetrievalResult("service_unavailable")
        return PreparedKnowledge(
            scope,
            command_id,
            expected_revision,
            language,
            fingerprint,
            result,
            answer,
            started,
            deadline,
        )

    def _search(self, scope: KnowledgeScope, query: str, deadline: float) -> RetrievalResult:
        try:
            if self.embeddings is None or time.monotonic() >= deadline:
                raise EmbeddingUnavailable("unavailable")
            vector = validate_vectors(self.embeddings.embed([query], deadline=deadline))[0]
            if time.monotonic() >= deadline:
                raise EmbeddingUnavailable("deadline")
            with self.factory() as db, db.begin():
                db.execute(text("SET TRANSACTION READ ONLY"))
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
