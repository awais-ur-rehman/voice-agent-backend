"""Planner-visible commands. Authorization and confirmation are never arguments."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from scheduler.contracts import Contract, Name


class Empty(Contract):
    pass


class Fields(Contract):
    task: Literal["book", "cancel", "reschedule"] | None = None
    date_phrase: str | None = Field(default=None, max_length=120)
    provider_id: UUID | None = None
    service_id: UUID | None = None
    subject: Literal["self", "other"] | None = None


class Lookup(Contract):
    phone: str = Field(min_length=5, max_length=40)


class Verify(Contract):
    factor: str = Field(min_length=1, max_length=120)


class NewPatient(Contract):
    name: Name
    name_ur: Name | None = None
    phone: str = Field(min_length=5, max_length=40)


class Reference(Contract):
    ref: str = Field(min_length=1, max_length=80)


class Info(Contract):
    topic: Literal[
        "fees", "hours", "providers", "services", "policy", "location", "preparation"
    ] = "services"


class Handoff(Contract):
    reason: Literal["requested", "verification_failed", "technical", "emergency"] = "requested"


ARGUMENTS: dict[str, type[Contract]] = {
    "update_dialogue_state": Fields,
    "find_patient": Lookup,
    "set_caller_contact": Lookup,
    "verify_patient": Verify,
    "prepare_patient": NewPatient,
    "create_patient": Empty,
    "get_clinic_info": Info,
    "search_knowledge": Empty,
    "get_available_slots": Empty,
    "hold_slot": Reference,
    "find_appointments": Empty,
    "select_appointment": Reference,
    "read_back": Empty,
    "book_appointment": Empty,
    "cancel_appointment": Empty,
    "reschedule_appointment": Empty,
    "confirm": Empty,
    "ask_caller": Empty,
    "resume_task": Empty,
    "request_handoff": Handoff,
    "end_call": Empty,
}


class Action(Contract):
    name: Literal[
        "update_dialogue_state",
        "find_patient",
        "set_caller_contact",
        "verify_patient",
        "prepare_patient",
        "create_patient",
        "get_clinic_info",
        "search_knowledge",
        "get_available_slots",
        "hold_slot",
        "find_appointments",
        "select_appointment",
        "read_back",
        "book_appointment",
        "cancel_appointment",
        "reschedule_appointment",
        "confirm",
        "ask_caller",
        "resume_task",
        "request_handoff",
        "end_call",
    ]
    arguments: dict = Field(default_factory=dict)


def parse_action(value: dict) -> Action:
    action = Action.model_validate(value)
    action.arguments = (
        ARGUMENTS[action.name]
        .model_validate(action.arguments)
        .model_dump(mode="json", exclude_none=True)
    )
    return action
