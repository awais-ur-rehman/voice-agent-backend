"""Voice-mode authorization uses the SQL transport binding, never caller state."""

import pytest

from brain.security import decode_runtime
from brain.sessions import command, renew_session
from scheduler.errors import AppError
from scheduler.models import AgentSession
from tests_api.conftest import NOW, SECRET
from tests_brain.test_sessions import setup_session


def test_voice_session_rejects_text_command_before_effect(database, seed):
    db, settings, created = setup_session(database, seed)
    with db:
        actor = decode_runtime(db, created["token"], settings, now=NOW)
        row = db.get(AgentSession, actor.id)
        row.transport_mode = "voice"
        db.commit()
        calls = []
        with pytest.raises(AppError, match="voice_worker_required"):
            command(
                db, actor, "text-bypass", 0, {}, NOW, SECRET, lambda db, state: calls.append(True)
            )
        assert calls == []


def test_voice_session_cannot_rotate_text_capability(database, seed):
    db, settings, created = setup_session(database, seed)
    with db:
        actor = decode_runtime(db, created["token"], settings, now=NOW)
        db.get(AgentSession, actor.id).transport_mode = "voice"
        db.commit()
        with pytest.raises(AppError, match="voice_worker_required"):
            renew_session(db, actor, NOW, settings)


def test_worker_lease_generation_and_deadline_are_authoritative(database, seed):
    from datetime import timedelta
    from uuid import uuid4

    from brain.voice_sessions import WorkerAuthority, require_worker
    from scheduler.models import VoiceSession
    from scheduler.security import token_hash

    db, settings, created = setup_session(database, seed)
    with db:
        session = db.get(AgentSession, created["session_id"])
        session.transport_mode = "voice"
        voice = VoiceSession(
            tenant_id=session.tenant_id,
            session_id=session.id,
            creator_id=seed["owner_a"],
            request_key="synthetic-call",
            room="opaque-room",
            participant="opaque-caller",
            status="active",
            generation=1,
            worker_id=uuid4(),
            worker_nonce_hash=token_hash("synthetic-worker"),
            worker_lease_until=NOW + timedelta(seconds=15),
            call_deadline=NOW + timedelta(minutes=10),
        )
        db.add(voice)
        db.flush()
        actor = decode_runtime(db, created["token"], settings, now=NOW)
        authority = WorkerAuthority(voice.id, voice.worker_id, 1, token_hash("synthetic-worker"))
        require_worker(db, actor, authority, NOW)
        with pytest.raises(AppError):
            require_worker(db, actor, authority, NOW + timedelta(seconds=15))
        voice.generation += 1
        db.flush()
        with pytest.raises(AppError):
            require_worker(db, actor, authority, NOW)


def test_creation_is_idempotent_and_worker_is_fenced_on_disconnect(database, seed):
    from datetime import timedelta
    from uuid import uuid4

    from sqlalchemy.orm import Session

    from brain.voice_sessions import claim_worker, create_voice, disconnect_voice, resume_voice
    from brain.voice_tokens import decode_worker
    from scheduler.config import Settings
    from scheduler.db import Principal

    settings = Settings(
        database_url="postgresql://unused",
        jwt_secret=SECRET,
        runtime_secret="runtime-distinct-only-test-secret-2026",
        voice_worker_secret="worker-distinct-only-test-secret-2026",
        livekit_api_key="synthetic",
        livekit_api_secret="livekit-distinct-only-test-secret-2026",
        _env_file=None,
    )
    staff = Principal(seed["owner_a"], seed["tenant_a"], "owner-a@example.com", "owner")
    with Session(database, expire_on_commit=False) as db:
        first = create_voice(db, staff, seed["agent_a"], "retry-key", NOW, settings)
        duplicate = create_voice(db, staff, seed["agent_a"], "retry-key", NOW, settings)
        assert first.id == duplicate.id
        assert db.get(AgentSession, first.session_id).transport_mode == "voice"
        first.status = "active"
        db.commit()
        worker = claim_worker(db, first.id, staff.tenant_id, uuid4(), NOW, settings)
        authority, _ = decode_worker(settings, worker.token, NOW)
        actor = decode_runtime(db, worker.runtime_token, settings, now=NOW)
        from brain.voice_sessions import require_worker

        require_worker(db, actor, authority, NOW)
        with pytest.raises(AppError):
            claim_worker(db, first.id, staff.tenant_id, uuid4(), NOW, settings)
        disconnect_voice(db, actor, authority, NOW)
        with pytest.raises(AppError):
            require_worker(db, actor, authority, NOW)
        resumed = resume_voice(db, staff, first.session_id, NOW + timedelta(seconds=10))
        assert resumed.generation == authority.generation + 1
        assert resumed.status == "active"
        assert resumed.delivery is None
        assert db.get(AgentSession, first.session_id).checkpoint["readback"] is None
        db.commit()


def test_voice_resume_requires_original_staff_and_unexpired_grace(database, seed):
    from datetime import timedelta

    from sqlalchemy.orm import Session

    from brain.voice_sessions import create_voice, resume_voice
    from scheduler.config import Settings
    from scheduler.db import Principal

    settings = Settings(
        database_url="postgresql://unused",
        jwt_secret=SECRET,
        runtime_secret="runtime-distinct-only-test-secret-2026",
        _env_file=None,
    )
    staff = Principal(seed["owner_a"], seed["tenant_a"], "owner-a@example.com", "owner")
    stranger = Principal(seed["staff_a"], seed["tenant_a"], "staff-a@example.com", "staff")
    with Session(database) as db:
        voice = create_voice(db, staff, seed["agent_a"], "resume-key", NOW, settings)
        voice.status = "disconnected"
        voice.disconnected_at = NOW
        db.flush()
        with pytest.raises(AppError):
            resume_voice(db, stranger, voice.session_id, NOW + timedelta(seconds=1))
        with pytest.raises(AppError):
            resume_voice(db, staff, voice.session_id, NOW + timedelta(seconds=30))


def test_same_worker_can_recover_lost_claim_without_new_generation(database, seed):
    from uuid import uuid4

    from sqlalchemy.orm import Session

    from brain.voice_sessions import claim_worker, create_voice
    from brain.voice_tokens import decode_worker
    from scheduler.config import Settings
    from scheduler.db import Principal

    settings = Settings(
        database_url="postgresql://unused",
        jwt_secret=SECRET,
        runtime_secret="runtime-distinct-only-test-secret-2026",
        voice_worker_secret="worker-distinct-only-test-secret-2026",
        livekit_api_key="synthetic",
        livekit_api_secret="livekit-distinct-only-test-secret-2026",
        _env_file=None,
    )
    staff = Principal(seed["owner_a"], seed["tenant_a"], "synthetic", "owner")
    with Session(database) as db:
        voice = create_voice(db, staff, seed["agent_a"], "claim-retry", NOW, settings)
        voice.status = "active"
        db.flush()
        worker_id = uuid4()
        first = claim_worker(db, voice.id, staff.tenant_id, worker_id, NOW, settings)
        recovered = claim_worker(db, voice.id, staff.tenant_id, worker_id, NOW, settings)
        assert first.generation == recovered.generation
        actor = decode_runtime(db, recovered.runtime_token, settings, now=NOW)
        authority, _ = decode_worker(settings, recovered.token, NOW)
        from brain.voice_sessions import require_worker

        require_worker(db, actor, authority, NOW)
        with pytest.raises(AppError):
            decode_runtime(db, first.runtime_token, settings, now=NOW)


def test_committed_new_readback_discards_only_old_playback_ledger(database, seed):
    from uuid import uuid4

    from sqlalchemy.orm import Session

    from brain.voice_sessions import claim_worker, create_voice
    from brain.voice_tokens import decode_worker
    from scheduler.config import Settings
    from scheduler.db import Principal

    settings = Settings(
        database_url="postgresql://unused",
        jwt_secret=SECRET,
        runtime_secret="runtime-ledger-test-distinct-secret-2026",
        voice_worker_secret="worker-ledger-test-distinct-secret-2026",
        livekit_api_key="synthetic",
        livekit_api_secret="livekit-ledger-test-distinct-secret-2026",
        _env_file=None,
    )
    staff = Principal(seed["owner_a"], seed["tenant_a"], "synthetic", "owner")
    with Session(database) as db:
        voice = create_voice(db, staff, seed["agent_a"], "ledger-next", NOW, settings)
        voice.status = "active"
        db.flush()
        grant = claim_worker(db, voice.id, staff.tenant_id, uuid4(), NOW, settings)
        actor = decode_runtime(db, grant.runtime_token, settings, now=NOW)
        authority, _ = decode_worker(settings, grant.token, NOW)
        parent = db.get(AgentSession, actor.id)
        state = dict(parent.checkpoint)
        state["readback"] = {"kind": "patient", "revision": 1}
        state["revision"] = 1
        parent.revision, parent.checkpoint = 1, state
        voice.delivery = {"readback_revision": 1, "completed_at": NOW.isoformat()}
        db.flush()

        def next_readback(db, state):
            state["readback"] = {"kind": "appointment", "revision": 2}
            return {"ok": True}

        command(
            db, actor, "next-readback", 1, {}, NOW, SECRET, next_readback, voice_authority=authority
        )
        assert voice.delivery is None


def test_owner_recovers_dead_worker_within_grace_and_fences_old_runtime(database, seed):
    from datetime import timedelta
    from uuid import uuid4

    from sqlalchemy.orm import Session

    from brain.voice_sessions import claim_worker, create_voice, resume_voice
    from scheduler.config import Settings
    from scheduler.db import Principal

    settings = Settings(
        database_url="postgresql://unused",
        jwt_secret=SECRET,
        runtime_secret="runtime-crash-test-distinct-secret-2026",
        voice_worker_secret="worker-crash-test-distinct-secret-2026",
        livekit_api_key="synthetic",
        livekit_api_secret="livekit-crash-test-distinct-secret-2026",
        _env_file=None,
    )
    staff = Principal(seed["owner_a"], seed["tenant_a"], "synthetic", "owner")
    with Session(database) as db:
        voice = create_voice(db, staff, seed["agent_a"], "dead-worker", NOW, settings)
        voice.status = "active"
        db.flush()
        grant = claim_worker(db, voice.id, staff.tenant_id, uuid4(), NOW, settings)
        voice.worker_lease_until = NOW - timedelta(seconds=1)
        db.flush()
        recovered = resume_voice(db, staff, voice.session_id, NOW)
        assert recovered.generation == grant.generation + 1
        assert recovered.status == "active"
        with pytest.raises(AppError):
            decode_runtime(db, grant.runtime_token, settings, now=NOW)
