"""Server-only synthetic provisioning and parent-authorized runtime gateway."""

from copy import deepcopy
from datetime import date, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from valkey.exceptions import ValkeyError

from scheduler.appointments import Book, book_transaction
from scheduler.dashboard.agents import draft_row, locked
from scheduler.db import Principal, tenant_context
from scheduler.errors import AppError
from scheduler.models import (
    Agent,
    AgentSession,
    AgentVersion,
    CommandReceipt,
    DraftTest,
    ExceptionDay,
    Patient,
    Provider,
    ProviderService,
    Schedule,
    Service,
    Tenant,
    User,
    VoiceSession,
)
from scheduler.slots import search

from .security import RuntimeActor, active_guard
from .sessions import create_session


def owned(db, user, identity, now, *, active=True, maintenance=False):
    tenant_context(db, user.tenant_id)
    actor = db.scalar(select(User).where(User.id == user.id, User.active.is_(True)))
    if not maintenance and (actor is None or actor.role not in {"owner", "admin"}):
        raise AppError("not_found", 404)
    row = db.scalar(
        select(DraftTest)
        .where(
            DraftTest.id == identity,
            DraftTest.tenant_id == user.tenant_id,
            DraftTest.creator_id == user.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise AppError("not_found", 404)
    if active and (row.status != "active" or row.expires_at <= now):
        raise AppError("test_expired", 410)
    return row


def child_user(row):
    return Principal(row.child_actor_id, row.child_tenant_id, "", "owner")


def internal_actor(db, request, user, identity, session_id):
    row = owned(db, user, identity, request.app.state.clock())
    if row.session_id != session_id:
        raise AppError("test_expired", 410)
    tenant_context(db, row.child_tenant_id)
    session = db.get(AgentSession, session_id)
    if session is None or session.tenant_id != row.child_tenant_id:
        raise AppError("not_found", 404)
    actor = RuntimeActor(
        session.id, session.tenant_id, session.agent_id, session.version_id, session.token_jti_hash
    )
    active_guard(session, actor, request.app.state.clock())
    return actor


def revoke(db, row, request, status="ended"):
    """Parent mapping lock precedes all child locks, including command commits."""
    from .voice_routes import _end_locked

    now, store = request.app.state.clock(), request.app.state.brain_store
    row.status = status
    db.flush()  # parent RLS context
    tenant_context(db, row.child_tenant_id)
    sessions = list(db.scalars(select(AgentSession).order_by(AgentSession.id).with_for_update()))
    rooms = []
    for session in sessions:
        voice = db.scalar(
            select(VoiceSession).where(VoiceSession.session_id == session.id).with_for_update()
        )
        hold = (session.checkpoint or {}).get("hold") or (session.checkpoint or {}).get(
            "cleanup_hold"
        )
        if voice:
            _end_locked(session, voice, store, now, "ended", release_hold=False)
        elif session.status == "active":
            session.revision += 1
            session.status = "ended"
            session.revoked_at = now
            session.token_jti_hash = ""
            session.lease_until = now
            session.checkpoint = {"revision": session.revision, "ended": True}
        if hold:
            session.checkpoint = {**(session.checkpoint or {}), "cleanup_hold": hold}
        rooms.append(
            (session.id, voice.room if voice else None, row.child_tenant_id, hold, session.revision)
        )
    for receipt in db.scalars(select(CommandReceipt)):
        receipt.response = None
        receipt.checkpoint = None
    db.flush()
    tenant_context(db, row.tenant_id)
    return rooms


def release_rooms(request, rooms):
    from .voice_cleanup import PREFIX as VOICE_PREFIX
    from .voice_rooms import LiveKitRooms
    from .voice_routes import _release_capacity

    store = request.app.state.brain_store
    try:
        for identity, room, tenant, hold, revision in rooms:
            if room:
                LiveKitRooms(request.app.state.settings).delete(room)
                _release_capacity(store, identity)
                store.client.delete(VOICE_PREFIX + str(identity))
            if hold:
                store.release_hold(str(tenant), hold["provider"], str(identity), hold["hold_id"])
            store.repair(str(identity), {"revision": revision, "ended": True})
    except ValkeyError:
        raise AppError("service_unavailable", 503) from None


def finish(request, user, identity, *, expired_only=False, maintenance=False):
    with request.app.state.session_factory() as db, db.begin():
        row = owned(
            db, user, identity, request.app.state.clock(), active=False, maintenance=maintenance
        )
        if row.cleanup_done:
            return
        if expired_only and row.expires_at > request.app.state.clock() and row.status == "active":
            return
        rooms = revoke(
            db, row, request, "expired" if row.expires_at <= request.app.state.clock() else "ended"
        )
    release_rooms(request, rooms)
    with request.app.state.session_factory() as db, db.begin():
        row = owned(
            db, user, identity, request.app.state.clock(), active=False, maintenance=maintenance
        )
        tenant_context(db, row.child_tenant_id)
        for runtime in db.scalars(select(AgentSession).with_for_update()):
            if (runtime.checkpoint or {}).get("cleanup_hold"):
                runtime.checkpoint = {"revision": runtime.revision, "ended": True}
        db.flush()
        tenant_context(db, row.tenant_id)
        row.cleanup_done = True


def clone(db, row, draft, request):
    """The explicit allowlist never queries parent patients, bookings or callbacks."""
    copied = {}
    for model in (Provider, Service, ProviderService, Schedule, ExceptionDay):
        copied[model] = [
            {
                column.name: deepcopy(getattr(item, column.name))
                for column in model.__table__.columns
            }
            for item in db.scalars(select(model))
        ]
    tenant_context(db, row.child_tenant_id)
    remap = {
        item["id"]: uuid4()
        for model in (Provider, Service, Schedule, ExceptionDay)
        for item in copied[model]
    }
    for model in (Provider, Service, ProviderService, Schedule, ExceptionDay):
        for data in copied[model]:
            data["tenant_id"] = row.child_tenant_id
            for key in ("id", "provider_id", "service_id"):
                if key in data:
                    data[key] = remap[data[key]]
            db.add(model(**data))
        db.flush()
    config = deepcopy(draft.config)
    clinic = db.get(Tenant, row.child_tenant_id)
    config["clinic_profile"] = {
        k: getattr(clinic, k)
        for k in ("address_en", "address_ur", "preparation_en", "preparation_ur")
    }
    agent = Agent(tenant_id=row.child_tenant_id, name=config.get("name", "Synthetic test agent"))
    db.add(agent)
    db.flush()
    version = AgentVersion(
        tenant_id=row.child_tenant_id, agent_id=agent.id, purpose="test", config=config
    )
    db.add(version)
    db.flush()
    agent.active_version_id = version.id
    db.flush()
    people = []
    for name, phone, dob in (
        ("Synthetic Test Amina", "+923000000001", date(1990, 1, 15)),
        ("Synthetic Test Bilal", "+923000000002", date(1985, 6, 20)),
    ):
        patient = Patient(
            tenant_id=row.child_tenant_id,
            name=name,
            name_ur="مصنوعی مریض",
            name_key=name.casefold(),
            phone=phone,
            dob=dob,
        )
        db.add(patient)
        db.flush()
        people.append({"id": str(patient.id), "name": name, "phone": phone, "dob": dob.isoformat()})
    appointment = None
    now = request.app.state.clock()
    day = now.astimezone(ZoneInfo(clinic.timezone)).date()
    # Find a real bookable fixture slot; closed/unconfigured clinics remain testable.
    for service in db.scalars(select(Service).where(Service.active.is_(True))):
        slots = search(
            db,
            clinic,
            service.id,
            day + timedelta(days=min(2, clinic.horizon_days)),
            day + timedelta(days=min(14, clinic.horizon_days)),
            now,
            limit=1,
        )
        if slots:
            slot = slots[0]
            booked = book_transaction(
                db,
                child_user(row),
                Book(
                    confirmed=True,
                    patient_id=UUID(people[0]["id"]),
                    service_id=service.id,
                    provider_id=slot.provider_id,
                    starts_at=slot.starts_at,
                ),
                now,
            )
            appointment = {
                "id": str(booked.id),
                "patient_id": str(booked.patient_id),
                "provider_id": str(booked.provider_id),
                "service_id": str(booked.service_id),
                "starts_at": booked.starts_at.isoformat(),
                "ends_at": booked.ends_at.isoformat(),
            }
            break
    created = create_session(db, child_user(row), agent.id, now, request.app.state.settings)
    db.flush()
    tenant_context(db, row.tenant_id)
    row.child_agent_id = agent.id
    row.session_id = UUID(created["session_id"])
    row.fixtures = {
        "patients": people,
        "appointment": appointment,
        "appointment_unavailable_reason": None
        if appointment
        else "No bookable slot in the next 14 days.",
    }
    db.flush()


def provision(db, user, agent_id, revision, key, replace, request):
    # This parent lock also bounds concurrent admission across different agents/creators.
    db.scalar(select(Tenant).where(Tenant.id == user.tenant_id).with_for_update())
    agent = locked(db, agent_id)
    if not agent.enabled:
        raise AppError("not_found", 404)
    draft = draft_row(db, agent, lock=True)
    previous = db.scalar(
        select(DraftTest).where(DraftTest.creator_id == user.id, DraftTest.request_key == key)
    )
    if previous:
        if previous.agent_id != agent_id or previous.draft_revision != revision:
            raise AppError("idempotency_conflict", 409)
        return previous, []
    if draft.revision != revision:
        raise AppError("revision_conflict", 409)
    rooms = []
    active = list(
        db.scalars(
            select(DraftTest)
            .where(DraftTest.status == "active")
            .order_by(DraftTest.id)
            .with_for_update()
        )
    )
    for existing in active:
        if existing.expires_at <= request.app.state.clock():
            rooms.extend(revoke(db, existing, request, "expired"))
        elif existing.creator_id == user.id and existing.agent_id == agent_id:
            if not replace:
                raise AppError("test_already_active", 409)
            rooms.extend(revoke(db, existing, request))
    if sum(row.status == "active" for row in active) >= 3:
        raise AppError("test_capacity", 409)
    identity = db.scalar(
        text("SELECT provision_draft_test(:actor,:agent,:revision,:key,:now)"),
        {
            "actor": user.id,
            "agent": agent_id,
            "revision": revision,
            "key": key,
            "now": request.app.state.clock(),
        },
    )
    row = db.get(DraftTest, identity)
    clone(db, row, draft, request)
    return row, rooms


def sweep(app):
    """Bounded SQL routing projection survives cache loss and inactive creators."""
    from types import SimpleNamespace

    with app.state.session_factory() as db, db.begin():
        records = list(
            db.execute(
                text("SELECT * FROM draft_tests_due(:now)"), {"now": app.state.clock()}
            ).mappings()
        )
    request = SimpleNamespace(app=app)
    for record in records:
        user = Principal(record["creator_id"], record["tenant_id"], "", "owner")
        finish(request, user, record["id"], expired_only=True, maintenance=True)
    return len(records)
