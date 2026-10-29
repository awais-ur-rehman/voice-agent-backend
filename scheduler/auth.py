import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from .contracts import ClinicPatch, Name
from .db import Principal, principal, session, tenant_context
from .errors import AppError
from .models import RefreshToken, User
from .security import DUMMY_HASH, access_token, passwords, refresh_secret, token_hash

router = APIRouter(prefix="/auth", tags=["Authentication"])


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class Refresh(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=40, max_length=256)


class Tokens(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class Me(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    email: str
    role: str


def issue(db, user, settings, family_id=None):
    raw = refresh_secret(user.tenant_id)
    db.add(
        RefreshToken(
            tenant_id=user.tenant_id,
            user_id=user.id,
            family_id=family_id or uuid4(),
            token_hash=token_hash(raw),
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_days),
        )
    )
    db.flush()
    return Tokens(
        access_token=access_token(user.id, user.tenant_id, settings),
        refresh_token=raw,
        expires_in=settings.access_minutes * 60,
    )


@router.post("/login", response_model=Tokens)
def login(body: Login, request: Request, db: Session = Depends(session)):
    user = (
        db.execute(text("SELECT * FROM auth_lookup(:email)"), {"email": str(body.email).lower()})
        .mappings()
        .first()
    )
    encoded = user["password_hash"] if user else DUMMY_HASH
    if not passwords.verify(body.password, encoded) or user is None:
        raise AppError("unauthorized", 401)
    tenant_context(db, user["tenant_id"])
    account = db.get(User, user["id"])
    if account is None:
        raise AppError("unauthorized", 401)
    return issue(db, account, request.app.state.settings)


def find_refresh(db: Session, raw: str):
    try:
        tenant_id = UUID(raw.split(".", 1)[0])
    except (ValueError, IndexError):
        return None
    tenant_context(db, tenant_id)
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash(raw)))
    if row is None:
        return None
    # Serialize the whole family BEFORE any token-row lock. A row-only revocation
    # UPDATE can otherwise miss a descendant committed while that UPDATE waits.
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:family, 0))"),
        {"family": f"refresh:{tenant_id}:{row.family_id}"},
    )
    return db.scalar(
        select(RefreshToken)
        .where(RefreshToken.id == row.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


@router.post("/refresh", response_model=Tokens)
def refresh(body: Refresh, request: Request, db: Session = Depends(session)):
    row = find_refresh(db, body.refresh_token)
    if row is None:
        return JSONResponse(AppError("unauthorized", 401).body(), status_code=401)
    now = datetime.now(UTC)
    if row.revoked_at is not None or row.expires_at <= now:
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == row.family_id)
            .values(revoked_at=now)
        )
        # Return, do not raise: the request transaction must COMMIT family revocation.
        return JSONResponse(AppError("unauthorized", 401).body(), status_code=401)
    user = db.get(User, row.user_id)
    if user is None or not user.active:
        row.revoked_at = now
        return JSONResponse(AppError("unauthorized", 401).body(), status_code=401)
    row.revoked_at = now
    return issue(db, user, request.app.state.settings, row.family_id)


@router.post("/logout", status_code=204)
def logout(body: Refresh, db: Session = Depends(session)):
    row = find_refresh(db, body.refresh_token)
    if row is not None:
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == row.family_id)
            .values(revoked_at=datetime.now(UTC))
        )
    return Response(status_code=204)


@router.get("/me", response_model=Me)
def me(user: Principal = Depends(principal)):
    return user


class Register(Login):
    clinic_name: Name
    timezone: str
    password: str = Field(min_length=12, max_length=128)
    display_name: Name | None = None

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value):
        return ClinicPatch.valid_zone(value)


@router.post("/register", response_model=Tokens, status_code=201)
def register(body: Register, request: Request, db: Session = Depends(session)):
    if not request.app.state.settings.registration_enabled:
        raise AppError("registration_disabled", 403)
    bucket = hashlib.sha256(
        (request.client.host if request.client else "unknown").encode()
    ).hexdigest()
    with request.app.state.session_factory() as limiter, limiter.begin():
        admitted = limiter.scalar(text("SELECT registration_admit(:bucket)"), {"bucket": bucket})
    if not admitted:
        raise AppError("rate_limited", 429)
    row = db.execute(
        text("SELECT * FROM provision_clinic(:name,:zone,:email,:hash,:display)"),
        {
            "name": body.clinic_name,
            "zone": body.timezone,
            "email": str(body.email).lower(),
            "hash": passwords.hash(body.password),
            "display": body.display_name,
        },
    ).one()
    tenant_context(db, row.clinic_id)
    return issue(db, db.get(User, row.user_id), request.app.state.settings)
