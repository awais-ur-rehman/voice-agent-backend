from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from analytics.models import CallRecord, TaskRecord
from brain.security import decode_runtime
from brain.sessions import purge_recovery, renew_session
from tests_api.conftest import NOW
from tests_brain.test_sessions import setup_session


def test_renew_mirrors_deadline_and_expiry_preserves_safe_record(database, seed):  # noqa: F811
    db, settings, created = setup_session(database, seed)
    with db:
        actor = decode_runtime(db, created["token"], settings, now=NOW)
        renew_session(db, actor, NOW + timedelta(minutes=5), settings)
        call = db.get(CallRecord, UUID(created["session_id"]))
        assert call is not None
        assert call.expires_at == NOW + timedelta(minutes=35)
        purge_recovery(db, NOW + timedelta(days=2))
        assert call.status == "expired" and call.outcome == "abandoned"
        assert call.end_reason == "timeout" and call.ended_at == NOW + timedelta(minutes=35)
        assert list(db.scalars(select(TaskRecord))) == []


from tests_brain.conftest import seed  # noqa: E402,F401
from tests_brain.test_draft_tests import dashboard, start  # noqa: E402,F401


def _voice_settings():
    from scheduler.config import Settings
    from tests_api.conftest import SECRET

    return Settings(
        database_url="postgresql://unused",
        jwt_secret=SECRET,
        runtime_secret="runtime-capture-only-test-secret-2026",
        voice_worker_secret="worker-capture-only-test-secret-2026",
        livekit_api_key="synthetic",
        livekit_api_secret="livekit-capture-only-test-secret-2026",
        _env_file=None,
    )


def test_voice_app_clock_provisioning_deadline_matches_authoritative_cleanup(database, seed):  # noqa: F811
    from sqlalchemy.orm import Session

    from brain.voice_routes import _expired
    from brain.voice_sessions import create_voice
    from scheduler.db import Principal
    from scheduler.models import AgentSession

    with Session(database) as db:
        staff = Principal(seed["owner_a"], seed["tenant_a"], "synthetic", "owner")
        voice = create_voice(db, staff, seed["agent_a"], "voice-admit", NOW, _voice_settings())
        call = db.get(CallRecord, voice.session_id)
        assert call.channel == "voice" and call.expires_at == NOW + timedelta(seconds=30)
        assert _expired(db.get(AgentSession, voice.session_id), voice, NOW + timedelta(seconds=30))


def test_voice_claim_disconnect_resume_reclaim_and_cleanup_mirror(database, seed):  # noqa: F811
    from uuid import uuid4

    from sqlalchemy.orm import Session

    from brain.voice_routes import _end_locked
    from brain.voice_sessions import claim_worker, create_voice, disconnect_voice, resume_voice
    from brain.voice_tokens import decode_worker
    from scheduler.db import Principal
    from scheduler.models import AgentSession

    settings = _voice_settings()
    with Session(database) as db:
        staff = Principal(seed["owner_a"], seed["tenant_a"], "synthetic", "owner")
        voice = create_voice(db, staff, seed["agent_a"], "voice-cycle", NOW, settings)
        voice.status = "active"
        grant = claim_worker(db, voice.id, staff.tenant_id, uuid4(), NOW, settings)
        call = db.get(CallRecord, voice.session_id)
        assert call.expires_at == NOW + timedelta(seconds=45)
        authority, _ = decode_worker(settings, grant.token, NOW)
        actor = decode_runtime(db, grant.runtime_token, settings, now=NOW)
        disconnect_voice(db, actor, authority, NOW + timedelta(seconds=5))
        assert call.status == "disconnected" and call.expires_at == NOW + timedelta(seconds=35)
        resume_voice(db, staff, voice.session_id, NOW + timedelta(seconds=10))
        assert call.status == "active" and call.expires_at == NOW + timedelta(minutes=10)
        claim_worker(db, voice.id, staff.tenant_id, uuid4(), NOW + timedelta(seconds=10), settings)
        assert call.expires_at == NOW + timedelta(seconds=55)
        claim_worker(db, voice.id, staff.tenant_id, uuid4(), NOW + timedelta(seconds=30), settings)
        assert call.expires_at == NOW + timedelta(seconds=75)
        _end_locked(
            db,
            db.get(AgentSession, voice.session_id),
            voice,
            None,
            NOW + timedelta(seconds=90),
            "ended",
        )
        assert call.status == "expired" and call.end_reason == "timeout"
        assert call.ended_at == NOW + timedelta(seconds=75)


def test_voice_failed_provisioning_is_error_and_empty_staff_end_is_abandoned(database, seed):  # noqa: F811
    from sqlalchemy.orm import Session

    from brain.voice_routes import _end_locked
    from brain.voice_sessions import create_voice
    from scheduler.db import Principal
    from scheduler.models import AgentSession

    with Session(database) as db:
        staff = Principal(seed["owner_a"], seed["tenant_a"], "synthetic", "owner")
        failed = create_voice(db, staff, seed["agent_a"], "failed", NOW, _voice_settings())
        _end_locked(db, db.get(AgentSession, failed.session_id), failed, None, NOW, "failed")
        call = db.get(CallRecord, failed.session_id)
        assert (
            call.status == "failed"
            and call.outcome == "error"
            and call.end_reason == "provisioning_failed"
        )
        ended = create_voice(db, staff, seed["agent_a"], "ended", NOW, _voice_settings())
        _end_locked(db, db.get(AgentSession, ended.session_id), ended, None, NOW, "ended")
        call = db.get(CallRecord, ended.session_id)
        assert call.outcome == "abandoned" and call.end_reason == "staff_end"


def test_draft_child_capture_stays_child_only_and_revocation_ends_it(dashboard, seed, database):  # noqa: F811
    from sqlalchemy.orm import Session

    from scheduler.models import DraftTest

    response = start(dashboard, seed["agent_a"])
    assert response.status_code == 201, response.text
    scope = response.json()["id"]
    with Session(database) as db:
        mapping = db.get(DraftTest, UUID(scope))
        call = db.get(CallRecord, mapping.session_id)
        assert call.purpose == "test" and call.tenant_id == mapping.child_tenant_id
        assert call.agent_id == mapping.child_agent_id and call.tenant_id != seed["tenant_a"]
        assert (
            list(db.scalars(select(CallRecord).where(CallRecord.tenant_id == seed["tenant_a"])))
            == []
        )
        sid = call.session_id
    assert dashboard.post(f"/dashboard/tests/{scope}/end").status_code == 200
    with Session(database) as db:
        assert db.get(CallRecord, sid).end_reason == "staff_end"


@pytest.mark.parametrize("seconds,allowed", [(44, True), (45, False), (46, False)])
def test_worker_reclaim_cannot_revive_beyond_existing_cleanup_grace(
    database,
    seed,  # noqa: F811
    seconds,
    allowed,
):  # noqa: F811
    from uuid import uuid4

    from sqlalchemy.orm import Session

    from brain.voice_sessions import claim_worker, create_voice
    from scheduler.db import Principal
    from scheduler.errors import AppError

    settings = _voice_settings()
    with Session(database) as db:
        staff = Principal(seed["owner_a"], seed["tenant_a"], "synthetic", "owner")
        voice = create_voice(db, staff, seed["agent_a"], "reclaim-cutoff", NOW, settings)
        voice.status = "active"
        claim_worker(db, voice.id, staff.tenant_id, uuid4(), NOW, settings)
        before = (voice.generation, voice.worker_id, voice.worker_lease_until)
        if allowed:
            claim_worker(
                db, voice.id, staff.tenant_id, uuid4(), NOW + timedelta(seconds=seconds), settings
            )
            assert voice.generation == before[0] + 1
        else:
            with pytest.raises(AppError, match="voice_unavailable"):
                claim_worker(
                    db,
                    voice.id,
                    staff.tenant_id,
                    uuid4(),
                    NOW + timedelta(seconds=seconds),
                    settings,
                )
            assert (voice.generation, voice.worker_id, voice.worker_lease_until) == before
            assert db.get(CallRecord, voice.session_id).expires_at == NOW + timedelta(seconds=45)


def test_worker_expiry_wins_renewal_waiting_for_call_lock(database, seed):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from analytics.transcripts import locked_call
    from scheduler.errors import AppError
    from scheduler.models import AgentSession
    from tests_analytics.test_privacy import _wait_for_block

    db, settings, created = setup_session(database, seed)
    with db:
        actor = decode_runtime(db, created["token"], settings, now=NOW)
        db.commit()
    ready, other_pid = Event(), []
    clock_now = [NOW + timedelta(minutes=1)]
    with Session(database) as worker_db:
        call = locked_call(worker_db, actor.id)
        holder = worker_db.scalar(text("SELECT pg_backend_pid()"))
        call.status, call.outcome, call.end_reason = "expired", "abandoned", "timeout"
        call.ended_at = call.expires_at
        worker_db.flush()

        def renew():
            with Session(database) as runtime_db, runtime_db.begin():
                other_pid.append(runtime_db.scalar(text("SELECT pg_backend_pid()")))
                ready.set()
                return renew_session(
                    runtime_db, actor, clock_now[0], settings, clock=lambda: clock_now[0]
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(renew)
            try:
                assert ready.wait(3)
                _wait_for_block(worker_db, other_pid[0], holder)
                clock_now[0] = NOW + timedelta(minutes=31)
            finally:
                worker_db.commit()
            with pytest.raises(AppError):
                future.result(timeout=5)
    with Session(database) as db:
        assert db.get(AgentSession, actor.id).expires_at == NOW + timedelta(minutes=30)
        assert db.get(CallRecord, actor.id).status == "expired"
