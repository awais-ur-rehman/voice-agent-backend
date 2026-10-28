"""Real PostgreSQL scheduling projections and provider-serialized templates."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from scheduler.models import Appointment, Provider, Schedule
from tests_api.conftest import PASSWORD


def availability(owner, provider):
    response = owner.get(f"/dashboard/providers/{provider}/availability")
    assert response.status_code == 200, response.text
    return response.json()


def template(owner, provider, body, *, preview=False, key="template-test-key"):
    return owner.post(
        f"/dashboard/providers/{provider}/availability/{'preview' if preview else 'apply'}",
        json=body,
        headers={"Idempotency-Key": key},
    )


def friday(snapshot, **extra):
    return {"revision": snapshot["revision"], "template": "friday_prayer"} | extra


def ramadan(snapshot, **extra):
    return {
        "revision": snapshot["revision"],
        "template": "ramadan",
        "start_date": "2027-02-10",
        "end_date": "2027-03-10",
        "start_time": "10:00",
        "end_time": "15:00",
    } | extra


def test_assignments_persist_paginate_and_hide_other_tenant(owner, seed):
    service = owner.post("/services", json={"name": "Review", "duration_min": 15}).json()["id"]
    path = f"/providers/{seed['provider_a']}/services/{service}"
    assert owner.put(path).status_code == 204
    first = owner.get("/dashboard/provider-services", params={"limit": 1}).json()
    second = owner.get("/dashboard/provider-services", params={"limit": 1, "offset": 1}).json()
    assert first["total"] == second["total"] == 2
    assert first["limit"] == 1 and second["offset"] == 1
    assert {x["service_id"] for x in first["items"] + second["items"]} == {
        service,
        str(seed["service_a"]),
    }
    assert owner.delete(path).status_code == 204
    assert owner.get("/dashboard/provider-services").json()["total"] == 1


def insert_appointment(database, seed, starts, *, suffix="a"):
    with Session(database) as db, db.begin():
        row = Appointment(
            id=uuid4(),
            tenant_id=seed[f"tenant_{suffix}"],
            patient_id=seed[f"patient_{suffix}"],
            provider_id=seed[f"provider_{suffix}"],
            service_id=seed[f"service_{suffix}"],
            starts_at=starts,
            ends_at=starts.replace(minute=30),
            duration_min=30,
            buffer_before_min=0,
            buffer_after_min=0,
        )
        db.add(row)
        return str(row.id)


def test_calendar_local_boundaries_names_pagination_and_tenant(owner, seed, database):
    owner.patch(f"/providers/{seed['provider_a']}", json={"name_ur": "ڈاکٹر"})
    owner.patch(f"/services/{seed['service_a']}", json={"name_ur": "معائنہ"})
    owner.put(
        f"/patients/{seed['patient_a']}",
        json={
            "name": "Synthetic Patient",
            "name_ur": "مریض",
            "phone": "+923001234567",
        },
    )
    insert_appointment(database, seed, datetime(2026, 11, 2, 18, tzinfo=UTC))
    first_id = insert_appointment(database, seed, datetime(2026, 11, 2, 19, tzinfo=UTC))
    insert_appointment(database, seed, datetime(2026, 11, 3, 18, tzinfo=UTC))
    insert_appointment(database, seed, datetime(2026, 11, 3, 19, tzinfo=UTC))
    insert_appointment(database, seed, datetime(2026, 11, 3, 10, tzinfo=UTC), suffix="b")
    query = {"date_from": "2026-11-03", "date_to": "2026-11-03", "limit": 1}
    response = owner.get("/dashboard/appointments", params=query)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 2 and page["limit"] == 1 and page["offset"] == 0
    item = page["items"][0]
    assert item["id"] == first_id
    assert item["patient_name"] == "Synthetic Patient" and item["patient_name_ur"] == "مریض"
    assert item["provider_name_ur"] == "ڈاکٹر" and item["service_name_ur"] == "معائنہ"
    assert "phone" not in item and "dob" not in item
    assert (
        owner.get("/dashboard/appointments", params=query | {"offset": 1}).json()["items"][0]["id"]
        != first_id
    )
    assert (
        owner.get(
            "/dashboard/appointments", params=query | {"provider_id": str(seed["provider_b"])}
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"date_from": "2026-11-03"},
        {"date_from": "2026-11-03", "date_to": "2026-11-02"},
        {"date_from": "2026-11-01", "date_to": "2026-12-13"},
        {"date_from": "9999-12-31", "date_to": "9999-12-31"},
    ],
)
def test_calendar_rejects_unbounded_or_invalid_ranges(owner, query):
    assert owner.get("/dashboard/appointments", params=query).status_code == 422


def test_calendar_allows_42_inclusive_days(owner):
    assert (
        owner.get(
            "/dashboard/appointments",
            params={
                "date_from": "2026-11-01",
                "date_to": "2026-12-12",
            },
        ).status_code
        == 200
    )


def test_friday_preview_no_writes_and_apply_preserves_other_days(owner, seed):
    provider = seed["provider_a"]
    before = availability(owner, provider)
    body = friday(before)
    preview = template(owner, provider, body, preview=True)
    assert preview.status_code == 200, preview.text
    assert availability(owner, provider) == before
    result = template(owner, provider, body)
    assert result.status_code == 200, result.text
    after = result.json()
    assert after == preview.json()["after"]
    assert after["revision"] != before["revision"]
    assert [
        (r["start_time"], r["end_time"], r["slot_min"]) for r in after["rules"] if r["weekday"] == 5
    ] == [
        ("09:00:00", "12:45:00", 30),
        ("14:30:00", "17:00:00", 30),
    ]
    assert [r for r in after["rules"] if r["weekday"] != 5] == [
        r for r in before["rules"] if r["weekday"] != 5
    ]
    assert template(owner, provider, body).json() == after
    assert template(owner, provider, friday(after), key="template-noop-key").json() == after
    assert template(owner, provider, body, key="template-stale-key").status_code == 409
    assert template(owner, provider, friday(after), key="template-test-key").status_code == 409
    assert owner.get("/availability-rules").status_code == 200


def test_friday_only_overlapping_intervals_and_selected_provider(owner, seed):
    provider = seed["provider_a"]
    other = owner.post("/providers", json={"name": "Second"}).json()["id"]
    for p in (provider, other):
        owner.post(
            "/availability-rules",
            json={"provider_id": str(p), "weekday": 5, "start_time": "18:00", "end_time": "19:00"},
        )
    other_before = availability(owner, other)
    before = availability(owner, provider)
    result = template(owner, provider, friday(before, break_start="08:00", break_end="10:00"))
    assert result.status_code == 200, result.text
    assert [
        (r["start_time"], r["end_time"]) for r in result.json()["rules"] if r["weekday"] == 5
    ] == [
        ("10:00:00", "17:00:00"),
        ("18:00:00", "19:00:00"),
    ]
    assert availability(owner, other) == other_before


def test_ramadan_preview_repeat_and_holiday_conflict_roll_back(owner, seed):
    provider = seed["provider_a"]
    before = availability(owner, provider)
    body = ramadan(before)
    preview = template(owner, provider, body, preview=True)
    assert preview.status_code == 200, preview.text
    assert availability(owner, provider) == before
    after = template(owner, provider, body).json()
    assert after == preview.json()["after"]
    assert after["exceptions"][0]["kind"] == "custom"
    assert template(owner, provider, ramadan(after), key="ramadan-noop-key").json() == after
    holiday = owner.post(
        "/exceptions",
        json={
            "provider_id": str(provider),
            "start_date": "2027-03-11",
            "end_date": "2027-03-11",
            "kind": "closed",
        },
    )
    assert holiday.status_code == 201
    current = availability(owner, provider)
    for preview in (True, False):
        response = template(
            owner,
            provider,
            ramadan(current, end_date="2027-03-12"),
            preview=preview,
            key="ramadan-conflict-key",
        )
        assert response.status_code == 409, response.text
        assert availability(owner, provider) == current


@pytest.mark.parametrize(
    "extra",
    [
        {"break_start": "15:00", "break_end": "12:00"},
        {"break_start": "12:00+05:00"},
        {"start_date": "2027-01-01"},
    ],
)
def test_invalid_friday_never_writes(owner, seed, extra):
    before = availability(owner, seed["provider_a"])
    assert template(owner, seed["provider_a"], friday(before, **extra)).status_code == 422
    assert availability(owner, seed["provider_a"]) == before


def test_template_roles_ownership_and_required_key(owner, seed):
    provider = seed["provider_a"]
    before = availability(owner, provider)
    assert owner.get(f"/dashboard/providers/{seed['provider_b']}/availability").status_code == 404
    assert template(owner, seed["provider_b"], friday(before)).status_code == 404
    assert (
        owner.post(
            f"/dashboard/providers/{provider}/availability/apply", json=friday(before)
        ).status_code
        == 422
    )
    login = owner.post(
        "/auth/login", json={"email": "staff-a@example.com", "password": PASSWORD}
    ).json()
    owner.headers["Authorization"] = "Bearer " + login["access_token"]
    assert availability(owner, provider) == before
    assert owner.get("/dashboard/provider-services").status_code == 200
    assert (
        owner.get(
            "/dashboard/appointments", params={"date_from": "2026-11-01", "date_to": "2026-11-01"}
        ).status_code
        == 200
    )
    for preview in (True, False):
        assert template(owner, provider, friday(before), preview=preview).status_code == 403


def test_competing_templates_only_one_snapshot_wins(owner, seed):
    provider = seed["provider_a"]
    body = friday(availability(owner, provider))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda i: template(owner, provider, body, key=f"competing-template-{i}"), range(2)
            )
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    assert len(availability(owner, provider)["rules"]) == 8


def test_legacy_edit_under_provider_lock_invalidates_template(owner, seed, database):
    provider = seed["provider_a"]
    before = availability(owner, provider)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(database) as db, db.begin():
            db.scalar(select(Provider).where(Provider.id == provider).with_for_update())
            rule = db.scalar(
                select(Schedule).where(Schedule.provider_id == provider, Schedule.weekday == 5)
            )
            rule.start_time = time(10)
            db.flush()
            future = pool.submit(template, owner, provider, friday(before))
        result = future.result(timeout=10)
    assert result.status_code == 409, result.text
    current = availability(owner, provider)
    assert [(r["start_time"], r["end_time"]) for r in current["rules"] if r["weekday"] == 5] == [
        ("10:00:00", "17:00:00")
    ]


def test_apply_failure_rolls_back_deleted_rules_and_receipt(owner, seed, database):
    """A real PK conflict after deletions must roll back the entire transaction."""
    from scheduler.models import Idempotency

    provider = seed["provider_a"]
    before = availability(owner, provider)
    body = friday(before)
    preview = template(owner, provider, body, preview=True).json()
    colliding_id = preview["changes"]["added_rules"][0]["id"]
    with Session(database) as db, db.begin():
        db.add(
            Schedule(
                id=__import__("uuid").UUID(colliding_id),
                tenant_id=seed["tenant_b"],
                provider_id=seed["provider_b"],
                weekday=5,
                start_time=time(18),
                end_time=time(19),
                slot_min=30,
            )
        )
    result = template(owner, provider, body, key="rollback-template-key")
    assert result.status_code == 409, result.text
    assert availability(owner, provider) == before
    with Session(database) as db:
        assert (
            db.scalar(select(Idempotency).where(Idempotency.key == "rollback-template-key")) is None
        )


def test_same_key_concurrent_apply_replays_one_result(owner, seed):
    provider = seed["provider_a"]
    body = friday(availability(owner, provider))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: template(owner, provider, body), range(2)))
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json() == results[1].json() == availability(owner, provider)


@pytest.mark.parametrize(
    "extra",
    [
        {"start_time": None},
        {"end_date": "2027-01-01"},
        {"start_time": "10:00+05:00"},
        {"end_time": "09:00"},
        {"break_start": "12:00"},
    ],
)
def test_invalid_ramadan_never_writes(owner, seed, extra):
    provider = seed["provider_a"]
    before = availability(owner, provider)
    assert template(owner, provider, ramadan(before, **extra)).status_code == 422
    assert availability(owner, provider) == before


def test_calendar_dst_fall_back_uses_25_hour_local_day(owner, seed, database):
    from scheduler.models import Tenant

    with Session(database) as db, db.begin():
        db.get(Tenant, seed["tenant_a"]).timezone = "America/New_York"
    insert_appointment(database, seed, datetime(2026, 11, 1, 3, tzinfo=UTC))
    first = insert_appointment(database, seed, datetime(2026, 11, 1, 4, tzinfo=UTC))
    last = insert_appointment(database, seed, datetime(2026, 11, 2, 4, tzinfo=UTC))
    insert_appointment(database, seed, datetime(2026, 11, 2, 5, tzinfo=UTC))
    result = owner.get(
        "/dashboard/appointments", params={"date_from": "2026-11-01", "date_to": "2026-11-01"}
    )
    assert result.status_code == 200, result.text
    assert [item["id"] for item in result.json()["items"]] == [first, last]


@pytest.mark.parametrize("method", ["put", "delete"])
def test_legacy_update_loaded_before_template_removal_returns_not_found(
    owner, seed, monkeypatch, method
):
    from threading import Event

    from scheduler import catalog

    provider = seed["provider_a"]
    before = availability(owner, provider)
    rule = next(r for r in before["rules"] if r["weekday"] == 5)
    loaded, resume = Event(), Event()
    original_get = catalog.get

    def pause_get(db, model, identity, lock=False):
        if model is Provider and lock and not resume.is_set():
            loaded.set()
            assert resume.wait(timeout=10)
        return original_get(db, model, identity, lock)

    monkeypatch.setattr(catalog, "get", pause_get)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            lambda: (
                owner.put(
                    f"/availability-rules/{rule['id']}",
                    json={k: v for k, v in rule.items() if k != "id"},
                )
                if method == "put"
                else owner.delete(f"/availability-rules/{rule['id']}")
            )
        )
        assert loaded.wait(timeout=5)
        try:
            result = template(owner, provider, friday(before))
            assert result.status_code == 200, result.text
        finally:
            resume.set()
        result = pending.result(timeout=10)
    assert result.status_code == 404, result.text
    assert len(availability(owner, provider)["rules"]) == 8


def test_dashboard_scheduling_requires_authentication(client, seed):
    for path in (
        "/dashboard/provider-services",
        "/dashboard/appointments?date_from=2026-11-01&date_to=2026-11-01",
        f"/dashboard/providers/{seed['provider_a']}/availability",
    ):
        assert client.get(path).status_code == 401
