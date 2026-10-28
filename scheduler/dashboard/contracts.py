from datetime import date
from uuid import UUID

from pydantic import Field

from scheduler.contracts import ClinicOut, ClinicPatch, Contract, PatientCreate, PatientOut


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
    name_ur: str | None = Field(max_length=120)
    dob: date | None


class Merge(Contract):
    destination_id: UUID
    source_revision: int = Field(ge=1)
    destination_revision: int = Field(ge=1)
    fields: RetainedFields
