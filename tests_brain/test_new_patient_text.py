from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from scheduler.models import Patient
from tests_brain.test_transport_safety import transport as transport


def test_new_patient_identity_stays_in_code_and_requires_separate_readback(transport, database):
    client, prefix, planner = transport
    revision = 0

    def turn(text):
        nonlocal revision
        response = client.post(
            prefix + "/turn",
            json={"command_id": str(uuid4()), "expected_revision": revision, "text": text},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        revision = result["revision"]
        return result

    assert turn("03007654321")["result"]["ok"]
    name = turn("Demo New Patient")
    assert not name["result"]["ok"] and name["result"]["error"]["field"] == "name_ur"
    assert not name["state"].get("readback")
    readback = turn("نمونہ نیا مریض")
    assert readback["result"]["ok"] and readback["state"]["readback"]["kind"] == "patient"
    with Session(database) as db:
        assert db.scalar(select(Patient).where(Patient.phone == "+923007654321")) is None
    created = turn("yes")
    assert created["result"]["ok"] and created["state"]["verified"]
    with Session(database) as db:
        patient = db.scalar(select(Patient).where(Patient.phone == "+923007654321"))
        assert patient.name == "Demo New Patient" and patient.name_ur == "نمونہ نیا مریض"
    assert planner.calls == []
