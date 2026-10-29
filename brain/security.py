"""Runtime capabilities are separate from staff credentials and checked against SQL."""

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from scheduler.db import bearer, session, tenant_context
from scheduler.errors import AppError
from scheduler.models import Agent, AgentSession
from scheduler.security import token_hash

AUDIENCE = "voice-agent-runtime"


@dataclass(frozen=True)
class RuntimeActor:
    id: UUID
    tenant_id: UUID
    agent_id: UUID
    version_id: UUID
    nonce_hash: str | None = field(default=None, repr=False, compare=False)


def runtime_secret(settings) -> str:
    secret = getattr(settings, "runtime_secret", None)
    if secret is None:
        raise AppError("unauthorized", 401)
    value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
    if len(value) < 32 or value == settings.jwt_secret.get_secret_value():
        raise AppError("unauthorized", 401)
    return value


def issue_runtime(row: AgentSession, settings) -> str:
    now = datetime.now(UTC)
    nonce = secrets.token_urlsafe(32)
    row.token_jti_hash = token_hash(nonce)
    return jwt.encode(
        {
            "sub": str(row.id),
            "tenant": str(row.tenant_id),
            "agent": str(row.agent_id),
            "version": str(row.version_id),
            "jti": nonce,
            "iat": now,
            "exp": now + timedelta(minutes=30),
            "iss": settings.jwt_issuer,
            "aud": AUDIENCE,
        },
        runtime_secret(settings),
        algorithm="HS256",
    )


def active_guard(row: AgentSession | None, actor: RuntimeActor, now: datetime) -> AgentSession:
    if (
        row is None
        or row.tenant_id != actor.tenant_id
        or row.agent_id != actor.agent_id
        or row.version_id != actor.version_id
        or row.status != "active"
        or row.revoked_at is not None
        or row.expires_at <= now
        or actor.nonce_hash is not None
        and not secrets.compare_digest(row.token_jti_hash, actor.nonce_hash)
    ):
        raise AppError("unauthorized", 401)
    return row


def decode_runtime(db: Session, token: str, settings, now: datetime | None = None) -> RuntimeActor:
    try:
        claims = jwt.decode(
            token,
            runtime_secret(settings),
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer=settings.jwt_issuer,
            options={
                "require": ["sub", "tenant", "agent", "version", "jti", "iat", "exp", "iss", "aud"]
            },
        )
        actor = RuntimeActor(
            UUID(claims["sub"]),
            UUID(claims["tenant"]),
            UUID(claims["agent"]),
            UUID(claims["version"]),
            token_hash(claims["jti"]),
        )
        nonce_hash = token_hash(claims["jti"])
    except (jwt.InvalidTokenError, ValueError, KeyError, TypeError):
        raise AppError("unauthorized", 401) from None
    tenant_context(db, actor.tenant_id)
    row = db.scalar(
        select(AgentSession)
        .where(AgentSession.id == actor.id)
        .execution_options(populate_existing=True)
    )
    row = active_guard(row, actor, now or datetime.now(UTC))
    agent = db.get(Agent, actor.agent_id)
    if not secrets.compare_digest(row.token_jti_hash, nonce_hash) or agent is None:
        raise AppError("unauthorized", 401)
    return actor


def runtime_actor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(session),
) -> RuntimeActor:
    if credentials is None:
        raise AppError("unauthorized", 401)
    return decode_runtime(
        db, credentials.credentials, request.app.state.settings, now=request.app.state.clock()
    )
