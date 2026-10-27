"""Opt-in hosted synthetic conversations; never included in credential-free CI."""

import json
import os
from uuid import uuid4

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from scheduler.config import Settings
from scheduler.main import create_app
from scheduler.models import Appointment
from tests_api.conftest import APP_URL, NOW, PASSWORD, ROOT, SECRET

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_NIM_PHASE2") != "1", reason="Explicit hosted synthetic evaluation only"
)


@pytest.mark.parametrize("lang", ["en", "ur"])
def test_nim_executed_lifecycle(seed, database, lang):
    # Keep the credential inside SecretStr and never serialize request headers/settings.
    credential = SecretStr(dotenv_values(ROOT / ".env").get("LLM_API_KEY") or "")
    if not credential.get_secret_value():
        pytest.fail("NIM credential missing; no hosted result claimed")
    settings = Settings(
        database_url=APP_URL,
        jwt_secret=SECRET,
        runtime_secret="isolated-live-runtime-secret-40-characters",
        valkey_url="redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
        llm_api_key=credential,
        llm_request_budget=100,
        _env_file=None,
    )
    app = create_app(settings, clock=lambda: NOW)
    evidence = {
        "language": lang,
        "model": settings.llm_model,
        "thinking": False,
        "turns": [],
        "provider_requests": 0,
        "passed": False,
        "failure_reason": None,
    }
    try:
        with TestClient(app) as client:
            staff = client.post(
                "/auth/login", json={"email": "owner-a@example.com", "password": PASSWORD}
            ).json()["access_token"]
            created = client.post(
                "/agent-sessions",
                headers={"Authorization": "Bearer " + staff},
                json={"agent_id": str(seed["agent_a"])},
            ).json()
            sid = created["session_id"]
            client.headers["Authorization"] = "Bearer " + created["token"]
            revision = 0

            def turn(en, ur):
                nonlocal revision
                response = client.post(
                    "/runtime/sessions/" + sid + "/turn",
                    json={
                        "command_id": str(uuid4()),
                        "expected_revision": revision,
                        "text": en if lang == "en" else ur,
                        "live": True,
                    },
                )
                assert response.status_code == 200, response.status_code
                data = response.json()
                revision = data["revision"]
                evidence["provider_requests"] += data["provider_requests"]
                evidence["turns"].append(
                    {
                        "number": len(evidence["turns"]) + 1,
                        "executed_action": data.get("executed_action"),
                        "ok": data["result"]["ok"],
                        "error": data["result"]["error"],
                        "task": data["state"].get("task"),
                        "stage": data["state"].get("stage"),
                        "say": data["result"]["say"],
                        "provider_requests": data["provider_requests"],
                    }
                )
                assert data["result"]["ok"], data["result"]["error"]
                return data["state"]

            turn("03001234567", "03001234567")
            turn("Synthetic Patient", "Synthetic Patient")
            s = turn(
                "Book Consultation with Dr Test a on 2026-11-03.",
                "ڈاکٹر ٹیسٹ کے ساتھ مشورے کی اپوائنٹمنٹ 2026-11-03 کو چاہیے۔",
            )
            assert s["fields"].get("date") == "2026-11-03"
            s = turn("Show the available times.", "دستیاب اوقات بتائیں۔")
            assert s.get("offers")
            s = turn("Choose the first offered time.", "پہلا دستیاب وقت منتخب کریں۔")
            assert s.get("hold")
            s = turn(
                "Read back the appointment details for confirmation.",
                "تصدیق کے لیے اپوائنٹمنٹ کی تفصیل دہرائیں۔",
            )
            assert s.get("readback")
            turn("yes", "جی ہاں")
            with Session(database) as db:
                first = db.scalar(
                    select(Appointment).where(
                        Appointment.patient_id == seed["patient_a"], Appointment.status == "booked"
                    )
                )
                assert first is not None
                original = first.id
            turn(
                "I want to reschedule my appointment.",
                "میں اپنی اپوائنٹمنٹ کا وقت تبدیل کرنا چاہتا ہوں۔",
            )
            s = turn("Find my upcoming appointments.", "میری آنے والی اپوائنٹمنٹس دکھائیں۔")
            assert s.get("appointments")
            s = turn("Choose the first appointment.", "پہلی اپوائنٹمنٹ منتخب کریں۔")
            assert s.get("appointment")
            turn("Change the date to 2026-11-04.", "تاریخ 2026-11-04 کر دیں۔")
            s = turn("What are the consultation fees?", "مشورے کی فیس کیا ہے؟")
            assert s["fields"]["date"] == "2026-11-04"
            turn("Actually change the date to 2026-11-05.", "بلکہ تاریخ 2026-11-05 کر دیں۔")
            s = turn("Show available times.", "دستیاب اوقات دکھائیں۔")
            assert s.get("offers")
            s = turn("Choose the first offered time.", "پہلا دستیاب وقت منتخب کریں۔")
            assert s.get("hold")
            s = turn(
                "Read back the appointment details for confirmation.",
                "تصدیق کے لیے اپوائنٹمنٹ کی تفصیل دہرائیں۔",
            )
            assert s.get("readback")
            turn("yes", "جی ہاں")
            with Session(database) as db:
                assert db.get(Appointment, original).status == "cancelled"
                replacement = db.scalar(
                    select(Appointment).where(Appointment.rescheduled_from_id == original)
                )
                assert replacement is not None
                replacement_id = replacement.id
            turn("I want to cancel my appointment.", "میں اپنی اپوائنٹمنٹ منسوخ کرنا چاہتا ہوں۔")
            turn("Find my upcoming appointments.", "میری آنے والی اپوائنٹمنٹس دکھائیں۔")
            turn("Choose the first appointment.", "پہلی اپوائنٹمنٹ منتخب کریں۔")
            s = turn("Read back the cancellation details.", "منسوخی کی تفصیل دہرائیں۔")
            assert s.get("readback")
            turn("yes", "جی ہاں")
            with Session(database) as db:
                assert db.get(Appointment, replacement_id).status == "cancelled"
            evidence["passed"] = True
    finally:
        evidence["failure_reason"] = app.state.brain_planner.last_failure
        output = ROOT / "results" / ("phase2-live-" + lang + "-" + str(uuid4()) + ".json")
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.parametrize("lang", ["en", "ur"])
def test_nim_family_task_parking_resume(seed, database, lang):
    from scheduler.models import Patient

    child_id = uuid4()
    with Session(database) as db, db.begin():
        db.add(
            Patient(
                id=child_id,
                tenant_id=seed["tenant_a"],
                name="Demo Child",
                name_key="demo child",
                name_ur="نمونہ بچہ",
                phone="+923001234567",
            )
        )
    credential = SecretStr(dotenv_values(ROOT / ".env").get("LLM_API_KEY") or "")
    settings = Settings(
        database_url=APP_URL,
        jwt_secret=SECRET,
        runtime_secret="isolated-live-runtime-secret-40-characters",
        valkey_url="redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
        llm_api_key=credential,
        _env_file=None,
    )
    app = create_app(settings, clock=lambda: NOW)
    evidence = {
        "suite": "family-task-resume",
        "language": lang,
        "model": settings.llm_model,
        "thinking": False,
        "turns": [],
        "provider_requests": 0,
        "passed": False,
    }
    try:
        with TestClient(app) as client:
            staff = client.post(
                "/auth/login", json={"email": "owner-a@example.com", "password": PASSWORD}
            ).json()["access_token"]
            created = client.post(
                "/agent-sessions",
                headers={"Authorization": "Bearer " + staff},
                json={"agent_id": str(seed["agent_a"])},
            ).json()
            sid = created["session_id"]
            client.headers["Authorization"] = "Bearer " + created["token"]
            revision = 0

            def turn(en, ur):
                nonlocal revision
                response = client.post(
                    "/runtime/sessions/" + sid + "/turn",
                    json={
                        "command_id": str(uuid4()),
                        "expected_revision": revision,
                        "text": en if lang == "en" else ur,
                    },
                )
                assert response.status_code == 200, response.status_code
                data = response.json()
                revision = data["revision"]
                evidence["provider_requests"] += data["provider_requests"]
                evidence["turns"].append(
                    {
                        "number": len(evidence["turns"]) + 1,
                        "executed_action": data.get("executed_action"),
                        "ok": data["result"]["ok"],
                        "error": data["result"]["error"],
                        "task": data["state"]["task"],
                        "stage": data["state"]["stage"],
                        "say": data["result"]["say"],
                        "provider_requests": data["provider_requests"],
                    }
                )
                assert data["result"]["ok"], data["result"]["error"]
                return data["state"]

            turn("03001234567", "03001234567")
            turn("Synthetic Patient", "Synthetic Patient")
            s = turn(
                "I want to book for my child instead of myself.",
                "اپنے بجائے اپنے بچے کے لیے اپوائنٹمنٹ بک کرنا چاہتا ہوں۔",
            )
            assert not s["verified"] and not s.get("subject")
            turn("03001234567", "03001234567")
            s = turn("Demo Child", "Demo Child")
            assert s["subject"] == str(child_id) and s["verified"]
            s = turn(
                "Book Consultation with Dr Test a on 2026-11-03.",
                "ڈاکٹر ٹیسٹ کے ساتھ مشورے کی اپوائنٹمنٹ 2026-11-03 کو چاہیے۔",
            )
            known = s["fields"].copy()
            assert known.get("date") == "2026-11-03"
            s = turn(
                "Before finishing this booking, I want to cancel an appointment instead.",
                "یہ بکنگ مکمل کرنے سے پہلے ایک اپوائنٹمنٹ منسوخ کرنا چاہتا ہوں۔",
            )
            assert s["task"] == "cancel" and s["parked"]
            s = turn(
                "Return to the previous booking request.", "پچھلی بکنگ کی درخواست دوبارہ جاری کریں۔"
            )
            assert s["task"] == "book" and s["fields"] == known and s["verified"]
            s = turn("Show the available times.", "دستیاب اوقات دکھائیں۔")
            assert s["offers"]
            s = turn("Choose the first offered time.", "پہلا دستیاب وقت منتخب کریں۔")
            assert s.get("hold")
            s = turn(
                "Read back the appointment details for confirmation.",
                "تصدیق کے لیے اپوائنٹمنٹ کی تفصیل دہرائیں۔",
            )
            assert s.get("readback")
            turn("yes", "جی ہاں")
            with Session(database) as db:
                rows = list(
                    db.scalars(select(Appointment).where(Appointment.tenant_id == seed["tenant_a"]))
                )
                assert len(rows) == 1 and rows[0].patient_id == child_id
            evidence["passed"] = True
    finally:
        evidence["failure_reason"] = app.state.brain_planner.last_failure
        output = ROOT / "results" / ("phase2-family-" + lang + "-" + str(uuid4()) + ".json")
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
