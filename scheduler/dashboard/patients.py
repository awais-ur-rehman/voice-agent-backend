import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, literal_column, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from scheduler.appointments import IdempotencyKey, audit
from scheduler.contracts import PatientCreate
from scheduler.db import Principal, manager, principal, session
from scheduler.errors import AppError
from scheduler.models import Appointment, Idempotency, Patient, Tenant
from scheduler.slots import get

from .contracts import Merge, PatientDetail, PatientPage

router = APIRouter(prefix="/dashboard/patients", tags=["Dashboard patients"])


def patient_gate(db, tenant_id, *, exclusive=False):
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    db.execute(
        text(f"SELECT {function}(hashtextextended(:key,0))"), {"key": f"patients:{tenant_id}"}
    )


def canonical_patient(db, tenant_id, identity, *, lock=False):
    if lock:
        patient_gate(db, tenant_id)
    seen = set()
    while identity not in seen:
        seen.add(identity)
        row = db.scalar(
            select(Patient)
            .where(Patient.id == identity, Patient.tenant_id == tenant_id)
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise AppError("not_found", 404)
        if row.merged_into_id is None:
            return get(db, Patient, row.id, True) if lock else row
        identity = row.merged_into_id
    raise AppError("merge_conflict", 409)


@router.get("", response_model=PatientPage)
def patients(
    q: str = Query("", max_length=120),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: Principal = Depends(principal),
    db: Session = Depends(session),
):
    query = select(Patient).where(Patient.merged_into_id.is_(None))
    if q:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses = [
            Patient.name.ilike(pattern),
            Patient.name_ur.ilike(pattern),
            Patient.phone.ilike(pattern),
            literal_column(
                "to_tsvector('simple'::regconfig, coalesce(name,'') || ' ' || coalesce(name_ur,''))"
            ).op("@@")(func.plainto_tsquery(literal_column("'simple'::regconfig"), q)),
            and_(Patient.name.op("%")(q), func.similarity(Patient.name, q) >= 0.6),
            and_(Patient.name_ur.op("%")(q), func.similarity(Patient.name_ur, q) >= 0.6),
        ]
        try:
            clauses.append(Patient.phone == PatientCreate.phone_number(q))
        except ValueError:
            pass
        query = query.where(or_(*clauses))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    return PatientPage(
        items=[
            PatientDetail.model_validate(p)
            for p in db.scalars(
                query.order_by(Patient.name, Patient.id).limit(limit).offset(offset)
            )
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{identity}", response_model=PatientDetail)
def patient(identity: UUID, user: Principal = Depends(principal), db: Session = Depends(session)):
    return get(db, Patient, identity)


@router.post("/{source_id}/merge", response_model=PatientDetail)
def merge(
    source_id: UUID,
    body: Merge,
    key: IdempotencyKey,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    digest = hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    operation = f"patient.merge:{source_id}"
    db.execute(
        insert(Idempotency)
        .values(tenant_id=user.tenant_id, key=key, operation=operation, request_hash=digest)
        .on_conflict_do_nothing()
    )
    receipt = db.scalar(select(Idempotency).where(Idempotency.key == key).with_for_update())
    if receipt is None:
        raise AppError("idempotency_conflict", 409)
    if receipt.operation != operation or receipt.request_hash != digest:
        raise AppError("idempotency_conflict", 409)
    if receipt.response is not None:
        return receipt.response
    patient_gate(db, user.tenant_id, exclusive=True)
    rows = {i: get(db, Patient, i, True) for i in sorted({source_id, body.destination_id})}
    source, destination = rows[source_id], rows[body.destination_id]
    if source_id == body.destination_id or source.merged_into_id or destination.merged_into_id:
        raise AppError("merge_conflict", 409)
    # Do not create chains by merging an existing canonical destination onward.
    if db.scalar(select(Patient.id).where(Patient.merged_into_id == source_id).limit(1)):
        raise AppError("merge_conflict", 409)
    if source.revision != body.source_revision or destination.revision != body.destination_revision:
        raise AppError("revision_conflict", 409)
    tenant = get(db, Tenant, user.tenant_id)
    day = func.date(func.timezone(tenant.timezone, Appointment.starts_at))
    excessive = db.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(Appointment.patient_id.in_(rows), Appointment.status == "booked")
        .group_by(day)
        .having(func.count() > tenant.daily_limit)
        .limit(1)
    )
    if excessive:
        raise AppError("outside_policy", 409)
    source.merged_into_id = destination.id
    db.flush()  # release source's active identity uniqueness before retained fields change
    for field, value in body.fields.model_dump().items():
        setattr(destination, field, value)
    destination.name_key = body.fields.name.casefold()
    # Ownership changes invalidate identity snapshots even when retained fields match.
    destination.revision += 1
    db.execute(
        update(Appointment)
        .where(Appointment.patient_id == source_id)
        .values(patient_id=destination.id)
    )
    audit(db, user, "patient.merged", source.id)
    db.flush()
    db.refresh(destination)
    receipt.response = PatientDetail.model_validate(destination).model_dump(mode="json")
    db.flush()
    return receipt.response
