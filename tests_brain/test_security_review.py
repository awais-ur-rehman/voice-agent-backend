"""Security-review regressions found in real runtime flows."""

import json
from uuid import uuid4

from fastapi.testclient import TestClient

from tests_brain.test_store import store as store  # noqa: F401
from tests_brain.test_tool_workflows import prepare
from tests_brain.test_tool_workflows import workflow as workflow  # noqa: F401


def test_prompt_does_not_export_dob_from_an_unclassified_first_turn():
    from brain.prompts import messages

    encoded = json.dumps(
        messages(
            {"task": "book", "fields": {}, "revision": 0},
            "Please book a visit. My date of birth is 1990-04-23.",
            {},
            {},
        )
    )
    assert "1990-04-23" not in encoded


def test_reschedule_cannot_change_service_used_for_hold_and_readback(workflow, seed):
    from scheduler.models import ProviderService, Service

    turn, state, db, *_ = workflow
    prepare(turn, state, seed)
    assert turn("book_appointment", text="yes")["ok"]
    assert turn("update_dialogue_state", {"task": "reschedule"})["ok"]
    assert turn("find_appointments")["ok"]
    assert turn("select_appointment", {"ref": state["appointments"][0]["ref"]})["ok"]
    other = Service(
        tenant_id=seed["tenant_a"], name="Long consultation", name_ur="طویل مشورہ", duration_min=60
    )
    db.add(other)
    db.flush()
    db.add(
        ProviderService(
            tenant_id=seed["tenant_a"], provider_id=seed["provider_a"], service_id=other.id
        )
    )
    db.flush()
    changed = turn(
        "update_dialogue_state", {"service_id": str(other.id), "date_phrase": "2026-11-04"}
    )
    assert not changed["ok"], "Reschedule must preserve the original appointment service"


def test_fee_digression_does_not_consume_verification_attempt(seed):
    from brain.contracts import parse_action
    from scheduler.config import Settings
    from scheduler.main import create_app
    from tests_api.conftest import APP_URL, NOW, PASSWORD, SECRET

    class FeePlanner:
        def close(self):
            pass

        def plan(self, state, text, config, facts):
            return parse_action({"name": "get_clinic_info", "arguments": {"topic": "fees"}})

    settings = Settings(
        database_url=APP_URL,
        jwt_secret=SECRET,
        runtime_secret="security-review-runtime-secret-distinct-2026",
        valkey_url="redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
        _env_file=None,
    )
    app = create_app(settings, clock=lambda: NOW)
    with TestClient(app) as client:
        app.state.brain_planner = FeePlanner()
        staff = client.post(
            "/auth/login", json={"email": "owner-a@example.com", "password": PASSWORD}
        ).json()["access_token"]
        created = client.post(
            "/agent-sessions",
            json={"agent_id": str(seed["agent_a"])},
            headers={"Authorization": "Bearer " + staff},
        ).json()
        client.headers["Authorization"] = "Bearer " + created["token"]
        prefix = "/runtime/sessions/" + created["session_id"]
        found = client.post(
            prefix + "/commands",
            json={
                "command_id": str(uuid4()),
                "expected_revision": 0,
                "action": {"name": "find_patient", "arguments": {"phone": "03001234567"}},
            },
        )
        assert found.status_code == 200
        response = client.post(
            prefix + "/turn",
            json={
                "command_id": str(uuid4()),
                "expected_revision": 1,
                "text": "What are your fees?",
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["result"]["ok"], result
        assert result["state"]["attempts"] == 0
        assert result["state"]["candidates"]


def test_callback_uses_explicit_caller_contact_after_family_subject_switch(workflow):
    from sqlalchemy import select

    from scheduler.models import Callback

    turn, state, db, *_ = workflow
    contact = turn("set_caller_contact", {"phone": "03007654321"})
    assert contact["ok"], contact
    assert turn("find_patient", {"phone": "03001234567"})["ok"]
    assert turn("verify_patient", {"factor": "Synthetic Patient"})["ok"]
    assert turn("update_dialogue_state", {"subject": "other"})["ok"]
    assert turn("request_handoff")["ok"]
    db.flush()
    callback = db.scalar(select(Callback))
    assert callback.phone == "+923007654321"
    assert callback.phone != "+923001234567"


def test_delayed_cache_repair_cannot_restore_state_after_end(store):
    from brain.routes import repair

    identity = str(uuid4())
    stale = {"revision": 5, "history": [{"role": "assistant", "content": "private readback"}]}
    assert repair(store, identity, stale)
    assert repair(store, identity, {"revision": 6, "ended": True})
    assert repair(store, identity, stale)
    current = store.load(identity)
    assert current is None or current.get("ended")
    assert "private readback" not in str(current)
