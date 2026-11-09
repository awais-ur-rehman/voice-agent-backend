"""Cross-layer fault injection over the isolated PostgreSQL/Valkey fixtures."""

import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from analytics.models import OperationEvent, Transcript, UsageInvocation
from brain.provider import Nemotron
from scheduler.config import Settings
from scheduler.main import create_app
from scheduler.models import AgentSession, Appointment, Audit, CommandReceipt, Idempotency
from scheduler.security import access_token
from tests_api.conftest import APP_URL, NOW, SECRET
from tests_api.conftest import client as client  # noqa: F401
from tests_api.conftest import owner as owner  # noqa: F401
from tests_api.test_appointments import book

VALKEY = "valkey://:phase2_valkey_test_only@127.0.0.1:56379/0"
CANARY = "synthetic-private-canary"


@pytest.fixture
def request_log(monkeypatch):
    stream = io.StringIO()
    logger = logging.getLogger("scheduler.requests")
    monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(stream)])
    return stream


@pytest.mark.parametrize("primary_status", [429, 503])
def test_late_secondary_cannot_change_timed_out_turn_during_drain(
    primary_status, seed, database, request_log
):
    secondary_entered, secondary_release = threading.Event(), threading.Event()
    requests = []

    def fake_http(request):
        requests.append(request.url.host)
        if request.url.host == "primary.invalid":
            return httpx2.Response(primary_status, text=CANARY)
        assert request.url.host == "secondary.invalid"
        secondary_entered.set()
        assert secondary_release.wait(8)
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": '{"name":"ask_caller","arguments":{}}'}}]},
        )

    settings = Settings(
        database_url=APP_URL,
        valkey_url=VALKEY,
        jwt_secret=SECRET,
        runtime_secret="synthetic-chaos-runtime-secret-distinct",
        llm_api_key="synthetic-chaos-primary",
        llm_base_url="https://primary.invalid/v1",
        llm_secondary_api_key="synthetic-chaos-secondary",
        llm_secondary_base_url="https://secondary.invalid/v1",
        llm_secondary_model="synthetic/secondary",
        llm_timeout_seconds=1.5,
        _env_file=None,
    )
    app = create_app(settings, clock=lambda: NOW)
    app.state.brain_planner.close()
    planner = Nemotron(settings, transport=httpx2.MockTransport(fake_http))
    app.state.brain_planner = planner
    staff = {"Authorization": f"Bearer {access_token(seed['owner_a'], seed['tenant_a'], settings)}"}
    with TestClient(app) as client:
        started = client.post(
            "/agent-sessions", headers=staff, json={"agent_id": str(seed["agent_a"])}
        )
        assert started.status_code == 201
        sid = started.json()["session_id"]
        runtime = {"Authorization": "Bearer " + started.json()["token"]}
        url = f"/runtime/sessions/{sid}"
        command = {
            "command_id": "synthetic-late-provider",
            "expected_revision": 0,
            "text": "I would like to schedule an appointment.",
        }
        with ThreadPoolExecutor(1) as executor:
            try:
                pending = executor.submit(client.post, url + "/turn", headers=runtime, json=command)
                assert secondary_entered.wait(3)
                assert not pending.done()
                settings.drain_mode = True
                assert client.get("/health/live").status_code == 200
                readiness = client.get("/health/ready")
                assert readiness.status_code == 503
                assert readiness.json()["error"]["code"] == "service_draining"
                refused = client.post(
                    "/agent-sessions", headers=staff, json={"agent_id": str(seed["agent_a"])}
                )
                assert refused.status_code == 503 and refused.headers["retry-after"] == "3"
                original = pending.result(timeout=5)
                assert original.status_code == 200
                assert original.headers["cache-control"] == "no-store"
                body = original.json()
                assert body["provider_requests"] == 2
                assert body["result"]["error"]["code"] == "upstream_timeout"
                assert body["revision"] == 1
                assert body["executed_action"] is None
                assert not planner.drain(0)
            finally:
                secondary_release.set()
        assert planner.drain(3)
        app.state.brain_store.delete(sid)
        assert app.state.brain_store.load(sid) is None
        retry = client.post(url + "/turn", headers=runtime, json=command)
        assert retry.status_code == 200
        assert retry.json()["result"] == body["result"]
        assert retry.json()["revision"] == 1 and retry.json()["provider_requests"] == 0
        assert app.state.brain_store.load(sid)["revision"] == 1
        assert requests == ["primary.invalid", "secondary.invalid"]
        assert CANARY not in original.text + retry.text
        app.state.brain_store.delete(sid)
    assert CANARY not in request_log.getvalue()
    with Session(database) as db:
        row = db.get(AgentSession, UUID(sid))
        assert row.revision == 1 and row.checkpoint["failures"] == 1
        assert len(db.scalars(select(CommandReceipt)).all()) == 1
        usage = {item.route: item for item in db.scalars(select(UsageInvocation))}
        assert set(usage) == {"primary", "secondary"}
        assert usage["primary"].status == "failure"
        assert usage["secondary"].status == "timeout"
        assert usage["secondary"].fallback_from == usage["primary"].invocation_id
        assert all(
            item.input_tokens is None and item.output_tokens is None for item in usage.values()
        )
        assert db.scalar(select(func.count()).select_from(OperationEvent)) == 0
        assert db.scalar(select(func.count()).select_from(Transcript)) == 0


@pytest.mark.parametrize(
    ("fault", "status", "code"),
    [("serialization", 409, "transaction_conflict"), ("connection", 503, "database_unavailable")],
)
def test_booking_failure_after_flush_rolls_back_and_same_key_retries(
    owner, seed, database, request_log, fault, status, code
):
    engine = owner.app.state.engine
    fired = []

    def fail_audit(conn, cursor, statement, parameters, context, executemany):
        if not statement.startswith("INSERT INTO audit_log"):
            return
        # The appointment and pending idempotency row are already flushed in
        # this transaction. Use the real connection to prove that boundary.
        cursor.execute("SELECT count(*) FROM appointments")
        fired.append(cursor.fetchone()[0])
        if fault == "serialization":
            cursor.execute(
                "DO $$ BEGIN RAISE EXCEPTION 'synthetic-private-canary' USING ERRCODE='40001'; END $$"
            )
        else:
            raise psycopg.OperationalError(CANARY)

    key = str(uuid4())
    event.listen(engine, "before_cursor_execute", fail_audit)
    try:
        response = book(owner, seed, key=key)
    finally:
        event.remove(engine, "before_cursor_execute", fail_audit)
    assert fired == [1]
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert CANARY not in response.text + request_log.getvalue()
    with Session(database) as db:
        for model in (Appointment, Idempotency, Audit):
            assert db.scalar(select(func.count()).select_from(model)) == 0
    retried = book(owner, seed, key=key)
    assert retried.status_code == 201
    assert book(owner, seed, key=key).json() == retried.json()
    with Session(database) as db:
        assert db.scalar(text("SELECT count(*) FROM appointments")) == 1
        assert db.scalar(text("SELECT count(*) FROM idempotency_records")) == 1
        assert (
            db.scalar(text("SELECT count(*) FROM audit_log WHERE action='appointment.booked'")) == 1
        )
