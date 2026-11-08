"""Scheduling attempts have identities independent of the dialogue checkpoint."""

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session

from analytics.contracts import TaskKind
from analytics.models import CallRecord, TaskRecord

PROGRESSION = {
    "find_patient",
    "verify_patient",
    "prepare_patient",
    "create_patient",
    "get_available_slots",
    "hold_slot",
    "find_appointments",
    "select_appointment",
    "read_back",
}


def start_task(db: Session, call: CallRecord, kind: TaskKind, now: datetime) -> TaskRecord:
    task = TaskRecord(
        tenant_id=call.tenant_id, session_id=call.session_id, kind=kind, started_at=now
    )
    db.add(task)
    db.flush()
    call.active_task_id = task.id
    return task


def reconcile_task(
    db: Session,
    call: CallRecord,
    before: dict,
    after: dict,
    action: dict | None,
    ok: bool,
    now: datetime,
) -> TaskRecord | None:
    active = db.get(TaskRecord, call.active_task_id) if call.active_task_id else None
    if not action or not ok:
        return active
    name, args = action.get("name"), action.get("arguments", {})
    kind = cast(TaskKind, after.get("task", "book"))
    if name == "resume_task":
        parked = list(call.parked_task_ids)
        if active and active.status == "active":
            active.status, active.ended_at = "abandoned", now
        # Only genuine analytics attempts have parked IDs. The dialogue may
        # also park its initial default task, which has not been attempted.
        if parked:
            restored = db.get(TaskRecord, UUID(parked[-1]))
            if (
                restored
                and restored.session_id == call.session_id
                and restored.status == "parked"
                and restored.kind == kind
            ):
                parked.pop()
                restored.status = "active"
                call.active_task_id, call.parked_task_ids = restored.id, parked
                return restored
        return start_task(db, call, kind, now)
    progresses = (
        name in PROGRESSION
        or name == "update_dialogue_state"
        and any(k in args for k in ("task", "date_phrase", "service_id", "provider_id", "subject"))
    )
    if not progresses:
        return active
    if active and active.status == "active" and active.kind == kind:
        return active
    if active and active.status == "active":
        active.status = "parked"
        parked = [*call.parked_task_ids, str(active.id)]
        for discarded in parked[:-3]:
            old = db.get(TaskRecord, UUID(discarded))
            if old:
                old.status, old.ended_at = "abandoned", now
        call.parked_task_ids = parked[-3:]
    return start_task(db, call, kind, now)
