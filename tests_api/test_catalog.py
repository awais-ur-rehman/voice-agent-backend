from tests_api.conftest import PASSWORD


def test_role_scoping_and_hidden_cross_tenant_ids(owner, seed):
    assert len(owner.get("/providers").json()) == 1
    assert owner.get(f"/patients/{seed['patient_b']}").status_code == 404
    assert (
        owner.post(
            "/availability-rules",
            json={
                "provider_id": str(seed["provider_b"]),
                "weekday": 1,
                "start_time": "09:00",
                "end_time": "12:00",
                "slot_min": 30,
            },
        ).status_code
        == 404
    )
    token = owner.post(
        "/auth/login", json={"email": "staff-a@example.com", "password": PASSWORD}
    ).json()["access_token"]
    owner.headers["Authorization"] = "Bearer " + token
    assert owner.post("/providers", json={"name": "Unauthorized"}).status_code == 403
    assert owner.get("/providers").status_code == 200


def test_configuration_crud_archive_and_invalid_windows(owner):
    provider = owner.post("/providers", json={"name": "Dr New"}).json()
    service = owner.post(
        "/services", json={"name": "Dental", "duration_min": 20, "price": "1500.00"}
    ).json()
    assert owner.put(f"/providers/{provider['id']}/services/{service['id']}").status_code == 204
    bad = owner.post(
        "/availability-rules",
        json={
            "provider_id": provider["id"],
            "weekday": 1,
            "start_time": "14:00",
            "end_time": "12:00",
            "slot_min": 15,
        },
    )
    assert bad.status_code == 422
    assert owner.patch("/clinic", json={"timezone": "invented/zone"}).status_code == 422
    assert owner.delete(f"/providers/{provider['id']}").status_code == 204
    assert (
        next(p for p in owner.get("/providers").json() if p["id"] == provider["id"])["active"]
        is False
    )


def test_patients_normalize_and_dedupe_and_forbid_tenant_input(owner, seed):
    result = owner.post("/patients", json={"name": "  New   Person ", "phone": "0301 2345678"})
    assert result.status_code == 201, result.text
    assert result.json()["name"] == "New Person"
    assert result.json()["phone"] == "+923012345678"
    assert (
        owner.post("/patients", json={"name": "new person", "phone": "+923012345678"}).status_code
        == 409
    )
    assert (
        owner.post(
            "/patients",
            json={"name": "Injected", "phone": "03012345678", "tenant_id": str(seed["tenant_b"])},
        ).status_code
        == 422
    )
    assert (
        owner.get("/patients", params={"search": "New Person"}).json()[0]["id"]
        == result.json()["id"]
    )


def test_slots_closed_exceptions_and_query_bounds(owner, seed):
    query = {
        "service_id": str(seed["service_a"]),
        "date_from": "2026-11-03",
        "date_to": "2026-11-03",
    }
    assert len(owner.get("/slots", params=query).json()) == 16
    close = owner.post(
        "/exceptions",
        json={
            "provider_id": str(seed["provider_a"]),
            "start_date": "2026-11-03",
            "end_date": "2026-11-03",
            "kind": "closed",
        },
    )
    assert close.status_code == 201, close.text
    assert owner.get("/slots", params=query).json() == []
    assert owner.delete("/exceptions/" + close.json()["id"]).status_code == 204
    assert len(owner.get("/slots", params=query).json()) == 16
    assert owner.get("/slots", params=query | {"date_to": "2027-03-03"}).status_code == 422
    assert (
        owner.get("/slots", params=query | {"service_id": str(seed["service_b"])}).status_code
        == 404
    )


def test_out_of_horizon_extreme_calendar_dates_return_empty_slots(owner, seed):
    query = {
        "service_id": str(seed["service_a"]),
        "date_from": "9999-12-31",
        "date_to": "9999-12-31",
    }
    response = owner.get("/slots", params=query)
    assert response.status_code == 200
    assert response.json() == []


def test_reviewed_urdu_names_can_be_saved_and_are_cleared_on_rename(owner, seed):
    response = owner.patch("/providers/" + str(seed["provider_a"]), json={"name_ur": "ڈاکٹر نمونہ"})
    assert response.status_code == 200, response.text
    assert response.json()["name_ur"] == "ڈاکٹر نمونہ"
    changed = owner.patch(
        "/providers/" + str(seed["provider_a"]), json={"name": "Renamed Provider"}
    )
    assert changed.status_code == 200 and changed.json()["name_ur"] is None
    patient = owner.post(
        "/patients", json={"name": "Demo Urdu", "name_ur": "نمونہ مریض", "phone": "03007654321"}
    )
    assert patient.status_code == 201 and patient.json()["name_ur"] == "نمونہ مریض"
