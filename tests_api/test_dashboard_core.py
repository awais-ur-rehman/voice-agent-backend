"""Synthetic dashboard boundary and canonical identity regressions."""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from scheduler.models import Tenant, User
from tests_api.conftest import PASSWORD


def registration():
    return dict(
        clinic_name="New Clinic",
        timezone="Asia/Karachi",
        email="new@example.com",
        password=PASSWORD,
        display_name="Owner",
    )


def test_registration_disabled_by_default(client):
    assert client.post("/auth/register", json=registration()).status_code == 403


def test_registration_atomic_normalized_and_no_injection(client, database):
    client.app.state.settings.registration_enabled = True
    for field, value in [("role", "admin"), ("tenant_id", str(uuid4()))]:
        assert (
            client.post("/auth/register", json=registration() | {field: value}).status_code == 422
        )
    response = client.post("/auth/register", json=registration())
    assert response.status_code == 201, response.text
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    me = client.get("/auth/me").json()
    assert me["role"] == "owner"
    assert client.get("/dashboard/clinic").json()["name"] == "New Clinic"
    assert (
        client.post(
            "/auth/register", json=registration() | {"email": "NEW@example.com"}
        ).status_code
        == 409
    )
    with Session(database) as db:
        assert len(list(db.scalars(select(Tenant).where(Tenant.name == "New Clinic")))) == 1
        assert db.scalar(
            select(User).where(User.email == "new@example.com")
        ).tenant_id == __import__("uuid").UUID(me["tenant_id"])


def test_profile_revision_setup_and_roles(owner, seed):
    profile = owner.get("/dashboard/clinic")
    assert profile.status_code == 200, profile.text
    revision = profile.json()["revision"]
    patch = dict(revision=revision, address_en="Synthetic Road", preparation_ur="تیاری")
    assert owner.patch("/dashboard/clinic", json=patch).status_code == 200
    assert owner.patch("/dashboard/clinic", json=patch).status_code == 409
    setup = owner.get("/dashboard/setup").json()
    assert setup["providers"] and setup["services"] and setup["hours"]
    assert not setup["agent"]
    assert (
        owner.patch(
            "/dashboard/clinic", json={"revision": revision + 1, "tenant_id": str(seed["tenant_b"])}
        ).status_code
        == 422
    )
    login = owner.post(
        "/auth/login", json={"email": "staff-a@example.com", "password": PASSWORD}
    ).json()
    owner.headers["Authorization"] = "Bearer " + login["access_token"]
    assert owner.patch("/dashboard/clinic", json=patch).status_code == 403


def add_patient(owner, **fields):
    response = owner.post(
        "/patients", json={"name": "Other Patient", "phone": "+923009998888"} | fields
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def merge(owner, source, dest, **extra):
    return owner.post(
        f"/dashboard/patients/{source}/merge",
        headers={"Idempotency-Key": extra.pop("key", "merge-test-key")},
        json={
            "destination_id": dest,
            "source_revision": 1,
            "destination_revision": 1,
            "fields": {"name": "Retained", "name_ur": None, "phone": "+923008887777", "dob": None},
        }
        | extra,
    )


def test_search_merge_tombstone_retry_history(owner, seed):
    source = str(seed["patient_a"])
    dest = add_patient(owner, name="Urdu Patient", name_ur="عائشہ")
    assert owner.get("/dashboard/patients", params={"q": "عائشہ"}).json()["total"] == 1
    assert owner.get("/dashboard/patients", params={"q": "03009998888"}).json()["total"] == 1
    booked = owner.post(
        "/appointments",
        headers={"Idempotency-Key": "before-merge-key"},
        json={
            "patient_id": source,
            "provider_id": str(seed["provider_a"]),
            "service_id": str(seed["service_a"]),
            "starts_at": "2026-11-03T04:00:00Z",
            "confirmed": True,
        },
    )
    assert booked.status_code == 201, booked.text
    result = merge(owner, source, dest)
    assert result.status_code == 200, result.text
    assert result.json()["id"] == dest
    assert merge(owner, source, dest).json() == result.json()
    assert owner.get(f"/dashboard/patients/{source}").json()["merged_into_id"] == dest
    assert owner.get("/dashboard/patients").json()["total"] == 1
    assert len(owner.get(f"/patients/{dest}/appointments").json()) == 1
    booked_body = booked.json()
    again = owner.post(
        "/appointments",
        headers={"Idempotency-Key": "after-merge-key"},
        json={
            "patient_id": source,
            "provider_id": str(seed["provider_a"]),
            "service_id": str(seed["service_a"]),
            "starts_at": "2026-11-03T05:00:00Z",
            "confirmed": True,
        },
    )
    assert again.status_code == 409
    assert (
        owner.post(
            f"/appointments/{booked_body['id']}/cancel",
            headers={"Idempotency-Key": "cancel-merge-key"},
            json={"confirmed": True},
        ).status_code
        == 200
    )
    assert merge(owner, dest, source, key="merge-cycle-key").status_code == 409


def test_merge_conflicts_cross_tenant_and_staff(owner, seed):
    source = str(seed["patient_a"])
    dest = add_patient(owner)
    assert merge(owner, source, str(seed["patient_b"])).status_code == 404
    assert merge(owner, source, dest, fields={}).status_code == 422
    login = owner.post(
        "/auth/login", json={"email": "staff-a@example.com", "password": PASSWORD}
    ).json()
    owner.headers["Authorization"] = "Bearer " + login["access_token"]
    assert merge(owner, source, dest).status_code == 403


def test_merge_requires_all_retained_fields_and_stale_revisions(owner, seed):
    dest = add_patient(owner)
    assert (
        merge(
            owner,
            str(seed["patient_a"]),
            dest,
            fields={"name": "Retained", "phone": "+923008887777"},
        ).status_code
        == 422
    )
    assert merge(owner, str(seed["patient_a"]), dest, source_revision=2).status_code == 409
    assert merge(owner, str(seed["patient_a"]), str(seed["patient_a"])).status_code == 409


def test_legacy_edits_invalidate_dashboard_revisions(owner, seed):
    revision = owner.get("/dashboard/clinic").json()["revision"]
    assert owner.patch("/clinic", json={"name": "Updated"}).status_code == 200
    assert (
        owner.patch("/dashboard/clinic", json={"revision": revision, "name": "Stale"}).status_code
        == 409
    )
    patient_id = str(seed["patient_a"])
    original = owner.get(f"/dashboard/patients/{patient_id}").json()
    assert (
        owner.put(
            f"/patients/{patient_id}", json={"name": "Updated", "phone": original["phone"]}
        ).status_code
        == 200
    )
    assert (
        owner.get(f"/dashboard/patients/{patient_id}").json()["revision"]
        == original["revision"] + 1
    )


def test_timezone_requires_confirmation(owner):
    revision = owner.get("/dashboard/clinic").json()["revision"]
    change = {"revision": revision, "timezone": "Europe/London"}
    assert owner.patch("/dashboard/clinic", json=change).status_code == 422
    assert (
        owner.patch(
            "/dashboard/clinic", json=change | {"confirm_timezone_change": True}
        ).status_code
        == 200
    )


def test_registration_rate_limit_persists_failures(client, database):
    from sqlalchemy import text

    with database.begin() as conn:
        conn.execute(text("TRUNCATE registration_attempts"))
    client.app.state.settings.registration_enabled = True
    assert client.post("/auth/register", json=registration()).status_code == 201
    for _ in range(4):
        assert client.post("/auth/register", json=registration()).status_code == 409
    assert (
        client.post(
            "/auth/register", json=registration() | {"email": "another@example.com"}
        ).status_code
        == 429
    )
    with database.begin() as conn:
        conn.execute(text("TRUNCATE registration_attempts"))


def test_two_sources_canonical_resolution_and_chain_rejection(owner, seed):
    from scheduler.dashboard.patients import canonical_patient
    from scheduler.db import tenant_context

    source = str(seed["patient_a"])
    dest = add_patient(owner)
    third = add_patient(owner, name="Third", phone="+923007776666")
    assert merge(owner, source, dest).status_code == 200
    # A second independent source can join a canonical destination with fresh revisions.
    assert (
        merge(owner, third, dest, key="second-source-key", destination_revision=2).status_code
        == 200
    )
    assert merge(owner, dest, third, key="reject-chain-key", source_revision=3).status_code == 409
    with owner.app.state.session_factory() as db, db.begin():
        tenant_context(db, seed["tenant_a"])
        assert str(canonical_patient(db, seed["tenant_a"], seed["patient_a"]).id) == dest


def test_merge_concurrent_destination_booking_preserves_limit(owner, seed, database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import text

    from tests_api.test_appointments import book

    dest = add_patient(owner)
    owner.patch("/clinic", json={"daily_limit": 1})
    assert book(owner, seed).status_code == 201
    barrier = Barrier(2)

    def booking():
        barrier.wait(timeout=5)
        return book(owner, seed, patient_id=dest, starts_at="2026-11-03T11:00:00+05:00")

    def merging():
        barrier.wait(timeout=5)
        return merge(owner, str(seed["patient_a"]), dest)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(booking), pool.submit(merging)
        results = [a.result(timeout=10), b.result(timeout=10)]
    assert sorted(r.status_code for r in results) == [200, 409] or sorted(
        r.status_code for r in results
    ) == [201, 409]
    with database.connect() as conn:
        counts = conn.execute(
            text(
                "SELECT patient_id,count(*) FROM appointments WHERE status='booked' GROUP BY patient_id"
            )
        ).all()
        assert all(count <= 1 for _, count in counts)


def test_stale_runtime_verification_does_not_transfer(owner, seed):
    from types import SimpleNamespace

    from brain.tools import ToolEngine
    from scheduler.db import tenant_context

    dest = add_patient(owner)
    state = {
        "verified": True,
        "subject": str(seed["patient_a"]),
        "stage": "collect_need",
        "lang": "en",
        "offers": [],
    }
    assert merge(owner, str(seed["patient_a"]), dest).status_code == 200
    with owner.app.state.session_factory() as db, db.begin():
        tenant_context(db, seed["tenant_a"])
        engine = ToolEngine(
            db, SimpleNamespace(tenant_id=seed["tenant_a"], id=seed["owner_a"]), None, None
        )
        response = engine.execute(state, {"name": "get_clinic_info", "arguments": {}})
    assert response["error"]["code"] == "not_verified"
    assert state["verified"] is False
    assert "subject" not in state


def test_destination_runtime_verification_invalidated_when_identity_changes(owner, seed):
    from types import SimpleNamespace

    from brain.tools import ToolEngine
    from scheduler.db import tenant_context
    from tests_api.conftest import NOW

    dest = add_patient(owner)
    state = {
        "verified": True,
        "subject": dest,
        "verified_revision": 1,
        "stage": "collect_need",
        "lang": "en",
        "offers": [],
    }
    assert merge(owner, str(seed["patient_a"]), dest).status_code == 200
    with owner.app.state.session_factory() as db, db.begin():
        tenant_context(db, seed["tenant_a"])
        engine = ToolEngine(
            db, SimpleNamespace(tenant_id=seed["tenant_a"], id=seed["owner_a"]), None, NOW
        )
        response = engine.execute(state, {"name": "get_clinic_info", "arguments": {}})
    assert response["error"]["code"] == "not_verified"
    assert state["verified"] is False


def test_registration_database_boundary_and_cross_tenant_merge_fk(owner, seed):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    factory = owner.app.state.session_factory
    with factory() as db, db.begin():
        role = db.execute(
            text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
        ).one()
        assert tuple(role) == (False, False)
    with pytest.raises(DBAPIError), factory() as db, db.begin():
        db.execute(text("INSERT INTO tenants(name) VALUES('Forbidden')"))
    with pytest.raises(DBAPIError), factory() as db, db.begin():
        db.execute(
            text("SELECT set_config('app.tenant_id',:tenant,true)"),
            {"tenant": str(seed["tenant_a"])},
        )
        db.execute(
            text("UPDATE patients SET merged_into_id=:dest WHERE id=:source"),
            {"dest": seed["patient_b"], "source": seed["patient_a"]},
        )
    with pytest.raises(DBAPIError), factory() as db, db.begin():
        db.execute(
            text(
                "SELECT * FROM provision_clinic('Injected','Asia/Karachi','x@example.com','hash','name','admin')"
            )
        )


def test_dashboard_pagination_search_and_patient_tenant_boundary(owner, seed):
    add_patient(owner, name="Search Patient")
    result = owner.get("/dashboard/patients", params={"limit": 1, "offset": 1}).json()
    assert (
        result["total"] == 2
        and len(result["items"]) == 1
        and result["limit"] == 1
        and result["offset"] == 1
    )
    assert owner.get("/dashboard/patients", params={"q": "Search"}).json()["total"] == 1
    assert owner.get(f"/dashboard/patients/{seed['patient_b']}").status_code == 404


def test_concurrent_two_source_merges_and_same_key_retry(owner, seed):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    source = str(seed["patient_a"])
    dest = add_patient(owner)
    barrier = Barrier(2)

    def same_merge(_):
        barrier.wait(timeout=5)
        return merge(owner, source, dest)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(same_merge, range(2)))
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json() == results[1].json()
    third = add_patient(owner, name="Third", phone="+923007776666")
    fourth = add_patient(owner, name="Fourth", phone="+923006665555")
    barrier = Barrier(2)

    def different_merge(args):
        identity, key = args
        barrier.wait(timeout=5)
        return merge(owner, identity, dest, key=key, destination_revision=2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(different_merge, [(third, "merge-third-key"), (fourth, "merge-fourth-key")])
        )
    assert sorted(r.status_code for r in results) == [200, 409]


def test_move_loaded_before_merge_locks_canonical_destination(owner, seed, monkeypatch):
    """A paused move must lock the retained identity after merge changes ownership."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from scheduler import appointments
    from tests_api.test_appointments import book

    loaded, resume = Event(), Event()
    first = book(owner, seed).json()
    dest = add_patient(owner)
    original_locks = appointments.locks

    def paused_locks(db, patient_id, providers):
        loaded.set()
        assert resume.wait(timeout=10)
        original_locks(db, patient_id, providers)
        import pytest
        from sqlalchemy import text
        from sqlalchemy.exc import OperationalError

        from scheduler.db import tenant_context

        with (
            pytest.raises(OperationalError),
            owner.app.state.session_factory() as probe,
            probe.begin(),
        ):
            tenant_context(probe, seed["tenant_a"])
            probe.execute(
                text("SELECT id FROM patients WHERE id=:id FOR UPDATE NOWAIT"), {"id": dest}
            )

    monkeypatch.setattr(appointments, "locks", paused_locks)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            lambda: owner.post(
                f"/appointments/{first['id']}/reschedule",
                headers={"Idempotency-Key": "move-over-merge-key"},
                json={"starts_at": "2026-11-04T10:00:00+05:00", "confirmed": True},
            )
        )
        assert loaded.wait(timeout=5)
        try:
            assert merge(owner, str(seed["patient_a"]), dest).status_code == 200
        finally:
            resume.set()
        result = pending.result(timeout=10)
    assert result.status_code == 201, result.text
    assert result.json()["patient_id"] == dest


def test_pre_dashboard_destination_verification_requires_reverification(owner, seed):
    from types import SimpleNamespace

    from brain.tools import ToolEngine
    from scheduler.db import tenant_context
    from tests_api.conftest import NOW

    dest = add_patient(owner)
    state = {"verified": True, "subject": dest, "stage": "collect_need", "lang": "en", "offers": []}
    assert merge(owner, str(seed["patient_a"]), dest).status_code == 200
    with owner.app.state.session_factory() as db, db.begin():
        tenant_context(db, seed["tenant_a"])
        engine = ToolEngine(
            db, SimpleNamespace(tenant_id=seed["tenant_a"], id=seed["owner_a"]), None, NOW
        )
        response = engine.execute(state, {"name": "get_clinic_info", "arguments": {}})
    assert response["error"]["code"] == "not_verified"
    assert state["verified"] is False


def test_merge_contract_exposes_every_retained_field_as_required(owner):
    schemas = owner.get("/openapi.json").json()["components"]["schemas"]
    retained = schemas["Merge"]["properties"]["fields"]["$ref"].split("/")[-1]
    assert set(schemas[retained]["required"]) == {"name", "name_ur", "phone", "dob"}


def test_merge_urdu_retained_name_validated_before_any_write(owner, seed, database):
    from sqlalchemy import text

    source = str(seed["patient_a"])
    dest = add_patient(owner)
    with database.connect() as conn:
        before = conn.execute(
            text("SELECT id,name,name_ur,revision,merged_into_id FROM patients ORDER BY id")
        ).all()
    for invalid in ("", "   "):
        response = merge(
            owner,
            source,
            dest,
            fields={"name": "Retained", "name_ur": invalid, "phone": "+923008887777", "dob": None},
        )
        assert response.status_code == 422, response.text
        with database.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT id,name,name_ur,revision,merged_into_id FROM patients ORDER BY id")
                ).all()
                == before
            )
            assert conn.execute(text("SELECT count(*) FROM idempotency_records")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one() == 0
    response = merge(
        owner,
        source,
        dest,
        fields={"name": "Retained", "name_ur": "  عائشہ   ", "phone": "+923008887777", "dob": None},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name_ur"] == "عائشہ"


def test_patient_simple_urdu_search_and_name_trigram(owner, seed, database):
    from sqlalchemy import text

    identity = add_patient(owner, name="Synthetic Alexandra", name_ur="عائشہ فاطمہ")
    # Token conjunction is order-independent under the required simple configuration.
    result = owner.get("/dashboard/patients", params={"q": "فاطمہ عائشہ"}).json()
    assert [p["id"] for p in result["items"]] == [identity]
    # Trigram name support tolerates a small spelling error.
    result = owner.get("/dashboard/patients", params={"q": "Synthetic Alexandar"}).json()
    assert [p["id"] for p in result["items"]] == [identity]
    assert owner.get("/dashboard/patients", params={"q": "%"}).json()["total"] == 0
    assert owner.get("/dashboard/patients", params={"q": "_"}).json()["total"] == 0
    assert owner.get("/dashboard/patients", params={"q": "03009998888"}).json()["total"] == 1
    with database.connect() as conn:
        assert conn.execute(
            text("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_trgm')")
        ).scalar_one()
        indexes = (
            conn.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename='patients'"))
            .scalars()
            .all()
        )
        assert any("gin_trgm_ops" in index for index in indexes)
        assert any("to_tsvector" in index and "'simple'" in index for index in indexes)


def test_patient_search_migration_preserves_existing_phase3_patients(database, seed):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from tests_api.conftest import ADMIN_URL, ROOT

    assert make_url(ADMIN_URL).database == "voice_agent_test"
    with database.connect() as conn:
        assert conn.execute(text("SELECT current_database()")).scalar_one() == "voice_agent_test"
        before = conn.execute(
            text("SELECT id,tenant_id,name,name_ur,phone FROM patients ORDER BY id")
        ).all()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", ADMIN_URL.replace("%", "%%"))
    command.downgrade(cfg, "0003")
    with database.connect() as conn:
        assert (
            conn.execute(
                text("SELECT id,tenant_id,name,name_ur,phone FROM patients ORDER BY id")
            ).all()
            == before
        )
    command.upgrade(cfg, "head")
    with database.connect() as conn:
        assert (
            conn.execute(
                text("SELECT id,tenant_id,name,name_ur,phone FROM patients ORDER BY id")
            ).all()
            == before
        )
        assert conn.execute(
            text("SELECT bool_and(revision=1 AND merged_into_id IS NULL) FROM patients")
        ).scalar_one()
        indexes = (
            conn.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename='patients'"))
            .scalars()
            .all()
        )
        assert sum("gin_trgm_ops" in index for index in indexes) == 2
        assert any("to_tsvector" in index and "'simple'" in index for index in indexes)
