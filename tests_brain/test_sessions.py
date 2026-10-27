from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from scheduler.db import Principal, tenant_context
from scheduler.errors import AppError
from tests_api.conftest import NOW, SECRET


def test_runtime_modules_are_available():
    from brain.state import initial_state

    state = initial_state(uuid4(), uuid4(), uuid4())
    assert state["revision"] == 0
    assert state["task"] == "book"
    assert state["offers"] == []
    assert state["attempts"] == 0
    assert state["lang"] == "en"


def setup_session(database, seed):
    from brain.sessions import create_session
    from scheduler.config import Settings
    from scheduler.models import Agent, AgentVersion

    settings = Settings(
        database_url="postgresql://unused",
        jwt_secret=SECRET,
        runtime_secret="runtime-only-test-secret-2026-distinct",
        _env_file=None,
    )
    db = Session(database, expire_on_commit=False)
    db.begin()
    tenant_context(db, seed["tenant_a"])
    agent = Agent(tenant_id=seed["tenant_a"], name="Synthetic Receptionist")
    db.add(agent)
    db.flush()
    version = AgentVersion(
        tenant_id=agent.tenant_id, agent_id=agent.id, config={"language_mode": "en"}
    )
    db.add(version)
    db.flush()
    agent.active_version_id = version.id
    staff = Principal(seed["owner_a"], seed["tenant_a"], "owner-a@example.com", "owner")
    result = create_session(db, staff, agent.id, NOW, settings)
    db.commit()
    return db, settings, result


def test_token_scope_revocation_and_session_expiry(database, seed):
    from brain.security import decode_runtime
    from scheduler.models import AgentSession
    from scheduler.security import access_token

    db, settings, result = setup_session(database, seed)
    with db:
        actor = decode_runtime(db, result["token"], settings, now=NOW)
        assert str(actor.id) == result["session_id"]
        with pytest.raises(AppError):
            decode_runtime(
                db, access_token(seed["owner_a"], seed["tenant_a"], settings), settings, now=NOW
            )
        row = db.get(AgentSession, actor.id)
        row.revoked_at = NOW
        db.flush()
        with pytest.raises(AppError):
            decode_runtime(db, result["token"], settings, now=NOW)
        row.revoked_at = None
        row.expires_at = NOW - timedelta(seconds=1)
        db.flush()
        with pytest.raises(AppError):
            decode_runtime(db, result["token"], settings, now=NOW)


def test_command_replay_revision_and_no_raw_payload(database, seed):
    from brain.security import decode_runtime
    from brain.sessions import command
    from scheduler.models import AgentSession, CommandReceipt

    db, settings, result = setup_session(database, seed)
    actor = decode_runtime(db, result["token"], settings, now=NOW)
    calls = []

    def effect(db, state):
        calls.append(True)
        state["verified"] = True
        return {
            "ok": True,
            "data": {},
            "say": {"en": "Verified", "ur": "تصدیق ہو گئی"},
            "error": None,
        }

    payload = {"text": "Synthetic factor secret 1234", "factor": "1990-01-01"}
    with db:
        response, state = command(db, actor, "command-1", 0, payload, NOW, SECRET, effect)
        db.commit()
        replay, recovered = command(db, actor, "command-1", 0, payload, NOW, SECRET, effect)
        assert replay == response
        assert recovered == state
        assert state["revision"] == 1
        assert len(calls) == 1
        with pytest.raises(AppError):
            command(db, actor, "command-1", 0, {"text": "changed"}, NOW, SECRET, effect)
        with pytest.raises(AppError):
            command(db, actor, "command-2", 0, payload, NOW, SECRET, effect)
        receipt = db.scalar(select(CommandReceipt))
        assert "1234" not in str(receipt.response) + str(receipt.checkpoint)
        assert "1990-01-01" not in str(receipt.response) + str(receipt.checkpoint)
        row = db.get(AgentSession, actor.id)
        row.revoked_at = NOW
        db.flush()
        with pytest.raises(AppError):
            command(db, actor, "command-1", 0, payload, NOW, SECRET, effect)


def test_new_tables_have_forced_rls(database, seed):
    with database.connect() as conn:
        tables = conn.execute(
            text(
                "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN ('agents','agent_versions','agent_sessions','command_receipts','callbacks')"
            )
        ).all()
        assert len(tables) == 5
        assert all(row.relrowsecurity and row.relforcerowsecurity for row in tables)


def test_checkpoint_excludes_factors_nested_in_parked_and_recent_turns():
    from brain.state import recovery_checkpoint

    checkpoint = recovery_checkpoint(
        {
            "fields": {"dob": "1990-01-01", "factor": "Secret"},
            "parked": [{"fields": {"factor": "Secret"}, "history": ["Secret"]}],
            "history": ["Secret"],
            "summary": "Secret",
            "attempts": 2,
        }
    )
    assert "Secret" not in str(checkpoint)
    assert "1990-01-01" not in str(checkpoint)
    assert checkpoint["attempts"] == 2


def test_token_rejects_scope_tampering_expired_jwt_and_staff_audience(database, seed):
    from datetime import UTC, datetime

    import jwt

    from brain.security import AUDIENCE, decode_runtime

    db, settings, result = setup_session(database, seed)
    secret = settings.runtime_secret.get_secret_value()
    claims = jwt.decode(
        result["token"], secret, algorithms=["HS256"], audience=AUDIENCE, issuer=settings.jwt_issuer
    )
    with db:
        for key, value in [
            ("tenant", str(seed["tenant_b"])),
            ("agent", str(seed["agent_b"])),
            ("version", str(seed["version_b"])),
            ("sub", str(uuid4())),
            ("jti", "different-nonce"),
            ("aud", settings.jwt_audience),
            ("exp", datetime.now(UTC) - timedelta(seconds=1)),
        ]:
            invalid = jwt.encode(claims | {key: value}, secret, algorithm="HS256")
            with pytest.raises(AppError):
                decode_runtime(db, invalid, settings, now=NOW)
        with pytest.raises(jwt.InvalidTokenError):
            jwt.decode(
                result["token"],
                settings.jwt_secret.get_secret_value(),
                algorithms=["HS256"],
                audience=settings.jwt_audience,
                issuer=settings.jwt_issuer,
            )


def test_cross_tenant_staff_cannot_create_session(database, seed):
    from brain.sessions import create_session

    db, settings, _ = setup_session(database, seed)
    with db, pytest.raises(AppError):
        staff = Principal(seed["owner_b"], seed["tenant_b"], "owner-b@example.com", "owner")
        create_session(db, staff, seed["agent_a"], NOW, settings)


def test_effect_and_receipt_rollback_together(database, seed):
    from brain.security import decode_runtime
    from brain.sessions import command
    from scheduler.models import AgentSession, Callback, CommandReceipt

    db, settings, result = setup_session(database, seed)
    with db:
        actor = decode_runtime(db, result["token"], settings, now=NOW)

        def effect(db, state):
            db.add(
                Callback(tenant_id=actor.tenant_id, session_id=actor.id, agent_id=actor.agent_id)
            )
            state["attempts"] = 1
            return {"ok": True}

        command(db, actor, "rollback", 0, {}, NOW, SECRET, effect)
        db.rollback()
        assert db.get(AgentSession, actor.id).revision == 0
        assert db.scalar(select(CommandReceipt)) is None
        assert db.scalar(select(Callback)) is None


def test_delayed_replay_returns_current_checkpoint(database, seed):
    from brain.security import decode_runtime
    from brain.sessions import command

    db, settings, result = setup_session(database, seed)
    with db:
        actor = decode_runtime(db, result["token"], settings, now=NOW)

        def effect(db, state):
            state["attempts"] += 1
            return {"ok": True}

        command(db, actor, "first", 0, {}, NOW, SECRET, effect)
        command(db, actor, "second", 1, {}, NOW, SECRET, effect)
        _, recovered = command(db, actor, "first", 0, {}, NOW, SECRET, effect)
        assert recovered["revision"] == 2
        assert recovered["attempts"] == 2


def test_retention_purge_is_bounded_and_preserves_nonidentifying_metadata(database, seed):
    from brain.security import decode_runtime
    from brain.sessions import command, purge_recovery
    from scheduler.models import AgentSession, CommandReceipt

    db, settings, result = setup_session(database, seed)
    with db:
        actor = decode_runtime(db, result["token"], settings, now=NOW)
        command(db, actor, "retention", 0, {}, NOW, SECRET, lambda db, state: {"ok": True})
        assert (
            purge_recovery(db, NOW + timedelta(hours=24) - timedelta(microseconds=1))["receipts"]
            == 0
        )
        assert purge_recovery(db, NOW + timedelta(hours=24))["receipts"] == 1
        row = db.scalar(select(CommandReceipt))
        assert row.response is None and row.checkpoint is None and row.request_digest
        assert purge_recovery(db, NOW + timedelta(hours=25))["sessions"] == 1
        assert db.get(AgentSession, actor.id).checkpoint is None


def test_app_role_rls_hides_all_five_new_tables_without_tenant(database, seed):
    from sqlalchemy import create_engine

    from tests_api.conftest import APP_URL

    engine = create_engine(APP_URL, hide_parameters=True)
    try:
        with engine.connect() as conn:
            for table in (
                "agents",
                "agent_versions",
                "agent_sessions",
                "command_receipts",
                "callbacks",
            ):
                assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            conn.execute(
                text("SELECT set_config('app.tenant_id',:tenant,true)"),
                {"tenant": str(seed["tenant_a"])},
            )
            assert conn.scalar(text("SELECT count(*) FROM agents")) == 1
            assert conn.scalar(text("SELECT count(*) FROM agent_versions")) == 1
    finally:
        engine.dispose()


def test_published_agent_version_cannot_be_modified(database, seed):
    from sqlalchemy.exc import DBAPIError

    with database.connect() as conn, pytest.raises(DBAPIError):
        conn.execute(
            text("UPDATE agent_versions SET config='{}'::jsonb WHERE id=:id"),
            {"id": seed["version_a"]},
        )


def test_renewal_interface_exists():
    from brain.sessions import renew_session

    assert callable(renew_session)


def test_renewal_rotates_capability_and_preserves_revision(database, seed):
    from brain.security import decode_runtime
    from brain.sessions import renew_session
    from scheduler.models import AgentSession

    db, settings, result = setup_session(database, seed)
    with db:
        actor = decode_runtime(db, result["token"], settings, now=NOW)
        renewed = renew_session(db, actor, NOW + timedelta(minutes=20), settings)
        assert renewed["token"] != result["token"]
        assert renewed["revision"] == 0
        assert db.get(AgentSession, actor.id).expires_at == NOW + timedelta(minutes=50)
        assert (
            decode_runtime(db, renewed["token"], settings, now=NOW + timedelta(minutes=20)) == actor
        )
        with pytest.raises(AppError):
            decode_runtime(db, result["token"], settings, now=NOW + timedelta(minutes=20))
        db.get(AgentSession, actor.id).expires_at = NOW
        db.flush()
        with pytest.raises(AppError):
            renew_session(db, actor, NOW + timedelta(minutes=20), settings)
