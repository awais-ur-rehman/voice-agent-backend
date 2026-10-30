"""Prepared answer fencing and single final audit write under the command transaction."""

import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid5

from sqlalchemy import select

from brain.security import active_guard
from knowledge.grounding import GroundedAnswer
from knowledge.models import Generation, Source
from knowledge.privacy import record_event
from knowledge.profile import PROFILE
from knowledge.retrieval import RetrievalResult
from knowledge.scope import RuntimeKnowledgeScope, resolve_scope
from scheduler.db import tenant_context
from scheduler.errors import AppError
from scheduler.models import Agent, AgentSession, Tenant


@dataclass(frozen=True)
class PreparedKnowledge:
    scope: RuntimeKnowledgeScope
    command_id: str
    expected_revision: int
    language: str
    fingerprint: str
    result: RetrievalResult
    answer: GroundedAnswer | None
    started: float
    deadline: float

    @property
    def query_id(self):
        assert self.scope.log.session_id is not None
        return uuid5(self.scope.log.session_id, self.command_id)


def validate_prepared(db, actor, prepared, *, command_id, expected_revision, language, now=None):
    now = now or datetime.now(UTC)
    scope = prepared.scope
    if (
        scope.log.session_id != actor.id
        or scope.log.tenant_id != actor.tenant_id
        or scope.log.agent_id != actor.agent_id
        or scope.version_id != actor.version_id
        or prepared.command_id != command_id
        or prepared.expected_revision != expected_revision
        or prepared.language != language
    ):
        raise AppError("idempotency_conflict", 409)
    current = active_guard(db.get(AgentSession, actor.id), actor, now)
    if current.revision != expected_revision:
        raise AppError("idempotency_conflict", 409)
    if resolve_scope(db, actor, now, lock=True) != scope:
        raise AppError("unauthorized", 401)
    outcome = prepared.result
    try:
        with db.begin_nested():
            # CommandReceipt also has a direct Tenant FK. Acquire its log tenant
            # before Agent/Source so source CRUD cannot hold Tenant while waiting on
            # our Agent and then block the receipt insert. Do not lock the original
            # test tenant: parent allocation takes Tenant before DraftTest.
            tenant_context(db, scope.log.tenant_id)
            db.scalar(
                select(Tenant)
                .where(Tenant.id == scope.log.tenant_id)
                .with_for_update(read=True, key_share=True)
            )
            # QueryEvent's implicit Agent FK must never be acquired after Source.
            # Both original corpus and synthetic log parents precede any source lock.
            for tenant, agent in sorted(
                {
                    (scope.corpus.tenant_id, scope.corpus.agent_id),
                    (scope.log.tenant_id, scope.log.agent_id),
                }
            ):
                tenant_context(db, tenant)
                parent = db.scalar(
                    select(Agent)
                    .where(Agent.id == agent)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )
                if parent is None or not parent.enabled or parent.archived_at is not None:
                    raise AppError("unauthorized", 401)
            tenant_context(db, scope.corpus.tenant_id)
            for chunk in sorted(outcome.chunks, key=lambda c: (c.source_id, c.chunk_id)):
                source = db.scalar(
                    select(Source)
                    .where(Source.id == chunk.source_id, Source.agent_id == scope.corpus.agent_id)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )
                generation = db.get(Generation, chunk.generation_id)
                if (
                    source is None
                    or source.deleted_at is not None
                    or source.status == "deleted"
                    or source.revision != chunk.source_revision
                    or source.active_generation_id != chunk.generation_id
                    or generation is None
                    or generation.source_id != chunk.source_id
                    or generation.agent_id != scope.corpus.agent_id
                    or generation.source_revision != chunk.generation_source_revision
                    or generation.sealed_at is None
                    or generation.profile != PROFILE.fingerprint
                    or chunk.profile != PROFILE.fingerprint
                ):
                    outcome = RetrievalResult("stale")
                    break
            if time.monotonic() >= prepared.deadline:
                outcome = RetrievalResult("service_unavailable")
    finally:
        tenant_context(db, actor.tenant_id)
    final = replace(
        prepared, result=outcome, answer=prepared.answer if outcome.reason == "ready" else None
    )
    record_event(
        db,
        scope.log,
        final.query_id,
        final.fingerprint,
        language,
        outcome,
        int((time.monotonic() - prepared.started) * 1000),
    )
    return final
