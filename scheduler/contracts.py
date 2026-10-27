from datetime import date, time
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class Patch(Contract):
    @model_validator(mode="before")
    @classmethod
    def reject_null(cls, values):
        if isinstance(values, dict) and any(v is None and k != "dob" for k, v in values.items()):
            raise ValueError("Fields cannot be null")
        return values


class ClinicPatch(Patch):
    name: Name | None = None
    timezone: str | None = None
    min_notice_min: int | None = Field(default=None, ge=0, le=43200)
    horizon_days: int | None = Field(default=None, ge=1, le=365)
    cancellation_min: int | None = Field(default=None, ge=0, le=43200)
    daily_limit: int | None = Field(default=None, ge=1, le=100)

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError("Unknown IANA timezone") from None
        return value


class ClinicOut(Contract):
    id: UUID
    name: str
    timezone: str
    currency: str
    min_notice_min: int
    horizon_days: int
    cancellation_min: int
    daily_limit: int


class UserCreate(Contract):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    role: Literal["admin", "staff"] = "staff"


class ProviderCreate(Contract):
    name: Name
    specialty: str = Field(default="", max_length=120)


class ProviderPatch(Patch):
    name: Name | None = None
    specialty: str | None = Field(default=None, max_length=120)
    active: bool | None = None


class ProviderOut(ProviderCreate):
    id: UUID
    active: bool


class ServiceCreate(Contract):
    name: Name
    duration_min: int = Field(ge=1, le=480)
    buffer_before_min: int = Field(default=0, ge=0, le=120)
    buffer_after_min: int = Field(default=0, ge=0, le=120)
    price: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)


class ServicePatch(Patch):
    name: Name | None = None
    duration_min: int | None = Field(default=None, ge=1, le=480)
    buffer_before_min: int | None = Field(default=None, ge=0, le=120)
    buffer_after_min: int | None = Field(default=None, ge=0, le=120)
    price: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    active: bool | None = None


class ServiceOut(ServiceCreate):
    id: UUID
    active: bool


class RuleCreate(Contract):
    provider_id: UUID
    weekday: int = Field(ge=1, le=7)
    start_time: time
    end_time: time
    slot_min: int = Field(default=15, ge=5, le=240)

    @model_validator(mode="after")
    def times(self):
        if self.start_time.tzinfo or self.end_time.tzinfo or self.start_time >= self.end_time:
            raise ValueError("Use ordered local wall-clock times")
        return self


class RuleOut(RuleCreate):
    id: UUID


class ExceptionCreate(Contract):
    provider_id: UUID
    start_date: date
    end_date: date
    kind: Literal["closed", "custom"]
    start_time: time | None = None
    end_time: time | None = None

    @model_validator(mode="after")
    def dates(self):
        if self.end_date < self.start_date:
            raise ValueError("Invalid date range")
        if self.kind == "closed" and (self.start_time is not None or self.end_time is not None):
            raise ValueError("Closed dates cannot have hours")
        if self.kind == "custom" and (
            self.start_time is None
            or self.end_time is None
            or self.start_time.tzinfo
            or self.end_time.tzinfo
            or self.start_time >= self.end_time
        ):
            raise ValueError("Custom hours require ordered local times")
        return self


class ExceptionOut(ExceptionCreate):
    id: UUID


class PatientCreate(Contract):
    name: Name
    phone: str = Field(min_length=5, max_length=40)
    dob: date | None = None

    @field_validator("name")
    @classmethod
    def normal_name(cls, value):
        return " ".join(value.split())

    @field_validator("phone")
    @classmethod
    def phone_number(cls, value):
        try:
            parsed = phonenumbers.parse(value, "PK")
        except phonenumbers.NumberParseException:
            raise ValueError("Invalid phone") from None
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError("Invalid phone")
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


class PatientOut(PatientCreate):
    id: UUID
