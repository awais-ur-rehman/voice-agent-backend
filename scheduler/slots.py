from datetime import date, datetime, timedelta
from uuid import UUID

from pydantic import AwareDatetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from .contracts import Contract
from .domain import DateException, Reservation, ScheduleRule, candidate_slots
from .errors import AppError
from .models import Appointment, ExceptionDay, Provider, ProviderService, Schedule, Service, Tenant


class Slot(Contract):
    provider_id: UUID
    service_id: UUID
    starts_at: AwareDatetime
    ends_at: AwareDatetime


def get(db: Session, model, identity, lock=False):
    query = select(model).where(model.id == identity)
    row = db.scalar(
        query.with_for_update().execution_options(populate_existing=True) if lock else query
    )
    if row is None:
        raise AppError("not_found", 404)
    return row


def provider_slots(
    db: Session,
    tenant: Tenant,
    provider: Provider,
    service: Service,
    day: date,
    now: datetime,
    exclude: UUID | None = None,
) -> list[Slot]:
    if not provider.active or not service.active:
        return []
    if db.get(ProviderService, (provider.id, service.id)) is None:
        return []
    rules = db.scalars(select(Schedule).where(Schedule.provider_id == provider.id)).all()
    exceptions = db.scalars(
        select(ExceptionDay).where(
            ExceptionDay.provider_id == provider.id,
            ExceptionDay.start_date <= day,
            ExceptionDay.end_date >= day,
        )
    ).all()
    # Query a UTC envelope wide enough for every IANA offset; narrow candidates in domain code.
    from datetime import UTC

    lower = datetime.combine(day, datetime.min.time(), UTC) - timedelta(days=1)
    upper = lower + timedelta(days=3)
    query = select(Appointment).where(
        Appointment.provider_id == provider.id,
        Appointment.status == "booked",
        Appointment.occupied_starts_at < upper,
        Appointment.occupied_ends_at > lower,
    )
    if exclude is not None:
        query = query.where(Appointment.id != exclude)
    busy = [Reservation(a.occupied_starts_at, a.occupied_ends_at) for a in db.scalars(query)]
    starts = candidate_slots(
        day=day,
        timezone=tenant.timezone,
        rules=[ScheduleRule(r.weekday, r.start_time, r.end_time, r.slot_min) for r in rules],
        exceptions=[
            DateException(e.start_date, e.end_date, e.kind, e.start_time, e.end_time)
            for e in exceptions
        ],
        duration=service.duration_min,
        before=service.buffer_before_min,
        after=service.buffer_after_min,
        busy=busy,
        now=now,
        notice=tenant.min_notice_min,
        horizon=tenant.horizon_days,
    )
    return [
        Slot(
            provider_id=provider.id,
            service_id=service.id,
            starts_at=start,
            ends_at=start + timedelta(minutes=service.duration_min),
        )
        for start in starts
    ]


def search(
    db: Session,
    tenant: Tenant,
    service_id: UUID,
    first: date,
    last: date,
    now: datetime,
    provider_id: UUID | None = None,
    limit: int = 100,
) -> list[Slot]:
    if last < first or (last - first).days > 30:
        raise AppError("validation_error", 422, "Search at most 31 days")
    service = get(db, Service, service_id)
    providers = (
        [get(db, Provider, provider_id)]
        if provider_id
        else list(db.scalars(select(Provider).where(Provider.active.is_(True))))
    )
    result = []
    day = first
    while day <= last:
        for provider in providers:
            result.extend(provider_slots(db, tenant, provider, service, day, now))
        day += timedelta(days=1)
    return sorted(result, key=lambda slot: (slot.starts_at, str(slot.provider_id)))[:limit]
