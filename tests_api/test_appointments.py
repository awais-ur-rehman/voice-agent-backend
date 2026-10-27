from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import text


def payload(seed, **updates):
    return {
        "patient_id": str(seed["patient_a"]),
        "provider_id": str(seed["provider_a"]),
        "service_id": str(seed["service_a"]),
        "starts_at": "2026-11-03T10:00:00+05:00",
        "confirmed": True,
    } | updates


def book(client, seed, key=None, **updates):
    return client.post(
        "/appointments",
        json=payload(seed, **updates),
        headers={"Idempotency-Key": key or str(uuid4())},
    )


def test_booking_replay_mismatch_and_confirmation(owner, seed):
    key = str(uuid4())
    first = book(owner, seed, key)
    assert first.status_code == 201, first.text
    assert book(owner, seed, key).json() == first.json()
    mismatch = book(owner, seed, key, starts_at="2026-11-03T11:00:00+05:00")
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "idempotency_conflict"
    assert book(owner, seed, confirmed=False).status_code == 422
    assert book(owner, seed, starts_at="2026-11-03T11:00:00").status_code == 422
    assert owner.post("/appointments", json=payload(seed)).status_code == 422


def test_conflicts_alternatives_and_adjacent_bookings(owner, seed):
    assert book(owner, seed).status_code == 201
    conflict = book(owner, seed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "slot_unavailable"
    assert len(conflict.json()["error"]["details"]["alternatives"]) == 3
    assert book(owner, seed, starts_at="2026-11-03T10:30:00+05:00").status_code == 201


def test_atomic_reschedule_rollback_and_cancel_rebooking(owner, seed):
    first = book(owner, seed).json()
    occupied = book(owner, seed, starts_at="2026-11-03T11:00:00+05:00").json()
    move = {"starts_at": occupied["starts_at"], "confirmed": True}
    assert (
        owner.post(
            f"/appointments/{first['id']}/reschedule",
            json=move,
            headers={"Idempotency-Key": str(uuid4())},
        ).status_code
        == 409
    )
    assert owner.get(f"/appointments/{first['id']}").json()["status"] == "booked"
    moved = owner.post(
        f"/appointments/{first['id']}/reschedule",
        json={"starts_at": "2026-11-04T10:00:00+05:00", "confirmed": True},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert moved.status_code == 201, moved.text
    assert moved.json()["rescheduled_from_id"] == first["id"]
    assert owner.get(f"/appointments/{first['id']}").json()["status"] == "cancelled"
    key = str(uuid4())
    cancelled = owner.post(
        f"/appointments/{moved.json()['id']}/cancel",
        json={"confirmed": True},
        headers={"Idempotency-Key": key},
    )
    assert cancelled.status_code == 200
    assert (
        owner.post(
            f"/appointments/{moved.json()['id']}/cancel",
            json={"confirmed": True},
            headers={"Idempotency-Key": key},
        ).json()
        == cancelled.json()
    )
    assert book(owner, seed, starts_at="2026-11-04T10:00:00+05:00").status_code == 201


def test_policies_and_cross_tenant_identifiers(owner, seed):
    assert book(owner, seed, patient_id=str(seed["patient_b"])).status_code == 404
    assert book(owner, seed, provider_id=str(seed["provider_b"])).status_code == 404
    assert book(owner, seed, starts_at="2026-11-01T05:00:00+05:00").status_code == 409
    assert book(owner, seed, starts_at="2027-11-01T10:00:00+05:00").status_code == 409
    first = book(owner, seed).json()
    owner.patch("/clinic", json={"cancellation_min": 43200})
    denied = owner.post(
        f"/appointments/{first['id']}/cancel",
        json={"confirmed": True},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert denied.status_code == 409
    assert owner.get(f"/appointments/{first['id']}").json()["status"] == "booked"


def test_concurrent_same_key_is_one_successful_operation(owner, seed, database):
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: book(owner, seed, key), range(6)))
    assert [r.status_code for r in results] == [201] * 6
    assert len({r.json()["id"] for r in results}) == 1
    with database.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM appointments WHERE status='booked'")
            ).scalar_one()
            == 1
        )
        assert conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one() == 1


def test_concurrent_same_slot_has_exactly_one_winner(owner, seed):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: book(owner, seed), range(8)))
    assert sum(r.status_code == 201 for r in results) == 1
    assert sum(r.status_code == 409 for r in results) == 7


def test_daily_limit_serializes_across_different_providers(owner, seed):
    owner.patch("/clinic", json={"daily_limit": 1})
    other = owner.post("/providers", json={"name": "Dr Parallel"}).json()["id"]
    owner.put(f"/providers/{other}/services/{seed['service_a']}")
    owner.post(
        "/availability-rules",
        json={
            "provider_id": other,
            "weekday": 2,
            "start_time": "09:00",
            "end_time": "17:00",
            "slot_min": 30,
        },
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda provider: book(owner, seed, provider_id=provider),
                [str(seed["provider_a"]), other],
            )
        )
    assert sorted(r.status_code for r in results) == [201, 409]


def test_confirmation_is_strict_json_true_not_truthy_number_or_string(owner, seed):
    for value in [False, 0, 1, "true", None]:
        assert book(owner, seed, confirmed=value).status_code == 422


def test_database_transaction_abort_returns_safe_retry_and_rolls_back(
    owner, seed, database, monkeypatch
):
    import psycopg
    from sqlalchemy.exc import OperationalError

    import scheduler.appointments as module

    original = module.new_appointment

    def abort(*args, **kwargs):
        raise OperationalError("synthetic", {}, psycopg.errors.DeadlockDetected("synthetic"))

    monkeypatch.setattr(module, "new_appointment", abort)
    key = str(uuid4())
    response = book(owner, seed, key)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "transaction_conflict"
    with database.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM appointments")).scalar_one() == 0
    monkeypatch.setattr(module, "new_appointment", original)
    assert book(owner, seed, key).status_code == 201


def test_failure_after_cancel_flush_rolls_back_original_audit_and_idempotency(
    owner, seed, database, monkeypatch
):
    import scheduler.appointments as module
    from scheduler.errors import AppError

    original = book(owner, seed).json()

    def abort(db, *args, **kwargs):
        status = db.execute(
            text("SELECT status FROM appointments WHERE id=:id"), {"id": original["id"]}
        ).scalar_one()
        assert status == "cancelled", "Failure must happen after the original cancellation flush"
        raise AppError("injected_failure", 409)

    monkeypatch.setattr(module, "new_appointment", abort)
    key = str(uuid4())
    response = owner.post(
        "/appointments/" + original["id"] + "/reschedule",
        json={"starts_at": "2026-11-04T10:00:00+05:00", "confirmed": True},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 409
    assert owner.get("/appointments/" + original["id"]).json()["status"] == "booked"
    with database.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM appointments")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one() == 1
        assert (
            conn.execute(
                text("SELECT count(*) FROM idempotency_records WHERE key=:key"), {"key": key}
            ).scalar_one()
            == 0
        )


def test_extreme_timestamp_offsets_are_validation_errors_and_max_date_filter_is_safe(owner, seed):
    for value in ["9999-12-31T23:59:59-12:00", "0001-01-01T00:00:00+14:00"]:
        response = book(owner, seed, starts_at=value)
        assert response.status_code == 422
    assert owner.get("/appointments", params={"date_to": "9999-12-31"}).status_code == 200
