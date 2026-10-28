"""Real localhost safe-checkpoint smoke; no speech, NIM or patient mutation."""

from uuid import uuid4

from brain.bootstrap import read_demo_credentials
from scheduler.config import ROOT
from scripts.smoke_phase1 import call, require


def main():
    clinics = read_demo_credentials(ROOT / ".local/phase1-demo-credentials.json")["clinics"]
    owner, other = clinics
    require(owner["owner_email"] == "demo-owner-a@example.com", "synthetic clinic only")
    require(call("GET", "/health/ready")[0] == 200, "database and cache ready")
    status, tokens = call(
        "POST",
        "/auth/login",
        {
            "email": owner["owner_email"],
            "password": owner["owner_password"],
        },
    )
    require(status == 200, "staff sign in")
    token = tokens["access_token"]
    status, catalog = call("GET", "/voice/agents", token=token)
    require(status == 200, "voice agent catalog")
    ids = {row["id"] for row in catalog["agents"]}
    require(owner["agent_id"] in ids and other["agent_id"] not in ids, "tenant-scoped agents")
    require(call("GET", "/voice/agents")[0] == 401, "anonymous staff API denied")
    require(
        call("POST", "/voice/worker/ready", {"en": True, "ur": False}, token)[0] == 401,
        "staff cannot forge worker readiness",
    )
    status, body = call(
        "POST",
        "/voice/sessions",
        {
            "agent_id": owner["agent_id"],
            "request_key": "safe-smoke-" + uuid4().hex,
        },
        token,
    )
    require(
        status == 503 and body["error"]["code"] == "voice_unavailable",
        "untested speech capability remains disabled",
    )
    print(
        "Phase 3 safe API smoke passed: staff login, tenant catalog, worker separation, explicit unavailable voice; no speech or patient mutation."
    )


if __name__ == "__main__":
    main()
