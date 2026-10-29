"""Short tenant -> agent -> source -> job mutations and replay receipts."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select, update

from scheduler.appointments import audit, request_digest, request_receipt
from scheduler.errors import AppError
from scheduler.models import Agent, Idempotency, Tenant, User
from scheduler.slots import get

from .contracts import (
    FAQSourceInput,
    FileSourceInput,
    SourceDetailOut,
    SourceOut,
    TextSourceInput,
    URLSourceInput,
)
from .models import Generation, Job, Source


def agent_row(db, agent_id, lock=False):
    row = get(db, Agent, agent_id, lock)
    if row.archived_at is not None:
        raise AppError("not_found", 404)
    return row


def source_row(db, agent_id, source_id, lock=False, deleted=False):
    query = select(Source).where(Source.agent_id == agent_id, Source.id == source_id)
    if not deleted:
        query = query.where(Source.deleted_at.is_(None))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None:
        raise AppError("not_found", 404)
    return row


def out(db, row):
    generation = db.get(Generation, row.active_generation_id) if row.active_generation_id else None
    return SourceOut(
        id=row.id,
        agent_id=row.agent_id,
        kind=row.kind,
        name=row.name,
        revision=row.revision,
        status=row.status,
        available=generation is not None and row.deleted_at is None,
        active_generation_id=row.active_generation_id,
        chunk_count=generation.chunk_count if generation else 0,
        error_code=row.error_code,
        error_at=row.error_at,
        updated_at=row.updated_at,
    )


def detail_out(db, row):
    """Project persisted current input explicitly; storage and future internal keys stay private."""
    current: TextSourceInput | FAQSourceInput | URLSourceInput | FileSourceInput
    if row.kind == "text":
        current = TextSourceInput(text=row.content["text"], format=row.content["format"])
    elif row.kind == "faq":
        current = FAQSourceInput(question=row.content["question"], answer=row.content["answer"])
    elif row.kind == "url":
        current = URLSourceInput(url=row.content["url"])
    elif row.kind == "file":
        current = FileSourceInput(format=row.content["format"])
    else:
        raise AppError("source_unavailable", 503)
    return SourceDetailOut(**out(db, row).model_dump(), input=current)


fingerprint = request_digest


def replay(db, key, operation, digest):
    receipt = db.scalar(select(Idempotency).where(Idempotency.key == key))
    if receipt:
        if receipt.operation != operation or receipt.request_hash != digest:
            raise AppError("idempotency_conflict", 409)
        return receipt.response
    return None


def cancel_jobs(db, row):
    db.execute(
        update(Job)
        .where(Job.source_id == row.id, Job.status.in_(["queued", "processing"]))
        .values(status="cancelled", lease_token=None, lease_until=None)
    )


def mutate(db, user, agent_id, source_id, action, body, key, blob_key=None, created_id=None):
    operation = f"knowledge:{agent_id}:{source_id or 'new'}:{action}"
    current = db.execute(select(User.active, User.role).where(User.id == user.id)).one_or_none()
    if current is None or not current.active or current.role not in {"owner", "admin"}:
        raise AppError("forbidden", 403)
    get(db, Tenant, user.tenant_id, True)
    agent_row(db, agent_id, True)
    receipt = request_receipt(db, user, key, operation, body)
    if receipt.response is not None:
        return receipt.response
    now = datetime.now(UTC)
    if source_id is None:
        if (
            db.scalar(
                select(func.count())
                .select_from(Source)
                .where(Source.agent_id == agent_id, Source.deleted_at.is_(None))
            )
            >= 64
        ):
            raise AppError("source_limit", 409)
        row = Source(
            id=created_id or uuid4(),
            tenant_id=user.tenant_id,
            agent_id=agent_id,
            kind=body.kind,
            name=body.name,
        )
        db.add(row)
    else:
        row = source_row(db, agent_id, source_id, True)
        if row.revision != body.revision:
            raise AppError("revision_conflict", 409)
        if action == "update" and row.kind != body.kind:
            raise AppError("source_kind_immutable", 409)
        cancel_jobs(db, row)
        row.revision += 1
    row.requested_revision = row.revision or 1
    row.error_code = row.error_at = None
    row.updated_at = now
    if action == "delete":
        row.deleted_at, row.status, row.active_generation_id = now, "deleted", None
    else:
        if action in ("create", "update"):
            row.name = body.name
            row.content = body.model_dump(
                mode="json", exclude={"revision", "checksum", "name", "kind"}
            )
            row.blob_key = blob_key
            row.checksum = getattr(body, "checksum", None)
        row.status = "queued"
        db.flush()
        db.add(
            Job(
                tenant_id=user.tenant_id,
                agent_id=agent_id,
                source_id=row.id,
                source_revision=row.revision,
            )
        )
    audit(db, user, f"knowledge.{action}", row.id)
    db.flush()
    receipt.response = out(db, row).model_dump(mode="json")
    db.flush()
    return receipt.response
