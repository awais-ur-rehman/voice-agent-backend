"""Committed safe events and request-local observations; no raw payload persistence."""

import hashlib
import hmac
from contextvars import ContextVar
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from analytics.contracts import Language, PromptId, SafeMetadata, ToolName, TurnObservation
from analytics.lifecycle import dirty, finish_session, sync_session
from analytics.models import StageMetric, TaskRecord, TurnEvent
from analytics.privacy import identity_values
from analytics.tasks import reconcile_task
from analytics.transcripts import append_text, locked_call
from scheduler.models import AgentSession, CommandReceipt

current_observation: ContextVar[TurnObservation | None] = ContextVar(
    "analytics_observation", default=None
)


def prompt_id(after: dict, action: dict | None, result: dict) -> PromptId:
    from brain.templates import MESSAGES

    says = result.get("say", {})
    if after.get("handoff"):
        return "handoff"
    if after.get("ended"):
        return "end"
    if after.get("readback") and says == after["readback"].get("say"):
        return "readback"
    if action and action.get("name") in {"get_clinic_info", "search_knowledge"}:
        return "info"
    mapping: dict[str, PromptId] = {
        "phone": "ask_patient",
        "new": "ask_patient",
        "factor": "ask_verification",
        "service": "ask_need",
        "date": "ask_date",
        "search_ready": "ask_slot",
        "choose_time": "ask_slot",
        "find_ready": "ask_appointment",
        "choose_appointment": "ask_appointment",
    }
    for key, category in mapping.items():
        if any(
            says.get(lang, "").endswith(MESSAGES[key][i]) for i, lang in enumerate(("en", "ur"))
        ):
            return category
    return "other"


def progressed(before: dict, after: dict) -> bool:
    # Ephemeral equality only; identifying values never leave this stack frame.
    keys = (
        "task",
        "stage",
        "verified",
        "subject",
        "fields",
        "candidates",
        "pending_patient",
        "appointment",
        "appointments",
        "selection",
        "hold",
        "offers",
        "parked",
        "ended",
        "handoff",
    )
    return any(before.get(key) != after.get(key) for key in keys)


def record_command(
    db: Session,
    row: AgentSession,
    *,
    command_id: str,
    before: dict,
    after: dict,
    action: dict | None,
    result: dict,
    observation: TurnObservation,
    now: datetime,
) -> None:
    call = locked_call(db, row.id)
    if call is None:
        return
    # command_id is the opaque server HMAC-derived identity supplied by command().
    # Never hash low-entropy client IDs with a public namespace.
    command_uuid = UUID(command_id)
    lang = cast(Language, after.get("lang", "unknown"))
    task = reconcile_task(db, call, before, after, action, bool(result.get("ok")), now)
    if task is None and observation.completed_task_ids:
        completed = db.get(TaskRecord, observation.completed_task_ids[-1])
        if (
            completed is not None
            and completed.session_id == row.id
            and completed.status == "completed"
        ):
            task = completed
    task_id = task.id if task else None
    metadata = observation.metadata.model_dump(mode="json", exclude_none=True)
    if metadata.get("stt_confidence", 1) < 0.5:
        call.quality_flags = sorted(set(call.quality_flags) | {"low_stt_confidence"})

    def event(kind, **values):
        event_id = uuid5(command_uuid, kind)
        db.add(
            TurnEvent(
                tenant_id=row.tenant_id,
                session_id=row.id,
                event_id=event_id,
                command_id=command_uuid,
                task_id=task_id,
                kind=kind,
                language=lang,
                safe_metadata=values.pop("safe_metadata", {}),
                occurred_at=now,
                **values,
            )
        )
        return event_id

    user_event = None
    if observation.input_text.strip() and observation.control is None:
        user_event = event("turn.user", safe_metadata=metadata | {"accepted": True})
        if task:
            task.caller_turns += 1
    if action:
        name = cast(ToolName, action["name"])
        event("tool.called", tool_name=name)
        event(
            "tool.result",
            tool_name=name,
            tool_status="success" if result.get("ok") else "failure",
            safe_metadata={} if result.get("ok") else {"error_code": "tool_failed"},
        )
    prompt = (
        "consent_clarification"
        if observation.suppress_text and not action and call.consent == "pending"
        else "consent"
        if observation.suppress_text and not action
        else prompt_id(after, action, result)
    )
    progress = progressed(before, after)
    previous_key = db.scalar(
        select(CommandReceipt.request_digest).where(
            CommandReceipt.session_id == row.id, CommandReceipt.revision == row.revision - 1
        )
    )
    previous = (
        db.scalar(
            select(TurnEvent).where(
                TurnEvent.session_id == row.id,
                TurnEvent.event_id == uuid5(UUID(hex=previous_key[:32]), "turn.assistant"),
            )
        )
        if previous_key
        else None
    )
    eligible = prompt.startswith("ask_") or prompt == "readback"
    repeated = bool(
        eligible
        and not progress
        and previous
        and previous.safe_metadata.get("prompt_id") == prompt
        and not previous.safe_metadata.get("progressed")
    )
    delivery = "interrupted" if observation.control == "delivery_interrupted" else "generated"
    assistant_meta = {
        "prompt_id": prompt,
        "progressed": progress,
        "repeated_prompt": repeated,
        "delivery": delivery,
    }
    assistant_meta["trusted"] = observation.metadata.trusted is True
    if (
        observation.control == "upstream_timeout"
        or observation.metadata.error_code == "provider_unavailable"
    ):
        assistant_meta["error_code"] = "provider_unavailable"
    assistant = event("turn.assistant", safe_metadata=assistant_meta)
    if repeated:
        call.quality_flags = sorted(set(call.quality_flags) | {"repeated_prompt"})
    if observation.control == "delivery_interrupted" and observation.metadata.trusted is True:
        # The route enforces current worker authority for voice sessions.
        interruptions = list(
            db.scalars(
                select(TurnEvent.id)
                .where(
                    TurnEvent.session_id == row.id,
                    TurnEvent.kind == "turn.assistant",
                    TurnEvent.safe_metadata["delivery"].astext == "interrupted",
                    TurnEvent.safe_metadata["trusted"].astext == "true",
                )
                .limit(3)
            )
        )
        if len(interruptions) >= 3:
            call.quality_flags = sorted(set(call.quality_flags) | {"interruptions"})
    if call.language != lang:
        event("lang.switched")
    call.language = lang
    for sample in observation.stages:
        db.execute(
            insert(StageMetric)
            .values(
                id=uuid4(),
                tenant_id=row.tenant_id,
                session_id=row.id,
                event_id=sample.event_id,
                turn_event_id=sample.turn_event_id,
                stage=sample.stage,
                language=sample.language,
                origin=sample.origin,
                duration_ms=sample.duration_ms,
                observed_at=sample.observed_at or now,
            )
            .on_conflict_do_nothing()
        )
    if (
        not observation.suppress_text
        and call.capture_enabled_at_start
        and call.consent == "accepted"
        and call.transcript_deleted_at is None
    ):
        from brain.knowledge import identity_literals

        literals = (
            observation.identity_literals
            + identity_literals(db, before)
            + identity_literals(db, after)
            + identity_values(before)
            + identity_values(after)
            + identity_values(action)
        )
        if user_event:
            append_text(
                db,
                row.id,
                speaker="user",
                value=observation.input_text,
                literals=literals,
                event_id=user_event,
                occurred_at=now,
                now=now,
            )
        say = result.get("say", {}).get(lang, "")
        if say and observation.control is None:
            append_text(
                db,
                row.id,
                speaker="assistant",
                value=say,
                literals=literals,
                event_id=assistant,
                occurred_at=now,
                now=now,
            )
    if after.get("handoff") and not before.get("handoff"):
        event("handoff.requested")
        finish_session(db, row, reason="handoff", now=now)
    elif after.get("ended") or after.get("stage") == "ended":
        finish_session(db, row, reason="caller_end", now=now)
    else:
        sync_session(db, row, now)
    dirty(db, call, now)
    db.flush()


def rejected_audio(
    db: Session,
    row: AgentSession,
    command_id: str,
    metadata: SafeMetadata,
    now: datetime,
    *,
    secret: str,
) -> None:
    """Safe deduped rejection; never a caller turn, tool, transcript or planner call."""
    call = locked_call(db, row.id)
    if call is None:
        return
    db.execute(
        insert(TurnEvent)
        .values(
            id=uuid4(),
            tenant_id=row.tenant_id,
            session_id=row.id,
            event_id=UUID(
                hex=hmac.new(
                    secret.encode(),
                    b"analytics:rejected-audio:v1:" + row.id.bytes + command_id.encode(),
                    hashlib.sha256,
                ).hexdigest()[:32]
            ),
            kind="metric.turn",
            language="unknown",
            safe_metadata=metadata.model_dump(mode="json", exclude_none=True),
            occurred_at=now,
        )
        .on_conflict_do_nothing()
    )
    if metadata.stt_confidence is not None and metadata.stt_confidence < 0.5:
        call.quality_flags = sorted(set(call.quality_flags) | {"low_stt_confidence"})
    dirty(db, call, now)


def record_delivery(db: Session, row: AgentSession, ledger: dict, now: datetime) -> None:
    """Existing validated readback playback completion, not proof of hearing.

    Only callers after acknowledge/finish_delivery may invoke this. A control
    event points to the original generated command; no extra assistant turn,
    prompt denominator, text, audio or inferred latency sample is created.
    """
    if not ledger.get("completed_at"):
        return
    call = locked_call(db, row.id)
    if call is None:
        return
    digest = db.scalar(
        select(CommandReceipt.request_digest).where(
            CommandReceipt.session_id == row.id,
            CommandReceipt.revision == ledger["readback_revision"],
        )
    )
    if digest is None:
        return
    command_uuid = UUID(hex=digest[:32])
    original = db.scalar(
        select(TurnEvent).where(
            TurnEvent.session_id == row.id,
            TurnEvent.command_id == command_uuid,
            TurnEvent.kind == "turn.assistant",
        )
    )
    if original is None:
        return
    inserted = db.scalar(
        insert(TurnEvent)
        .values(
            id=uuid4(),
            tenant_id=row.tenant_id,
            session_id=row.id,
            event_id=uuid5(command_uuid, f"delivery:readback:{ledger['generation']}"),
            command_id=command_uuid,
            task_id=original.task_id,
            kind="metric.turn",
            language=original.language,
            safe_metadata={
                "delivery": "delivered",
                "output_state": "delivered",
                "trusted": True,
                "prompt_id": "readback",
            },
            occurred_at=now,
        )
        .on_conflict_do_nothing()
        .returning(TurnEvent.id)
    )
    if inserted is not None:
        dirty(db, call, now)
