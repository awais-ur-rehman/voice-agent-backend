from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, FetchedValue, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Row:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID]


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str]
    timezone: Mapped[str] = mapped_column(default="Asia/Karachi")
    currency: Mapped[str] = mapped_column(default="PKR")
    min_notice_min: Mapped[int] = mapped_column(default=120)
    horizon_days: Mapped[int] = mapped_column(default=90)
    cancellation_min: Mapped[int] = mapped_column(default=1440)
    daily_limit: Mapped[int] = mapped_column(default=3)


class User(Row, Base):
    __tablename__ = "users"
    email: Mapped[str]
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(default="staff")
    active: Mapped[bool] = mapped_column(default=True)


class RefreshToken(Row, Base):
    __tablename__ = "refresh_tokens"
    user_id: Mapped[UUID]
    family_id: Mapped[UUID]
    token_hash: Mapped[str]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Provider(Row, Base):
    __tablename__ = "providers"
    name: Mapped[str]
    specialty: Mapped[str] = mapped_column(default="")
    active: Mapped[bool] = mapped_column(default=True)


class Service(Row, Base):
    __tablename__ = "services"
    name: Mapped[str]
    duration_min: Mapped[int]
    buffer_before_min: Mapped[int] = mapped_column(default=0)
    buffer_after_min: Mapped[int] = mapped_column(default=0)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    active: Mapped[bool] = mapped_column(default=True)


class ProviderService(Base):
    __tablename__ = "provider_services"
    tenant_id: Mapped[UUID]
    provider_id: Mapped[UUID] = mapped_column(primary_key=True)
    service_id: Mapped[UUID] = mapped_column(primary_key=True)


class Schedule(Row, Base):
    __tablename__ = "availability_rules"
    provider_id: Mapped[UUID]
    weekday: Mapped[int]
    start_time: Mapped[time]
    end_time: Mapped[time]
    slot_min: Mapped[int] = mapped_column(default=15)


class ExceptionDay(Row, Base):
    __tablename__ = "availability_exceptions"
    provider_id: Mapped[UUID]
    start_date: Mapped[date]
    end_date: Mapped[date]
    kind: Mapped[str]
    start_time: Mapped[time | None]
    end_time: Mapped[time | None]


class Patient(Row, Base):
    __tablename__ = "patients"
    name: Mapped[str]
    name_key: Mapped[str]
    phone: Mapped[str]
    dob: Mapped[date | None]


class Appointment(Row, Base):
    __tablename__ = "appointments"
    provider_id: Mapped[UUID]
    patient_id: Mapped[UUID]
    service_id: Mapped[UUID]
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    occupied_starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=FetchedValue()
    )
    occupied_ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=FetchedValue()
    )
    duration_min: Mapped[int]
    buffer_before_min: Mapped[int]
    buffer_after_min: Mapped[int]
    status: Mapped[str] = mapped_column(default="booked")
    rescheduled_from_id: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Idempotency(Base):
    __tablename__ = "idempotency_records"
    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    operation: Mapped[str]
    request_hash: Mapped[str]
    response: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Audit(Row, Base):
    __tablename__ = "audit_log"
    actor_id: Mapped[UUID]
    action: Mapped[str]
    entity_id: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
