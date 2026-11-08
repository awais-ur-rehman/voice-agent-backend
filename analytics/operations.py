"""Canonical effects, called only inside the domain idempotency execute closure."""

from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from analytics.contracts import Outcome, TaskKind
from analytics.lifecycle import dirty
from analytics.models import OperationEvent, TaskRecord
from analytics.tasks import start_task
from analytics.transcripts import locked_call


def record_operation(db: Session, session_id: UUID, kind: TaskKind, now: datetime) -> None:
    call = locked_call(db, session_id)
    if call is None:
        return  # No retroactive capture for pre-deployment sessions.
    task = db.get(TaskRecord, call.active_task_id) if call.active_task_id else None
    if task is None or task.status != "active" or task.kind != kind:
        task = start_task(db, call, kind, now)
    db.add(
        OperationEvent(
            tenant_id=call.tenant_id,
            session_id=call.session_id,
            event_id=uuid4(),
            operation=kind,
            task_id=task.id,
            occurred_at=now,
        )
    )
    task.status, task.ended_at = "completed", now
    call.active_task_id = None
    # This identity attributes the completing caller turn only. It never grants
    # task completion: capture rechecks the persisted status/session after any
    # savepoint rollback. Later requests receive a fresh observation.
    from analytics.capture import current_observation

    observation = current_observation.get()
    if observation is not None and len(observation.completed_task_ids) < 8:
        observation.completed_task_ids.append(task.id)
    call.outcome = cast(
        Outcome, {"book": "booked", "cancel": "cancelled", "reschedule": "rescheduled"}[kind]
    )
    dirty(db, call, now)
    db.flush()
