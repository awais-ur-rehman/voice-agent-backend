"""Synthetic dashboard scopes exercise the real unprivileged database boundary."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from scheduler.config import Settings
from scheduler.main import create_app
from scheduler.models import Agent, AgentVersion, Patient, Provider, Service
from tests_api.conftest import APP_URL, NOW, PASSWORD, SECRET


@pytest.fixture
def dashboard(seed):
    settings = Settings(
        database_url=APP_URL,
        jwt_secret=SECRET,
        runtime_secret="isolated-draft-runtime-secret-over-32-characters",
        livekit_api_key="synthetic-test-key",
        livekit_api_secret="synthetic-livekit-secret-over-32-characters",
        valkey_url="redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
        _env_file=None,
    )
    app = create_app(settings, clock=lambda: NOW)
    with TestClient(app) as client:
        login(client, "owner-a")
        yield client


def login(client, account):
    response = client.post(
        "/auth/login", json={"email": account + "@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]


def start(client, agent, key=None, **extra):
    return client.post(
        f"/dashboard/agents/{agent}/tests",
        headers={"Idempotency-Key": key or str(uuid4())},
        json={"draft_revision": 1, **extra},
    )


def test_snapshot_clones_only_schedule_and_saved_draft(dashboard, seed, database):
    response = start(dashboard, seed["agent_a"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert "token" not in str(body)
    assert body["draft_revision"] == 1
    assert body["transport_capabilities"]["voice"]["available"] is False
    assert body["fixtures"]["patients"][0]["dob"]
    with Session(database) as db:
        mapping = (
            db.execute(text("SELECT * FROM draft_tests WHERE id=:id"), {"id": body["id"]})
            .mappings()
            .one()
        )
        child = mapping["child_tenant_id"]
        assert child != seed["tenant_a"]
        people = list(db.scalars(select(Patient).where(Patient.tenant_id == child)))
        assert len(people) == 2
        assert all(p.id != seed["patient_a"] and p.phone != "+923001234567" for p in people)
        assert (
            db.scalar(select(Provider).where(Provider.tenant_id == child)).id != seed["provider_a"]
        )
        assert db.scalar(select(Service).where(Service.tenant_id == child)).id != seed["service_a"]
        version = db.scalar(select(AgentVersion).where(AgentVersion.tenant_id == child))
        assert version.purpose == "test" and version.number is None
        assert db.get(Agent, seed["agent_a"]).active_version_id == seed["version_a"]
    assert dashboard.get("/dashboard/tests/" + body["id"]).json() == body


def test_scope_retry_ownership_end_and_blocked_voice(dashboard, seed):
    key = str(uuid4())
    first = start(dashboard, seed["agent_a"], key)
    assert first.status_code == 201, first.text
    scope = first.json()["id"]
    assert start(dashboard, seed["agent_a"], key).json()["id"] == scope
    assert start(dashboard, seed["agent_a"]).status_code == 409
    assert (
        dashboard.post(f"/dashboard/tests/{scope}/voice", json={"request_key": "test"}).status_code
        == 503
    )
    login(dashboard, "owner-b")
    assert dashboard.get(f"/dashboard/tests/{scope}").status_code == 404
    login(dashboard, "staff-a")
    assert dashboard.get(f"/dashboard/tests/{scope}").status_code == 403
    login(dashboard, "owner-a")
    assert dashboard.post(f"/dashboard/tests/{scope}/end").status_code == 200
    assert dashboard.post(f"/dashboard/tests/{scope}/end").status_code == 200
    assert (
        dashboard.post(
            f"/dashboard/tests/{scope}/turns",
            json={"command_id": "late", "revision": 0, "text": "hello"},
        ).status_code
        == 410
    )
    assert start(dashboard, seed["agent_a"]).status_code == 201


def send(client, scope, revision, utterance, command_id=None):
    return client.post(
        f"/dashboard/tests/{scope}/turns",
        json={"command_id": command_id or str(uuid4()), "revision": revision, "text": utterance},
    )


class BookingPlanner:
    def plan(self, state, utterance, config, facts):
        from brain.contracts import Action

        if utterance == "2026-11-04":
            return Action(
                name="update_dialogue_state",
                arguments={
                    "task": "book",
                    "date_phrase": utterance,
                    "provider_id": facts["providers"][0]["id"],
                    "service_id": facts["services"][0]["id"],
                },
            )
        if utterance == "slots":
            return Action(name="get_available_slots", arguments={})
        if utterance == "first":
            return Action(name="hold_slot", arguments={"ref": state["offers"][0]["ref"]})
        return Action(name="read_back", arguments={})

    def close(self):
        pass


def test_real_planner_gateway_books_only_synthetic_records(dashboard, seed, database):
    from scheduler.models import Appointment

    body = start(dashboard, seed["agent_a"]).json()
    scope = body["id"]
    dashboard.app.state.brain_planner = BookingPlanner()
    revision = 0
    patient = body["fixtures"]["patients"][1]
    for utterance in (
        patient["phone"],
        patient["dob"],
        "2026-11-04",
        "slots",
        "first",
        "review",
        "yes",
    ):
        response = send(dashboard, scope, revision, utterance)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["tool_trace"][0]["ok"], result
        assert patient["phone"] not in str(result["tool_trace"])
        assert patient["dob"] not in str(result["tool_trace"])
        revision = result["revision"]
    with Session(database) as db:
        mapping = (
            db.execute(text("SELECT * FROM draft_tests WHERE id=:id"), {"id": scope})
            .mappings()
            .one()
        )
        assert (
            len(
                list(
                    db.scalars(
                        select(Appointment).where(
                            Appointment.tenant_id == mapping["child_tenant_id"]
                        )
                    )
                )
            )
            == 2
        )
        assert (
            list(db.scalars(select(Appointment).where(Appointment.tenant_id == seed["tenant_a"])))
            == []
        )


def test_inference_cannot_commit_after_expiry_or_end(dashboard, seed, database):
    from datetime import timedelta

    from brain.contracts import Action
    from scheduler.models import AgentSession

    for expire in (True, False):
        body = start(dashboard, seed["agent_a"]).json()
        scope = body["id"]

        class RacePlanner:
            def plan(self, *args):
                if expire:
                    dashboard.app.state.clock = lambda: NOW + timedelta(minutes=31)
                else:
                    assert dashboard.post(f"/dashboard/tests/{scope}/end").status_code == 200
                return Action(name="get_clinic_info", arguments={"topic": "services"})

            def close(self):
                pass

        dashboard.app.state.brain_planner = RacePlanner()
        response = send(dashboard, scope, 0, "tell me")
        assert response.status_code == 410, response.text
        if expire:
            assert dashboard.get(f"/dashboard/tests/{scope}").json()["status"] == "expired"
        with Session(database) as db:
            sid = db.execute(
                text("SELECT session_id FROM draft_tests WHERE id=:id"), {"id": scope}
            ).scalar()
            assert db.get(AgentSession, sid).status == "ended"
        dashboard.app.state.clock = lambda: NOW


def test_forged_ids_and_cross_creator_cannot_access(dashboard, seed, database):
    from scheduler.models import User
    from scheduler.security import passwords

    scope = start(dashboard, seed["agent_a"]).json()["id"]
    with Session(database) as db, db.begin():
        db.add(
            User(
                tenant_id=seed["tenant_a"],
                email="admin-a@example.com",
                role="admin",
                password_hash=passwords.hash(PASSWORD),
            )
        )
    login(dashboard, "admin-a")
    assert dashboard.get(f"/dashboard/tests/{scope}").status_code == 404
    assert send(dashboard, scope, 0, "hello").status_code == 404
    assert dashboard.post(f"/dashboard/tests/{scope}/end").status_code == 404
    login(dashboard, "owner-a")
    assert dashboard.get(f"/dashboard/tests/{uuid4()}").status_code == 404
    assert start(dashboard, seed["agent_b"]).status_code == 404
    assert (
        start(dashboard, seed["agent_a"], child_tenant_id=str(seed["tenant_b"])).status_code == 422
    )


def test_replacement_limits_disable_and_pin(dashboard, seed, database):
    from scheduler.models import AgentDraft

    first = start(dashboard, seed["agent_a"]).json()
    with Session(database) as db, db.begin():
        draft = db.scalar(select(AgentDraft).where(AgentDraft.agent_id == seed["agent_a"]))
        draft.config = {"language_mode": "en"}
        draft.revision += 1
    response = dashboard.post(
        f"/dashboard/agents/{seed['agent_a']}/tests",
        headers={"Idempotency-Key": str(uuid4())},
        json={"draft_revision": 2, "replace": True},
    )
    assert response.status_code == 201, response.text
    assert dashboard.get("/dashboard/tests/" + first["id"]).json()["status"] == "ended"
    with Session(database) as db:
        child = db.execute(
            text("SELECT child_tenant_id FROM draft_tests WHERE id=:id"), {"id": first["id"]}
        ).scalar()
        assert (
            db.scalar(select(AgentVersion).where(AgentVersion.tenant_id == child)).config[
                "language_mode"
            ]
            == "bilingual"
        )
    for index in range(3):
        agent = dashboard.post("/dashboard/agents", json={"name": f"Test {index}"}).json()["id"]
        assert start(dashboard, agent).status_code == (201 if index < 2 else 409)
    with Session(database) as db, db.begin():
        db.get(Agent, seed["agent_b"]).enabled = False
    login(dashboard, "owner-b")
    assert start(dashboard, seed["agent_b"]).status_code == 404


def test_trace_reports_validation_code_and_retry_is_stable(dashboard, seed):
    from brain.contracts import Action

    scope = start(dashboard, seed["agent_a"]).json()["id"]

    class ForgedPlanner(BookingPlanner):
        def plan(self, *args):
            return Action(
                name="update_dialogue_state", arguments={"provider_id": str(seed["provider_a"])}
            )

    dashboard.app.state.brain_planner = ForgedPlanner()
    response = send(dashboard, scope, 0, "choose doctor", "same-command")
    assert response.status_code == 200, response.text
    assert response.json()["tool_trace"][0]["code"] == "validation_error"
    assert send(dashboard, scope, 0, "choose doctor", "same-command").json() == response.json()


def test_dashboard_contract_exports_all_staff_paths_and_no_worker_or_runtime():
    import json

    from scripts.export_dashboard_contracts import ROOT, render

    schema = json.loads(render())
    for path in (
        "/auth/register",
        "/auth/me",
        "/patients",
        "/providers",
        "/appointments",
        "/dashboard/appointments",
        "/dashboard/tests/{identity}/voice/resume",
        "/dashboard/tests/{identity}/turns",
    ):
        assert path in schema["paths"]
    assert not any(
        path.startswith(("/runtime", "/worker", "/agent-sessions")) for path in schema["paths"]
    )
    assert "WorkerGrant" not in schema["components"]["schemas"]
    assert "AgentConfig" in schema["components"]["schemas"]
    assert (ROOT / "packages/schemas/dashboard.v1.json").read_text() == render()


def test_voice_gateway_original_staff_jwt_reset_and_cleanup(dashboard, seed, database, monkeypatch):
    import json

    from brain.voice_rooms import LiveKitRooms
    from scheduler.models import AgentSession, VoiceSession

    dashboard.app.state.settings.voice_enabled = True
    monkeypatch.setattr(LiveKitRooms, "create", lambda self, room: None)
    monkeypatch.setattr(LiveKitRooms, "delete", lambda self, room: None)
    store = dashboard.app.state.brain_store
    store.client.delete("voice:capacity")
    store.client.set("voice:ready", json.dumps({"en": True, "ur": True}), ex=15)
    body = start(dashboard, seed["agent_a"]).json()
    identity = body["id"]
    with Session(database) as db, db.begin():
        sid = db.execute(
            text("SELECT session_id FROM draft_tests WHERE id=:id"), {"id": identity}
        ).scalar()
        runtime = db.get(AgentSession, sid)
        runtime.checkpoint = {
            **runtime.checkpoint,
            "readback": {"kind": "appointment", "revision": 0},
        }
    created = dashboard.post(
        f"/dashboard/tests/{identity}/voice", json={"request_key": "voice-test"}
    )
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]
    assert created.json()["media"] is not None
    assert dashboard.get(f"/voice/sessions/{sid}").status_code == 404
    assert dashboard.get(f"/dashboard/tests/{identity}/voice").status_code == 200
    with Session(database) as db, db.begin():
        voice = db.scalar(select(VoiceSession).where(VoiceSession.session_id == sid))
        voice.status = "disconnected"
        voice.disconnected_at = NOW
    assert dashboard.post(f"/dashboard/tests/{identity}/voice/resume").status_code == 200
    assert dashboard.get(f"/dashboard/tests/{identity}").json()["conversation"][
        "text_confirmation_reset"
    ]
    assert send(dashboard, identity, 0, "yes").status_code == 403
    assert dashboard.post(f"/dashboard/tests/{identity}/voice/end").status_code == 200
    assert store.client.get("voice:capacity") is None
    with Session(database) as db:
        child = db.execute(
            text("SELECT child_tenant_id FROM draft_tests WHERE id=:id"), {"id": identity}
        ).scalar()
        assert all(
            s.status == "ended" and not s.token_jti_hash and not s.checkpoint.get("readback")
            for s in db.scalars(select(AgentSession).where(AgentSession.tenant_id == child))
        )
        assert (
            db.scalar(select(VoiceSession).where(VoiceSession.session_id == sid)).worker_nonce_hash
            is None
        )
    store.client.delete("voice:ready")


def test_end_is_idempotent_and_cleanup_survives_registry_loss(dashboard, seed, database):
    from datetime import timedelta

    from brain.draft_tests import sweep
    from scheduler.models import AgentSession, User

    scope = start(dashboard, seed["agent_a"]).json()["id"]
    one = dashboard.post(f"/dashboard/tests/{scope}/end").json()
    assert dashboard.post(f"/dashboard/tests/{scope}/end").json() == one
    scope = start(dashboard, seed["agent_a"]).json()["id"]
    dashboard.app.state.brain_store.client.delete("draft:cleanup:" + scope)
    with Session(database) as db, db.begin():
        db.get(User, seed["owner_a"]).active = False
    dashboard.app.state.clock = lambda: NOW + timedelta(minutes=31)
    sweep(dashboard.app)
    with Session(database) as db:
        sid = db.execute(
            text("SELECT session_id FROM draft_tests WHERE id=:id"), {"id": scope}
        ).scalar()
        assert db.get(AgentSession, sid).status == "ended"


def test_scope_mapping_rls_and_provisioning_boundary(dashboard, seed):
    from sqlalchemy.exc import DBAPIError

    from scheduler.db import tenant_context

    body = start(dashboard, seed["agent_a"]).json()
    with dashboard.app.state.session_factory() as db, db.begin():
        tenant_context(db, seed["tenant_b"])
        assert (
            db.execute(text("SELECT id FROM draft_tests WHERE id=:id"), {"id": body["id"]}).scalar()
            is None
        )
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(
                text("SELECT provision_draft_test(:actor,:agent,1,'invalid-request',now())"),
                {"actor": seed["owner_a"], "agent": seed["agent_a"]},
            )
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(text("SELECT * FROM synthetic_tenants"))
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(
                text("UPDATE draft_tests SET child_tenant_id=:child WHERE id=:id"),
                {"child": seed["tenant_b"], "id": body["id"]},
            )


def test_end_revokes_sql_even_when_cache_cleanup_fails(dashboard, seed, database, monkeypatch):
    from valkey.exceptions import ConnectionError

    from scheduler.models import AgentSession

    scope = start(dashboard, seed["agent_a"]).json()["id"]
    store = dashboard.app.state.brain_store
    real_repair = store.repair

    def unavailable(*args, **kwargs):
        raise ConnectionError("synthetic outage")

    monkeypatch.setattr(store, "repair", unavailable)
    response = dashboard.post(f"/dashboard/tests/{scope}/end")
    assert response.status_code == 503
    with Session(database) as db:
        sid = db.execute(
            text("SELECT session_id FROM draft_tests WHERE id=:id"), {"id": scope}
        ).scalar()
        assert db.get(AgentSession, sid).status == "ended"
    monkeypatch.setattr(store, "repair", real_repair)
    assert dashboard.post(f"/dashboard/tests/{scope}/end").status_code == 200


def test_export_has_typed_success_bodies_without_settings_loading(monkeypatch):
    import json

    import scheduler.config
    from scripts.export_dashboard_contracts import render

    def forbidden():
        raise AssertionError("Export must not load deployment settings")

    monkeypatch.setattr(scheduler.config, "load_settings", forbidden)
    document = json.loads(render())
    for methods in document["paths"].values():
        for operation in methods.values():
            for code, response in operation.get("responses", {}).items():
                if code.startswith("2") and "content" in response:
                    assert response["content"]["application/json"]["schema"]


def test_production_patient_booking_callback_and_session_are_not_cloned(dashboard, seed, database):
    from datetime import timedelta

    from scheduler.models import (
        AgentSession,
        Appointment,
        Callback,
        ExceptionDay,
        Schedule,
        Tenant,
        User,
    )

    with Session(database) as db, db.begin():
        db.get(Tenant, seed["tenant_a"]).address_en = "Synthetic public address"
        parent_session = AgentSession(
            tenant_id=seed["tenant_a"],
            agent_id=seed["agent_a"],
            version_id=seed["version_a"],
            checkpoint={"private_marker": "parent-only"},
            token_jti_hash="synthetic-hash",
            expires_at=NOW + timedelta(minutes=30),
        )
        db.add(parent_session)
        db.flush()
        db.add(
            Callback(
                tenant_id=seed["tenant_a"],
                session_id=parent_session.id,
                agent_id=seed["agent_a"],
                phone="+923001234567",
            )
        )
        parent_appointment = Appointment(
            tenant_id=seed["tenant_a"],
            patient_id=seed["patient_a"],
            provider_id=seed["provider_a"],
            service_id=seed["service_a"],
            starts_at=NOW + timedelta(days=4, hours=5),
            ends_at=NOW + timedelta(days=4, hours=5, minutes=30),
            duration_min=30,
            buffer_before_min=0,
            buffer_after_min=0,
        )
        db.add(parent_appointment)
        db.add(
            ExceptionDay(
                tenant_id=seed["tenant_a"],
                provider_id=seed["provider_a"],
                start_date=NOW.date() + timedelta(days=8),
                end_date=NOW.date() + timedelta(days=8),
                kind="closed",
            )
        )
    body = start(dashboard, seed["agent_a"]).json()
    with Session(database) as db:
        child = db.execute(
            text("SELECT child_tenant_id FROM draft_tests WHERE id=:id"), {"id": body["id"]}
        ).scalar()
        assert db.get(Tenant, child).address_en == "Synthetic public address"
        assert list(db.scalars(select(Callback).where(Callback.tenant_id == child))) == []
        assert len(list(db.scalars(select(Appointment).where(Appointment.tenant_id == child)))) == 1
        assert (
            len(list(db.scalars(select(AgentSession).where(AgentSession.tenant_id == child)))) == 1
        )
        assert len(list(db.scalars(select(Schedule).where(Schedule.tenant_id == child)))) == 7
        child_provider = db.scalar(select(Provider.id).where(Provider.tenant_id == child))
        assert (
            db.scalar(select(ExceptionDay).where(ExceptionDay.tenant_id == child)).provider_id
            == child_provider
        )
        assert db.scalar(select(User).where(User.tenant_id == child)).active is False
        assert "parent-only" not in str(
            db.scalar(select(AgentSession).where(AgentSession.tenant_id == child)).checkpoint
        )


def test_concurrent_duplicate_start_creates_one_scope(dashboard, seed, database):
    from concurrent.futures import ThreadPoolExecutor

    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: start(dashboard, seed["agent_a"], key), range(2)))
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    with Session(database) as db:
        assert db.execute(text("SELECT count(*) FROM draft_tests")).scalar() == 1


def test_end_releases_real_slot_hold_and_clears_recovery(dashboard, seed, database):
    from scheduler.models import AgentSession, CommandReceipt

    body = start(dashboard, seed["agent_a"]).json()
    dashboard.app.state.brain_planner = BookingPlanner()
    person = body["fixtures"]["patients"][1]
    revision = 0
    for utterance in (person["phone"], person["dob"], "2026-11-04", "slots", "first"):
        response = send(dashboard, body["id"], revision, utterance)
        assert response.status_code == 200 and response.json()["tool_trace"][0]["ok"]
        revision = response.json()["revision"]
    with Session(database) as db:
        mapping = (
            db.execute(text("SELECT * FROM draft_tests WHERE id=:id"), {"id": body["id"]})
            .mappings()
            .one()
        )
        hold = db.get(AgentSession, mapping["session_id"]).checkpoint["hold"]
    assert dashboard.app.state.brain_store.holds(str(mapping["child_tenant_id"]), hold["provider"])
    assert dashboard.post(f"/dashboard/tests/{body['id']}/end").status_code == 200
    assert not dashboard.app.state.brain_store.holds(
        str(mapping["child_tenant_id"]), hold["provider"]
    )
    with Session(database) as db:
        state = db.get(AgentSession, mapping["session_id"]).checkpoint
        assert "hold" not in state and "cleanup_hold" not in state
        assert all(
            r.response is None and r.checkpoint is None
            for r in db.scalars(
                select(CommandReceipt).where(CommandReceipt.session_id == mapping["session_id"])
            )
        )


def test_planner_end_call_commits_terminal_turn_and_releases_scope(dashboard, seed, database):
    from brain.contracts import Action
    from scheduler.models import AgentSession, CommandReceipt, DraftTest

    class TerminalPlanner(BookingPlanner):
        def plan(self, state, utterance, config, facts):
            if utterance == "goodbye":
                return Action(name="end_call", arguments={})
            return super().plan(state, utterance, config, facts)

    body = start(dashboard, seed["agent_a"]).json()
    dashboard.app.state.brain_planner = TerminalPlanner()
    person = body["fixtures"]["patients"][1]
    revision = 0
    for utterance in (person["phone"], person["dob"], "2026-11-04", "slots", "first"):
        response = send(dashboard, body["id"], revision, utterance)
        assert response.status_code == 200 and response.json()["tool_trace"][0]["ok"]
        revision = response.json()["revision"]
    with Session(database) as db:
        scope = db.get(DraftTest, body["id"])
        sid, child = scope.session_id, scope.child_tenant_id
        hold = db.get(AgentSession, sid).checkpoint["hold"]
    assert dashboard.app.state.brain_store.holds(str(child), hold["provider"])

    response = send(dashboard, body["id"], revision, "goodbye", "terminal-command")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ended"
    assert response.json()["revision"] == revision + 1
    assert response.json()["tool_trace"] == [{"action": "end_call", "ok": True, "code": None}]
    assert response.json()["messages"][0]["text"]
    with Session(database) as db:
        scope = db.get(DraftTest, body["id"])
        runtime = db.get(AgentSession, sid)
        receipt = db.scalar(
            select(CommandReceipt).where(
                CommandReceipt.session_id == sid, CommandReceipt.command_id == "terminal-command"
            )
        )
        assert scope.status == "ended" and scope.cleanup_done
        assert runtime.status == "ended" and runtime.revoked_at is not None
        assert runtime.revision == receipt.revision == revision + 1
        assert not runtime.token_jti_hash and runtime.lease_until == NOW
        assert runtime.checkpoint["ended"] and "hold" not in runtime.checkpoint
        assert receipt.response is None and receipt.checkpoint is None
    assert not dashboard.app.state.brain_store.holds(str(child), hold["provider"])
    inspected = dashboard.get(f"/dashboard/tests/{body['id']}").json()
    assert inspected["status"] == "ended" and inspected["conversation"]["revision"] == revision + 1
    assert send(dashboard, body["id"], revision, "goodbye", "terminal-command").status_code == 410
    assert send(dashboard, body["id"], revision + 1, "hello").status_code == 410
    assert start(dashboard, seed["agent_a"]).status_code == 201


def test_terminal_turn_still_rolls_back_if_scope_expires_after_tool_execution(
    dashboard, seed, database, monkeypatch
):
    from datetime import timedelta

    from brain.contracts import Action
    from brain.tools import ToolEngine
    from scheduler.models import CommandReceipt, DraftTest

    class Planner(BookingPlanner):
        def plan(self, state, utterance, config, facts):
            return Action(
                name="end_call" if utterance == "goodbye" else "get_clinic_info",
                arguments={} if utterance == "goodbye" else {"topic": "services"},
            )

    scope = start(dashboard, seed["agent_a"]).json()["id"]
    dashboard.app.state.brain_planner = Planner()
    dashboard.app.state.clock = lambda: NOW + timedelta(minutes=29)
    assert send(dashboard, scope, 0, "information").status_code == 200
    execute = ToolEngine.execute

    def expires_after_effects(self, *args, **kwargs):
        response = execute(self, *args, **kwargs)
        dashboard.app.state.clock = lambda: NOW + timedelta(minutes=31)
        return response

    monkeypatch.setattr(ToolEngine, "execute", expires_after_effects)
    response = send(dashboard, scope, 1, "goodbye", "expired-terminal")
    assert response.status_code == 410, response.text
    with Session(database) as db:
        sid = db.get(DraftTest, scope).session_id
        assert (
            db.scalar(
                select(CommandReceipt).where(
                    CommandReceipt.session_id == sid,
                    CommandReceipt.command_id == "expired-terminal",
                )
            )
            is None
        )


@pytest.mark.parametrize("operation", ["publish", "rollback"])
def test_publication_and_test_allocation_share_lock_order(dashboard, seed, database, operation):
    """Pause publication at its first row lock; allocation must wait without a cycle."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    from sqlalchemy import event

    from brain.agent_config import AgentConfig

    path = f"/dashboard/agents/{seed['agent_a']}"
    draft = dashboard.get(path + "/draft").json()
    saved = dashboard.put(
        path + "/draft", json={"revision": draft["revision"], "config": AgentConfig().model_dump()}
    ).json()
    held, release, allocation_started = Event(), Event(), Event()
    pids = []
    engine = dashboard.app.state.engine
    sqlstates = []

    def database_error(context):
        sqlstates.append(getattr(context.original_exception, "sqlstate", None))

    def after_lock(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" not in statement:
            return
        pid = cursor.connection.info.backend_pid
        if not held.is_set():
            pids.append(pid)
            held.set()
            assert release.wait(10), "publication barrier timed out"

    def before_lock(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement and held.is_set():
            pid = cursor.connection.info.backend_pid
            if pid != pids[0]:
                if pid not in pids:
                    pids.append(pid)
                allocation_started.set()

    body = {"revision": saved["revision"]}
    if operation == "rollback":
        body["version_id"] = str(seed["version_a"])
    event.listen(engine, "handle_error", database_error)
    event.listen(engine, "after_cursor_execute", after_lock)
    event.listen(engine, "before_cursor_execute", before_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            publishing = pool.submit(
                dashboard.post,
                path + "/" + operation,
                json=body,
                headers={"Idempotency-Key": "barrier-publication"},
            )
            try:
                assert held.wait(5)
                allocating = pool.submit(
                    start, dashboard, seed["agent_a"], draft_revision=saved["revision"]
                )
                assert allocation_started.wait(5)
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    with database.connect() as observer:
                        waiting = observer.scalar(
                            text(
                                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": pids[1]},
                        )
                    if waiting:
                        break
                    sleep(0.01)
                else:
                    pytest.fail("allocation never reached a database lock wait")
            finally:
                release.set()
            published, allocated = publishing.result(timeout=10), allocating.result(timeout=10)
    finally:
        event.remove(engine, "handle_error", database_error)
        event.remove(engine, "after_cursor_execute", after_lock)
        event.remove(engine, "before_cursor_execute", before_lock)
    assert "40P01" not in sqlstates, sqlstates
    assert published.status_code == 200, published.text
    assert allocated.status_code in (201, 409), allocated.text
    if allocated.status_code == 409:
        assert allocated.json()["error"]["code"] == "revision_conflict", allocated.text
