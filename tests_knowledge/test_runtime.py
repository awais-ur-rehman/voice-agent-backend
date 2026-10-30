"""Grounded runtime boundaries with synthetic providers and the isolated SQL database."""

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from knowledge.models import QueryEvent
from tests_brain.conftest import seed as brain_seed
from tests_brain.test_draft_tests import dashboard as brain_dashboard
from tests_brain.test_draft_tests import send, start
from tests_knowledge.test_retrieval import Embeddings, corpus


@pytest.fixture
def seed(database):
    return brain_seed.__wrapped__(database)


@pytest.fixture
def dashboard(seed):
    yield from brain_dashboard.__wrapped__(seed)


class Grounder:
    def __init__(self, reply="Bring your referral letter.", mutate=None):
        self.reply, self.mutate, self.calls = reply, mutate, []

    def ground(self, query, language, chunks, *, deadline):
        self.calls.append((query, language))
        if self.mutate:
            self.mutate()
        return json.dumps(
            {
                "answer": self.reply,
                "language": language,
                "citations": [
                    {
                        "source_id": str(chunks[0].source_id),
                        "chunk_id": str(chunks[0].chunk_id),
                        "quote": chunks[0].text,
                    }
                ],
            }
        )

    def plan(self, *args):
        raise AssertionError("Public FAQ must not use the action planner")

    def close(self):
        pass


def setup_runtime(dashboard, database, seed, reply="Bring your referral letter.", content=None):
    sid, gid, chunks = corpus(database, seed["tenant_a"], seed["agent_a"], [content or reply])
    dashboard.app.state.settings.knowledge_enabled = True
    dashboard.app.state.knowledge_embeddings = Embeddings()
    provider = Grounder(reply)
    dashboard.app.state.brain_planner = provider
    test = start(dashboard, seed["agent_a"]).json()
    return test, provider, sid, gid, chunks


def test_clear_faq_during_verification_reads_parent_and_logs_only_child(dashboard, database, seed):
    test, provider, sid, _, chunks = setup_runtime(dashboard, database, seed)
    first = send(dashboard, test["id"], 0, test["fixtures"]["patients"][0]["phone"]).json()
    response = send(dashboard, test["id"], first["revision"], "What documents should I bring?")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["messages"][0]["text"].startswith("Bring your referral letter.")
    assert result["knowledge"]["status"] == "ready"
    assert result["knowledge"]["citations"][0]["chunk_id"] == str(chunks[0])
    with Session(database) as db:
        from scheduler.models import AgentSession, DraftTest

        mapping = db.get(DraftTest, UUID(test["id"]))
        state = db.get(AgentSession, mapping.session_id).checkpoint
        assert state["attempts"] == 0 and not state["verified"]
        events = list(db.scalars(select(QueryEvent)))
        assert len(events) == 1 and events[0].tenant_id == mapping.child_tenant_id
        assert events[0].purpose == "test" and events[0].redacted_question is None
        assert str(sid) in str(events[0].refs)
    assert provider.calls == [("What documents should I bring?", "en")]


@pytest.mark.parametrize(
    "utterance",
    [
        "My date of birth is 1990-01-01; what should I bring?",
        "What should Synthetic Test Amina bring?",
        "What should +923000000001 bring?",
        "What should my child bring?",
        "Book an appointment; what should I bring?",
        "میری تاریخ پیدائش 1990-01-01 ہے؛ کیا لاؤں؟",
        "میرے بچے کو کیا لانا چاہیے؟",
    ],
)
def test_mixed_identity_faq_has_no_provider_or_identity_effects(
    dashboard, database, seed, utterance
):
    test, provider, *_ = setup_runtime(dashboard, database, seed)
    first = send(dashboard, test["id"], 0, test["fixtures"]["patients"][0]["phone"]).json()
    response = send(dashboard, test["id"], first["revision"], utterance)
    assert response.status_code == 200, response.text
    assert "separately" in response.json()["messages"][0]["text"]
    assert provider.calls == []
    with Session(database) as db:
        from scheduler.models import AgentSession, DraftTest

        mapping = db.get(DraftTest, UUID(test["id"]))
        state = db.get(AgentSession, mapping.session_id).checkpoint
        assert state["attempts"] == 0 and not state["verified"]
        assert not state.get("pending_patient")
        assert list(db.scalars(select(QueryEvent))) == []


@pytest.mark.parametrize("kind", ["delete", "reindex"])
def test_source_changed_during_answer_discards_reply_without_sql_inference_locks(
    dashboard, database, seed, kind
):
    test, provider, source, *_ = setup_runtime(dashboard, database, seed)

    def mutate():
        with database.connect() as db:
            assert (
                db.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname='voice_agent_test' AND usename='scheduler_app' AND state='idle in transaction'"
                    )
                )
                == 0
            )
        base = f"/dashboard/agents/{seed['agent_a']}/knowledge/sources/{source}"
        if kind == "delete":
            response = dashboard.delete(
                base + "?revision=1", headers={"Idempotency-Key": str(uuid4())}
            )
        else:
            response = dashboard.post(
                base + "/reindex", json={"revision": 1}, headers={"Idempotency-Key": str(uuid4())}
            )
        assert response.status_code == 200, response.text

    provider.mutate = mutate
    response = send(dashboard, test["id"], 0, "What documents should I bring?")
    assert response.status_code == 200, response.text
    assert response.json()["knowledge"] == {"status": "stale", "citations": []}
    assert "Bring your referral" not in str(response.json())
    with Session(database) as db:
        event = db.scalar(select(QueryEvent))
        assert event.decision == "stale" and event.refs == []


@pytest.mark.parametrize("change", ["creator", "expiry", "end"])
def test_test_authority_rechecked_after_inference(dashboard, database, seed, change):
    from datetime import timedelta

    from scheduler.models import User
    from tests_api.conftest import NOW

    test, provider, *_ = setup_runtime(dashboard, database, seed)

    def mutate():
        if change == "creator":
            with Session(database) as db, db.begin():
                db.get(User, seed["owner_a"]).active = False
        elif change == "expiry":
            dashboard.app.state.clock = lambda: NOW + timedelta(minutes=31)
        else:
            assert dashboard.post(f"/dashboard/tests/{test['id']}/end").status_code == 200

    provider.mutate = mutate
    response = send(dashboard, test["id"], 0, "What documents should I bring?")
    assert response.status_code in (404, 410), response.text
    with Session(database) as db:
        assert list(db.scalars(select(QueryEvent))) == []


def test_exact_command_replay_keeps_bounded_receipt_without_retrieval(dashboard, database, seed):
    from scheduler.models import AgentSession, CommandReceipt, DraftTest

    test, provider, source, *_ = setup_runtime(dashboard, database, seed)
    command_id = "arbitrary-command-not-a-uuid"
    first = send(dashboard, test["id"], 0, "What documents should I bring?", command_id)
    assert first.status_code == 200, first.text
    with Session(database) as db, db.begin():
        db.execute(
            text("UPDATE knowledge_sources SET revision=2,requested_revision=2 WHERE id=:id"),
            {"id": source},
        )
    retry = send(dashboard, test["id"], 0, "What documents should I bring?", command_id)
    assert retry.json() == first.json()
    assert len(provider.calls) == 1
    with Session(database) as db:
        mapping = db.get(DraftTest, UUID(test["id"]))
        receipt = db.scalar(select(CommandReceipt))
        assert "What documents" not in str(receipt.response) + str(receipt.checkpoint)
        assert "text" not in str(receipt.response["knowledge"]["citations"][0].keys())
        assert len(list(db.scalars(select(QueryEvent)))) == 1
        runtime = db.get(AgentSession, mapping.session_id)
        cached = dashboard.app.state.brain_store.load(str(runtime.id))
        assert "referral letter" not in str(cached.get("history"))
    fresh = send(dashboard, test["id"], 1, "What documents should I bring?")
    assert fresh.json()["knowledge"]["status"] == "ready"  # old active generation remains ready
    assert len(provider.calls) == 2


@pytest.mark.parametrize(
    "utterance,topic", [("What are the fees?", "fees"), ("What are the clinic hours?", "hours")]
)
def test_structured_fees_hours_precede_conflicting_documents(
    dashboard, database, seed, utterance, topic
):
    test, provider, *_ = setup_runtime(
        dashboard, database, seed, "Everything is free and always open."
    )
    response = send(dashboard, test["id"], 0, utterance)
    assert response.status_code == 200, response.text
    assert response.json()["tool_trace"][0]["action"] == "get_clinic_info"
    assert provider.calls == []
    assert "Everything is free" not in str(response.json())


def test_bare_dob_and_wrong_bare_factors_keep_local_attempt_limit(dashboard, database, seed):
    from scheduler.models import AgentSession, DraftTest

    test, provider, *_ = setup_runtime(dashboard, database, seed)
    patient = test["fixtures"]["patients"][0]
    with Session(database) as db, db.begin():
        from datetime import date

        from scheduler.models import Patient

        db.get(Patient, UUID(patient["id"])).dob = date(1990, 1, 1)
    first = send(dashboard, test["id"], 0, patient["phone"]).json()
    verified = send(dashboard, test["id"], first["revision"], "1990-01-01").json()
    assert verified["tool_trace"][0]["ok"]
    first = send(dashboard, test["id"], verified["revision"], patient["phone"]).json()
    revision = first["revision"]
    for factor in ("Wrong Name", "1991-01-01", "Someone Different"):
        response = send(dashboard, test["id"], revision, factor).json()
        assert not response["tool_trace"][0]["ok"]
        revision = response["revision"]
    with Session(database) as db:
        mapping = db.get(DraftTest, UUID(test["id"]))
        state = db.get(AgentSession, mapping.session_id).checkpoint
        assert state["attempts"] == 3 and state["handoff"]
    assert provider.calls == []


def test_faq_invalidates_patient_readback_but_preserves_pending_patient(dashboard, database, seed):
    from scheduler.models import AgentSession, DraftTest, Patient

    test, provider, *_ = setup_runtime(dashboard, database, seed)
    with Session(database) as db, db.begin():
        mapping = db.get(DraftTest, UUID(test["id"]))
        runtime = db.get(AgentSession, mapping.session_id)
        session_id, child_tenant_id = mapping.session_id, mapping.child_tenant_id
        runtime.checkpoint = runtime.checkpoint | {
            "lookup_empty": True,
            "lookup_phone": "+923000000009",
            "pending_patient": {
                "name": "Synthetic New",
                "name_ur": "مصنوعی نیا",
                "phone": "+923000000009",
            },
            "readback": {"kind": "patient", "revision": 0, "say": {"en": "old", "ur": "پرانا"}},
        }
    faq = send(dashboard, test["id"], 0, "What documents should I bring?")
    assert faq.status_code == 200, faq.text
    yes = send(dashboard, test["id"], 1, "yes")
    assert yes.status_code == 200, yes.text
    with Session(database) as db:
        state = db.get(AgentSession, session_id).checkpoint
        assert state["pending_patient"]["name_ur"] == "مصنوعی نیا"
        assert state["readback"]["revision"] == 2  # fresh code-owned readback
        assert (
            len(list(db.scalars(select(Patient).where(Patient.tenant_id == child_tenant_id)))) == 2
        )


def test_faq_invalidates_booking_readback_and_preserves_real_hold(dashboard, database, seed):
    from scheduler.models import AgentSession, Appointment, DraftTest
    from tests_brain.test_draft_tests import BookingPlanner

    test, provider, *_ = setup_runtime(dashboard, database, seed)
    dashboard.app.state.brain_planner = BookingPlanner()
    patient = test["fixtures"]["patients"][1]
    revision = 0
    for utterance in (patient["phone"], patient["dob"], "2026-11-04", "slots", "first", "review"):
        response = send(dashboard, test["id"], revision, utterance).json()
        assert response["tool_trace"][0]["ok"], response
        revision = response["revision"]
    with Session(database) as db:
        mapping = db.get(DraftTest, UUID(test["id"]))
        before = db.get(AgentSession, mapping.session_id).checkpoint
        session_id, child = mapping.session_id, mapping.child_tenant_id
    dashboard.app.state.brain_planner = provider
    response = send(dashboard, test["id"], revision, "What documents should I bring?").json()
    with Session(database) as db:
        after = db.get(AgentSession, session_id).checkpoint
        assert after["hold"] == before["hold"] and after["task"] == before["task"]
        assert after["readback"] is None
    yes = send(dashboard, test["id"], response["revision"], "yes").json()
    assert yes["tool_trace"][0]["action"] == "read_back"
    with Session(database) as db:
        assert len(list(db.scalars(select(Appointment).where(Appointment.tenant_id == child)))) == 1
    confirmed = send(dashboard, test["id"], yes["revision"], "yes").json()
    assert confirmed["tool_trace"][0]["action"] == "confirm"
    with Session(database) as db:
        assert len(list(db.scalars(select(Appointment).where(Appointment.tenant_id == child)))) == 2
        assert (
            list(db.scalars(select(Appointment).where(Appointment.tenant_id == seed["tenant_a"])))
            == []
        )


@pytest.mark.parametrize(
    "failure,status",
    [("forged", "rejected"), ("provider", "service_unavailable"), ("no_answer", "rejected")],
)
def test_grounding_failure_is_final_single_event_and_never_queues_callback(
    dashboard, database, seed, failure, status
):
    from brain.provider import ProviderError
    from scheduler.models import Callback

    test, provider, *_ = setup_runtime(dashboard, database, seed)

    def ground(query, language, chunks, *, deadline):
        if failure == "provider":
            raise ProviderError("PRIVATE BODY MUST NOT BE LOGGED")
        if failure == "no_answer":
            return '{"answer":"","language":"en","citations":[]}'
        return json.dumps(
            {
                "answer": "Bring your referral letter.",
                "language": "en",
                "citations": [
                    {
                        "source_id": str(uuid4()),
                        "chunk_id": str(chunks[0].chunk_id),
                        "quote": chunks[0].text,
                    }
                ],
            }
        )

    provider.ground = ground
    response = send(dashboard, test["id"], 0, "What should I bring?")
    assert response.status_code == 200, response.text
    assert response.json()["knowledge"]["status"] == status
    assert "queued" not in str(response.json())
    with Session(database) as db:
        events = list(db.scalars(select(QueryEvent)))
        assert len(events) == 1 and events[0].decision == status
        assert list(db.scalars(select(Callback))) == []


def test_scope_helper_is_narrow_and_restores_child_rls_on_failure(dashboard, database, seed):
    from dataclasses import replace

    from brain.security import RuntimeActor
    from knowledge.scope import resolve_scope
    from scheduler.db import tenant_context
    from scheduler.errors import AppError
    from scheduler.models import AgentSession, DraftTest, User
    from tests_api.conftest import NOW

    test, *_ = setup_runtime(dashboard, database, seed)
    with Session(database) as db:
        mapping = db.get(DraftTest, UUID(test["id"]))
        session = db.get(AgentSession, mapping.session_id)
        actor = RuntimeActor(session.id, session.tenant_id, session.agent_id, session.version_id)
    with dashboard.app.state.session_factory() as db, db.begin():
        tenant_context(db, actor.tenant_id)
        assert db.scalar(text("SELECT current_user")) == "scheduler_app"
        assert (
            db.scalar(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
            is False
        )
        assert list(db.scalars(select(QueryEvent))) == []
        from knowledge.models import Source

        assert list(db.scalars(select(Source))) == []
        scope = resolve_scope(db, actor, NOW)
        assert scope.corpus.agent_id == seed["agent_a"]
        assert scope.log.tenant_id == actor.tenant_id
        assert db.scalar(text("SELECT current_setting('app.tenant_id')")) == str(actor.tenant_id)
        for forged in (
            replace(actor, id=uuid4()),
            replace(actor, agent_id=seed["agent_b"]),
            replace(actor, tenant_id=seed["tenant_b"]),
        ):
            with pytest.raises(AppError):
                resolve_scope(db, forged, NOW)
            assert db.scalar(text("SELECT current_setting('app.tenant_id')")) == str(
                forged.tenant_id
            )
    with Session(database) as db, db.begin():
        db.get(User, seed["owner_a"]).active = False
    with dashboard.app.state.session_factory() as db, db.begin():
        with pytest.raises(AppError):
            resolve_scope(db, actor, NOW)
        assert db.scalar(text("SELECT current_setting('app.tenant_id')")) == str(actor.tenant_id)


def test_urdu_grounding_uses_current_language(dashboard, database, seed):
    from scheduler.models import AgentSession, DraftTest

    test, provider, *_ = setup_runtime(dashboard, database, seed, "اپنا حوالہ خط ساتھ لائیں۔")
    with Session(database) as db, db.begin():
        mapping = db.get(DraftTest, UUID(test["id"]))
        runtime = db.get(AgentSession, mapping.session_id)
        runtime.checkpoint = runtime.checkpoint | {"lang": "ur"}
    response = send(dashboard, test["id"], 0, "کیا دستاویزات لاؤں؟")
    assert response.status_code == 200, response.text
    assert response.json()["messages"][0]["text"].startswith("اپنا حوالہ خط")
    assert provider.calls == [("کیا دستاویزات لاؤں؟", "ur")]


@pytest.mark.parametrize("synthetic", [False, True])
def test_final_answer_source_locks_and_query_fk_follow_parent_order(
    dashboard, database, seed, synthetic
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    from sqlalchemy import event

    test, provider, source, *_ = setup_runtime(dashboard, database, seed)
    if not synthetic:
        created = dashboard.post("/agent-sessions", json={"agent_id": str(seed["agent_a"])}).json()
    held, release, started = Event(), Event(), Event()
    pids, sqlstates = [], []
    engine = dashboard.app.state.engine

    def after_lock(conn, cursor, statement, parameters, context, executemany):
        if "knowledge_sources" in statement and "FOR SHARE" in statement and not held.is_set():
            pids.append(cursor.connection.info.backend_pid)
            held.set()
            assert release.wait(10), "final validation barrier timed out"

    def before_lock(conn, cursor, statement, parameters, context, executemany):
        if held.is_set() and "FOR UPDATE" in statement:
            pid = cursor.connection.info.backend_pid
            if pid != pids[0]:
                if pid not in pids:
                    pids.append(pid)
                started.set()

    def db_error(context):
        sqlstates.append(getattr(context.original_exception, "sqlstate", None))

    def answer():
        if synthetic:
            return send(dashboard, test["id"], 0, "What documents should I bring?")
        return dashboard.post(
            f"/runtime/sessions/{created['session_id']}/turn",
            headers={"Authorization": "Bearer " + created["token"]},
            json={
                "command_id": str(uuid4()),
                "expected_revision": 0,
                "text": "What documents should I bring?",
            },
        )

    event.listen(engine, "after_cursor_execute", after_lock)
    event.listen(engine, "before_cursor_execute", before_lock)
    event.listen(engine, "handle_error", db_error)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            answering = pool.submit(answer)
            try:
                assert held.wait(5)
                mutation = pool.submit(
                    dashboard.post,
                    f"/dashboard/agents/{seed['agent_a']}/knowledge/sources/{source}/reindex",
                    json={"revision": 1},
                    headers={"Idempotency-Key": str(uuid4())},
                )
                assert started.wait(5)
                deadline = monotonic() + 2
                while monotonic() < deadline:
                    with database.connect() as observer:
                        blocked = observer.scalar(
                            text("SELECT :holder=ANY(pg_blocking_pids(:waiter))"),
                            {"holder": pids[0], "waiter": pids[1]},
                        )
                    if blocked:
                        break
                    sleep(0.01)
                else:
                    pytest.fail("source CRUD did not reach observed parent lock wait")
            finally:
                release.set()
            response, changed = answering.result(timeout=10), mutation.result(timeout=10)
    finally:
        event.remove(engine, "after_cursor_execute", after_lock)
        event.remove(engine, "before_cursor_execute", before_lock)
        event.remove(engine, "handle_error", db_error)
    assert "40P01" not in sqlstates, sqlstates
    assert response.status_code == 200, response.text
    assert changed.status_code == 200, changed.text
    envelope = response.json() if synthetic else response.json()["result"]
    assert envelope["knowledge"]["status"] == "ready"
    with Session(database) as db:
        assert len(list(db.scalars(select(QueryEvent)))) == 1


def test_overlong_runtime_query_is_rejected_before_embedding_or_event(dashboard, database, seed):
    test, provider, *_ = setup_runtime(dashboard, database, seed)
    response = send(dashboard, test["id"], 0, "What " + "x" * 996)
    assert response.status_code == 422, response.text
    assert provider.calls == []
    with Session(database) as db:
        assert list(db.scalars(select(QueryEvent))) == []


def test_structured_profile_precedes_conflicting_documents(dashboard, database, seed):
    from scheduler.models import Tenant

    with Session(database) as db, db.begin():
        clinic = db.get(Tenant, seed["tenant_a"])
        clinic.preparation_en = "Bring the reviewed clinic checklist."
        clinic.preparation_ur = "کلینک کی تصدیق شدہ فہرست لائیں۔"
    test, provider, *_ = setup_runtime(dashboard, database, seed, "No documents are needed.")
    response = send(dashboard, test["id"], 0, "What documents should I bring?")
    assert response.status_code == 200, response.text
    assert response.json()["messages"][0]["text"] == "Bring the reviewed clinic checklist."
    assert provider.calls == []


@pytest.mark.parametrize(
    "binding", ["command", "revision", "language", "actor", "generation", "profile"]
)
def test_prepared_reply_fences_identity_revision_language_generation_and_profile(
    dashboard, database, seed, binding
):
    import time
    from dataclasses import replace

    from brain.security import RuntimeActor
    from knowledge.runtime import validate_prepared
    from knowledge.scope import resolve_scope
    from knowledge.service import KnowledgeService
    from scheduler.db import tenant_context
    from scheduler.errors import AppError
    from scheduler.models import AgentSession, DraftTest
    from tests_api.conftest import NOW, SECRET

    test, provider, source, generation, _ = setup_runtime(dashboard, database, seed)
    with Session(database) as db:
        mapping = db.get(DraftTest, UUID(test["id"]))
        session = db.get(AgentSession, mapping.session_id)
        actor = RuntimeActor(session.id, session.tenant_id, session.agent_id, session.version_id)
    factory = dashboard.app.state.session_factory
    with factory() as db, db.begin():
        scope = resolve_scope(db, actor, NOW)
    prepared = KnowledgeService(
        factory, Embeddings(), audit_secret=SECRET, answers=provider
    ).prepare(
        scope,
        "What should I bring?",
        "en",
        command_id="bound",
        expected_revision=0,
        deadline=time.monotonic() + 15,
    )
    if binding == "generation":
        with Session(database) as db, db.begin():
            db.execute(
                text("UPDATE knowledge_sources SET active_generation_id=NULL WHERE id=:id"),
                {"id": source},
            )
    elif binding == "profile":
        prepared = replace(
            prepared,
            result=replace(
                prepared.result, chunks=(replace(prepared.result.chunks[0], profile="0" * 64),)
            ),
        )
    with factory() as db, db.begin():
        tenant_context(db, actor.tenant_id)
        kwargs = {
            "command_id": "other" if binding == "command" else "bound",
            "expected_revision": 1 if binding == "revision" else 0,
            "language": "ur" if binding == "language" else "en",
            "now": NOW,
        }
        checked_actor = replace(actor, id=uuid4()) if binding == "actor" else actor
        if binding in {"generation", "profile"}:
            final = validate_prepared(db, checked_actor, prepared, **kwargs)
            assert final.result.reason == "stale" and final.answer is None
        else:
            with pytest.raises(AppError):
                validate_prepared(db, checked_actor, prepared, **kwargs)
        assert db.scalar(text("SELECT current_setting('app.tenant_id')")) == str(actor.tenant_id)


def test_concurrent_same_command_has_one_final_event_and_receipt(dashboard, database, seed):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from scheduler.models import CommandReceipt

    test, provider, *_ = setup_runtime(dashboard, database, seed)
    boundary = Barrier(2)
    provider.mutate = lambda: boundary.wait(timeout=5)

    def query():
        return send(dashboard, test["id"], 0, "What documents should I bring?", "same-command")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: query(), range(2)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert results[0].json() == results[1].json()
    with Session(database) as db:
        assert len(list(db.scalars(select(QueryEvent)))) == 1
        assert len(list(db.scalars(select(CommandReceipt)))) == 1


def test_revision_changes_during_inference_discard_prepared_answer(dashboard, database, seed):
    test, provider, *_ = setup_runtime(dashboard, database, seed)

    def advance():
        result = send(dashboard, test["id"], 0, "What are the fees?")
        assert result.status_code == 200, result.text

    provider.mutate = advance
    response = send(dashboard, test["id"], 0, "What documents should I bring?")
    assert response.status_code == 409, response.text
    with Session(database) as db:
        assert list(db.scalars(select(QueryEvent))) == []


def test_emergency_precedes_mixed_identity_question(dashboard, database, seed):
    test, provider, *_ = setup_runtime(dashboard, database, seed)
    response = send(
        dashboard, test["id"], 0, "I cannot breathe; my DOB is 1990-01-01; what should I bring?"
    )
    assert response.status_code == 200, response.text
    assert response.json()["tool_trace"][0]["action"] == "request_handoff"
    assert "emergency" in response.json()["messages"][0]["text"]
    assert provider.calls == []


def test_original_corpus_search_transaction_is_read_only(dashboard, database, seed):
    from sqlalchemy import event

    test, *_ = setup_runtime(dashboard, database, seed)
    observations = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if "WITH corpus AS MATERIALIZED" in statement:
            observations.append(conn.exec_driver_sql("SHOW transaction_read_only").scalar())

    engine = dashboard.app.state.engine
    event.listen(engine, "after_cursor_execute", observe)
    try:
        response = send(dashboard, test["id"], 0, "What documents should I bring?")
    finally:
        event.remove(engine, "after_cursor_execute", observe)
    assert response.status_code == 200, response.text
    assert observations == ["on"]


def test_explicit_runtime_search_cannot_bypass_identity_privacy(dashboard, database, seed):
    _, provider, *_ = setup_runtime(dashboard, database, seed)
    created = dashboard.post("/agent-sessions", json={"agent_id": str(seed["agent_a"])}).json()
    headers = {"Authorization": "Bearer " + created["token"]}
    url = f"/runtime/sessions/{created['session_id']}/commands"
    found = dashboard.post(
        url,
        headers=headers,
        json={
            "command_id": "lookup",
            "expected_revision": 0,
            "action": {"name": "find_patient", "arguments": {"phone": "+923001234567"}},
        },
    )
    assert found.status_code == 200, found.text
    response = dashboard.post(
        url,
        headers=headers,
        json={
            "command_id": "faq",
            "expected_revision": 1,
            "action": {"name": "search_knowledge", "arguments": {}},
            "text": "My DOB is 1990-01-01; what should I bring?",
        },
    )
    assert response.status_code == 200, response.text
    assert "separately" in response.json()["result"]["say"]["en"]
    assert provider.calls == []


def test_busy_source_rolls_back_command_without_losing_pending_work(dashboard, database, seed):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from knowledge.models import Source
    from scheduler.db import tenant_context
    from scheduler.models import AgentSession, DraftTest

    test, provider, source, *_ = setup_runtime(dashboard, database, seed)
    held, release = Event(), Event()

    def lock_source():
        with dashboard.app.state.session_factory() as db, db.begin():
            tenant_context(db, seed["tenant_a"])
            db.scalar(select(Source).where(Source.id == source).with_for_update())
            held.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        locking = pool.submit(lock_source)
        try:
            assert held.wait(2)
            response = send(dashboard, test["id"], 0, "What documents should I bring?")
        finally:
            release.set()
        locking.result(timeout=2)
    assert response.status_code == 503, response.text
    with Session(database) as db:
        mapping = db.get(DraftTest, UUID(test["id"]))
        state = db.get(AgentSession, mapping.session_id).checkpoint
        assert state["revision"] == 0 and state["task"] == "book"
        assert list(db.scalars(select(QueryEvent))) == []


def test_final_parent_scope_lock_is_bounded_before_child_command(dashboard, database, seed):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from scheduler.db import tenant_context
    from scheduler.models import DraftTest

    test, provider, *_ = setup_runtime(dashboard, database, seed)
    held, release = Event(), Event()

    def lock_mapping():
        with dashboard.app.state.session_factory() as db, db.begin():
            tenant_context(db, seed["tenant_a"])
            db.scalar(select(DraftTest).where(DraftTest.id == UUID(test["id"])).with_for_update())
            held.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:

        def during_inference():
            pool.submit(lock_mapping)
            assert held.wait(2)

        provider.mutate = during_inference
        request = pool.submit(send, dashboard, test["id"], 0, "What documents should I bring?")
        try:
            assert held.wait(2)
            try:
                response = request.result(timeout=2.5)
            except TimeoutError:
                pytest.fail("final parent mapping lock was not bounded")
            assert response.status_code in (409, 503), response.text
        finally:
            release.set()
    with Session(database) as db:
        assert list(db.scalars(select(QueryEvent))) == []
