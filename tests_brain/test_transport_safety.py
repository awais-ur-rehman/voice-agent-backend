"""Transport safety regressions against real SQL and Valkey state."""

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from scheduler.models import AgentSession, Callback, CommandReceipt
from tests_api.conftest import APP_URL, NOW, PASSWORD, SECRET


@pytest.fixture
def transport(seed):
    from brain.contracts import parse_action
    from scheduler.config import Settings
    from scheduler.main import create_app

    class RecordingPlanner:
        def __init__(self):
            self.calls = []

        def close(self):
            pass

        def plan(self, state, text, config, facts):
            self.calls.append(text)
            return parse_action({"name": "ask_caller", "arguments": {}})

    settings = Settings(
        database_url=APP_URL,
        jwt_secret=SECRET,
        runtime_secret="transport-safety-runtime-secret-distinct-2026",
        valkey_url="redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
        _env_file=None,
    )
    app = create_app(settings, clock=lambda: NOW)
    with TestClient(app) as client:
        planner = RecordingPlanner()
        app.state.brain_planner.close()
        app.state.brain_planner = planner
        staff = client.post(
            "/auth/login", json={"email": "owner-a@example.com", "password": PASSWORD}
        ).json()["access_token"]
        created = client.post(
            "/agent-sessions",
            json={"agent_id": str(seed["agent_a"])},
            headers={"Authorization": "Bearer " + staff},
        ).json()
        client.headers["Authorization"] = "Bearer " + created["token"]
        yield client, "/runtime/sessions/" + created["session_id"], planner


@pytest.mark.parametrize(
    "text",
    ["I cannot breathe", "I can't breathe", "I have difficulty breathing", "مجھے سانس نہیں آ رہا"],
)
def test_emergency_bypasses_planner_and_persists_handoff(transport, database, text):
    client, prefix, planner = transport
    response = client.post(
        prefix + "/turn", json={"command_id": str(uuid4()), "expected_revision": 0, "text": text}
    )
    assert response.status_code == 200, response.text
    assert planner.calls == [], "Emergency routing must run before any model request"
    data = response.json()
    assert data["result"]["ok"]
    assert data["state"]["handoff"]
    assert data["provider_requests"] == 0
    assert data["result"]["say"]["en"] and data["result"]["say"]["ur"]
    with Session(database) as db:
        callback = db.scalar(select(Callback))
        assert callback is not None and callback.reason == "emergency"


def test_three_trusted_silence_events_reprompt_then_end(transport):
    client, prefix, planner = transport
    for revision, delay in enumerate((6, 12)):
        response = client.post(
            prefix + "/events",
            json={"command_id": str(uuid4()), "expected_revision": revision, "kind": "silence"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["result"]["ok"]
        assert data["result"]["data"]["retry_after_seconds"] == delay
        assert not data["state"].get("ended")
    response = client.post(
        prefix + "/events",
        json={"command_id": str(uuid4()), "expected_revision": 2, "kind": "silence"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"]["ended"]
    assert client.get(prefix).status_code == 401
    assert planner.calls == []


def test_command_expiring_during_effect_rolls_back_effect_and_receipt(database, seed):
    from brain.security import decode_runtime
    from brain.sessions import command
    from scheduler.errors import AppError
    from tests_brain.test_sessions import setup_session

    db, settings, created = setup_session(database, seed)
    with db:
        actor = decode_runtime(db, created["token"], settings, now=NOW)
        db.rollback()
        ticks = iter((NOW, NOW + timedelta(minutes=31)))

        def effect(db, state):
            db.add(
                Callback(
                    tenant_id=actor.tenant_id,
                    session_id=actor.id,
                    agent_id=actor.agent_id,
                    reason="caller_requested",
                )
            )
            db.flush()
            state["verified"] = True
            return {"ok": True, "data": {}, "say": {"en": "Done", "ur": "ہو گیا"}, "error": None}

        with pytest.raises(AppError):
            with db.begin():
                command(
                    db,
                    actor,
                    "expires-mid-effect",
                    0,
                    {},
                    NOW,
                    SECRET,
                    effect,
                    clock=lambda: next(ticks),
                )
        assert db.scalar(select(Callback)) is None
        assert db.scalar(select(CommandReceipt)) is None
        row = db.get(AgentSession, actor.id)
        assert row.revision == 0
        assert not row.checkpoint["verified"]


def test_silence_event_contract_accepts_trusted_silence_event():
    from brain.routes import Event

    event = Event.model_validate(
        {"command_id": "silence-1", "expected_revision": 0, "kind": "silence"}
    )
    assert event.kind == "silence"
