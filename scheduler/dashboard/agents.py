"""Tenant-owned agent management; mutations and admission serialize on the agent row."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import AwareDatetime, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from brain.agent_config import AgentConfig
from brain.voice_catalog import catalog
from scheduler.appointments import IdempotencyKey, audit
from scheduler.contracts import Contract
from scheduler.db import Principal, manager, principal, session
from scheduler.errors import AppError
from scheduler.models import Agent, AgentDraft, AgentVersion, Idempotency, Tenant
from scheduler.slots import get

router = APIRouter(prefix="/dashboard", tags=["Dashboard agents"])


class VoiceOut(Contract):
    id: str
    language: Literal["en", "ur"]
    gender: Literal["female", "male"]
    engine: Literal["kokoro", "parler"]
    model_revision: str
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    license: str
    readiness: Literal["provisional"]
    preview_available: Literal[False]
    preview_url: None
    unavailable_reason: str


class AgentCreate(Contract):
    name: str = Field(min_length=1, max_length=100)


class Revision(Contract):
    revision: int = Field(ge=1)


class AgentPatch(Revision):
    enabled: bool


class DraftPut(Revision):
    config: AgentConfig

    @field_validator("config", mode="before")
    @classmethod
    def explicit_upgrade(cls, value):
        if isinstance(value, dict) and (
            value.get("schema_version") != 2 or not {"voice_en_id", "voice_ur_id"} <= value.keys()
        ):
            raise ValueError("Saving v2 requires explicit schema and voice choices")
        return value


class Rollback(Revision):
    version_id: UUID


class DraftOut(Revision):
    config: dict
    updated_at: AwareDatetime


class AgentOut(Revision):
    id: UUID
    name: str
    enabled: bool
    archived_at: AwareDatetime | None
    active_version_id: UUID | None
    language_mode: str
    published_number: int | None
    draft_changed: bool


class VersionOut(Contract):
    id: UUID
    number: int | None
    config: dict
    publisher_id: UUID | None
    created_at: AwareDatetime
    rollback_source_id: UUID | None
    warnings: list[str] = Field(default_factory=list)


def locked(db, identity):
    row = get(db, Agent, identity, True)
    if row.archived_at is not None:
        raise AppError("not_found", 404)
    return row


def draft_row(db, agent, *, lock=False):
    query = select(AgentDraft).where(
        AgentDraft.agent_id == agent.id, AgentDraft.tenant_id == agent.tenant_id
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None:
        version = db.get(AgentVersion, agent.active_version_id) if agent.active_version_id else None
        row = AgentDraft(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            config=deepcopy(version.config)
            if version
            else AgentConfig(name=agent.name).model_dump(),
        )
        db.add(row)
        db.flush()
    return row


def agent_out(db, row):
    version = db.get(AgentVersion, row.active_version_id) if row.active_version_id else None
    draft = db.scalar(select(AgentDraft).where(AgentDraft.agent_id == row.id))
    config = version.config if version else (draft.config if draft else {})
    return AgentOut(
        id=row.id,
        revision=row.revision,
        name=row.name,
        enabled=row.enabled,
        archived_at=row.archived_at,
        active_version_id=row.active_version_id,
        language_mode=config.get("language_mode", "bilingual"),
        published_number=version.number if version else None,
        draft_changed=version is None or draft is not None and draft.config != version.config,
    )


@router.get("/voices", response_model=list[VoiceOut])
def voices(user: Principal = Depends(principal)):
    return catalog()


@router.get("/agents", response_model=list[AgentOut])
def agents(user: Principal = Depends(principal), db: Session = Depends(session)):
    return [
        agent_out(db, row)
        for row in db.scalars(
            select(Agent).where(Agent.archived_at.is_(None)).order_by(Agent.name, Agent.id)
        )
    ]


@router.post("/agents", response_model=AgentOut, status_code=201)
def create(body: AgentCreate, user: Principal = Depends(manager), db: Session = Depends(session)):
    row = Agent(tenant_id=user.tenant_id, name=body.name, enabled=True)
    db.add(row)
    db.flush()
    db.add(
        AgentDraft(
            tenant_id=user.tenant_id,
            agent_id=row.id,
            config=AgentConfig(name=body.name).model_dump(),
            updated_by=user.id,
        )
    )
    audit(db, user, "agent.create", row.id)
    db.flush()
    return agent_out(db, row)


@router.get("/agents/{identity}", response_model=AgentOut)
def agent(identity: UUID, user: Principal = Depends(principal), db: Session = Depends(session)):
    return agent_out(db, get(db, Agent, identity))


@router.patch("/agents/{identity}", response_model=AgentOut)
def patch(
    identity: UUID,
    body: AgentPatch,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    row = locked(db, identity)
    if row.revision != body.revision:
        raise AppError("revision_conflict", 409)
    row.enabled = body.enabled
    audit(db, user, "agent.enable" if body.enabled else "agent.disable", row.id)
    db.flush()
    db.refresh(row)
    return agent_out(db, row)


@router.delete("/agents/{identity}", response_model=AgentOut)
def archive(identity: UUID, user: Principal = Depends(manager), db: Session = Depends(session)):
    row = locked(db, identity)
    row.archived_at = datetime.now(UTC)
    row.archived_by = user.id
    row.enabled = False
    audit(db, user, "agent.archive", row.id)
    db.flush()
    db.refresh(row)
    return agent_out(db, row)


@router.get("/agents/{identity}/draft", response_model=DraftOut)
def draft(identity: UUID, user: Principal = Depends(manager), db: Session = Depends(session)):
    return draft_row(db, locked(db, identity), lock=True)


@router.put("/agents/{identity}/draft", response_model=DraftOut)
def save(
    identity: UUID,
    body: DraftPut,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    row = draft_row(db, locked(db, identity), lock=True)
    if row.revision != body.revision:
        raise AppError("revision_conflict", 409)
    row.config = body.config.model_dump()
    row.revision += 1
    row.updated_by = user.id
    row.updated_at = datetime.now(UTC)
    audit(db, user, "agent.draft", identity)
    db.flush()
    return row


def version_out(row):
    return VersionOut.model_validate(row).model_copy(
        update={
            "warnings": [
                "Selected voice profiles are provisional; speech admission remains unavailable."
            ]
            if row.config.get("schema_version") == 2
            else []
        }
    )


def publish_version(db, user, agent, draft, config, source=None):
    number = (
        db.scalar(
            select(func.max(AgentVersion.number)).where(
                AgentVersion.agent_id == agent.id, AgentVersion.purpose == "production"
            )
        )
        or 0
    ) + 1
    version = AgentVersion(
        tenant_id=user.tenant_id,
        agent_id=agent.id,
        config=deepcopy(config),
        number=number,
        publisher_id=user.id,
        rollback_source_id=source,
    )
    db.add(version)
    db.flush()
    agent.active_version_id = version.id
    agent.name = config.get("name", agent.name)
    draft.config = deepcopy(config)
    draft.revision += 1
    draft.updated_at = datetime.now(UTC)
    draft.updated_by = user.id
    audit(db, user, "agent.rollback" if source else "agent.publish", agent.id)
    db.flush()
    return version_out(version).model_dump(mode="json")


def publication(db, user, identity, body, key, rollback=False):
    # Lock agent before draft, including retries. Disabling/admission use this same row.
    row = locked(db, identity)
    digest = hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    operation = f"agent:{identity}:" + ("rollback" if rollback else "publish")
    db.execute(
        insert(Idempotency)
        .values(tenant_id=user.tenant_id, key=key, operation=operation, request_hash=digest)
        .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
    )
    receipt = db.scalar(select(Idempotency).where(Idempotency.key == key).with_for_update())
    if receipt.operation != operation or receipt.request_hash != digest:
        raise AppError("idempotency_conflict", 409)
    if receipt.response is not None:
        return receipt.response
    draft = draft_row(db, row, lock=True)
    if draft.revision != body.revision:
        raise AppError("revision_conflict", 409)
    if rollback:
        source = db.scalar(
            select(AgentVersion).where(
                AgentVersion.id == body.version_id,
                AgentVersion.agent_id == identity,
                AgentVersion.purpose == "production",
            )
        )
        if source is None:
            raise AppError("not_found", 404)
        config = deepcopy(source.config)
    else:
        if draft.config.get("schema_version") != 2:
            raise AppError("agent_upgrade_required", 422)
        try:
            config = AgentConfig.model_validate(draft.config).model_dump()
        except ValueError:
            raise AppError("validation_error", 422) from None
        tenant = get(db, Tenant, user.tenant_id, True)
        config["clinic_profile"] = {
            field: getattr(tenant, field)
            for field in ("address_en", "address_ur", "preparation_en", "preparation_ur")
        }
    response = publish_version(db, user, row, draft, config, body.version_id if rollback else None)
    receipt.response = response
    db.flush()
    return response


@router.post("/agents/{identity}/publish", response_model=VersionOut)
def publish(
    identity: UUID,
    body: Revision,
    key: IdempotencyKey,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    return publication(db, user, identity, body, key)


@router.post("/agents/{identity}/rollback", response_model=VersionOut)
def rollback(
    identity: UUID,
    body: Rollback,
    key: IdempotencyKey,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    return publication(db, user, identity, body, key, True)


@router.get("/agents/{identity}/versions", response_model=list[VersionOut])
def versions(identity: UUID, user: Principal = Depends(manager), db: Session = Depends(session)):
    get(db, Agent, identity)
    return [
        version_out(row)
        for row in db.scalars(
            select(AgentVersion)
            .where(AgentVersion.agent_id == identity, AgentVersion.purpose == "production")
            .order_by(AgentVersion.number.desc())
        )
    ]
