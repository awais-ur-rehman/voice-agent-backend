"""Server-only corpus authority, separate from the tenant receiving query audit writes."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text

from knowledge.retrieval import KnowledgeScope
from scheduler.db import tenant_context
from scheduler.errors import AppError
from scheduler.models import Agent, AgentVersion, DraftTest, User


@dataclass(frozen=True)
class RuntimeKnowledgeScope:
    corpus: KnowledgeScope
    log: KnowledgeScope
    version_id: UUID

    def __post_init__(self):
        if self.log.session_id is None or self.corpus.session_id != self.log.session_id:
            raise ValueError("runtime_knowledge_session_required")


def resolve_scope(db, actor, now, *, lock=False):
    tenant_context(db, actor.tenant_id)
    try:
        with db.begin_nested():
            version = db.get(AgentVersion, actor.version_id)
            if version is None or version.agent_id != actor.agent_id:
                raise AppError("unauthorized", 401)
            if version.purpose != "test":
                scope = KnowledgeScope(actor.tenant_id, actor.agent_id, "runtime", actor.id)
                return RuntimeKnowledgeScope(scope, scope, actor.version_id)
            record = (
                db.execute(
                    text("SELECT * FROM draft_test_knowledge_scope(:session,:agent,:version,:now)"),
                    {
                        "session": actor.id,
                        "agent": actor.agent_id,
                        "version": actor.version_id,
                        "now": now,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if record is None:
                raise AppError("test_expired", 410)
            tenant_context(db, record["tenant_id"])
            query = select(DraftTest).where(DraftTest.id == record["test_id"])
            if lock:
                query = query.with_for_update(read=True)
            mapping = db.scalar(query.execution_options(populate_existing=True))
            creator_query = select(User).where(User.id == record["creator_id"])
            if lock:
                creator_query = creator_query.with_for_update(read=True)
            creator = db.scalar(creator_query.execution_options(populate_existing=True))
            parent = db.get(Agent, record["agent_id"])
            if (
                mapping is None
                or mapping.status != "active"
                or mapping.expires_at <= now
                or mapping.child_tenant_id != actor.tenant_id
                or mapping.child_agent_id != actor.agent_id
                or actor.id not in (mapping.session_id, mapping.voice_session_id)
                or creator is None
                or not creator.active
                or creator.role not in {"owner", "admin"}
                or parent is None
                or not parent.enabled
                or parent.archived_at is not None
            ):
                raise AppError("test_expired", 410)
            return RuntimeKnowledgeScope(
                KnowledgeScope(mapping.tenant_id, mapping.agent_id, "test", actor.id, mapping.id),
                KnowledgeScope(actor.tenant_id, actor.agent_id, "test", actor.id, mapping.id),
                actor.version_id,
            )
    finally:
        tenant_context(db, actor.tenant_id)
