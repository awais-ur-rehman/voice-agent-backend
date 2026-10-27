from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .errors import AppError
from .models import User

bearer = HTTPBearer(auto_error=False)


def session(request: Request):
    with request.app.state.session_factory() as db, db.begin():
        yield db


def tenant_context(db: Session, tenant_id: UUID) -> None:
    db.execute(text("SELECT set_config('app.tenant_id',:tenant,true)"), {"tenant": str(tenant_id)})


@dataclass(frozen=True)
class Principal:
    id: UUID
    tenant_id: UUID
    email: str
    role: str


def principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(session),
) -> Principal:
    if credentials is None:
        raise AppError("unauthorized", 401)
    settings = request.app.state.settings
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "tenant", "iat", "exp", "iss", "aud"]},
        )
        user_id, tenant_id = UUID(claims["sub"]), UUID(claims["tenant"])
    except (jwt.InvalidTokenError, ValueError, KeyError, TypeError):
        raise AppError("unauthorized", 401) from None
    tenant_context(db, tenant_id)
    user = db.scalar(select(User).where(User.id == user_id, User.active.is_(True)))
    if user is None:
        raise AppError("unauthorized", 401)
    return Principal(user.id, user.tenant_id, user.email, user.role)


def manager(user: Principal = Depends(principal)) -> Principal:
    if user.role not in {"owner", "admin"}:
        raise AppError("forbidden", 403)
    return user
