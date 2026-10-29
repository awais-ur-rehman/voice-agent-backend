from fastapi import APIRouter, Depends
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from scheduler.db import Principal, manager, principal, session
from scheduler.errors import AppError
from scheduler.models import Agent, Provider, ProviderService, Schedule, Tenant
from scheduler.slots import get

from .contracts import ClinicProfile, ProfilePatch, Setup

router = APIRouter(prefix="/dashboard", tags=["Dashboard clinic"])


@router.get("/clinic", response_model=ClinicProfile)
def clinic(user: Principal = Depends(principal), db: Session = Depends(session)):
    return get(db, Tenant, user.tenant_id)


@router.patch("/clinic", response_model=ClinicProfile)
def patch(body: ProfilePatch, user: Principal = Depends(manager), db: Session = Depends(session)):
    row = get(db, Tenant, user.tenant_id, True)
    if row.revision != body.revision:
        raise AppError("revision_conflict", 409)
    if body.timezone and body.timezone != row.timezone and not body.confirm_timezone_change:
        raise AppError("timezone_confirmation_required", 422)
    for key, value in body.model_dump(
        exclude_unset=True, exclude={"revision", "confirm_timezone_change"}
    ).items():
        setattr(row, key, value)
    db.flush()
    db.refresh(row)
    return row


@router.get("/setup", response_model=Setup)
def setup(user: Principal = Depends(principal), db: Session = Depends(session)):
    tenant = get(db, Tenant, user.tenant_id)
    return Setup(
        profile=bool(tenant.name and tenant.address_en or tenant.address_ur),
        providers=bool(db.scalar(select(exists().where(Provider.active.is_(True))))),
        services=bool(
            db.scalar(select(exists().where(ProviderService.tenant_id == user.tenant_id)))
        ),
        hours=bool(db.scalar(select(exists().where(Schedule.tenant_id == user.tenant_id)))),
        agent=bool(db.scalar(select(exists().where(Agent.active_version_id.is_not(None))))),
    )
