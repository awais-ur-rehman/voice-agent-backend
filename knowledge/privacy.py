"""Privacy-first query audit: retain no free-form question or source content."""

import hashlib
import hmac
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from knowledge.models import QueryEvent
from scheduler.errors import AppError


def derive_query_key(secret: str) -> bytes:
    if len(secret) < 32:
        raise ValueError("query_audit_secret_required")
    return hmac.new(secret.encode(), b"knowledge-query-key-v1", hashlib.sha256).digest()


def query_fingerprint(scope, query, language, *, key: bytes):
    payload = [
        str(scope.tenant_id),
        str(scope.agent_id),
        query,
        language,
        scope.purpose,
        str(scope.session_id or ""),
        str(scope.test_id or ""),
    ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return hmac.new(key, b"knowledge-query-v1\0" + canonical, hashlib.sha256).hexdigest()


def prior_event(db, scope, query_id, fingerprint):
    event = db.scalar(
        select(QueryEvent).where(
            QueryEvent.tenant_id == scope.tenant_id,
            QueryEvent.agent_id == scope.agent_id,
            QueryEvent.command_id == query_id,
        )
    )
    if event is not None and event.query_hash != fingerprint:
        raise AppError("query_id_conflict", 409)
    return event


def record_event(db, scope, query_id, fingerprint, language, result, duration_ms):
    # No Source locks are taken here. The implicit Agent FK lock cannot invert
    # CRUD's Tenant -> Agent -> Source order. Runtime/test final locks are Task5.
    # Complete suppression is intentional: regex/name lists cannot prove arbitrary
    # multilingual questions free of patient factors. Nullable column is the contract.
    db.execute(
        insert(QueryEvent)
        .values(
            id=uuid4(),
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id,
            command_id=query_id,
            session_id=scope.session_id if scope.purpose in {"runtime", "test"} else None,
            query_hash=fingerprint,
            language=language,
            purpose=scope.purpose,
            decision=result.reason,
            redacted_question=None,
            refs=[chunk.reference() for chunk in result.chunks],
            duration_ms=max(0, min(2147483647, duration_ms)),
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "agent_id", "command_id"])
    )
    prior_event(db, scope, query_id, fingerprint)
