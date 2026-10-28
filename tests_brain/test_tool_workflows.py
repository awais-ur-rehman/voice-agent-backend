"""Executed tool turns against the real scheduling database and Valkey."""

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scheduler.db import tenant_context
from scheduler.models import Appointment, Patient
from tests_api.conftest import APP_URL, NOW, SECRET


@pytest.fixture
def workflow(seed, database):
    from valkey import Valkey

    from brain.store import StateStore
    from brain.tools import ToolEngine

    store = StateStore(
        Valkey.from_url(
            "redis://:phase2_valkey_test_only@127.0.0.1:56379/0", decode_responses=True
        ),
        prefix="workflow-" + str(uuid4()),
    )
    from brain.security import RuntimeActor

    actor = RuntimeActor(uuid4(), seed["tenant_a"], seed["agent_a"], seed["version_a"])
    state = {
        "revision": 0,
        "task": "book",
        "stage": "identify",
        "verified": False,
        "attempts": 0,
        "fields": {},
        "offers": [],
        "history": [],
        "summary": {},
        "lang": "en",
    }
    with Session(create_engine(APP_URL, hide_parameters=True)) as db, db.begin():
        tenant_context(db, actor.tenant_id)
        from brain.sessions import create_session
        from scheduler.config import Settings
        from scheduler.db import Principal

        created = create_session(
            db,
            Principal(seed["owner_a"], seed["tenant_a"], "owner-a@example.com", "owner"),
            seed["agent_a"],
            NOW,
            Settings(
                database_url=APP_URL,
                jwt_secret=SECRET,
                runtime_secret="different-runtime-test-secret-32-characters",
                _env_file=None,
            ),
        )
        actor = RuntimeActor(
            UUID(created["session_id"]), seed["tenant_a"], seed["agent_a"], seed["version_a"]
        )
        engine = ToolEngine(db, actor, store, NOW)

        def turn(name, args=None, text=""):
            result = engine.execute(state, {"name": name, "arguments": args or {}}, text)
            state["revision"] += 1
            return result

        yield turn, state, db, store, actor


def identify(turn):
    r = turn("find_patient", {"phone": "03001234567"})
    assert r["ok"] and "Synthetic" not in json.dumps(r)
    r = turn("verify_patient", {"factor": "Synthetic Patient"})
    assert r["ok"], r


def prepare(turn, state, seed):
    identify(turn)
    assert turn(
        "update_dialogue_state",
        {
            "service_id": str(seed["service_a"]),
            "provider_id": str(seed["provider_a"]),
            "date_phrase": "2026-11-03",
        },
    )["ok"]
    assert turn("get_available_slots")["ok"]
    assert turn("hold_slot", {"ref": state["offers"][0]["ref"]})["ok"]
    assert turn("read_back")["ok"]


def test_booking_confirmation_replay_and_code_readback(workflow, seed):
    turn, state, db, store, actor = workflow
    prepare(turn, state, seed)
    readback = state["readback"]
    assert not turn("book_appointment", text="yes but another day")["ok"]
    assert db.scalar(select(Appointment)) is None
    assert state["readback"] == readback
    result = turn("book_appointment", text="yes")
    assert result["ok"], result
    assert db.scalar(select(Appointment)).patient_id == seed["patient_a"]
    assert not turn("book_appointment", text="yes")["ok"]
    assert len(list(db.scalars(select(Appointment)))) == 1


def test_verification_exhaustion_is_not_reset_by_task_or_subject(workflow):
    turn, state, *_ = workflow
    turn("find_patient", {"phone": "03001234567"})
    for _ in range(2):
        assert not turn("verify_patient", {"factor": "wrong"})["ok"]
    turn("update_dialogue_state", {"task": "cancel", "subject": "other"})
    turn("find_patient", {"phone": "03001234567"})
    assert not turn("verify_patient", {"factor": "wrong"})["ok"]
    assert state["attempts"] == 3 and state["handoff"]
    assert not turn("verify_patient", {"factor": "Synthetic Patient"})["ok"]
    assert "wrong" not in json.dumps(state)


def test_correction_releases_hold_and_invalidates_readback(workflow, seed):
    turn, state, *_ = workflow
    prepare(turn, state, seed)
    assert turn("update_dialogue_state", {"date_phrase": "2026-11-04"})["ok"]
    assert not state.get("hold") and not state.get("readback")
    assert not turn("book_appointment", text="yes")["ok"]


def test_private_tools_require_verified_exact_subject(workflow):
    turn, state, *_ = workflow
    assert not turn("find_appointments")["ok"]
    identify(turn)
    assert turn("update_dialogue_state", {"task": "cancel", "subject": "other"})["ok"]
    assert not state["verified"]
    assert not turn("find_appointments")["ok"]


def test_new_patient_needs_separate_name_phone_readback(workflow):
    turn, state, db, *_ = workflow
    assert turn("find_patient", {"phone": "03007654321"})["ok"]
    assert not turn("create_patient", text="yes")["ok"]
    assert turn(
        "prepare_patient", {"name": "Demo Child", "name_ur": "ڈیمو بچہ", "phone": "03007654321"}
    )["ok"]
    assert not turn("create_patient", text="maybe")["ok"]
    assert turn("create_patient", text="yes")["ok"]
    assert db.scalar(select(Patient).where(Patient.phone == "+923007654321")) is not None
    assert state["verified"]


def test_park_resume_and_fact_digression_preserve_known_fields(workflow, seed):
    turn, state, *_ = workflow
    prepare(turn, state, seed)
    old_fields = state["fields"].copy()
    assert turn("get_clinic_info", {"topic": "fees"})["ok"]
    assert state["fields"] == old_fields and state["readback"]
    assert turn("update_dialogue_state", {"task": "cancel"})["ok"]
    assert state["parked"] and not state.get("hold") and not state.get("readback")
    assert turn("resume_task")["ok"]
    assert state["task"] == "book" and state["fields"] == old_fields
    assert not state.get("hold") and not state.get("readback")


def test_failed_new_patient_correction_cannot_confirm_previous_name(workflow):
    turn, state, db, *_ = workflow
    turn("find_patient", {"phone": "03007654321"})
    assert turn(
        "prepare_patient", {"name": "Alice Demo", "name_ur": "ایلس ڈیمو", "phone": "03007654321"}
    )["ok"]
    assert not turn("prepare_patient", {"name": "Bob Demo", "phone": "03007654321"})["ok"]
    assert not turn("create_patient", text="yes")["ok"]
    assert db.scalar(select(Patient).where(Patient.phone == "+923007654321")) is None


def test_changed_service_after_readback_requires_new_offer_and_confirmation(workflow, seed):
    from scheduler.models import Service

    turn, state, db, *_ = workflow
    prepare(turn, state, seed)
    db.get(Service, seed["service_a"]).duration_min = 60
    db.flush()
    assert not turn("book_appointment", text="yes")["ok"]
    assert db.scalar(select(Appointment)) is None


def test_reschedule_readback_contains_original_and_replacement_date(workflow, seed):
    turn, state, *_ = workflow
    prepare(turn, state, seed)
    assert turn("book_appointment", text="yes")["ok"]
    turn("update_dialogue_state", {"task": "reschedule"})
    turn("find_appointments")
    turn("select_appointment", {"ref": state["appointments"][0]["ref"]})
    turn("update_dialogue_state", {"date_phrase": "2026-11-04"})
    turn("get_available_slots")
    turn("hold_slot", {"ref": state["offers"][0]["ref"]})
    r = turn("read_back")
    assert "Tuesday" in r["say"]["en"] and "Wednesday" in r["say"]["en"]


@pytest.mark.parametrize("failures,want_success", [(1, True), (2, False)])
def test_transient_mutation_retries_once_then_hands_off(
    workflow, seed, monkeypatch, failures, want_success
):
    from sqlalchemy.exc import OperationalError

    import brain.tools as tools
    from scheduler.models import Callback, Idempotency

    turn, state, db, store, actor = workflow
    prepare(turn, state, seed)
    held_until = state["hold"]["expires_at_ms"]
    original = tools.book_transaction
    attempts = 0

    class LockTimeout(Exception):
        sqlstate = "55P03"

    def fail_then_execute(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= failures:
            raise OperationalError("synthetic statement", {}, LockTimeout())
        return original(*args, **kwargs)

    monkeypatch.setattr(tools, "book_transaction", fail_then_execute)
    r = turn("book_appointment", text="yes")
    if not r["ok"]:
        seconds, microseconds = store.client.time()
        pytest.fail(
            str(
                {
                    "result": r,
                    "attempts": attempts,
                    "lease_expires_at_ms": held_until,
                    "server_now_ms": seconds * 1000 + microseconds // 1000,
                }
            )
        )
    rows = list(db.scalars(select(Appointment)))
    assert len(rows) == (1 if want_success else 0)
    if want_success:
        assert len(list(db.scalars(select(Idempotency)))) == 1
    else:
        assert state["handoff"] and db.scalar(select(Callback)).reason == "technical"


@pytest.mark.parametrize("phone", ["03007654321", "invalid-phone"])
def test_phone_correction_invalidates_verification_hold_and_readback(workflow, seed, phone):
    turn, state, db, *_ = workflow
    prepare(turn, state, seed)
    turn("find_patient", {"phone": phone})
    assert not state["verified"]
    assert not state.get("hold") and not state.get("readback")
    assert not turn("book_appointment", text="yes")["ok"]
    assert db.scalar(select(Appointment)) is None


def test_successful_tool_resets_consecutive_technical_failures(workflow):
    from brain.tools import ToolEngine

    turn, state, db, store, actor = workflow
    from tests_api.conftest import NOW

    engine = ToolEngine(db, actor, store, NOW)
    assert not engine.trusted_event(state, "upstream_timeout")["ok"]
    assert turn("get_clinic_info", {"topic": "fees"})["ok"]
    assert state.get("failures", 0) == 0
    assert not engine.trusted_event(state, "upstream_timeout")["ok"]
    assert not state.get("handoff")


def test_invalid_phone_after_lookup_cannot_leave_orphaned_candidates(workflow):
    turn, state, *_ = workflow
    assert turn("find_patient", {"phone": "03001234567"})["ok"]
    assert state["candidates"]
    assert not turn("find_patient", {"phone": "1111111111"})["ok"]
    assert not state.get("candidates")
    assert not turn("verify_patient", {"factor": "Synthetic Patient"})["ok"]


def test_cancel_date_correction_reselects_matching_appointment(workflow, seed):
    turn, state, db, *_ = workflow
    prepare(turn, state, seed)
    first = turn("book_appointment", text="yes")["data"]["appointment"]["id"]
    turn("update_dialogue_state", {"date_phrase": "2026-11-04"})
    turn("get_available_slots")
    turn("hold_slot", {"ref": state["offers"][0]["ref"]})
    turn("read_back")
    second = turn("book_appointment", text="yes")["data"]["appointment"]["id"]
    turn("update_dialogue_state", {"task": "cancel"})
    turn("find_appointments")
    turn("select_appointment", {"ref": state["appointments"][0]["ref"]})
    assert turn("update_dialogue_state", {"date_phrase": "2026-11-04"})["ok"]
    assert not state.get("appointment")
    assert not turn("cancel_appointment", text="yes")["ok"]
    found = turn("find_appointments")
    assert found["ok"] and len(state["appointments"]) == 1
    assert state["appointments"][0]["id"] == second
    turn("select_appointment", {"ref": state["appointments"][0]["ref"]})
    turn("read_back")
    assert turn("cancel_appointment", text="yes")["ok"]
    assert db.get(Appointment, UUID(first)).status == "booked"
    assert db.get(Appointment, UUID(second)).status == "cancelled"
