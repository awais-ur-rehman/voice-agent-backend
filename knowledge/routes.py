"""Authenticated raw uploads and manager-controlled source lifecycle."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated
from uuid import UUID, uuid4

from anyio import CancelScope
from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from scheduler.appointments import IdempotencyKey
from scheduler.db import Principal, manager, session
from scheduler.errors import AppError

from . import sources
from .contracts import (
    Capabilities,
    Digest,
    FileFormat,
    Name,
    Preview,
    Revision,
    SearchQuery,
    SearchResult,
    SourceCreate,
    SourceDetailOut,
    SourceOut,
    SourceUpdate,
    Upload,
)
from .models import Chunk, Source
from .storage import BlobStore

router = APIRouter(prefix="/dashboard/agents/{agent_id}/knowledge", tags=["Knowledge"])


def enabled(request: Request):
    if not request.app.state.settings.knowledge_enabled:
        raise AppError("knowledge_disabled", 503)


@router.get("/capabilities", response_model=Capabilities, operation_id="knowledge_capabilities")
def capabilities(
    agent_id: UUID,
    request: Request,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    sources.agent_row(db, agent_id)
    active = request.app.state.settings.knowledge_enabled
    from knowledge.readiness import processing_status

    settings = request.app.state.settings
    secret = settings.knowledge_blob_secret
    reason = processing_status(
        settings.knowledge_blob_root, secret.get_secret_value() if secret else "", enabled=active
    )
    from knowledge.retrieval import KnowledgeScope
    from knowledge.service import KnowledgeService

    db.commit()  # Health RPC observes existing state with no SQL transaction held.
    query_reason = (
        KnowledgeService(
            request.app.state.session_factory,
            request.app.state.knowledge_embeddings,
            audit_secret=request.app.state.settings.jwt_secret.get_secret_value(),
        ).availability(KnowledgeScope(user.tenant_id, agent_id, "preview"))
        if active
        else "disabled"
    )
    return Capabilities(
        enabled=active,
        processing_available=reason is None,
        unavailable_reason=reason,
        query_available=query_reason is None,
        query_unavailable_reason=query_reason,
    )


@router.get("/sources", response_model=list[SourceOut], operation_id="knowledge_sources")
def listing(agent_id: UUID, user: Principal = Depends(manager), db: Session = Depends(session)):
    sources.agent_row(db, agent_id)
    return [
        sources.out(db, row)
        for row in db.scalars(
            select(Source)
            .where(Source.agent_id == agent_id, Source.deleted_at.is_(None))
            .order_by(Source.created_at, Source.id)
        )
    ]


@router.post(
    "/sources",
    response_model=SourceOut,
    status_code=201,
    operation_id="knowledge_create_source",
    dependencies=[Depends(enabled)],
)
def create(
    agent_id: UUID,
    body: SourceCreate,
    key: IdempotencyKey,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    return sources.mutate(db, user, agent_id, None, "create", body, key)


BINARY = {
    "requestBody": {
        "required": True,
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}


def preflight(db, agent_id, source_id, body, key):
    sources.agent_row(db, agent_id)
    action = "update" if source_id else "create"
    operation = f"knowledge:{agent_id}:{source_id or 'new'}:{action}"
    existing = sources.replay(db, key, operation, sources.fingerprint(body))
    if existing is not None:
        return existing
    if source_id:
        row = sources.source_row(db, agent_id, source_id)
        if row.kind != "file":
            raise AppError("source_kind_immutable", 409)
        if row.revision != body.revision:
            raise AppError("revision_conflict", 409)
    return None


async def upload_sql[Result](operation: Callable[..., Result], *args: object) -> Result:
    """Join the SQL worker before cancellation can unwind its request Session."""
    worker = asyncio.create_task(run_in_threadpool(operation, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # AnyIO scope cancellation and repeated task.cancel() must both leave
        # the dependency alive until the worker releases the connection.
        with CancelScope(shield=True):
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        if not worker.cancelled():
            worker.exception()
        raise


async def receive(request, db, user, agent_id, source_id, body, key):
    existing = await upload_sql(preflight, db, agent_id, source_id, body, key)
    if existing is not None:
        return existing
    # Authentication/ownership/replay preflight precedes stream. No row lock is held during IO.
    identity = source_id or uuid4()
    settings = request.app.state.settings
    secret = settings.knowledge_blob_secret
    if secret is None:
        raise AppError("storage_unavailable", 503)
    pending = await BlobStore(
        settings.knowledge_blob_root, secret=secret.get_secret_value()
    ).receive(
        request.stream(),
        user.tenant_id,
        identity,
        (body.revision or 0) + 1,
        body.checksum,
    )

    # Release only after the actual outer SQL transaction ends, including rollback/cancellation.
    def release(session, transaction):
        if transaction.parent is None:
            pending.release()

    event.listen(db, "after_transaction_end", release)
    # Server-held fencing also covers a lost connection with unknown commit outcome.
    await upload_sql(
        db.execute,
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 7241))"),
        {"key": pending.key},
    )
    # A complete orphan is deliberately retained if commit outcome is unknown; cleanup uses grace.
    return await upload_sql(
        sources.mutate,
        db,
        user,
        agent_id,
        source_id,
        "update" if source_id else "create",
        body,
        key,
        pending.key,
        identity,
    )


@router.post(
    "/sources/upload",
    response_model=SourceOut,
    status_code=201,
    operation_id="knowledge_upload_source",
    openapi_extra=BINARY,
    dependencies=[Depends(enabled)],
)
async def upload(
    agent_id: UUID,
    request: Request,
    key: IdempotencyKey,
    name: Annotated[Name, Query()],
    format: Annotated[FileFormat, Query()],
    checksum: Annotated[Digest, Header(alias="X-Content-SHA256")],
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    return await receive(
        request,
        db,
        user,
        agent_id,
        None,
        Upload(name=name, format=format, checksum=checksum.lower()),
        key,
    )


@router.put(
    "/sources/{source_id}/content",
    response_model=SourceOut,
    operation_id="knowledge_replace_file",
    openapi_extra=BINARY,
    dependencies=[Depends(enabled)],
)
async def replace(
    agent_id: UUID,
    source_id: UUID,
    request: Request,
    key: IdempotencyKey,
    name: Annotated[Name, Query()],
    format: Annotated[FileFormat, Query()],
    revision: Annotated[int, Query(ge=1)],
    checksum: Annotated[Digest, Header(alias="X-Content-SHA256")],
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    return await receive(
        request,
        db,
        user,
        agent_id,
        source_id,
        Upload(name=name, format=format, checksum=checksum.lower(), revision=revision),
        key,
    )


@router.get("/sources/{source_id}", response_model=SourceDetailOut, operation_id="knowledge_source")
def detail(
    agent_id: UUID,
    source_id: UUID,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    sources.agent_row(db, agent_id)
    return sources.detail_out(db, sources.source_row(db, agent_id, source_id))


@router.put(
    "/sources/{source_id}",
    response_model=SourceOut,
    operation_id="knowledge_update_source",
    dependencies=[Depends(enabled)],
)
def update(
    agent_id: UUID,
    source_id: UUID,
    body: SourceUpdate,
    key: IdempotencyKey,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    return sources.mutate(db, user, agent_id, source_id, "update", body, key)


@router.delete(
    "/sources/{source_id}",
    response_model=SourceOut,
    operation_id="knowledge_delete_source",
    dependencies=[Depends(enabled)],
)
def delete(
    agent_id: UUID,
    source_id: UUID,
    key: IdempotencyKey,
    revision: Annotated[int, Query(ge=1)],
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    return sources.mutate(db, user, agent_id, source_id, "delete", Revision(revision=revision), key)


@router.post(
    "/sources/{source_id}/reindex",
    response_model=SourceOut,
    operation_id="knowledge_reindex_source",
    dependencies=[Depends(enabled)],
)
def reindex(
    agent_id: UUID,
    source_id: UUID,
    body: Revision,
    key: IdempotencyKey,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    return sources.mutate(db, user, agent_id, source_id, "reindex", body, key)


@router.get(
    "/sources/{source_id}/preview", response_model=Preview, operation_id="knowledge_preview_source"
)
def preview(
    agent_id: UUID,
    source_id: UUID,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    sources.agent_row(db, agent_id)
    row = sources.source_row(db, agent_id, source_id)
    excerpts = (
        list(
            db.scalars(
                select(Chunk.text)
                .where(Chunk.generation_id == row.active_generation_id, Chunk.source_id == row.id)
                .order_by(Chunk.ordinal)
                .limit(4)
            )
        )
        if row.active_generation_id
        else []
    )
    return Preview(
        source_id=row.id,
        available=bool(row.active_generation_id),
        reason="ready" if row.active_generation_id else "processing_unavailable",
        excerpts=[value[:2000] for value in excerpts],
    )


@router.post(
    "/search",
    response_model=SearchResult,
    operation_id="knowledge_search",
    dependencies=[Depends(enabled)],
)
def search(
    agent_id: UUID,
    body: SearchQuery,
    request: Request,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    from knowledge.retrieval import KnowledgeScope
    from knowledge.service import KnowledgeService

    sources.agent_row(db, agent_id)
    db.commit()  # Release authentication/read transaction before embedding RPC.
    scope = KnowledgeScope(user.tenant_id, agent_id, "preview")
    result = KnowledgeService(
        request.app.state.session_factory,
        request.app.state.knowledge_embeddings,
        audit_secret=request.app.state.settings.jwt_secret.get_secret_value(),
    ).retrieve(
        scope, body.query, body.language, query_id=body.query_id, deadline=time.monotonic() + 10
    )
    public = []
    for chunk in result.chunks:
        value = asdict(chunk)
        del value["source_revision"], value["generation_source_revision"]
        public.append(value)
    return {"reason": result.reason, "chunks": public}
