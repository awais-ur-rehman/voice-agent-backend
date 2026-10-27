"""Shared scheduling transactions and staff HTTP routes; runtime guards live in brain."""

import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import AwareDatetime, StrictBool, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .actors import AuditActor
from .contracts import Contract
from .db import Principal, principal, session
from .errors import AppError
from .models import Appointment, Audit, Idempotency, Patient, Provider, Service, Tenant
from .slots import get, provider_slots, search

router = APIRouter(tags=["Appointments"], dependencies=[Depends(principal)])
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
]


class Confirm(Contract):
    confirmed: StrictBool

    @field_validator("confirmed")
    @classmethod
    def explicitly_confirmed(cls, value):
        if value is not True:
            raise ValueError("Critical fields must be confirmed")
        return value


class Move(Confirm):
    starts_at: AwareDatetime
    provider_id: UUID | None = None

    @field_validator("starts_at")
    @classmethod
    def utc(cls, value):
        try:
            return value.astimezone(UTC)
        except OverflowError:
            raise ValueError("Timestamp is outside the supported UTC range") from None


class Book(Move):
    patient_id: UUID
    service_id: UUID
    provider_id: UUID


class AppointmentOut(Contract):
    id: UUID
    patient_id: UUID
    provider_id: UUID
    service_id: UUID
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    status: str
    rescheduled_from_id: UUID | None
    created_at: AwareDatetime


def audit(db: Session, user: AuditActor, action: str, identity: UUID):
    db.add(
        Audit(
            tenant_id=user.tenant_id,
            actor_id=None if hasattr(user, "agent_id") else user.id,
            agent_id=getattr(user, "agent_id", None),
            action=action,
            entity_id=identity,
        )
    )


def idempotent(
    db: Session,
    user: AuditActor,
    key: str,
    operation: str,
    body: Contract,
    execute,
    alternatives=None,
):
    digest = hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    db.execute(
        insert(Idempotency)
        .values(tenant_id=user.tenant_id, key=key, operation=operation, request_hash=digest)
        .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
    )
    record = db.scalar(select(Idempotency).where(Idempotency.key == key).with_for_update())
    if record is None:
        raise AppError("idempotency_conflict", 409)
    if record.operation != operation or record.request_hash != digest:
        raise AppError("idempotency_conflict", 409)
    if record.response is not None:
        return record.response
    try:
        with db.begin_nested():
            result = execute()
            response = AppointmentOut.model_validate(result).model_dump(mode="json")
            db.flush()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23P01":
            raise AppError(
                "slot_unavailable",
                409,
                details={"alternatives": alternatives() if alternatives else []},
            ) from None
        raise
    record.response = response
    db.flush()
    return response


def locks(db: Session, patient_id: UUID, provider_ids: set[UUID]):
    get(db, Patient, patient_id, True)
    for provider_id in sorted(provider_ids):
        get(db, Provider, provider_id, True)


def available(
    db: Session,
    tenant: Tenant,
    patient_id: UUID,
    provider: Provider,
    service: Service,
    start: datetime,
    now: datetime,
    exclude: UUID | None = None,
):
    local_day = start.astimezone(ZoneInfo(tenant.timezone)).date()
    if start < now + timedelta(minutes=tenant.min_notice_min) or local_day > now.astimezone(
        ZoneInfo(tenant.timezone)
    ).date() + timedelta(days=tenant.horizon_days):
        raise AppError("outside_policy", 409)
    zone = ZoneInfo(tenant.timezone)
    first = datetime.combine(local_day, time.min, zone).astimezone(UTC)
    last = datetime.combine(local_day + timedelta(days=1), time.min, zone).astimezone(UTC)
    count = (
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.status == "booked",
            Appointment.starts_at >= first,
            Appointment.starts_at < last,
        )
    )
    if exclude is not None:
        count = count.where(Appointment.id != exclude)
    if (db.scalar(count) or 0) >= tenant.daily_limit:
        raise AppError("outside_policy", 409, "Daily appointment limit reached")
    slots = provider_slots(db, tenant, provider, service, local_day, now, exclude)
    if not any(slot.starts_at == start for slot in slots):
        raise AppError(
            "slot_unavailable",
            409,
            details={"alternatives": [slot.model_dump(mode="json") for slot in slots[:3]]},
        )


def new_appointment(db, user, patient, provider, service, start, source=None):
    row = Appointment(
        tenant_id=user.tenant_id,
        patient_id=patient,
        provider_id=provider.id,
        service_id=service.id,
        starts_at=start,
        ends_at=start + timedelta(minutes=service.duration_min),
        duration_min=service.duration_min,
        buffer_before_min=service.buffer_before_min,
        buffer_after_min=service.buffer_after_min,
        status="booked",
        rescheduled_from_id=source,
    )
    db.add(row)
    db.flush()
    return row


def alternate(db, user, service_id, provider_id, start, now):
    tenant = get(db, Tenant, user.tenant_id)
    day = start.astimezone(ZoneInfo(tenant.timezone)).date()
    return [
        slot.model_dump(mode="json")
        for slot in search(db, tenant, service_id, day, day, now, provider_id, 3)
    ]


def book_transaction(db, user, body: Book, now):
    locks(db, body.patient_id, {body.provider_id})
    tenant, provider, service = (
        get(db, Tenant, user.tenant_id),
        get(db, Provider, body.provider_id),
        get(db, Service, body.service_id),
    )
    available(db, tenant, body.patient_id, provider, service, body.starts_at, now)
    row = new_appointment(db, user, body.patient_id, provider, service, body.starts_at)
    audit(db, user, "appointment.booked", row.id)
    return row


@router.post("/appointments", response_model=AppointmentOut, status_code=201)
def book(
    body: Book,
    request: Request,
    key: IdempotencyKey,
    user: Principal = Depends(principal),
    db: Session = Depends(session),
):
    now = request.app.state.clock()

    def execute():
        return book_transaction(db, user, body, now)

    return idempotent(
        db,
        user,
        key,
        "book",
        body,
        execute,
        lambda: alternate(db, user, body.service_id, body.provider_id, body.starts_at, now),
    )


@router.get("/appointments", response_model=list[AppointmentOut])
def appointments(
    patient_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: Principal = Depends(principal),
    db: Session = Depends(session),
):
    query = select(Appointment)
    if patient_id is not None:
        get(db, Patient, patient_id)
        query = query.where(Appointment.patient_id == patient_id)
    zone = ZoneInfo(get(db, Tenant, user.tenant_id).timezone)
    if date_from and date_from > date.min:
        query = query.where(Appointment.starts_at >= datetime.combine(date_from, time.min, zone))
    if date_to and date_to < date.max:
        query = query.where(
            Appointment.starts_at < datetime.combine(date_to + timedelta(days=1), time.min, zone)
        )
    if date_from and date_to and date_to < date_from:
        raise AppError("validation_error", 422)
    return list(
        db.scalars(
            query.order_by(Appointment.starts_at, Appointment.id).limit(limit).offset(offset)
        )
    )


@router.get("/patients/{patient_id}/appointments", response_model=list[AppointmentOut])
def history(
    patient_id: UUID,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: Principal = Depends(principal),
    db: Session = Depends(session),
):
    return appointments(patient_id, None, None, limit, offset, user, db)


@router.get("/appointments/{identity}", response_model=AppointmentOut)
def appointment(identity: UUID, db: Session = Depends(session)):
    return get(db, Appointment, identity)


def reschedule_transaction(db, user, identity, body: Move, now):
    original = get(db, Appointment, identity)
    provider_id = body.provider_id or original.provider_id
    locks(db, original.patient_id, {original.provider_id, provider_id})
    current = get(db, Appointment, identity, True)
    tenant = get(db, Tenant, user.tenant_id)
    if current.status != "booked":
        raise AppError("not_booked", 409)
    if current.starts_at < now + timedelta(minutes=tenant.cancellation_min):
        raise AppError("outside_policy", 409)
    provider, service = get(db, Provider, provider_id), get(db, Service, current.service_id)
    available(db, tenant, current.patient_id, provider, service, body.starts_at, now, current.id)
    current.status = "cancelled"
    db.flush()
    replacement = new_appointment(
        db, user, current.patient_id, provider, service, body.starts_at, current.id
    )
    audit(db, user, "appointment.rescheduled", replacement.id)
    return replacement


@router.post("/appointments/{identity}/reschedule", response_model=AppointmentOut, status_code=201)
def reschedule(
    identity: UUID,
    body: Move,
    request: Request,
    key: IdempotencyKey,
    user: Principal = Depends(principal),
    db: Session = Depends(session),
):
    now = request.app.state.clock()
    original = get(db, Appointment, identity)
    provider_id = body.provider_id or original.provider_id

    def execute():
        return reschedule_transaction(db, user, identity, body, now)

    return idempotent(
        db,
        user,
        key,
        "reschedule:" + str(identity),
        body,
        execute,
        lambda: alternate(db, user, original.service_id, provider_id, body.starts_at, now),
    )


def cancel_transaction(db, user, identity, now):
    original = get(db, Appointment, identity)
    locks(db, original.patient_id, {original.provider_id})
    current = get(db, Appointment, identity, True)
    tenant = get(db, Tenant, user.tenant_id)
    if current.status != "booked":
        raise AppError("not_booked", 409)
    if current.starts_at < now + timedelta(minutes=tenant.cancellation_min):
        raise AppError("outside_policy", 409)
    current.status = "cancelled"
    db.flush()
    audit(db, user, "appointment.cancelled", current.id)
    return current


@router.post("/appointments/{identity}/cancel", response_model=AppointmentOut)
def cancel(
    identity: UUID,
    body: Confirm,
    request: Request,
    key: IdempotencyKey,
    user: Principal = Depends(principal),
    db: Session = Depends(session),
):
    get(db, Appointment, identity)

    def execute():
        return cancel_transaction(db, user, identity, request.app.state.clock())

    return idempotent(db, user, key, "cancel:" + str(identity), body, execute)
