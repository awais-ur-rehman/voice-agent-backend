"""Local synthetic HTTP smoke. No NIM calls, passwords, capabilities or transcripts printed."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from brain.bootstrap import read_demo_credentials
from scheduler.config import ROOT
from scripts.smoke_phase1 import call, require


def main():
    clinics = read_demo_credentials(ROOT / ".local/phase1-demo-credentials.json")["clinics"]
    a, b = clinics
    require(a["owner_email"] == "demo-owner-a@example.com", "synthetic clinic only")
    require(call("GET", "/health/ready")[0] == 200, "database and cache ready")
    status, login = call(
        "POST", "/auth/login", {"email": a["owner_email"], "password": a["owner_password"]}
    )
    require(status == 200, "staff login")
    staff = login["access_token"]
    _, patient = call("GET", "/patients/" + a["patient_id"], token=staff)
    _, clinic = call("GET", "/clinic", token=staff)
    days = max(3, clinic["cancellation_min"] // 1440 + 2)
    require(days + 2 <= clinic["horizon_days"], "synthetic policy horizon")
    day = (datetime.now(UTC) + timedelta(days=days)).date()
    status, created = call("POST", "/agent-sessions", {"agent_id": a["agent_id"]}, staff)
    require(status == 201, "scoped session creation")
    token, sid, revision = created["token"], created["session_id"], 0
    prefix = "/runtime/sessions/" + sid
    require(call("GET", "/patients", token=token)[0] == 401, "runtime cannot authorize staff API")
    require(
        call("POST", "/agent-sessions", {"agent_id": b["agent_id"]}, staff)[0] == 404,
        "cross-tenant agent denied",
    )

    def command(name, args=None, text="", *, identity=None, expected=None):
        nonlocal revision
        body = {
            "command_id": identity or str(uuid4()),
            "expected_revision": revision if expected is None else expected,
            "text": text,
            "action": {"name": name, "arguments": args or {}},
        }
        status, data = call("POST", prefix + "/commands", body, token)
        require(status == 200 and data["result"]["ok"], name)
        require(data["provider_requests"] == 0, "scripted provenance")
        revision = data["revision"]
        return data

    command("find_patient", {"phone": patient["phone"]})
    command("verify_patient", {"factor": patient["name"]})
    command(
        "update_dialogue_state",
        {
            "service_id": a["service_id"],
            "provider_id": a["provider_id"],
            "date_phrase": day.isoformat(),
        },
    )
    data = command("get_available_slots")
    command("hold_slot", {"ref": data["state"]["offers"][0]["ref"]})
    command("read_back")
    key, old_revision = str(uuid4()), revision
    booked = command("book_appointment", text="yes", identity=key)["result"]
    replay = command("book_appointment", text="yes", identity=key, expected=old_revision)
    require(replay["result"] == booked, "exact committed receipt replay")
    booked_id = booked["data"]["appointment"]["id"]
    command("update_dialogue_state", {"task": "reschedule"})
    data = command("find_appointments")
    original = next(v for v in data["state"]["appointments"] if v["id"] == booked_id)
    command("select_appointment", {"ref": original["ref"]})
    command("update_dialogue_state", {"date_phrase": (day + timedelta(days=1)).isoformat()})
    data = command("get_available_slots")
    command("hold_slot", {"ref": data["state"]["offers"][0]["ref"]})
    command("read_back")
    moved = command("reschedule_appointment", text="yes")["result"]["data"]["appointment"]
    require(moved["rescheduled_from_id"] == booked_id, "replacement links original")
    require(
        call("GET", "/appointments/" + booked_id, token=staff)[1]["status"] == "cancelled",
        "original cancelled",
    )
    command("update_dialogue_state", {"task": "cancel"})
    data = command("find_appointments")
    chosen = next(v for v in data["state"]["appointments"] if v["id"] == moved["id"])
    command("select_appointment", {"ref": chosen["ref"]})
    command("read_back")
    cancelled = command("cancel_appointment", text="yes")
    require(
        cancelled["result"]["data"]["appointment"]["status"] == "cancelled", "replacement cancelled"
    )
    command("end_call")
    require(call("GET", prefix, token=token)[0] == 401, "ended capability rejected")
    print(
        "Phase 2 HTTP smoke passed: runtime scope, verification, holds, booking/replay, reschedule, cancel and end; zero provider calls."
    )


if __name__ == "__main__":
    main()
