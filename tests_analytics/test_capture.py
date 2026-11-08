"""Real command/domain boundaries; reuse the scheduling workflow fixtures."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select

from analytics.models import (
    CallRecord,
    OperationEvent,
    StageMetric,
    TaskRecord,
    Transcript,
    TurnEvent,
)
from brain.sessions import command
from brain.tools import ToolEngine
from scheduler.models import AgentSession
from tests_api.conftest import NOW, SECRET
from tests_brain.conftest import seed  # noqa: F401
from tests_brain.test_tool_workflows import prepare, workflow  # noqa: F401


@pytest.fixture
def captured(workflow):  # noqa: F811
    _, state, db, store, actor = workflow
    parent = db.get(AgentSession, actor.id)
    parent.checkpoint = deepcopy(state)
    db.flush()
    engine = ToolEngine(db, actor, store, NOW)

    def turn(name, args=None, text="", *, key=None, revision=None):
        action = {"name": name, "arguments": args or {}}
        payload = {"action": action, "text": text}
        response, updated = command(
            db,
            actor,
            key or str(uuid4()),
            parent.revision if revision is None else revision,
            payload,
            NOW,
            SECRET,
            lambda db, s: engine.execute(s, action, text),
        )
        state.clear()
        state.update(updated)
        return response

    return turn, state, db, store, actor


def test_capture_default_off_info_does_not_start_initial_book_task(captured):
    turn, _, db, _, actor = captured
    call = db.get(CallRecord, actor.id)
    assert call is not None, "admission must have a durable safe lifecycle mirror"
    assert call.started_at == NOW and call.channel == "text" and call.consent == "off"
    turn("get_clinic_info", {"topic": "hours"}, text="When do you open?")
    assert list(db.scalars(select(TaskRecord))) == []
    assert list(db.scalars(select(Transcript))) == []
    assert len(list(db.scalars(select(TurnEvent).where(TurnEvent.kind == "turn.user")))) == 1
    assert {x.stage for x in db.scalars(select(StageMetric))} == {"command_total", "tool"}
    turn("end_call")
    assert call.outcome == "info_only" and call.end_reason == "caller_end"


def test_actual_operation_and_command_replay_count_once(captured, seed):  # noqa: F811
    turn, state, db, _, actor = captured
    prepare(turn, state, seed)
    turn("book_appointment", text="yes but another day")
    assert list(db.scalars(select(OperationEvent))) == []
    key, revision = str(uuid4()), state["revision"]
    assert turn("confirm", text="yes", key=key, revision=revision)["ok"]
    assert turn("confirm", text="yes", key=key, revision=revision)["ok"]
    ops = list(db.scalars(select(OperationEvent)))
    assert len(ops) == 1 and ops[0].operation == "book"
    tasks = list(db.scalars(select(TaskRecord)))
    assert len(tasks) == 1 and tasks[0].status == "completed"
    assert tasks[0].caller_turns == 2
    assert db.get(CallRecord, actor.id).outcome == "booked"
    assert len(list(db.scalars(select(TurnEvent).where(TurnEvent.tool_status == "failure")))) == 1


def test_new_patient_confirmation_counts_no_appointment(captured):
    turn, _, db, _, _ = captured
    assert turn("find_patient", {"phone": "03007654321"})["ok"]
    assert turn(
        "prepare_patient", {"name": "Demo Child", "name_ur": "ڈیمو بچہ", "phone": "03007654321"}
    )["ok"]
    assert turn("confirm", text="yes")["ok"]
    assert list(db.scalars(select(OperationEvent))) == []
    tasks = list(db.scalars(select(TaskRecord)))
    assert len(tasks) == 1 and tasks[0].status == "active"


def test_outer_rollback_removes_operations_and_task_completion(captured, seed):  # noqa: F811
    turn, state, db, _, _ = captured
    prepare(turn, state, seed)
    with pytest.raises(RuntimeError), db.begin_nested():
        assert turn("confirm", text="yes")["ok"]
        assert len(list(db.scalars(select(OperationEvent)))) == 1
        raise RuntimeError("synthetic rollback")
    assert list(db.scalars(select(OperationEvent))) == []
    assert db.scalar(select(TaskRecord)).status == "active"


def test_park_resume_retains_instance_and_new_intent_starts_next(captured, seed):  # noqa: F811
    turn, state, db, _, actor = captured
    prepare(turn, state, seed)
    call = db.get(CallRecord, actor.id)
    assert call is not None
    original = call.active_task_id
    turn("update_dialogue_state", {"task": "cancel"}, text="cancel")
    assert call.active_task_id != original
    assert call.parked_task_ids == [str(original)]
    turn("resume_task", text="resume")
    assert call.active_task_id == original and call.parked_task_ids == []
    assert db.get(TaskRecord, original).status == "active"


def test_book_reschedule_cancel_are_three_operations_and_three_instances(captured, seed):  # noqa: F811
    turn, state, db, _, actor = captured
    prepare(turn, state, seed)
    assert turn("confirm", text="yes")["ok"]
    turn("update_dialogue_state", {"task": "reschedule"}, text="move it")
    turn("find_appointments")
    turn("select_appointment", {"ref": state["appointments"][0]["ref"]})
    turn("update_dialogue_state", {"date_phrase": "2026-11-04"})
    turn("get_available_slots")
    turn("hold_slot", {"ref": state["offers"][0]["ref"]})
    turn("read_back")
    assert turn("confirm", text="yes")["ok"]
    turn("update_dialogue_state", {"task": "cancel"}, text="cancel it")
    turn("find_appointments")
    turn("select_appointment", {"ref": state["appointments"][0]["ref"]})
    turn("read_back")
    assert turn("confirm", text="yes")["ok"]
    assert sorted(x.operation for x in db.scalars(select(OperationEvent))) == [
        "book",
        "cancel",
        "reschedule",
    ]
    tasks = list(db.scalars(select(TaskRecord)))
    assert len(tasks) == 3 and all(x.status == "completed" for x in tasks)
    assert sum(x.caller_turns for x in tasks) == 5
    assert db.get(CallRecord, actor.id).outcome == "cancelled"


def test_distinct_command_domain_replay_does_not_emit_another_operation(captured, seed):  # noqa: F811
    from datetime import datetime
    from uuid import UUID

    from scheduler.appointments import Book

    turn, state, db, store, actor = captured
    prepare(turn, state, seed)
    before = deepcopy(state)
    assert turn("confirm", text="yes")["ok"]
    body = Book(
        patient_id=UUID(before["subject"]),
        provider_id=UUID(before["selection"]["provider_id"]),
        service_id=UUID(before["selection"]["service_id"]),
        starts_at=datetime.fromisoformat(before["selection"]["starts_at"]),
        confirmed=True,
    )

    def forbidden():
        raise AssertionError("domain replay invoked the mutation closure")

    engine = ToolEngine(db, actor, store, NOW)
    response, _ = command(
        db,
        actor,
        "distinct-domain-replay",
        state["revision"],
        {},
        NOW,
        SECRET,
        lambda db, s: engine._commit(before, body, forbidden),
    )
    assert response["status"] == "booked"
    assert len(list(db.scalars(select(OperationEvent)))) == 1
    assert len(list(db.scalars(select(TaskRecord)))) == 1


def test_default_off_cannot_be_enabled_retroactively(captured):
    from analytics.models import AnalyticsSettings

    turn, _, db, _, actor = captured
    db.get(AnalyticsSettings, actor.tenant_id).capture_enabled = True
    turn("find_patient", {"phone": "03001234567"}, text="Synthetic Patient 03001234567")
    assert db.get(CallRecord, actor.id).consent == "off"
    assert list(db.scalars(select(Transcript))) == []


def test_expiry_preserves_success_and_operation_count(captured, seed):  # noqa: F811
    from datetime import timedelta

    from analytics.lifecycle import finish_session

    turn, state, db, _, actor = captured
    prepare(turn, state, seed)
    assert turn("confirm", text="yes")["ok"]
    finish_session(
        db, db.get(AgentSession, actor.id), reason="timeout", now=NOW + timedelta(minutes=30)
    )
    call = db.get(CallRecord, actor.id)
    assert call.outcome == "booked" and call.end_reason == "timeout"
    assert len(list(db.scalars(select(OperationEvent)))) == 1


def test_no_backfill_for_predeployment_session(captured):
    from sqlalchemy import delete

    turn, _, db, _, actor = captured
    db.execute(delete(CallRecord).where(CallRecord.session_id == actor.id))
    turn("get_clinic_info", {"topic": "hours"}, text="What are your hours?")
    assert db.get(CallRecord, actor.id) is None
    assert list(db.scalars(select(TurnEvent))) == []


def test_identifying_client_command_id_never_enters_safe_analytics(captured):
    turn, _, db, _, _ = captured
    secret = "Synthetic Private Person phone 03001234567"
    turn("get_clinic_info", {"topic": "hours"}, text=secret, key=secret)
    records = list(db.scalars(select(TurnEvent)))
    safe = str(
        [
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in records
        ]
    )
    assert secret not in safe and "03001234567" not in safe
    from uuid import uuid5

    assert all(row.command_id != uuid5(row.session_id, secret) for row in records)
    assert list(db.scalars(select(Transcript))) == []


def test_prompt_order_uses_committed_commands_not_random_ids(captured):
    from uuid import UUID

    turn, _, db, _, actor = captured
    turn("ask_caller", text="hello")
    first = db.scalar(select(TurnEvent).where(TurnEvent.kind == "turn.assistant"))
    first.id = UUID(int=(1 << 128) - 1)
    turn("get_clinic_info", {"topic": "hours"}, text="hours")
    latest = db.scalar(
        select(TurnEvent).where(
            TurnEvent.kind == "turn.assistant",
            TurnEvent.safe_metadata["prompt_id"].astext == "info",
        )
    )
    latest.id = UUID(int=1)
    turn("ask_caller", text="hello again")
    assert "repeated_prompt" not in db.get(CallRecord, actor.id).quality_flags
    turn("ask_caller", text="hello again again")
    assert "repeated_prompt" in db.get(CallRecord, actor.id).quality_flags


def test_text_control_interruptions_are_not_trusted_voice_quality(captured):
    _, state, db, store, actor = captured
    engine = ToolEngine(db, actor, store, NOW)
    for index in range(3):
        command(
            db,
            actor,
            str(uuid4()),
            index,
            {"kind": "delivery_interrupted"},
            NOW,
            SECRET,
            lambda db, s: engine.trusted_event(s, "delivery_interrupted"),
        )
    assert "interruptions" not in db.get(CallRecord, actor.id).quality_flags


@pytest.mark.parametrize("reason", ["caller_end", "staff_end", "timeout"])
def test_empty_session_has_no_completed_goal(captured, reason):
    from datetime import timedelta

    from analytics.lifecycle import finish_session

    _, _, db, _, actor = captured
    finish_session(
        db, db.get(AgentSession, actor.id), reason=reason, now=NOW + timedelta(minutes=30)
    )
    assert db.get(CallRecord, actor.id).outcome == "abandoned"


def test_answered_faq_idle_timeout_is_info_only(captured):
    from datetime import timedelta

    from analytics.lifecycle import finish_session

    turn, _, db, _, actor = captured
    assert turn("get_clinic_info", {"topic": "hours"}, text="hours")["ok"]
    finish_session(
        db, db.get(AgentSession, actor.id), reason="timeout", now=NOW + timedelta(minutes=30)
    )
    assert db.get(CallRecord, actor.id).outcome == "info_only"


def test_concurrent_exact_booking_command_replay_has_one_committed_effect(captured, seed, database):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import text as sql_text
    from sqlalchemy.orm import Session

    from scheduler.models import Appointment
    from tests_analytics.test_privacy import _wait_for_block

    turn, state, db, store, actor = captured
    prepare(turn, state, seed)
    revision = state["revision"]
    bind = db.get_bind()
    db.commit()
    entered, second_ready, release = Event(), Event(), Event()
    pids = {}
    key = str(uuid4())
    action = {"name": "confirm", "arguments": {}}
    payload = {"action": action, "text": "yes"}

    def run(which):
        with Session(bind) as other, other.begin():
            pids[which] = other.scalar(sql_text("SELECT pg_backend_pid()"))
            if which == "second":
                second_ready.set()

            def execute(db, s):
                assert which == "first", "exact replay reran the tool"
                entered.set()
                assert release.wait(5)
                return ToolEngine(db, actor, store, NOW).execute(s, action, "yes")

            return command(other, actor, key, revision, payload, NOW, SECRET, execute)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, "first")
        try:
            assert entered.wait(3)
            second = pool.submit(run, "second")
            assert second_ready.wait(3)
            with Session(database) as observer:
                _wait_for_block(observer, pids["second"], pids["first"])
        finally:
            release.set()
        first_response, first_state = first.result(timeout=5)
        second_response, second_state = second.result(timeout=5)
        assert first_response == second_response
        assert first_state["revision"] == second_state["revision"]
    with Session(database) as check:
        assert len(list(check.scalars(select(Appointment)))) == 1
        assert len(list(check.scalars(select(OperationEvent)))) == 1
        assert (
            len(
                list(
                    check.scalars(
                        select(TurnEvent).where(
                            TurnEvent.tool_name == "confirm", TurnEvent.kind == "tool.result"
                        )
                    )
                )
            )
            == 1
        )


@pytest.mark.parametrize("finish_first", [True, False])
def test_readback_delivery_requires_real_complete_ledger_and_dedupes(captured, seed, finish_first):  # noqa: F811
    from datetime import timedelta

    import analytics.capture as capture
    from brain.voice_contracts import PlaybackAck
    from brain.voice_playback import acknowledge, finish_delivery, publish_segment

    assert hasattr(capture, "record_delivery"), (
        "trusted complete-ledger delivery capture is missing"
    )
    turn, state, db, _, actor = captured
    prepare(turn, state, seed)
    row = db.get(AgentSession, actor.id)
    ledger = publish_segment(
        None, state, 1, "synthetic", "segment", 24000, 24000, NOW, NOW + timedelta(seconds=120)
    )
    capture.record_delivery(db, row, ledger, NOW)
    assert list(db.scalars(select(TurnEvent).where(TurnEvent.kind == "metric.turn"))) == []

    def ack():
        acknowledge(
            ledger,
            PlaybackAck(
                version=1,
                session_id=row.id,
                generation=1,
                sequence=1,
                segment_id="segment",
                played_samples=24000,
            ),
            "synthetic",
            NOW + timedelta(seconds=1),
        )

    if finish_first:
        finish_delivery(ledger, NOW)
    else:
        ack()
    capture.record_delivery(db, row, ledger, NOW + timedelta(seconds=1))
    assert list(db.scalars(select(TurnEvent).where(TurnEvent.kind == "metric.turn"))) == []
    if finish_first:
        ack()
    else:
        finish_delivery(ledger, NOW + timedelta(seconds=1))
    for _ in range(2):
        capture.record_delivery(db, row, ledger, NOW + timedelta(seconds=1))
    deliveries = list(db.scalars(select(TurnEvent).where(TurnEvent.kind == "metric.turn")))
    assert len(deliveries) == 1 and deliveries[0].safe_metadata["delivery"] == "delivered"
    generated = db.scalar(
        select(TurnEvent).where(
            TurnEvent.kind == "turn.assistant", TurnEvent.command_id == deliveries[0].command_id
        )
    )
    assert generated.safe_metadata["delivery"] == "generated"
    assert {x.stage for x in db.scalars(select(StageMetric))} == {"command_total", "tool"}


def test_completed_task_does_not_absorb_later_info_turns(captured, seed):  # noqa: F811
    turn, state, db, _, actor = captured
    prepare(turn, state, seed)
    assert turn("confirm", text="yes")["ok"]
    completed = db.scalar(select(TaskRecord))
    assert completed.status == "completed" and completed.caller_turns == 1
    assert turn("get_clinic_info", {"topic": "hours"}, text="Thanks, when do you open?")["ok"]
    assert completed.caller_turns == 1
    assert db.get(CallRecord, actor.id).active_task_id is None
    assert turn("update_dialogue_state", {"task": "book"}, text="another booking")["ok"]
    assert len(list(db.scalars(select(TaskRecord)))) == 2
