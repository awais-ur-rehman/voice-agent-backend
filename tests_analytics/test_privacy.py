"""Consent is separate from scheduling; retention and erasure are authoritative."""

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics.models import AnalyticsSettings, CallRecord, Transcript
from scheduler.config import Settings
from scheduler.main import create_app
from tests_api.conftest import APP_URL, NOW, PASSWORD, SECRET
from tests_brain.conftest import seed  # noqa: F401


@pytest.fixture
def console(database, seed):  # noqa: F811
    with Session(database) as db, db.begin():
        db.add(AnalyticsSettings(tenant_id=seed["tenant_a"], capture_enabled=True))
    app = create_app(
        Settings(
            database_url=APP_URL,
            jwt_secret=SECRET,
            runtime_secret="runtime-route-test-secret-different-40chars",
            valkey_url="redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
            _env_file=None,
        ),
        clock=lambda: NOW,
    )

    class NeverPlanner:
        def close(self):
            pass

        def plan(self, *args):
            raise AssertionError("pending consent or safety must never invoke planner")

    with TestClient(app) as client:
        app.state.brain_planner = NeverPlanner()
        token = client.post(
            "/auth/login", json={"email": "owner-a@example.com", "password": PASSWORD}
        ).json()["access_token"]
        created = client.post(
            "/agent-sessions",
            headers={"Authorization": "Bearer " + token},
            json={"agent_id": str(seed["agent_a"])},
        ).json()
        client.headers["Authorization"] = "Bearer " + created["token"]
        revision = 0

        def send(text, action=None):
            nonlocal revision
            body = {"command_id": str(uuid4()), "expected_revision": revision, "text": text}
            if action:
                body["action"] = action
            response = client.post(
                f"/runtime/sessions/{created['session_id']}/" + ("commands" if action else "turn"),
                json=body,
            )
            assert response.status_code == 200, response.text
            revision = response.json()["revision"]
            return response.json()

        yield created, send, app
    app.state.engine.dispose()


def test_pending_notice_exact_consent_and_unretained_decision(console, database):
    created, send, _ = console
    assert "record" in created["state"]["greeting"]["en"].lower()
    assert "ریکارڈ" in created["state"]["greeting"]["en"]
    result = send("yes but book for Demo Child")
    assert result["state"]["task"] == "book" and not result["state"].get("pending_patient")
    with Session(database) as db:
        assert db.get(CallRecord, UUID(created["session_id"])).consent == "pending"
        assert list(db.scalars(select(Transcript))) == []
    send("  جی ہاں! ")
    with Session(database) as db:
        row = db.get(CallRecord, UUID(created["session_id"]))
        assert row.consent == "accepted" and row.consent_at == NOW
        assert row.consent_notice_version == "recording-v1"
        assert list(db.scalars(select(Transcript))) == []
    send("What are your opening hours?")
    with Session(database) as db:
        texts = [x.text for x in db.scalars(select(Transcript))]
        assert any("opening hours" in text for text in texts)
        assert not any("جی ہاں" in text or "Demo Child" in text for text in texts)


def test_emergency_precedes_pending_consent_without_text_retention(console, database):
    created, send, _ = console
    result = send("I have chest pain")
    assert result["result"]["ok"] and result["state"]["handoff"]
    with Session(database) as db:
        row = db.get(CallRecord, UUID(created["session_id"]))
        assert row is not None and row.outcome == "handoff"
        assert list(db.scalars(select(Transcript))) == []


def test_decline_and_explicit_action_cannot_bypass_pending(console, database):
    created, send, _ = console
    result = send("yes book it", {"name": "find_patient", "arguments": {"phone": "03001234567"}})
    assert not result["state"].get("candidates")
    send("No thanks.")
    send("What are your opening hours?")
    with Session(database) as db:
        row = db.get(CallRecord, UUID(created["session_id"]))
        assert row.consent == "declined"
        assert list(db.scalars(select(Transcript))) == []


def test_redaction_uses_before_after_and_current_identity_arguments(console, database):
    from analytics.models import TurnEvent

    created, send, _ = console
    send("yes")
    send(
        "My phone 03007654321 email synthetic@example.com DOB ۱۹۹۰/۰۱/۱۵",
        {"name": "find_patient", "arguments": {"phone": "03007654321"}},
    )
    send(
        "Demo Child ڈیمو بچہ",
        {
            "name": "prepare_patient",
            "arguments": {"name": "Demo Child", "name_ur": "ڈیمو بچہ", "phone": "03007654321"},
        },
    )
    send(
        "Replace Demo Child with New Child",
        {
            "name": "prepare_patient",
            "arguments": {"name": "New Child", "name_ur": "نیا بچہ", "phone": "03007654321"},
        },
    )
    with Session(database) as db:
        rows = list(db.scalars(select(Transcript)))
        assert len(rows) == 6
        text = " ".join(row.text for row in rows)
        for private in (
            "03007654321",
            "synthetic@example.com",
            "۱۹۹۰",
            "Demo Child",
            "New Child",
            "ڈیمو بچہ",
            "نیا بچہ",
        ):
            assert private not in text
        assert "[redacted]" in text
        safe = str([row.safe_metadata for row in db.scalars(select(TurnEvent))])
        assert "Demo Child" not in safe and "phone" not in safe


def _owner(seed):  # noqa: F811
    from scheduler.db import Principal

    return Principal(seed["owner_a"], seed["tenant_a"], "synthetic", "owner")


def _append(db, session_id, *, now=NOW, occurred_at=NOW):
    from analytics.models import TurnEvent
    from analytics.transcripts import append_text

    call = db.get(CallRecord, session_id)
    event_id = uuid4()
    db.add(
        TurnEvent(
            tenant_id=call.tenant_id,
            session_id=session_id,
            event_id=event_id,
            kind="turn.user",
            language="en",
            occurred_at=occurred_at,
        )
    )
    db.flush()
    append_text(
        db,
        session_id,
        speaker="user",
        value="Synthetic safe phrase",
        literals=(),
        event_id=event_id,
        occurred_at=occurred_at,
        now=now,
    )


def _wait_for_block(db, pid, holder):
    import time

    from sqlalchemy import text

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if holder in db.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}):
            return
        time.sleep(0.01)
    pytest.fail("controlled contender never blocked on expected privacy transaction")


@pytest.mark.parametrize("first", ["insert", "delete"])
def test_concurrent_delete_insert_serializes_on_call_tombstone(console, database, seed, first):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import text

    from analytics.transcripts import delete_transcripts
    from scheduler.db import tenant_context

    created, send, _ = console
    send("yes")
    sid = UUID(created["session_id"])
    ready = Event()
    other_pid = []
    with Session(database) as first_db:
        tenant_context(first_db, seed["tenant_a"])
        holder = first_db.scalar(text("SELECT pg_backend_pid()"))
        if first == "insert":
            _append(first_db, sid)
        else:
            delete_transcripts(first_db, _owner(seed), sid, NOW)

        def contender():
            with Session(database) as second_db, second_db.begin():
                tenant_context(second_db, seed["tenant_a"])
                other_pid.append(second_db.scalar(text("SELECT pg_backend_pid()")))
                ready.set()
                if first == "insert":
                    delete_transcripts(second_db, _owner(seed), sid, NOW)
                else:
                    _append(second_db, sid)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(contender)
            try:
                assert ready.wait(3)
                _wait_for_block(first_db, other_pid[0], holder)
            finally:
                first_db.commit()
            future.result(timeout=5)
    with Session(database) as db, db.begin():
        assert db.get(CallRecord, sid).transcript_deleted_at == NOW
        _append(db, sid)
        assert list(db.scalars(select(Transcript))) == []


@pytest.mark.parametrize("first", ["insert", "disable"])
def test_concurrent_disable_insert_holds_current_settings_lock(console, database, seed, first):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import text

    from analytics.contracts import SettingsUpdate
    from analytics.transcripts import update_settings
    from scheduler.db import tenant_context

    created, send, _ = console
    send("yes")
    sid = UUID(created["session_id"])
    ready = Event()
    other_pid = []
    update = SettingsUpdate(revision=1, capture_enabled=False, retention_days=7)
    with Session(database) as first_db:
        tenant_context(first_db, seed["tenant_a"])
        holder = first_db.scalar(text("SELECT pg_backend_pid()"))
        if first == "insert":
            _append(first_db, sid)
        else:
            update_settings(first_db, _owner(seed), update, NOW)

        def contender():
            with Session(database) as second_db, second_db.begin():
                tenant_context(second_db, seed["tenant_a"])
                other_pid.append(second_db.scalar(text("SELECT pg_backend_pid()")))
                ready.set()
                if first == "insert":
                    update_settings(second_db, _owner(seed), update, NOW)
                else:
                    _append(second_db, sid)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(contender)
            try:
                assert ready.wait(3)
                _wait_for_block(first_db, other_pid[0], holder)
            finally:
                first_db.commit()
            future.result(timeout=5)
    with Session(database) as db, db.begin():
        _append(db, sid)
        assert len(list(db.scalars(select(Transcript)))) == (1 if first == "insert" else 0)


def test_retention_shortening_cannot_be_undone_and_staff_cannot_read(console, database, seed):  # noqa: F811
    from datetime import timedelta

    from analytics.contracts import SettingsUpdate
    from analytics.transcripts import read_transcripts, update_settings
    from scheduler.db import Principal
    from scheduler.errors import AppError

    created, send, _ = console
    send("yes")
    sid = UUID(created["session_id"])
    later = NOW + timedelta(days=2)
    with Session(database) as db, db.begin():
        _append(db, sid)
        update_settings(
            db,
            _owner(seed),
            SettingsUpdate(revision=1, capture_enabled=True, retention_days=1),
            later,
        )
        assert read_transcripts(db, _owner(seed), sid, later) == []
        _append(db, sid, now=later)
        update_settings(
            db,
            _owner(seed),
            SettingsUpdate(revision=2, capture_enabled=True, retention_days=7),
            later,
        )
        assert read_transcripts(db, _owner(seed), sid, later) == []
        with pytest.raises(AppError, match="forbidden"):
            read_transcripts(
                db, Principal(seed["staff_a"], seed["tenant_a"], "synthetic", "staff"), sid, later
            )


@pytest.mark.parametrize(
    "phrase,decision",
    [
        ("YES PLEASE!!", "accepted"),
        ("I agree.", "accepted"),
        ("allow recording", "accepted"),
        ("جی", "accepted"),
        ("ہاں", "accepted"),
        ("اجازت ہے", "accepted"),
        ("No thanks!", "declined"),
        ("don't record", "declined"),
        ("do not record", "declined"),
        ("نہیں", "declined"),
        ("ریکارڈ نہ کریں", "declined"),
        ("اجازت نہیں", "declined"),
        ("yesterday", None),
        ("yes but don't record", None),
        ("نہیں لیکن بک کریں", None),
    ],
)
def test_consent_is_whole_phrase_only(phrase, decision):
    from analytics.privacy import consent_decision

    assert consent_decision(phrase) == decision


def test_native_consent_rejected_audio_and_trusted_interruptions(database, seed):  # noqa: F811
    from datetime import timedelta

    from analytics.models import TurnEvent
    from brain.voice_sessions import create_voice
    from tests_analytics.test_lifecycle import _voice_settings

    settings = _voice_settings().model_copy(update={"database_url": APP_URL})
    # Build Settings normally so secrets and URLs retain validated types.
    settings = Settings(
        **(
            settings.model_dump()
            | {
                "valkey_url": "redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
                "voice_bootstrap_secret": "bootstrap-capture-only-test-secret-2026",
            }
        ),
        _env_file=None,
    )
    with Session(database) as db, db.begin():
        db.add(AnalyticsSettings(tenant_id=seed["tenant_a"], capture_enabled=True))
        db.flush()
        voice = create_voice(db, _owner(seed), seed["agent_a"], "native-privacy", NOW, settings)
        voice.status = "active"
        voice_id, sid = voice.id, voice.session_id
    now = [NOW]
    app = create_app(settings, clock=lambda: now[0])
    with TestClient(app) as client:

        def forbidden(*args):
            raise AssertionError("consent/rejected audio must not call planner")

        app.state.brain_planner.plan = forbidden
        claim = client.post(
            "/voice/worker/claim",
            headers={
                "Authorization": "Bearer " + settings.voice_bootstrap_secret.get_secret_value()
            },
            json={
                "tenant_id": str(seed["tenant_a"]),
                "voice_id": str(voice_id),
                "worker_id": str(uuid4()),
            },
        )
        assert claim.status_code == 200, claim.text
        grant = claim.json()
        assert "ریکارڈ" in grant["greetings"]["en"]
        headers = {
            "Authorization": "Bearer " + grant["token"],
            "X-Runtime-Capability": grant["runtime_token"],
        }
        path = f"/voice/worker/sessions/{sid}"
        rejected = {
            "command_id": "private name 03001234567",
            "expected_revision": 0,
            "text": "private name 03001234567",
            "language": "en",
            "confidence": 0.4,
            "uncertain": True,
            "utterance_started_at": NOW.isoformat(),
        }
        for _ in range(2):
            assert client.post(path + "/turn", headers=headers, json=rejected).status_code == 422
        accepted = rejected | {
            "command_id": "consent",
            "text": "yes please",
            "confidence": 0.99,
            "uncertain": False,
        }
        response = client.post(path + "/turn", headers=headers, json=accepted)
        assert response.status_code == 200, response.text
        assert response.json()["provider_requests"] == 0
        for index in range(3):
            response = client.post(
                path + "/interrupt", headers=headers, json={"command_id": f"interrupt-{index}"}
            )
            assert response.status_code == 200, response.text
        now[0] = NOW + timedelta(seconds=5)
        assert client.post(path + "/heartbeat", headers=headers).status_code == 200
        with Session(database) as db:
            call = db.get(CallRecord, sid)
            assert call.consent == "accepted" and call.expires_at == NOW + timedelta(seconds=50)
            assert {"low_stt_confidence", "interruptions"} <= set(call.quality_flags)
            assert (
                len(list(db.scalars(select(TurnEvent).where(TurnEvent.kind == "metric.turn")))) == 1
            )
            assert (
                len(list(db.scalars(select(TurnEvent).where(TurnEvent.kind == "turn.user")))) == 1
            )
            assert list(db.scalars(select(Transcript))) == []
        from brain.provider import ProviderError

        def failed_provider(*args):
            raise ProviderError("synthetic failure", requests=1)

        app.state.brain_planner.plan = failed_provider
        failed = client.post(
            path + "/turn",
            headers=headers,
            json=accepted
            | {
                "command_id": "failed-provider",
                "expected_revision": 4,
                "text": "Please help with an appointment",
            },
        )
        assert failed.status_code == 200 and not failed.json()["result"]["ok"]
        with Session(database) as db:
            assert (
                len(list(db.scalars(select(TurnEvent).where(TurnEvent.kind == "turn.user")))) == 2
            )
            assert list(db.scalars(select(TurnEvent).where(TurnEvent.kind == "tool.result"))) == []
            assert (
                len(
                    list(
                        db.scalars(
                            select(TurnEvent).where(
                                TurnEvent.kind == "turn.assistant",
                                TurnEvent.safe_metadata["error_code"].astext
                                == "provider_unavailable",
                            )
                        )
                    )
                )
                == 1
            )
    app.state.engine.dispose()


@pytest.mark.parametrize(
    "phrase,terminal",
    [
        ("I want to speak to a person", "handoff"),
        ("end this call", "ended"),
        ("مجھے کسی انسان سے بات کرنی ہے", "handoff"),
        ("میں کسی انسان سے بات کرنا چاہتا ہوں", "handoff"),
        ("میں کسی انسان سے بات کرنا چاہتی ہوں", "handoff"),
        ("خدا حافظ", "ended"),
    ],
)
def test_pending_consent_allows_exact_ordinary_human_and_end_requests(
    console, database, phrase, terminal
):
    created, send, _ = console
    result = send(phrase)
    assert result["state"].get(terminal) is True
    with Session(database) as db:
        call = db.get(CallRecord, UUID(created["session_id"]))
        assert call.outcome == ("handoff" if terminal == "handoff" else "abandoned")
        assert list(db.scalars(select(Transcript))) == []


def test_pending_consent_does_not_treat_incidental_end_as_hangup(console, database):
    created, send, _ = console
    result = send("What happens at the end of a visit?")
    assert not result["state"].get("ended") and not result["state"].get("handoff")
    with Session(database) as db:
        assert db.get(CallRecord, UUID(created["session_id"])).consent == "pending"
        assert list(db.scalars(select(Transcript))) == []
