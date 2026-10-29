"""Tenant-scoped scheduling projections and atomic, code-owned hour templates."""

import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from scheduler.appointments import AppointmentOut, IdempotencyKey, audit
from scheduler.contracts import ExceptionOut, RuleOut
from scheduler.db import Principal, manager, principal, session
from scheduler.errors import AppError
from scheduler.models import (
    Appointment,
    ExceptionDay,
    Idempotency,
    Patient,
    Provider,
    ProviderService,
    Schedule,
    Service,
    Tenant,
)
from scheduler.slots import get

from .contracts import (
    AppointmentPage,
    Availability,
    AvailabilityChanges,
    AvailabilityPreview,
    AvailabilityTemplate,
    CalendarAppointment,
    ProviderServiceItem,
    ProviderServicePage,
)

router = APIRouter(prefix="/dashboard", tags=["Dashboard scheduling"])


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@router.get("/provider-services", response_model=ProviderServicePage)
def provider_services(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: Principal = Depends(principal),
    db: Session = Depends(session),
):
    query = select(ProviderService).where(ProviderService.tenant_id == user.tenant_id)
    return ProviderServicePage(
        items=[
            ProviderServiceItem.model_validate(r)
            for r in db.scalars(
                query.order_by(ProviderService.provider_id, ProviderService.service_id)
                .limit(limit)
                .offset(offset)
            )
        ],
        total=db.scalar(select(func.count()).select_from(query.subquery())) or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/appointments", response_model=AppointmentPage)
def appointments(
    date_from: date,
    date_to: date,
    provider_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: Principal = Depends(principal),
    db: Session = Depends(session),
):
    if date_to < date_from or (date_to - date_from).days > 41:
        raise AppError("validation_error", 422, "Select at most 42 inclusive clinic-local dates")
    tenant = get(db, Tenant, user.tenant_id)
    if provider_id:
        get(db, Provider, provider_id)
    try:
        zone = ZoneInfo(tenant.timezone)
        lower = datetime.combine(date_from, time.min, zone).astimezone(UTC)
        upper = datetime.combine(date_to + timedelta(days=1), time.min, zone).astimezone(UTC)
    except (OverflowError, ValueError):
        raise AppError("validation_error", 422, "Unsupported date range") from None
    query = (
        select(Appointment, Patient, Provider, Service)
        .join(
            Patient,
            and_(Patient.id == Appointment.patient_id, Patient.tenant_id == Appointment.tenant_id),
        )
        .join(
            Provider,
            and_(
                Provider.id == Appointment.provider_id, Provider.tenant_id == Appointment.tenant_id
            ),
        )
        .join(
            Service,
            and_(Service.id == Appointment.service_id, Service.tenant_id == Appointment.tenant_id),
        )
        .where(
            Appointment.tenant_id == user.tenant_id,
            Appointment.starts_at >= lower,
            Appointment.starts_at < upper,
        )
    )
    if provider_id:
        query = query.where(Appointment.provider_id == provider_id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = []
    for appointment, patient, provider, service in db.execute(
        query.order_by(Appointment.starts_at, Appointment.id).limit(limit).offset(offset)
    ):
        items.append(
            CalendarAppointment(
                **AppointmentOut.model_validate(appointment).model_dump(),
                patient_name=patient.name,
                patient_name_ur=patient.name_ur,
                provider_name=provider.name,
                provider_name_ur=provider.name_ur,
                service_name=service.name,
                service_name_ur=service.name_ur,
            )
        )
    return AppointmentPage(items=items, total=total, limit=limit, offset=offset)


def snapshot(
    provider: Provider, rules: list[RuleOut], exceptions: list[ExceptionOut]
) -> Availability:
    rules = sorted(rules, key=lambda r: (r.weekday, r.start_time, r.end_time, str(r.id)))
    exceptions = sorted(exceptions, key=lambda e: (e.start_date, e.end_date, str(e.id)))
    revision = digest(
        {
            "provider_id": str(provider.id),
            "active": provider.active,
            "rules": [r.model_dump(mode="json") for r in rules],
            "exceptions": [e.model_dump(mode="json") for e in exceptions],
        }
    )
    return Availability(revision=revision, rules=rules, exceptions=exceptions)


def load_snapshot(db: Session, provider: Provider) -> Availability:
    return snapshot(
        provider,
        [
            RuleOut.model_validate(r)
            for r in db.scalars(
                select(Schedule)
                .where(
                    Schedule.provider_id == provider.id,
                    Schedule.tenant_id == provider.tenant_id,
                )
                .execution_options(populate_existing=True)
            )
        ],
        [
            ExceptionOut.model_validate(e)
            for e in db.scalars(
                select(ExceptionDay)
                .where(
                    ExceptionDay.provider_id == provider.id,
                    ExceptionDay.tenant_id == provider.tenant_id,
                )
                .execution_options(populate_existing=True)
            )
        ],
    )


@router.get("/providers/{identity}/availability", response_model=Availability)
def availability(
    identity: UUID, user: Principal = Depends(principal), db: Session = Depends(session)
):
    # Share the legacy CRUD provider lock so rules and exceptions form one coherent snapshot.
    return load_snapshot(db, get(db, Provider, identity, True))


def transform(
    provider: Provider, before: Availability, body: AvailabilityTemplate
) -> AvailabilityPreview:
    if body.revision != before.revision:
        raise AppError("revision_conflict", 409)
    removed: list[UUID] = []
    added_rules: list[RuleOut] = []
    added_exceptions: list[ExceptionOut] = []
    if body.template == "friday_prayer":
        start, end = body.break_start or time(12, 45), body.break_end or time(14, 30)
        for rule in before.rules:
            if rule.weekday != 5 or rule.end_time <= start or rule.start_time >= end:
                continue
            removed.append(rule.id)
            for lower, upper in (
                (rule.start_time, min(rule.end_time, start)),
                (max(rule.start_time, end), rule.end_time),
            ):
                if lower < upper:
                    added_rules.append(
                        rule.model_copy(
                            update={
                                "id": uuid5(
                                    rule.id, f"friday:{lower.isoformat()}:{upper.isoformat()}"
                                ),
                                "start_time": lower,
                                "end_time": upper,
                            }
                        )
                    )
    else:
        # Validation guarantees dates/hours; reuse the legacy custom exception contract.
        candidate = ExceptionOut.model_validate(
            {
                "id": uuid5(
                    provider.id,
                    json.dumps(body.model_dump(mode="json", exclude={"revision"}), sort_keys=True),
                ),
                "provider_id": provider.id,
                "kind": "custom",
                "start_date": body.start_date,
                "end_date": body.end_date,
                "start_time": body.start_time,
                "end_time": body.end_time,
            }
        )
        identical = False
        for existing in before.exceptions:
            if existing.start_date > candidate.end_date or existing.end_date < candidate.start_date:
                continue
            if existing.model_dump(exclude={"id"}) == candidate.model_dump(exclude={"id"}):
                identical = True
            else:
                raise AppError(
                    "availability_conflict", 409, "Template overlaps an existing exception"
                )
        if not identical:
            added_exceptions.append(candidate)
    after = snapshot(
        provider,
        [r for r in before.rules if r.id not in removed] + added_rules,
        before.exceptions + added_exceptions,
    )
    return AvailabilityPreview(
        revision=before.revision,
        changes=AvailabilityChanges(
            removed_rule_ids=removed, added_rules=added_rules, added_exceptions=added_exceptions
        ),
        after=after,
    )


@router.post("/providers/{identity}/availability/preview", response_model=AvailabilityPreview)
def preview(
    identity: UUID,
    body: AvailabilityTemplate,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    provider = get(db, Provider, identity, True)
    return transform(provider, load_snapshot(db, provider), body)


@router.post("/providers/{identity}/availability/apply", response_model=Availability)
def apply(
    identity: UUID,
    body: AvailabilityTemplate,
    key: IdempotencyKey,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    operation = f"availability.template:{identity}"
    request_hash = digest(body.model_dump(mode="json"))
    db.execute(
        insert(Idempotency)
        .values(
            tenant_id=user.tenant_id,
            key=key,
            operation=operation,
            request_hash=request_hash,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
    )
    receipt = db.scalar(
        select(Idempotency)
        .where(
            Idempotency.tenant_id == user.tenant_id,
            Idempotency.key == key,
        )
        .with_for_update()
    )
    if receipt is None or receipt.operation != operation or receipt.request_hash != request_hash:
        raise AppError("idempotency_conflict", 409)
    if receipt.response is not None:
        return receipt.response
    provider = get(db, Provider, identity, True)
    result = transform(provider, load_snapshot(db, provider), body)
    for rule_id in result.changes.removed_rule_ids:
        db.delete(get(db, Schedule, rule_id))
    for rule in result.changes.added_rules:
        db.add(Schedule(tenant_id=user.tenant_id, **rule.model_dump()))
    for exception in result.changes.added_exceptions:
        db.add(ExceptionDay(tenant_id=user.tenant_id, **exception.model_dump()))
    if result.after.revision != result.revision:
        audit(db, user, "availability.template_applied", provider.id)
    db.flush()
    receipt.response = result.after.model_dump(mode="json")
    db.flush()
    return receipt.response
