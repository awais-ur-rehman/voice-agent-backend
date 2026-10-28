from datetime import date, time
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from scheduler.appointments import AppointmentOut
from scheduler.contracts import (
    ClinicOut,
    ClinicPatch,
    Contract,
    ExceptionOut,
    Name,
    PatientCreate,
    PatientOut,
    RuleOut,
)


class ClinicProfile(ClinicOut):
    revision: int
    address_en: str
    address_ur: str
    preparation_en: str
    preparation_ur: str


class ProfilePatch(ClinicPatch):
    revision: int = Field(ge=1)
    address_en: str | None = Field(default=None, max_length=2000)
    address_ur: str | None = Field(default=None, max_length=2000)
    preparation_en: str | None = Field(default=None, max_length=4000)
    preparation_ur: str | None = Field(default=None, max_length=4000)
    confirm_timezone_change: bool = False


class Setup(Contract):
    profile: bool
    providers: bool
    services: bool
    hours: bool
    agent: bool


class PatientDetail(PatientOut):
    revision: int
    merged_into_id: UUID | None


class PatientPage(Contract):
    items: list[PatientDetail]
    total: int
    limit: int
    offset: int


class RetainedFields(PatientCreate):
    name_ur: Name | None
    dob: date | None


class Merge(Contract):
    destination_id: UUID
    source_revision: int = Field(ge=1)
    destination_revision: int = Field(ge=1)
    fields: RetainedFields


class ProviderServiceItem(Contract):
    provider_id: UUID
    service_id: UUID


class ProviderServicePage(Contract):
    items: list[ProviderServiceItem]
    total: int
    limit: int
    offset: int


class CalendarAppointment(AppointmentOut):
    patient_name: str
    patient_name_ur: str | None
    provider_name: str
    provider_name_ur: str | None
    service_name: str
    service_name_ur: str | None


class AppointmentPage(Contract):
    items: list[CalendarAppointment]
    total: int
    limit: int
    offset: int


class Availability(Contract):
    revision: str
    rules: list[RuleOut]
    exceptions: list[ExceptionOut]


class AvailabilityTemplate(Contract):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    template: Literal["friday_prayer", "ramadan"]
    break_start: time | None = None
    break_end: time | None = None
    start_date: date | None = None
    end_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None

    @model_validator(mode="after")
    def validate_template(self):
        start: time | None
        end: time | None
        if self.template == "friday_prayer":
            if any(
                v is not None
                for v in (self.start_date, self.end_date, self.start_time, self.end_time)
            ):
                raise ValueError("Friday prayer accepts break times only")
            start, end = self.break_start or time(12, 45), self.break_end or time(14, 30)
        else:
            if self.break_start is not None or self.break_end is not None:
                raise ValueError("Ramadan accepts dates and custom hours only")
            if self.start_date is None or self.end_date is None or self.end_date < self.start_date:
                raise ValueError("Ramadan requires an ordered date range")
            start, end = self.start_time, self.end_time
        if start is None or end is None or start.tzinfo or end.tzinfo or start >= end:
            raise ValueError("Use ordered local wall-clock times")
        return self


class AvailabilityChanges(Contract):
    removed_rule_ids: list[UUID]
    added_rules: list[RuleOut]
    added_exceptions: list[ExceptionOut]


class AvailabilityPreview(Contract):
    revision: str
    changes: AvailabilityChanges
    after: Availability
