from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import contracts as c
from .auth import Me
from .db import Principal, manager, principal, session
from .errors import AppError
from .models import (
    ExceptionDay,
    Patient,
    Provider,
    ProviderService,
    Schedule,
    Service,
    Tenant,
    User,
)
from .security import passwords
from .slots import Slot, get, search

router = APIRouter(tags=["Clinic and scheduling"], dependencies=[Depends(principal)])


def page(db, model, limit, offset):
    return list(db.scalars(select(model).order_by(model.id).limit(limit).offset(offset)))


def apply(db, row, body):
    values = body.model_dump(exclude_unset=True)
    if "name" in values and "name_ur" not in values and hasattr(row, "name_ur"):
        values["name_ur"] = None
    for name, value in values.items():
        setattr(row, name, value)
    db.flush()
    return row


@router.get("/clinic", response_model=c.ClinicOut)
def clinic(user: Principal = Depends(principal), db: Session = Depends(session)):
    return get(db, Tenant, user.tenant_id)


@router.patch("/clinic", response_model=c.ClinicOut)
def clinic_update(
    body: c.ClinicPatch, user: Principal = Depends(manager), db: Session = Depends(session)
):
    return apply(db, get(db, Tenant, user.tenant_id, True), body)


@router.get("/users", response_model=list[Me])
def users(
    user: Principal = Depends(manager),
    db: Session = Depends(session),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return page(db, User, limit, offset)


@router.post("/users", response_model=Me, status_code=201)
def create_user(
    body: c.UserCreate, user: Principal = Depends(manager), db: Session = Depends(session)
):
    if user.role != "owner" and body.role != "staff":
        raise AppError("forbidden", 403)
    row = User(
        tenant_id=user.tenant_id,
        email=str(body.email).lower(),
        password_hash=passwords.hash(body.password),
        role=body.role,
    )
    db.add(row)
    db.flush()
    return row


@router.get("/providers", response_model=list[c.ProviderOut])
def providers(
    db: Session = Depends(session),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return page(db, Provider, limit, offset)


@router.post("/providers", response_model=c.ProviderOut, status_code=201)
def create_provider(
    body: c.ProviderCreate, user: Principal = Depends(manager), db: Session = Depends(session)
):
    row = Provider(tenant_id=user.tenant_id, **body.model_dump())
    db.add(row)
    db.flush()
    return row


@router.get("/providers/{identity}", response_model=c.ProviderOut)
def provider(identity: UUID, db: Session = Depends(session)):
    return get(db, Provider, identity)


@router.patch("/providers/{identity}", response_model=c.ProviderOut)
def update_provider(
    identity: UUID,
    body: c.ProviderPatch,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    return apply(db, get(db, Provider, identity, True), body)


@router.delete("/providers/{identity}", status_code=204)
def archive_provider(
    identity: UUID, user: Principal = Depends(manager), db: Session = Depends(session)
):
    get(db, Provider, identity, True).active = False
    return Response(status_code=204)


@router.get("/services", response_model=list[c.ServiceOut])
def services(
    db: Session = Depends(session),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return page(db, Service, limit, offset)


@router.post("/services", response_model=c.ServiceOut, status_code=201)
def create_service(
    body: c.ServiceCreate, user: Principal = Depends(manager), db: Session = Depends(session)
):
    row = Service(tenant_id=user.tenant_id, **body.model_dump())
    db.add(row)
    db.flush()
    return row


@router.get("/services/{identity}", response_model=c.ServiceOut)
def service(identity: UUID, db: Session = Depends(session)):
    return get(db, Service, identity)


@router.patch("/services/{identity}", response_model=c.ServiceOut)
def update_service(
    identity: UUID,
    body: c.ServicePatch,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    row = get(db, Service, identity)
    providers = list(
        db.scalars(
            select(ProviderService.provider_id).where(ProviderService.service_id == identity)
        )
    )
    for provider_id in sorted(providers):
        get(db, Provider, provider_id, True)
    return apply(db, row, body)


@router.delete("/services/{identity}", status_code=204)
def archive_service(
    identity: UUID, user: Principal = Depends(manager), db: Session = Depends(session)
):
    update_service(identity, c.ServicePatch(active=False), user, db)
    return Response(status_code=204)


@router.put("/providers/{provider_id}/services/{service_id}", status_code=204)
def associate(
    provider_id: UUID,
    service_id: UUID,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    get(db, Provider, provider_id, True)
    get(db, Service, service_id)
    if db.get(ProviderService, (provider_id, service_id)) is None:
        db.add(
            ProviderService(
                tenant_id=user.tenant_id, provider_id=provider_id, service_id=service_id
            )
        )
        db.flush()
    return Response(status_code=204)


@router.delete("/providers/{provider_id}/services/{service_id}", status_code=204)
def dissociate(
    provider_id: UUID,
    service_id: UUID,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    get(db, Provider, provider_id, True)
    get(db, Service, service_id)
    row = db.get(ProviderService, (provider_id, service_id))
    if row:
        db.delete(row)
    return Response(status_code=204)


@router.get("/availability-rules", response_model=list[c.RuleOut])
def rules(
    db: Session = Depends(session),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return page(db, Schedule, limit, offset)


@router.post("/availability-rules", response_model=c.RuleOut, status_code=201)
def create_rule(
    body: c.RuleCreate, user: Principal = Depends(manager), db: Session = Depends(session)
):
    get(db, Provider, body.provider_id, True)
    row = Schedule(tenant_id=user.tenant_id, **body.model_dump())
    db.add(row)
    db.flush()
    return row


@router.put("/availability-rules/{identity}", response_model=c.RuleOut)
def update_rule(
    identity: UUID,
    body: c.RuleCreate,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    row = get(db, Schedule, identity)
    for provider_id in sorted({row.provider_id, body.provider_id}):
        get(db, Provider, provider_id, True)
    return apply(db, row, body)


@router.delete("/availability-rules/{identity}", status_code=204)
def delete_rule(identity: UUID, user: Principal = Depends(manager), db: Session = Depends(session)):
    row = get(db, Schedule, identity)
    get(db, Provider, row.provider_id, True)
    db.delete(row)
    return Response(status_code=204)


@router.get("/exceptions", response_model=list[c.ExceptionOut])
def exceptions(
    db: Session = Depends(session),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return page(db, ExceptionDay, limit, offset)


@router.post("/exceptions", response_model=c.ExceptionOut, status_code=201)
def create_exception(
    body: c.ExceptionCreate, user: Principal = Depends(manager), db: Session = Depends(session)
):
    get(db, Provider, body.provider_id, True)
    row = ExceptionDay(tenant_id=user.tenant_id, **body.model_dump())
    db.add(row)
    db.flush()
    return row


@router.put("/exceptions/{identity}", response_model=c.ExceptionOut)
def update_exception(
    identity: UUID,
    body: c.ExceptionCreate,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    row = get(db, ExceptionDay, identity)
    for provider_id in sorted({row.provider_id, body.provider_id}):
        get(db, Provider, provider_id, True)
    return apply(db, row, body)


@router.delete("/exceptions/{identity}", status_code=204)
def delete_exception(
    identity: UUID, user: Principal = Depends(manager), db: Session = Depends(session)
):
    row = get(db, ExceptionDay, identity)
    get(db, Provider, row.provider_id, True)
    db.delete(row)
    return Response(status_code=204)


@router.get("/patients", response_model=list[c.PatientOut])
def patients(
    db: Session = Depends(session),
    search: str = Query("", max_length=120),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    query = (
        select(Patient)
        .where(Patient.merged_into_id.is_(None))
        .where(Patient.name.ilike("%" + search + "%") | Patient.phone.ilike("%" + search + "%"))
    )
    return list(db.scalars(query.order_by(Patient.id).limit(limit).offset(offset)))


@router.post("/patients", response_model=c.PatientOut, status_code=201)
def create_patient(
    body: c.PatientCreate, user: Principal = Depends(principal), db: Session = Depends(session)
):
    row = Patient(tenant_id=user.tenant_id, name_key=body.name.casefold(), **body.model_dump())
    db.add(row)
    db.flush()
    return row


@router.get("/patients/{identity}", response_model=c.PatientOut)
def patient(identity: UUID, db: Session = Depends(session)):
    return get(db, Patient, identity)


@router.put("/patients/{identity}", response_model=c.PatientOut)
def update_patient(identity: UUID, body: c.PatientCreate, db: Session = Depends(session)):
    from .dashboard.patients import patient_gate

    original = get(db, Patient, identity)
    patient_gate(db, original.tenant_id)
    row = get(db, Patient, identity, True)
    if row.merged_into_id:
        raise AppError("patient_merged", 409)
    row.name_key = body.name.casefold()
    return apply(db, row, body)


@router.get("/slots", response_model=list[Slot])
def slots(
    service_id: UUID,
    date_from: date,
    date_to: date,
    request: Request,
    provider_id: UUID | None = None,
    limit: int = Query(100, ge=1, le=100),
    user: Principal = Depends(principal),
    db: Session = Depends(session),
):
    return search(
        db,
        get(db, Tenant, user.tenant_id),
        service_id,
        date_from,
        date_to,
        request.app.state.clock(),
        provider_id,
        limit,
    )
