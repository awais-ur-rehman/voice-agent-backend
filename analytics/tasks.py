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
    def owned(task_id: UUID | str | None) -> TaskRecord | None:
        task = db.get(TaskRecord, UUID(str(task_id))) if task_id else None
        return (
            task
            if task and task.tenant_id == call.tenant_id and task.session_id == call.session_id
            else None
        )

    def abandon(task_id: UUID | str | None) -> None:
        task = owned(task_id)
        if task and task.status in {"active", "parked"}:
            task.status, task.ended_at = "abandoned", now

    active = owned(call.active_task_id)
    if not action or not ok:
        return active
    name, args = action.get("name"), action.get("arguments", {})
    kind = cast(TaskKind, after.get("task", "book"))
    parked = list(call.parked_task_ids)
    depth = len(before.get("parked", []))
    if len(parked) != depth:
        # An unknown association must never steal a different frame's attempt
        # or block the authorized core operation. Do not reconstruct history.
        for task_id in parked:
            abandon(task_id)
        parked = [None] * depth
    if name == "resume_task":
        abandon(call.active_task_id)
        call.active_task_id = None
        task_id = parked.pop() if parked else None
        restored = owned(task_id)
        call.parked_task_ids = parked
        if restored and restored.status == "parked" and restored.kind == kind:
            restored.status = "active"
            call.active_task_id = restored.id
            return restored
        abandon(task_id)
        return None
    if name == "update_dialogue_state" and before.get("task", "book") != kind:
        # Core pushes every old frame, including defaults and completed tasks.
        # Null preserves that position without retaining any checkpoint data.
        slot = None
        if active and active.status == "active":
            active.status = "parked"
            slot = str(active.id)
        parked.append(slot)
        kept = len(after.get("parked", []))
        for task_id in parked[: len(parked) - kept]:
            abandon(task_id)
        call.parked_task_ids = parked[-kept:] if kept else []
        call.active_task_id = None
        return start_task(db, call, kind, now)
    call.parked_task_ids = parked
    progresses = (
        name in PROGRESSION
        or name == "update_dialogue_state"
        and any(k in args for k in ("task", "date_phrase", "service_id", "provider_id", "subject"))
    )
    if not progresses:
        return active
    if active and active.status == "active" and active.kind == kind:
        return active
    abandon(call.active_task_id)
    return start_task(db, call, kind, now)
